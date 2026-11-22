"""FastAPI layer. Guarantee: POST /url-safe ALWAYS answers HTTP 200 with exactly the four fields
{url, verdict, detections, summary} - including for malformed bodies, timeouts and internal errors -
because Telegraph revokes miners that return errors or go silent."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from intentkit.logging_setup import setup_logging
from intentkit.request import GENERIC_ALIASES, read_fields

from .config import Settings
from .pipeline import UrlResult, UrlSafePipeline, fallback_result
from .verdict import Verdict

log = logging.getLogger("urlsafe.app")
VERSION = "0.1.0"
FIELDS = {"url": ("link", "uri", "domain", "host", *GENERIC_ALIASES)}


class UrlSafeResponse(BaseModel):
    """Exactly the response fields of Telegraph's URL_SAFE_GENERAL intent. Nothing else."""

    model_config = ConfigDict(extra="forbid")
    url: str
    verdict: Verdict
    detections: int = Field(ge=0, le=1000)
    summary: str


def _payload(res: UrlResult) -> dict:
    m = UrlSafeResponse(url=res.url, verdict=res.verdict, detections=min(max(res.detections, 0), 1000),
                        summary=res.summary)
    return {"url": m.url, "verdict": m.verdict.value, "detections": m.detections, "summary": m.summary}


def create_app(settings: Optional[Settings] = None, pipeline: Optional[UrlSafePipeline] = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging(settings.log_level)
        client = httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent},
            limits=httpx.Limits(max_connections=60, max_keepalive_connections=20),
            follow_redirects=True,
        )
        app.state.pipeline = pipeline if pipeline is not None else UrlSafePipeline(settings, client)
        try:
            await asyncio.wait_for(app.state.pipeline.start(), timeout=settings.feed_timeout + 2)
        except Exception:  # noqa: BLE001 - a feed that is slow to load must not stop the service starting
            log.warning("feed preload did not finish; it will load in the background")
        feed = app.state.pipeline.feed
        log.info("startup", extra={"fields": {"feed_items": feed.size if feed else None}})
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(title="Telegraph URL Safety Miner", version=VERSION, lifespan=lifespan)

    @app.get("/healthz")
    async def healthz(request: Request):
        pl: UrlSafePipeline = request.app.state.pipeline
        return {"status": "ok", "version": VERSION, "cache_items": pl.cache.size(),
                "feed": {"ready": bool(pl.feed and pl.feed.ready), "items": pl.feed.size if pl.feed else 0}}

    @app.get("/miner.yaml")
    async def miner_yaml():
        """Serve the registration YAML verbatim: the on-chain SHA-256 covers exactly these bytes."""
        path = Path(__file__).resolve().parent.parent / "miner.yaml"
        if not path.is_file():
            return JSONResponse({"status": "not_found"}, status_code=404)
        return Response(content=path.read_bytes(), media_type="application/yaml")

    @app.get("/")
    async def root():
        return {"service": "telegraph-url-safety-miner", "intent": "URL_SAFE_GENERAL",
                "endpoints": ["POST /url-safe", "GET /healthz"]}

    @app.post("/url-safe")
    async def url_safe(request: Request):
        pl: UrlSafePipeline = request.app.state.pipeline
        raw = (await read_fields(request, FIELDS))["url"]
        try:
            res = await asyncio.wait_for(pl.check(raw), timeout=settings.global_timeout)
        except asyncio.TimeoutError:
            log.warning("global timeout", extra={"fields": {"event": "global_timeout", "url": raw[:300]}})
            res = fallback_result(raw, "timed out")
        except Exception as exc:  # noqa: BLE001
            log.exception("unhandled error in /url-safe")
            res = fallback_result(raw, f"internal error: {type(exc).__name__}")
        return JSONResponse(_payload(res))

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error")
        if request.url.path.rstrip("/") == "/url-safe":
            return JSONResponse(_payload(fallback_result("", "internal error")))
        return JSONResponse({"status": "error"}, status_code=500)

    return app


app = create_app()
