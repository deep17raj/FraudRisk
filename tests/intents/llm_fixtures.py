"""Canned responses for every source an LLM-assisted intent reads, and for Groq itself."""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

import httpx

from tests.intents.helpers import FakeHttp

GROQ = "https://api.groq.com/openai/v1/chat/completions"
WIKI = "https://en.wikipedia.org/w/api.php"
DDG = "https://api.duckduckgo.com/"
GNEWS = "https://news.google.com/rss/search"
PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_FETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
OPENALEX = "https://api.openalex.org/works"
OSV_BATCH = "https://api.osv.dev/v1/querybatch"
OSV_VULN = "https://api.osv.dev/v1/vulns/"
POLYMARKET = "https://gamma-api.polymarket.com/public-search"


def wiki(*pages: tuple[str, str]) -> dict:
    return {"query": {"pages": {str(i): {"title": t, "index": i + 1, "extract": x} for i, (t, x) in enumerate(pages)}}}


def ddg(text: str = "", url: str = "", heading: str = "") -> dict:
    return {"AbstractText": text, "AbstractURL": url, "Heading": heading, "AbstractSource": "Wikipedia", "Answer": ""}


def rss(*items: tuple[str, str, str, str]) -> str:
    """(headline, link, date 'Wed, 18 Nov 2026 22:48:00 GMT', source)"""
    body = "".join(f"<item><title>{t} - {s}</title><link>{l}</link><pubDate>{d}</pubDate><source>{s}</source></item>"
                   for t, l, d, s in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{body}</channel></rss>'


def pubmed_xml(*arts: tuple[str, str, str, str]) -> str:
    """(pmid, title, abstract, year)"""
    body = "".join(f"<PubmedArticle><MedlineCitation><PMID>{p}</PMID><Article><ArticleTitle>{t}</ArticleTitle>"
                   f"<Abstract><AbstractText>{a}</AbstractText></Abstract></Article></MedlineCitation>"
                   f"<PubmedData><History><PubMedPubDate><Year>{y}</Year></PubMedPubDate></History></PubmedData>"
                   f"<PubDate><Year>{y}</Year></PubDate></PubmedArticle>" for p, t, a, y in arts)
    return f"<PubmedArticleSet>{body}</PubmedArticleSet>"


def openalex(*works: tuple[str, str, str, int, int]) -> dict:
    """(title, doi, abstract, year, cites)"""
    out = []
    for title, doi, abstract, year, cites in works:
        words = abstract.split()
        out.append({"title": title, "doi": doi, "publication_year": year, "cited_by_count": cites,
                    "abstract_inverted_index": {w: [i] for i, w in enumerate(words)}})
    return {"results": out}


def groq_reply(content: Any) -> dict:
    return {"choices": [{"message": {"content": content if isinstance(content, str) else json.dumps(content)}}]}


def groq_error(code: str, message: str = "x") -> dict:
    return {"error": {"message": message, "type": "invalid_request_error", "code": code}}


class Groq:
    """A scriptable Groq: a list of rules consumed one per call (the last repeats). A rule is a dict (200 reply body),
    an int (status), a (status, headers) tuple, an Exception, or a callable(request) -> any of those."""

    def __init__(self, *rules) -> None:
        self.rules, self.calls = list(rules), []

    def __call__(self, request: httpx.Request):
        body = json.loads(request.content) if request.content else {}
        self.calls.append({"model": body.get("model"), "json_mode": bool(body.get("response_format")),
                           "messages": body.get("messages"), "auth": request.headers.get("authorization"),
                           "max_tokens": body.get("max_tokens"), "reasoning_effort": body.get("reasoning_effort")})
        rule = self.rules[min(len(self.calls) - 1, len(self.rules) - 1)] if self.rules else 500
        return rule(request) if callable(rule) else rule

    @property
    def models(self) -> list:
        return [c["model"] for c in self.calls]


def transport(routes: dict[str, Any], groq: Optional[Groq] = None) -> FakeHttp:
    r = dict(routes)
    if groq is not None:
        r[GROQ] = _groq_rule(groq)
    return FakeHttp(r)


def _groq_rule(groq: Groq) -> Callable:
    def rule(request: httpx.Request):
        out = groq(request)
        if isinstance(out, tuple) and len(out) == 2 and isinstance(out[1], dict):   # (status, headers): a 429 + retry-after
            return httpx.Response(out[0], headers=out[1], text="")
        return out                                                                  # incl. (rule, delay_seconds)
    return rule
