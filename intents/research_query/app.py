"""RESEARCH_QUERY_GENERAL: a one-paragraph answer to a research question, citing the sources it used.

Sources are scholarly: PubMed abstracts and OpenAlex works (title, year, citation count, abstract), plus Wikipedia for
background. An extractive answer is built from the abstracts first, so a complete cited answer exists without a model;
if `GROQ_API_KEY` is set and Groq answers in time, its grounded paragraph replaces it. `sources` are URLs of documents
that were retrieved (DOI, PubMed or Wikipedia links), never URLs a model wrote. `source_count` is how many were used.
"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

from intentkit.answers import extractive, valid_answer
from intentkit.groq import parse_json_object
from intentkit.llm_pipeline import UNTRUSTED_NOTE, LlmPipeline, today
from intentkit.pipeline import Req, Result
from intentkit.retrieval import dedupe_rank, format_sources, gather_docs, openalex, pubmed, search_phrase, wikipedia
from intentkit.service import StrictModel, create_service_app
from intentkit.text import brief, clip

from .config import Settings

VERSION = "0.1.0"
FIELDS = {"question": ("query", "q", "text", "input", "prompt", "topic")}

SYSTEM = ("You are a careful research assistant. Answer the research question in ONE paragraph of at most 6 sentences "
          "using ONLY the numbered sources. State what the evidence says and how strong or limited it is, cite sources "
          "as [1], [2], and do not claim more than the abstracts support. If the sources do not answer the question, say "
          "so plainly. " + UNTRUSTED_NOTE)


class ResearchQueryResponse(StrictModel):
    """Exactly the response fields of Telegraph's RESEARCH_QUERY_GENERAL intent. Nothing else."""

    answer: str = ""
    source_count: int = Field(default=0, ge=0, le=1000)
    sources: list[str] = Field(default_factory=list)
    brief: str = ""


def _msg(text: str) -> ResearchQueryResponse:
    return ResearchQueryResponse(answer=text, source_count=0, sources=[], brief=brief(text))


class ResearchQueryPipeline(LlmPipeline):
    intent = "RESEARCH_QUERY_GENERAL"
    WARM = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi?retmode=json",
            "https://en.wikipedia.org/w/api.php?action=query&meta=siteinfo&format=json")

    def key(self, req: Req) -> Optional[str]:
        q = re.sub(r"\s+", " ", req.get("question", "")).strip().lower()
        return f"rq:{q}" if q else None

    def empty(self, req: Req, reason: str) -> BaseModel:
        q = req.get("question", "").strip()
        return _msg(f'The research on "{clip(q, 120)}" could not be completed ({reason}).') if q else ResearchQueryResponse()

    async def compute(self, req: Req) -> Result:
        s: Settings = self.settings
        q = re.sub(r"\s+", " ", req.get("question", "")).strip()
        if not q:
            return Result(_msg("No research question was provided."), degraded=True)
        deadline = self.deadline()
        phrase = search_phrase(q, max_words=10)
        docs = await gather_docs(
            [openalex(self.up, phrase, 5, s.openalex_api_key), pubmed(self.up, phrase, 4), wikipedia(self.up, phrase, 2)],
            timeout=min(s.provider_timeout + 1.0, max(0.5, self.left(deadline))), soft=3.0)
        # Scholarly documents lead; Wikipedia only adds background.
        ranked = dedupe_rank(docs, q, s.max_sources + 2, min_overlap=0.25)
        ranked = ([d for d in ranked if d.source != "Wikipedia"] + [d for d in ranked if d.source == "Wikipedia"])[:s.max_sources]
        if not ranked:
            return Result(_msg(f'No sources were found for the question "{clip(q, 120)}".'), degraded=True)

        answer, used = self._extractive_answer(q, ranked)
        used_llm = False
        if self.groq.available() and self.left(deadline) > 1.0:
            user = (f"Today's date: {today()}\nResearch question: <<<{q}>>>\n\nSources:\n{format_sources(ranked, 800)}\n\n"
                    'Return JSON: {"answer": "<one paragraph with [n] citations>", "used": [<source numbers relied on>]}')
            raw = await self.groq.chat(SYSTEM, user, json_mode=True, budget=self.left(deadline) - 0.3, max_tokens=600)
            ok = valid_answer(parse_json_object(raw), "answer", ranked, min_chars=40)
            if ok:
                answer, used, used_llm = ok[0], ok[1], True
        return Result(ResearchQueryResponse(answer=answer, source_count=len(used), sources=[d.url for d in used],
                                            brief=brief(answer)),
                      degraded=not used_llm and self.groq.configured, ttl=self.llm_ttl(used_llm))

    @staticmethod
    def _extractive_answer(q: str, ranked) -> tuple[str, list]:
        text, used = extractive(q, ranked, per_doc=1, max_docs=3, max_chars=1000)
        return f"Based on the retrieved literature: {text}", used


def create_app(settings: Optional[Settings] = None, pipeline: Optional[ResearchQueryPipeline] = None) -> FastAPI:
    return create_service_app(
        title="Telegraph Research Query Miner", intent="RESEARCH_QUERY_GENERAL", path="/research-query", version=VERSION,
        fields=FIELDS, model=ResearchQueryResponse, settings=settings or Settings.from_env(), pipeline=pipeline,
        make_pipeline=ResearchQueryPipeline, max_len=800)
