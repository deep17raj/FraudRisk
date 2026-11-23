"""Documents for an answer to be grounded in: keyless public sources, plus optional keyed search APIs.

Every provider returns a list of `Doc` and never raises (an unreachable source is an empty list), so a caller
can gather them all at once and use whatever came back. The URLs on a `Doc` are the ONLY URLs an intent may cite:
sources are never taken from a language model's output.

Keyless: Wikipedia, DuckDuckGo's instant answer, Google News RSS (headlines, for "latest" questions), PubMed
(biomedical abstracts) and OpenAlex (scholarly works; its free daily budget is shared per IP, so it is optional
for the caller and degrades to nothing when exhausted). Keyed and optional: Tavily, Brave and Serper search
(`TAVILY_API_KEY`, `BRAVE_API_KEY`, `SERPER_API_KEY`), used only when the key is set.
"""
from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Awaitable, Callable, Iterable, Optional
from urllib.parse import quote

from .text import clip, overlap, strip_tags
from .upstream import Upstream

WIKI = "https://en.wikipedia.org/w/api.php"
DDG = "https://api.duckduckgo.com/"
GNEWS = "https://news.google.com/rss/search"
PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_FETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
OPENALEX = "https://api.openalex.org/works"
SNIPPET_CHARS = 900


@dataclass(frozen=True)
class Doc:
    title: str
    url: str
    snippet: str
    source: str
    date: str = ""


async def wikipedia(up: Upstream, query: str, limit: int = 3) -> list[Doc]:
    d = await up.get_json(WIKI, params={
        "action": "query", "generator": "search", "gsrsearch": query[:300], "gsrlimit": limit, "prop": "extracts",
        "exintro": 1, "explaintext": 1, "exlimit": limit, "format": "json", "redirects": 1})
    pages = ((d or {}).get("query") or {}).get("pages") if isinstance(d, dict) else None
    out: list[Doc] = []
    for p in sorted((pages or {}).values(), key=lambda p: p.get("index", 99)):
        title, text = p.get("title", ""), (p.get("extract") or "").strip()
        if title and text:
            out.append(Doc(title, f"https://en.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}",
                           clip(text, SNIPPET_CHARS), "Wikipedia"))
    return out


async def duckduckgo(up: Upstream, query: str) -> list[Doc]:
    d = await up.get_json(DDG, params={"q": query[:300], "format": "json", "no_html": 1, "skip_disambig": 1})
    if not isinstance(d, dict):
        return []
    out: list[Doc] = []
    text, url = (d.get("AbstractText") or "").strip(), d.get("AbstractURL") or ""
    if text and url:
        out.append(Doc(d.get("Heading") or query, url, clip(text, SNIPPET_CHARS), d.get("AbstractSource") or "DuckDuckGo"))
    if d.get("Answer") and isinstance(d["Answer"], str):
        out.append(Doc(query, "https://duckduckgo.com/?q=" + quote(query), clip(d["Answer"], 300), "DuckDuckGo"))
    return out


def _safe_xml(raw: str) -> Optional[ET.Element]:
    """Parse RSS text, refusing anything with a DTD or entity declaration (entity-expansion bombs)."""
    if "<!DOCTYPE" in raw[:2000].upper() or "<!ENTITY" in raw.upper():
        return None
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        return None


async def google_news(up: Upstream, query: str, limit: int = 6) -> list[Doc]:
    res = await up.fetch(GNEWS, params={"q": query[:200], "hl": "en-US", "gl": "US", "ceid": "US:en"}, text=True)
    root = _safe_xml(res[1]) if res is not None and res[0] == 200 and isinstance(res[1], str) else None
    out: list[Doc] = []
    for it in (root.findall(".//item") if root is not None else [])[:limit]:
        title, link = (it.findtext("title") or "").strip(), (it.findtext("link") or "").strip()
        if not title or not link:
            continue
        date = ""
        try:
            date = parsedate_to_datetime(it.findtext("pubDate") or "").date().isoformat()
        except (TypeError, ValueError):
            pass
        src = (it.findtext("source") or "Google News").strip()
        headline = title.rsplit(" - ", 1)[0].strip() if title.endswith(f" - {src}") else title   # Google appends " - Source"
        out.append(Doc(headline, link, headline, src, date))
    return out


