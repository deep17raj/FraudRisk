"""FastAPI layer. Guarantee: POST /fact-check ALWAYS answers HTTP 200 with exactly the four fields
{claim, verdict, reasoning, sources} - including for malformed bodies, timeouts and internal errors -
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
from pydantic import BaseModel, ConfigDict

from .config import Settings
from .logging_setup import setup_logging
from .models import Verdict
from .pipeline import FactCheckPipeline, fallback_result

log = logging.getLogger("factcheck.app")
VERSION = "0.1.0"


class FactCheckResponse(BaseModel):
    """Exactly the response fields of Telegraph's FACT_CHECK_GENERAL intent. Nothing else."""

    model_config = ConfigDict(extra="forbid")
    claim: str
    verdict: Verdict
    reasoning: str
    sources: list[str]


def _payload(claim: str, verdict: Verdict, reasoning: str, sources: list[str]) -> dict:
    # Validate through the strict model, then emit plain JSON with the enum as its exact string.
    m = FactCheckResponse(claim=claim, verdict=verdict, reasoning=reasoning, sources=sources)
    return {"claim": m.claim, "verdict": m.verdict.value, "reasoning": m.reasoning, "sources": m.sources}


def create_app(settings: Optional[Settings] = None, pipeline: Optional[FactCheckPipeline] = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging(settings.log_level)
        client = httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent},
            limits=httpx.Limits(max_connections=60, max_keepalive_connections=20),
            follow_redirects=True,
            http2=True,   # Wikimedia's edge 403s some HTTP/1.1 clients; HTTP/2 is also faster for keep-alive
        )
        if pipeline is None:
            app.state.pipeline = FactCheckPipeline(settings, client)
        else:
            app.state.pipeline = pipeline
        pl = app.state.pipeline
        log.info("startup", extra={"fields": {
            "sources": [s.name for s in pl.sources], "llm": pl.stance.uses_llm, "model": settings.model,
            "stance_mode": settings.stance_mode}})
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(title="Telegraph Fact-Check Miner", version=VERSION, lifespan=lifespan)

    @app.get("/healthz")
    async def healthz(request: Request):
        pl: FactCheckPipeline = request.app.state.pipeline
        return {
            "status": "ok",
            "version": VERSION,
            "sources": [s.name for s in pl.sources],
            "llm_enabled": pl.stance.uses_llm,
            "cache_items": pl.cache.size(),
        }

    @app.get("/miner.yaml")
    async def miner_yaml():
        """Serve the registration YAML as raw bytes, so YAML_URL can point here.

        The on-chain commitment is the SHA-256 of exactly these bytes: the file is sent verbatim
        (no re-serialisation, no newline translation).
        """
        path = Path(__file__).resolve().parent.parent / "miner.yaml"
        if not path.is_file():
            return JSONResponse({"status": "not_found"}, status_code=404)
        return Response(content=path.read_bytes(), media_type="application/yaml")

    @app.get("/")
    async def root():
        return {"service": "telegraph-fact-check-miner", "intent": "FACT_CHECK_GENERAL",
                "endpoints": ["POST /fact-check", "GET /healthz"]}

    @app.post("/fact-check")
    async def fact_check(request: Request):
        pl: FactCheckPipeline = request.app.state.pipeline
        claim = ""
        try:
            body = await request.json()
            if isinstance(body, dict) and isinstance(body.get("claim"), str):
                claim = body["claim"].strip()
        except Exception:
            claim = ""
        if not claim:
            claim = (request.query_params.get("claim") or "").strip()
        claim = claim[:2000]

        if not claim:
            return JSONResponse(_payload(
                "", Verdict.UNVERIFIED,
                "No claim was provided, so there is nothing to check. Send a JSON body like "
                '{"claim": "<a specific, checkable factual assertion>"}.', []))
        try:
            res = await asyncio.wait_for(pl.check(claim), timeout=settings.global_timeout)
        except asyncio.TimeoutError:
            log.warning("global timeout", extra={"fields": {"event": "global_timeout", "claim": claim[:300]}})
            res = fallback_result(claim, "global_timeout")
        except Exception as exc:
            log.exception("unhandled error in /fact-check")
            res = fallback_result(claim, f"exception:{type(exc).__name__}")
        return JSONResponse(_payload(claim, res.verdict, res.reasoning, res.sources))

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error")
        if request.url.path == "/fact-check":
            return JSONResponse(_payload("", Verdict.UNVERIFIED,
                                         "An internal error prevented this claim from being checked. "
                                         "It is left Unverified.", []))
        return JSONResponse({"status": "error"}, status_code=500)

    return app


app = create_app()
