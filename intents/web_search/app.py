"""WEB_SEARCH_GENERAL.

Retrieval first (DuckDuckGo instant answers, Wikipedia, Google News headlines, plus Tavily / Brave / Serper when
their keys are set), all at once within a time budget. From those documents an extractive answer is built, so a
complete answer exists before any model is involved. If `GROQ_API_KEY` is set and the model answers within the time
left, its grounded answer replaces the extractive one. `sources` are always URLs of documents that were retrieved.
"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

from intentkit.answers import extractive, prefer_reference, valid_answer
from intentkit.groq import parse_json_object
from intentkit.llm_pipeline import UNTRUSTED_NOTE, LlmPipeline, today
from intentkit.pipeline import Req, Result
from intentkit.retrieval import (brave, dedupe_rank, duckduckgo, format_sources, gather_docs, google_news, search_phrase,
                                 serper, tavily, wikipedia)
from intentkit.service import StrictModel, create_service_app
from intentkit.text import brief, clip

from .config import Settings

VERSION = "0.1.0"
FIELDS = {"query": ("q", "search", "question", "text", "input", "prompt")}
_RECENT = re.compile(r"\b(latest|news|today|current|currently|now|recent|recently|this (week|month|year)|yesterday|"
                     r"breaking|update|announced|price of|live)\b", re.I)

SYSTEM = ("You answer a web-search query using ONLY the numbered sources supplied. Write 2-5 plain sentences, "
          "most important fact first, cite the sources you rely on as [1], [2]. If the sources do not contain the answer, "
          "say so in one sentence instead of guessing. " + UNTRUSTED_NOTE)


class WebSearchResponse(StrictModel):
    """Exactly the response fields of Telegraph's WEB_SEARCH_GENERAL intent. Nothing else."""

    answer: str = ""
    sources: list[str] = Field(default_factory=list)
    brief: str = ""


def _msg(text: str) -> WebSearchResponse:
    return WebSearchResponse(answer=text, sources=[], brief=brief(text))


class WebSearchPipeline(LlmPipeline):
    intent = "WEB_SEARCH_GENERAL"
    WARM = ("https://en.wikipedia.org/w/api.php?action=query&meta=siteinfo&format=json",
            "https://api.duckduckgo.com/?q=a&format=json", "https://news.google.com/rss/search?q=a")

    def key(self, req: Req) -> Optional[str]:
        q = re.sub(r"\s+", " ", req.get("query", "")).strip().lower()
        return f"ws:{q}" if q else None

    def empty(self, req: Req, reason: str) -> BaseModel:
        q = req.get("query", "").strip()
        return _msg(f'The search for "{clip(q, 120)}" could not be completed ({reason}).') if q else WebSearchResponse()

    def _providers(self, q: str) -> list:
        s: Settings = self.settings
        phrase = search_phrase(q)          # keyword engines (news, instant answers, encyclopedia) prefer keywords
        calls = [duckduckgo(self.up, phrase), wikipedia(self.up, phrase, 3), google_news(self.up, phrase, 6)]
        if s.tavily_api_key:
            calls.insert(0, tavily(self.up, s.tavily_api_key, q))
        if s.brave_api_key:
            calls.insert(0, brave(self.up, s.brave_api_key, q))
        if s.serper_api_key:
            calls.insert(0, serper(self.up, s.serper_api_key, q))
        return calls

    async def compute(self, req: Req) -> Result:
        s: Settings = self.settings
        q = re.sub(r"\s+", " ", req.get("query", "")).strip()
        if not q:
            return Result(_msg("No search query was provided."), degraded=True)
        deadline = self.deadline()
        docs = await gather_docs(self._providers(q), timeout=min(s.provider_timeout + 1.0, max(0.5, self.left(deadline))),
                                 soft=2.2)
        ranked = prefer_reference(dedupe_rank(docs, q, s.max_sources + 4, min_overlap=0.2), bool(_RECENT.search(q)))
        ranked = ranked[: s.max_sources]
        if not ranked:
            return Result(_msg(f'No sources were found for "{clip(q, 120)}".'), degraded=True)

        answer, used = extractive(q, ranked)                       # the complete keyless answer, already in hand
        used_llm = False
        if self.groq.available() and self.left(deadline) > 1.0:
            user = (f"Today's date: {today()}\nQuery: <<<{q}>>>\n\nSources:\n{format_sources(ranked)}\n\n"
                    'Return JSON: {"answer": "<2-5 sentences with [n] citations>", "used": [<source numbers relied on>]}')
            raw = await self.groq.chat(SYSTEM, user, json_mode=True, budget=self.left(deadline) - 0.3)
            ok = valid_answer(parse_json_object(raw), "answer", ranked)
            if ok:
                answer, used, used_llm = ok[0], ok[1], True
        sources = [d.url for d in used]
        return Result(WebSearchResponse(answer=answer, sources=sources, brief=brief(answer) or brief(ranked[0].title)),
                      degraded=not used_llm and self.groq.configured, ttl=self.llm_ttl(used_llm))


def create_app(settings: Optional[Settings] = None, pipeline: Optional[WebSearchPipeline] = None) -> FastAPI:
    return create_service_app(
        title="Telegraph Web Search Miner", intent="WEB_SEARCH_GENERAL", path="/web-search", version=VERSION,
        fields=FIELDS, model=WebSearchResponse, settings=settings or Settings.from_env(), pipeline=pipeline,
        make_pipeline=WebSearchPipeline, max_len=600)
