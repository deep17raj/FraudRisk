from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

from intentkit.ask import sanctions_name
from intentkit.cache import TTLCache
from intentkit.pipeline import Req
from intentkit.request import GENERIC_ALIASES
from intentkit.service import StrictModel, create_service_app

from .config import Settings
from .lists import SanctionsIndex

VERSION = "0.1.0"
FIELDS = {"name": ("entity", "person", "subject", "party", "company", "individual", *GENERIC_ALIASES)}


class SanctionsResponse(StrictModel):
    """Exactly the response fields of Telegraph's SANCTIONS_SCREENING_MATCH_GENERAL intent. Nothing else."""

    name: str = ""
    matched: bool = False
    matches: list[str] = Field(default_factory=list)
    lists: list[str] = Field(default_factory=list)
    match_score: Optional[float] = Field(default=None, ge=0, le=1, allow_inf_nan=False)


class SanctionsPipeline:
    intent = "SANCTIONS_SCREENING_MATCH_GENERAL"

    def __init__(self, settings: Settings, client) -> None:
        self.settings, self.client = settings, client
        self.index = SanctionsIndex(settings.list_ttl, settings.list_timeout)
        self.cache = TTLCache(max_items=settings.cache_max_items)

    async def start(self) -> None:
        await self.index.start(self.client, wait=35.0)

    def fallback(self, req: Req, reason: str) -> SanctionsResponse:
        return SanctionsResponse(name=req.get("name", "")[:300])

    async def answer(self, req: Req) -> SanctionsResponse:
        s: Settings = self.settings
        name = sanctions_name(req.get("name", ""))[:300]
        if not name:
            return SanctionsResponse()
        self.index.ensure_fresh(self.client)
        if not self.index.ready or self.index.missing:        # a list is still downloading: do not answer from half the data
            await self.index.settle(max(0.0, min(5.0, s.global_timeout - 2.5)))
        if not self.index.ready:
            # Nothing was checked, so say so: no lists, no score, never a "clear".
            return SanctionsResponse(name=name)
        key = f"{name.casefold()}|{self.index.loaded_at}"
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        hits = await asyncio.get_running_loop().run_in_executor(None, self.index.search, name, s.max_matches)
        best = hits[0].score if hits else 0.0
        matched = best >= s.match_threshold
        shown = [h for h in hits if h.score >= s.match_threshold] if matched else []
        res = SanctionsResponse(
            name=name, matched=matched, matches=[h.entry.describe() for h in shown],
            lists=list(self.index.lists), match_score=round(best, 6))
        self.cache.set(key, res, s.cache_ttl)
        return res


def create_app(settings: Optional[Settings] = None, pipeline: Optional[SanctionsPipeline] = None) -> FastAPI:
    return create_service_app(
        title="Telegraph Sanctions Screening Miner", intent="SANCTIONS_SCREENING_MATCH_GENERAL",
        path="/sanctions-screening", version=VERSION, fields=FIELDS, model=SanctionsResponse,
        settings=settings or Settings.from_env(), pipeline=pipeline, make_pipeline=SanctionsPipeline,
        start_timeout=40.0, max_len=300)
