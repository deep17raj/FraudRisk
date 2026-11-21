"""Wikipedia source (free, no key). One MediaWiki call returns article intros plus full-text
search snippets, which reach deeper into articles than the intro alone."""
from __future__ import annotations

import asyncio

import httpx

from ..models import Evidence, ParsedClaim
from ..text_utils import content_terms, split_sentences, strip_html
from .base import Source

API = "https://en.wikipedia.org/w/api.php"


def best_sentences(text: str, terms: set[str], k: int = 3, max_chars: int = 600) -> str:
    """Pick the k sentences most relevant to `terms`, returned in original article order."""
    sents = split_sentences(text)
    if not sents:
        return ""
    scored = []
    for i, s in enumerate(sents):
        st = set(content_terms(s))
        scored.append((len(st & terms) / (len(terms) or 1), -i, i))
    top = sorted(scored, reverse=True)[:k]
    idx = sorted(i for _, _, i in top)
    return " ".join(sents[i] for i in idx)[:max_chars]


class WikipediaSource(Source):
    name = "wikipedia"

    def __init__(self, user_agent: str, limit: int = 4) -> None:
        super().__init__()
        self.user_agent = user_agent
        self.limit = limit

    def enabled(self) -> bool:
        return True

    async def _query(self, q: str, client: httpx.AsyncClient, timeout: float) -> list[dict]:
        params = {
            "action": "query", "format": "json", "formatversion": "2",
            "generator": "search", "gsrsearch": q, "gsrlimit": str(self.limit), "gsrnamespace": "0",
            "prop": "extracts|info", "exintro": "1", "explaintext": "1", "exlimit": str(self.limit),
            "inprop": "url",
            "list": "search", "srsearch": q, "srlimit": str(self.limit), "srprop": "snippet",
            "redirects": "1",
        }
        r = await client.get(API, params=params, headers={"User-Agent": self.user_agent}, timeout=timeout)
        r.raise_for_status()
        data = r.json().get("query", {})
        pages = {p["pageid"]: p for p in data.get("pages", [])}
        snippets = {s["pageid"]: strip_html(s.get("snippet", "")) for s in data.get("search", [])}
        out = []
        for pid, p in sorted(pages.items(), key=lambda kv: kv[1].get("index", 99)):
            out.append({"page": p, "snippet": snippets.get(pid, "")})
        return out

    async def search(self, claim: ParsedClaim, client: httpx.AsyncClient, timeout: float) -> list[Evidence]:
        results = await asyncio.gather(
            *[self._query(q, client, timeout) for q in claim.search_queries[:3]], return_exceptions=True
        )
        good = [r for r in results if not isinstance(r, BaseException)]
        if not good:
            raise next(r for r in results if isinstance(r, BaseException))
        terms = set(content_terms(claim.text))
        seen: set[int] = set()
        evidence: list[Evidence] = []
        for batch in good:
            for item in batch:
                p = item["page"]
                if p["pageid"] in seen:
                    continue
                seen.add(p["pageid"])
                intro = best_sentences(p.get("extract", ""), terms)
                snip = item["snippet"]
                text = intro
                if snip and snip not in intro:
                    text = f"{intro} … {snip}".strip(" …")
                if not text:
                    continue
                evidence.append(
                    Evidence(
                        url=p.get("fullurl") or f"https://en.wikipedia.org/?curid={p['pageid']}",
                        title=p.get("title", ""),
                        snippet=text[:900],
                        provider=self.name,
                    )
                )
        return evidence[: self.limit + 4]
