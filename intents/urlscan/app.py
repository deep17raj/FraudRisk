"""URL_SCAN_VERDICT: `safe` is true only when the URL was checked and nothing flagged it.

It reuses `urlsafe.pipeline.UrlSafePipeline` (OpenPhish's live feed, Cloudflare's security DNS and the URL's
own structure), so the two intents can never disagree about a URL. `safe` is false for Malicious and
Suspicious, and also for Unknown (nothing could be consulted): a boolean cannot say "unknown", and
claiming "safe" without evidence would be the worse error.
"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI

from intentkit.pipeline import Req
from intentkit.request import GENERIC_ALIASES
from intentkit.service import StrictModel, create_service_app
from urlsafe.config import Settings
from urlsafe.pipeline import UrlSafePipeline
from urlsafe.verdict import Verdict

VERSION = "0.1.0"
FIELDS = {"url": ("link", "uri", "domain", "host", *GENERIC_ALIASES)}


class UrlScanResponse(StrictModel):
    """Exactly the response field of Telegraph's URL_SCAN_VERDICT intent. Nothing else."""

    safe: bool = False


class UrlScanPipeline:
    intent = "URL_SCAN_VERDICT"

    def __init__(self, settings: Settings, client) -> None:
        self.inner = UrlSafePipeline(settings, client)
        self.cache = self.inner.cache

    async def start(self) -> None:
        await self.inner.start()

    async def answer(self, req: Req) -> UrlScanResponse:
        res = await self.inner.check(req.get("url", ""))
        return UrlScanResponse(safe=res.verdict is Verdict.SAFE)

    def fallback(self, req: Req, reason: str) -> UrlScanResponse:
        return UrlScanResponse(safe=False)


def create_app(settings: Optional[Settings] = None, pipeline: Optional[UrlScanPipeline] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    return create_service_app(
        title="Telegraph URL Scan Verdict Miner", intent="URL_SCAN_VERDICT", path="/url-scan", version=VERSION,
        fields=FIELDS, model=UrlScanResponse, settings=settings, pipeline=pipeline, make_pipeline=UrlScanPipeline,
        start_timeout=settings.feed_timeout + 2)
