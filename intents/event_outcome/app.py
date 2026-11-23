"""EVENT_OUTCOME_RESOLUTION_GENERAL.

An outcome is `yes` or `no` only on evidence; otherwise it is `unresolved` with `resolved_yes` false.
  1. A date in the event that is still in the future means the event cannot have settled: `unresolved` at once.
  2. A closed Polymarket market whose wording and numbers match the event closely settles it (deterministic, cited).
  3. Else, if `GROQ_API_KEY` is set and Groq answers in time, it may judge the event from retrieved Wikipedia and news
     documents, given today's date. Its answer is used only when it cites retrieved documents; the sources returned are
     those documents' URLs, never ones the model wrote.
  4. Else `unresolved`. Without a model and without a matching settled market nothing is guessed.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Literal, Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

from intentkit.groq import parse_json_object
from intentkit.llm_pipeline import UNTRUSTED_NOTE, LlmPipeline, today
from intentkit.pipeline import Req, Result
from intentkit.retrieval import dedupe_rank, format_sources, gather_docs, google_news, search_phrase, wikipedia
from intentkit.service import StrictModel, create_service_app
from intentkit.text import clip

from .config import Settings
from .polymarket import find_settled

VERSION = "0.1.0"
FIELDS = {"event": ("question", "query", "text", "input", "q", "market", "prompt")}
_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                       "september", "october", "november", "december"], 1)}

SYSTEM = ("You resolve whether a specific real-world event happened, using ONLY the numbered sources. Today's date is "
          "given: never resolve an event that has not yet happened or whose result the sources do not state. Answer "
          '"yes" if the sources show the event happened / the condition was met, "no" if they show it did not, and '
          '"unresolved" if they do not clearly settle it. Name the winner only if the event has one. ' + UNTRUSTED_NOTE)


class EventOutcomeResponse(StrictModel):
    """Exactly the response fields of Telegraph's EVENT_OUTCOME_RESOLUTION_GENERAL intent. Nothing else."""

    event: str = ""
    outcome: Literal["yes", "no", "unresolved"] = "unresolved"
    resolved_yes: bool = False
    winner: str = ""
    resolved_at: str = ""
    sources: list[str] = Field(default_factory=list)