async def pubmed(up: Upstream, query: str, limit: int = 4) -> list[Doc]:
    d = await up.get_json(PUBMED_SEARCH, params={"db": "pubmed", "term": query[:300], "retmax": limit,
                                                  "retmode": "json", "sort": "relevance"})
    ids = (((d or {}).get("esearchresult") or {}).get("idlist") or []) if isinstance(d, dict) else []
    if not ids:
        return []
    res = await up.fetch(PUBMED_FETCH, params={"db": "pubmed", "id": ",".join(ids[:limit]), "retmode": "xml"}, text=True)
    root = _safe_xml(res[1]) if res is not None and res[0] == 200 and isinstance(res[1], str) else None
    out: list[Doc] = []
    for art in (root.findall("PubmedArticle") if root is not None else []):
        pmid = art.findtext(".//PMID") or ""
        title = "".join(art.find(".//ArticleTitle").itertext()) if art.find(".//ArticleTitle") is not None else ""
        abstract = " ".join("".join(x.itertext()) for x in art.findall(".//Abstract/AbstractText"))
        year = art.findtext(".//PubDate/Year") or (art.findtext(".//PubDate/MedlineDate") or "")[:4]
        if pmid and title and abstract:
            out.append(Doc(strip_tags(title), f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", clip(abstract, SNIPPET_CHARS),
                           "PubMed", year))
    return out


def _abstract(inverted: Optional[dict]) -> str:
    pos: dict[int, str] = {}
    for word, places in (inverted or {}).items():
        for p in places:
            pos[p] = word
    return " ".join(pos[i] for i in sorted(pos))


async def openalex(up: Upstream, query: str, limit: int = 4, api_key: str = "") -> list[Doc]:
    params = {"search": query[:300], "per-page": limit,
              "select": "id,title,doi,publication_year,cited_by_count,abstract_inverted_index"}
    if api_key:
        params["api_key"] = api_key
    d = await up.get_json(OPENALEX, params=params)
    out: list[Doc] = []
    for w in ((d or {}).get("results") or []) if isinstance(d, dict) else []:
        abstract, url = _abstract(w.get("abstract_inverted_index")), w.get("doi") or w.get("id") or ""
        if w.get("title") and url and abstract:
            cites = w.get("cited_by_count")
            out.append(Doc(strip_tags(w["title"]), url, clip(f"{abstract} (cited by {cites} works)" if cites else abstract,
                                                            SNIPPET_CHARS), "OpenAlex", str(w.get("publication_year") or "")))
    return out


async def tavily(up: Upstream, key: str, query: str) -> list[Doc]:
    res = await up.fetch("https://api.tavily.com/search", method="POST", key="tavily",
                         json_body={"query": query[:380], "max_results": 6, "search_depth": "basic", "include_answer": False},
                         headers={"Authorization": f"Bearer {key}"})
    items = (res[1].get("results") if res is not None and res[0] == 200 and isinstance(res[1], dict) else None) or []
    return [Doc(it.get("title", ""), it["url"], clip(it["content"], SNIPPET_CHARS), "Tavily", it.get("published_date") or "")
            for it in items if it.get("url") and it.get("content")]


async def brave(up: Upstream, key: str, query: str) -> list[Doc]:
    res = await up.fetch("https://api.search.brave.com/res/v1/web/search", params={"q": query[:380], "count": 8},
                         headers={"X-Subscription-Token": key, "Accept": "application/json"}, key="brave")
    items = (((res[1].get("web") or {}).get("results")) if res is not None and res[0] == 200 and isinstance(res[1], dict) else None) or []
    return [Doc(strip_tags(it.get("title", "")), it["url"], clip(strip_tags(it["description"]), SNIPPET_CHARS), "Brave",
                it.get("page_age") or "") for it in items if it.get("url") and it.get("description")]


