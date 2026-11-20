"""FastAPI layer.

Guarantee: POST /fraud-risk ALWAYS answers HTTP 200 with exactly the four fields
{risk_level, risk_score, signals, summary} - including for a malformed body, a timeout or an
internal error - because Telegraph revokes routing from miners that return errors, empty
bodies or nothing at all. `risk_level` is always one of LOW/MEDIUM/HIGH/CRITICAL and
`risk_score` is always a float in [0, 1] consistent with it.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .config import Settings
from .logging_setup import setup_logging
from .models import Assessment, RiskLevel
from .pipeline import FraudRiskPipeline, fallback_assessment
from .scoring import SCORE_DECIMALS, level_for_score

log = logging.getLogger("fraudrisk.app")
VERSION = "0.1.0"

EMPTY_SUBJECT_SUMMARY = (
    "No subject was provided, so there is nothing to assess. Send a JSON body like "
    '{"subject": "<an IP, card BIN, email, domain, wallet or a described transaction>"}.'
)


class FraudRiskResponse(BaseModel):
    """Exactly the response fields of Telegraph's FRAUD_RISK_GENERAL intent. Nothing else."""

    model_config = ConfigDict(extra="forbid")
    risk_level: RiskLevel
    risk_score: float = Field(ge=0.0, le=1.0)
    signals: list[str]
    summary: str


def _payload(level: RiskLevel, score: float, signals: list[str], summary: str) -> dict:
    """Final shaping and the last consistency guard before anything leaves the process."""
    score = round(max(0.0, min(1.0, float(score))), SCORE_DECIMALS)
    derived = level_for_score(score)
    if derived is not level:
        # Unreachable by construction (the scorer derives the level from this same score);
        # kept because an inconsistent pair is the one thing a judge penalises hardest.
        log.error("level/score inconsistency corrected", extra={"fields": {
            "event": "consistency_corrected", "given": level.value,
            "derived": derived.value, "score": score}})
        level = derived
    m = FraudRiskResponse(risk_level=level, risk_score=score,
                          signals=[s.strip() for s in signals if s and s.strip()],
                          summary=summary)
    return {"risk_level": m.risk_level.value, "risk_score": m.risk_score,
            "signals": m.signals, "summary": m.summary}


def _from_assessment(res: Assessment) -> dict:
    return _payload(res.level, res.score, res.signals, res.summary)


def create_app(settings: Optional[Settings] = None,
               pipeline: Optional[FraudRiskPipeline] = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging(settings.log_level)
        client = httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent},
            limits=httpx.Limits(max_connections=60, max_keepalive_connections=20),
            follow_redirects=True,
            http2=True,
        )
        app.state.pipeline = pipeline or FraudRiskPipeline(settings, client)
        pl: FraudRiskPipeline = app.state.pipeline
        log.info("startup", extra={"fields": {
            "event": "startup", "version": VERSION,
            "sources": [s.name for s in pl.sources],
            "llm": pl.reasoner.uses_llm, "model": settings.model, "effort": settings.effort}})
        # Warm the bulk feeds so the very first request already has them; never fatal.
        try:
            await asyncio.wait_for(pl.feeds.preload(client), timeout=settings.feed_refresh_timeout)
        except Exception as exc:
            log.warning("feed preload incomplete", extra={"fields": {
                "event": "feed_preload_failed", "error": f"{type(exc).__name__}: {exc}"}})
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(title="Telegraph Fraud Risk Miner", version=VERSION, lifespan=lifespan)

    @app.get("/healthz")
    async def healthz(request: Request):
        pl: FraudRiskPipeline = request.app.state.pipeline
        return {
            "status": "ok",
            "version": VERSION,
            "sources": [s.name for s in pl.sources],
            "llm_enabled": pl.reasoner.uses_llm,
            "feeds": pl.feeds.status(),
            "cache_items": pl.cache.size(),
        }

    @app.get("/")
    async def root():
        return {"service": "telegraph-fraud-risk-miner", "intent": "FRAUD_RISK_GENERAL",
                "endpoints": ["POST /fraud-risk", "GET /healthz", "GET /miner.yaml"]}

    @app.get("/miner.yaml")
    async def miner_yaml():
        """Serve the registration config so YAML_URL can point straight at this host.

        The bytes are returned exactly as they are on disk, unmodified: the SHA-256 recorded
        on-chain must match what this route serves, so nothing here may re-encode or reformat
        them. `register.sh` verifies the hash of this response against the local file.
        """
        path = Path(__file__).resolve().parent.parent / "miner.yaml"
        try:
            raw = path.read_bytes()
        except OSError:
            return JSONResponse({"error": "miner.yaml is not bundled with this deployment"},
                                status_code=404)
        return Response(content=raw, media_type="application/yaml",
                        headers={"Cache-Control": "no-cache"})

    @app.post("/fraud-risk")
    async def fraud_risk(request: Request):
        pl: FraudRiskPipeline = request.app.state.pipeline
        subject = ""
        try:
            body = await request.json()
            if isinstance(body, dict):
                for field_name in ("subject", "query", "input", "text"):
                    value = body.get(field_name)
                    if isinstance(value, str) and value.strip():
                        subject = value.strip()
                        break
            elif isinstance(body, str):
                subject = body.strip()
        except Exception:
            subject = ""
        if not subject:
            subject = (request.query_params.get("subject") or "").strip()
        subject = subject[:settings.max_subject_chars]

        if not subject:
            return JSONResponse(_payload(RiskLevel.LOW, 0.0, [], EMPTY_SUBJECT_SUMMARY))

        try:
            res = await asyncio.wait_for(pl.assess(subject), timeout=settings.global_timeout)
        except asyncio.TimeoutError:
            log.warning("global timeout", extra={"fields": {
                "event": "global_timeout", "subject": subject[:300]}})
            res = fallback_assessment(subject, "global_timeout")
        except Exception as exc:
            log.exception("unhandled error in /fraud-risk")
            res = fallback_assessment(subject, f"exception:{type(exc).__name__}")
        return JSONResponse(_from_assessment(res))

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error")
        if request.url.path == "/fraud-risk":
            return JSONResponse(_payload(
                RiskLevel.LOW, 0.0, [],
                "An internal error prevented this subject from being assessed. It is graded at "
                "the floor on insufficient information, not on any indication that it is safe."))
        return JSONResponse({"status": "error"}, status_code=500)

    return app


app = create_app()