def dates_in(text: str) -> list[date]:
    """Calendar dates named in the text (ISO, 'March 11, 2026', '11 March 2026', 'March 2026' as its first day)."""
    out: list[date] = []
    for y, m, d in re.findall(r"\b(\d{4})-(\d{2})-(\d{2})\b", text):
        try:
            out.append(date(int(y), int(m), int(d)))
        except ValueError:
            pass
    names = "|".join(_MONTHS)
    for m, d, y in re.findall(rf"\b({names})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", text, re.I):
        try:
            out.append(date(int(y), _MONTHS[m.lower()], int(d)))
        except ValueError:
            pass
    for d, m, y in re.findall(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({names}),?\s+(\d{{4}})\b", text, re.I):
        try:
            out.append(date(int(y), _MONTHS[m.lower()], int(d)))
        except ValueError:
            pass
    taken = [m.span() for m in re.finditer(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{names}),?\s+\d{{4}}\b", text, re.I)]
    for m in re.finditer(rf"\b({names})\s+(\d{{4}})\b", text, re.I):
        if any(a <= m.start() and m.end() <= b for a, b in taken):      # part of a fuller date such as "11 March 2026"
            continue
        out.append(date(int(m.group(2)), _MONTHS[m.group(1).lower()], 1))
    return out


def is_future(text: str, now: Optional[date] = None) -> bool:
    now = now or datetime.now(timezone.utc).date()
    return any(d > now for d in dates_in(text))


class EventOutcomePipeline(LlmPipeline):
    intent = "EVENT_OUTCOME_RESOLUTION_GENERAL"
    WARM = ("https://en.wikipedia.org/w/api.php?action=query&meta=siteinfo&format=json",
            "https://news.google.com/rss/search?q=a")

    def key(self, req: Req) -> Optional[str]:
        e = re.sub(r"\s+", " ", req.get("event", "")).strip().lower()
        return f"eo:{e}" if e else None

    def empty(self, req: Req, reason: str) -> BaseModel:
        return EventOutcomeResponse(event=req.get("event", "").strip()[:300])

    async def compute(self, req: Req) -> Result:
        s: Settings = self.settings
        event = re.sub(r"\s+", " ", req.get("event", "")).strip()
        if not event:
            return Result(EventOutcomeResponse(), degraded=True)
        base = EventOutcomeResponse(event=event[:300])
        if is_future(event):
            return Result(base)                                   # cannot have settled yet; this is the honest answer
        deadline = self.deadline()

        phrase = search_phrase(event, max_words=10)
        market_task = find_settled(self.up, event, s.market_match, timeout=min(3.5, s.provider_timeout, max(0.5, self.left(deadline))))
        docs_task = gather_docs([wikipedia(self.up, phrase, 3), google_news(self.up, phrase, 6)],
                                timeout=min(s.provider_timeout, max(0.5, self.left(deadline))), soft=2.5) if self.groq.available() else None
        market, docs = await _both(market_task, docs_task)
        if market is not None:
            sources = [market.url] + ([market.source] if market.source else [])
            return Result(EventOutcomeResponse(event=event[:300], outcome="yes" if market.yes else "no",
                                               resolved_yes=market.yes, winner="", resolved_at=market.resolved_at,
                                               sources=sources))
        if not docs or not self.groq.available() or self.left(deadline) <= 1.0:
            return Result(base, degraded=self.groq.configured and not docs)
        ranked = dedupe_rank(docs, event, 6, min_overlap=0.2)
        if not ranked:
            return Result(base)
        user = (f"Today's date: {today()}\nEvent: <<<{event}>>>\n\nSources:\n{format_sources(ranked, 600)}\n\n"
                'Return JSON: {"outcome": "yes|no|unresolved", "winner": "<name or empty>", '
                '"resolved_at": "<YYYY-MM-DD or empty>", "evidence": [<source numbers that state the result>]}')
        obj = parse_json_object(await self.groq.chat(SYSTEM, user, json_mode=True, budget=self.left(deadline) - 0.3,
                                                     max_tokens=300, temperature=0.0))
        result = self._vet(obj, event, ranked)
        return Result(result or base, degraded=result is None and self.groq.configured, ttl=self.llm_ttl(result is not None))

    @staticmethod
    def _vet(obj: Optional[dict], event: str, ranked) -> Optional[EventOutcomeResponse]:
        if not obj or obj.get("outcome") not in ("yes", "no"):
            return None
        ev = obj.get("evidence")
        nums = sorted({int(n) for n in ev if str(n).isdigit() and 1 <= int(n) <= len(ranked)}) if isinstance(ev, list) else []
        if not nums:                                              # a settled outcome must point at what it relied on
            return None
        when = str(obj.get("resolved_at") or "")
        today_ = datetime.now(timezone.utc).date()
        try:
            when = when if date.fromisoformat(when) <= today_ else ""
        except ValueError:
            when = ""
        yes = obj["outcome"] == "yes"
        return EventOutcomeResponse(event=event[:300], outcome=obj["outcome"], resolved_yes=yes,
                                    winner=clip(str(obj.get("winner") or ""), 80), resolved_at=when,
                                    sources=[ranked[n - 1].url for n in nums])


async def _both(market_coro, docs_coro):
    import asyncio
    market, docs = await asyncio.gather(market_coro, docs_coro if docs_coro is not None else _none(), return_exceptions=True)
    return (None if isinstance(market, BaseException) else market), ([] if isinstance(docs, BaseException) or docs is None else docs)


async def _none():
    return None


def create_app(settings: Optional[Settings] = None, pipeline: Optional[EventOutcomePipeline] = None) -> FastAPI:
    return create_service_app(
        title="Telegraph Event Outcome Miner", intent="EVENT_OUTCOME_RESOLUTION_GENERAL", path="/event-outcome",
        version=VERSION, fields=FIELDS, model=EventOutcomeResponse, settings=settings or Settings.from_env(),
        pipeline=pipeline, make_pipeline=EventOutcomePipeline, max_len=600)