async def serper(up: Upstream, key: str, query: str) -> list[Doc]:
    res = await up.fetch("https://google.serper.dev/search", method="POST", key="serper",
                         json_body={"q": query[:300], "num": 8}, headers={"X-API-KEY": key})
    items = (res[1].get("organic") if res is not None and res[0] == 200 and isinstance(res[1], dict) else None) or []
    return [Doc(it.get("title", ""), it["link"], clip(it["snippet"], SNIPPET_CHARS), "Serper", it.get("date") or "")
            for it in items if it.get("link") and it.get("snippet")]


async def gather_docs(calls: Iterable[Awaitable[list[Doc]]], timeout: float, soft: Optional[float] = None) -> list[Doc]:
    """Run provider calls at once and keep what finished. One provider failing or hanging loses only itself.

    `timeout` is the hard limit. With `soft`, once that many seconds have passed AND some document has arrived, stop
    waiting for stragglers, so one slow source cannot hold up an answer the fast ones already make possible.
    """
    tasks = [asyncio.ensure_future(c) for c in calls]
    if not tasks:
        return []
    loop = asyncio.get_running_loop()
    start, pending = loop.time(), set(tasks)

    def have_docs() -> bool:
        return any(t.done() and not t.cancelled() and t.exception() is None and t.result() for t in tasks)

    while pending:
        remaining = timeout - (loop.time() - start)
        if soft is not None and have_docs():
            remaining = min(remaining, soft - (loop.time() - start))
        if remaining <= 0:
            break
        _, pending = await asyncio.wait(pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    out: list[Doc] = []
    for t in tasks:                                       # keep provider order: it is also the preference order
        if t.done() and not t.cancelled() and t.exception() is None:
            out += t.result()
    return out


_ASK = re.compile(r"^(what|who|whom|whose|when|where|which|why|how)(?:'s| is| are| was| were| do| does| did)?\s+", re.I)
_FILLER = re.compile(r"\b(please|tell me|can you|could you|i want to know|i would like to know|search for|look up|find)\b",
                     re.I)


def search_phrase(query: str, max_words: int = 9) -> str:
    """A compact keyword phrase for engines that match keywords better than sentences (news search)."""
    s = _FILLER.sub(" ", re.sub(r"[?!\"“”]", " ", query or ""))
    s = _ASK.sub("", s.strip())
    s = re.sub(r"'s\b", "", s)
    words = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9.\-]*", s) if w.lower() not in {"the", "a", "an", "of", "on", "in", "is", "are"}]
    return " ".join(words[:max_words]) or (query or "")[:120]


def dedupe_rank(docs: Iterable[Doc], query: str, limit: int, min_overlap: float = 0.0) -> list[Doc]:
    """Drop repeated URLs and weakly related documents, best match first (ties keep provider order)."""
    seen: set[str] = set()
    scored: list[tuple[float, int, Doc]] = []
    for i, d in enumerate(docs):
        key = re.sub(r"[#?].*$", "", d.url.lower().rstrip("/"))
        if key in seen:
            continue
        seen.add(key)
        score = overlap(query, f"{d.title} {d.snippet}")
        if score >= min_overlap:
            scored.append((score, i, d))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [d for _, _, d in scored[:limit]]


def format_sources(docs: list[Doc], per_doc: int = 700) -> str:
    """The numbered source block put in a prompt: untrusted text, clearly delimited."""
    return "\n\n".join(f"[{i}] {d.title} | {d.source} | {d.date or 'undated'} | {d.url}\n{clip(d.snippet, per_doc)}"
                       for i, d in enumerate(docs, 1))
