"""One FastAPI app per intent, built the same way every time.

Guarantee: `POST <path>` ALWAYS answers HTTP 200 with exactly the response model's fields, for a
malformed body, a timeout, an internal error, and every upstream being down. Telegraph revokes miners
that error or go silent, so nothing here may raise to the client.

A pipeline is any object with:
    cache                         a `TTLCache` (the gateway's /healthz reads `cache.size()`)
    async answer(req) -> Model    `req` maps each declared request field to a stripped string
    fallback(req, reason) -> Model  the honest "could not establish" answer; must always be valid
    async start()                 optional warm-up, bounded by `start_timeout`
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Callable, Mapping, Optional, Sequence

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .logging_setup import setup_logging
from .request import read_fields
from .settings import BaseSettings

log = logging.getLogger("intentkit.service")

#: A number that may be unknown (null) but is never NaN or infinite (neither is valid JSON).
Num = Annotated[Optional[float], Field(allow_inf_nan=False)]


class StrictModel(BaseModel):
    """Base for response models: exactly the spec's fields, nothing else."""

    model_config = ConfigDict(extra="forbid")


def _dump(pipeline, model: type[BaseModel], req: Mapping[str, str], res: Any, reason: str) -> dict:
    try:
        if not isinstance(res, model):
            res = model.model_validate(res)
        return res.model_dump(mode="json")
    except Exception:  # noqa: BLE001 - an invalid answer is replaced by the honest empty one
        log.exception("answer did not fit the response model", extra={"fields": {"reason": reason}})
        return pipeline.fallback(dict(req), "invalid answer").model_dump(mode="json")


def create_service_app(
    *,
    title: str,
    intent: str,
    path: str,
    version: str,
    fields: Mapping[str, Sequence[str]],
    model: type[BaseModel],
    settings: BaseSettings,
    pipeline: Any = None,
    make_pipeline: Callable[[BaseSettings, httpx.AsyncClient], Any],
    start_timeout: float = 5.0,
    max_len: int = 2000,
) -> FastAPI:
    path = "/" + path.strip("/")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging(settings.log_level)
        client = httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent},
            limits=httpx.Limits(max_connections=60, max_keepalive_connections=20),
            follow_redirects=True,
        )
        app.state.pipeline = pipeline if pipeline is not None else make_pipeline(settings, client)
        start = getattr(app.state.pipeline, "start", None)
        if start is not None:
            try:
                await asyncio.wait_for(start(), timeout=start_timeout)
            except Exception:  # noqa: BLE001 - a slow warm-up must not stop the service starting
                log.warning("warm-up did not finish; it will complete in the background",
                            extra={"fields": {"intent": intent}})
        log.info("startup", extra={"fields": {"intent": intent}})
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(title=title, version=version, lifespan=lifespan)
    app.state.response_model = model      # lets tests compare the model with Telegraph's published spec
    app.state.intent = intent
    app.state.make_pipeline = make_pipeline
    app.state.fields = dict(fields)

    @app.get("/healthz")
    async def healthz(request: Request):
        pl = request.app.state.pipeline
        return {"status": "ok", "version": version, "intent": intent, "cache_items": pl.cache.size()}

    @app.get("/miner.yaml")
    async def miner_yaml():
        """Serve the registration YAML verbatim: the on-chain SHA-256 covers exactly these bytes."""
        p = Path(__file__).resolve().parent.parent / "miner.yaml"
        if not p.is_file():
            return JSONResponse({"status": "not_found"}, status_code=404)
        return Response(content=p.read_bytes(), media_type="application/yaml")

    @app.get("/")
    async def root():
        return {"service": title, "intent": intent, "endpoints": [f"POST {path}", "GET /healthz"]}

    @app.post(path)
    async def answer(request: Request):
        pl = request.app.state.pipeline
        req = await read_fields(request, fields, max_len=max_len, accept_numbers=True)
        reason = ""
        try:
            res = await asyncio.wait_for(pl.answer(req), timeout=settings.global_timeout)
        except asyncio.TimeoutError:
            reason = "global timeout"
            log.warning("global timeout", extra={"fields": {"event": "global_timeout", "intent": intent,
                                                            "req": {k: v[:200] for k, v in req.items()}}})
            res = pl.fallback(req, reason)
        except Exception as exc:  # noqa: BLE001
            reason = f"exception:{type(exc).__name__}"
            log.exception(f"unhandled error in {path}")
            res = pl.fallback(req, reason)
        return JSONResponse(_dump(pl, model, req, res, reason))

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error")
        if request.url.path.rstrip("/") == path:
            pl = getattr(request.app.state, "pipeline", None)
            if pl is not None:
                return JSONResponse(pl.fallback({k: "" for k in fields}, "exception").model_dump(mode="json"))
        return JSONResponse({"status": "error"}, status_code=500)

    return app
