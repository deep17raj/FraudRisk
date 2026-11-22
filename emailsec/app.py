"""FastAPI layer. Guarantee: POST /email-security ALWAYS answers HTTP 200 with exactly the one field
{lookup_ok} - including for malformed bodies, timeouts and internal errors - because Telegraph
revokes miners that return errors or go silent."""
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

from intentkit.logging_setup import setup_logging
from intentkit.request import GENERIC_ALIASES, read_fields

from .config import Settings
from .pipeline import PREFERENCE, EmailSecPipeline, Lookup, fallback_lookup

log = logging.getLogger("emailsec.app")
VERSION = "0.1.0"
FIELDS = {
    "domain": ("name", "host", "email", *GENERIC_ALIASES),
    "record_type": ("type", "rtype", "record", "dns_type"),
}


class EmailSecResponse(BaseModel):
    """Exactly the response fields of Telegraph's EMAIL_SECURITY_GENERAL intent. Nothing else."""

    model_config = ConfigDict(extra="forbid")
    lookup_ok: bool


def _payload(res: Lookup) -> dict:
    return EmailSecResponse(lookup_ok=res.ok).model_dump()


def create_app(settings: Optional[Settings] = None, pipeline: Optional[EmailSecPipeline] = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging(settings.log_level)
        client = httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent},
            limits=httpx.Limits(max_connections=60, max_keepalive_connections=20),
            follow_redirects=True,
        )
        app.state.pipeline = pipeline if pipeline is not None else EmailSecPipeline(settings, client)
        log.info("startup", extra={"fields": {"resolvers": list(PREFERENCE)}})
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(title="Telegraph Email Security Miner", version=VERSION, lifespan=lifespan)

    @app.get("/healthz")
    async def healthz(request: Request):
        pl: EmailSecPipeline = request.app.state.pipeline
        return {"status": "ok", "version": VERSION, "resolvers": list(PREFERENCE), "cache_items": pl.cache.size()}

    @app.get("/miner.yaml")
    async def miner_yaml():
        """Serve the registration YAML verbatim: the on-chain SHA-256 covers exactly these bytes."""
        path = Path(__file__).resolve().parent.parent / "miner.yaml"
        if not path.is_file():
            return JSONResponse({"status": "not_found"}, status_code=404)
        return Response(content=path.read_bytes(), media_type="application/yaml")

    @app.get("/")
    async def root():
        return {"service": "telegraph-email-security-miner", "intent": "EMAIL_SECURITY_GENERAL",
                "endpoints": ["POST /email-security", "GET /healthz"]}

    @app.post("/email-security")
    async def email_security(request: Request):
        pl: EmailSecPipeline = request.app.state.pipeline
        got = await read_fields(request, FIELDS, max_len=500)
        domain, rtype = got["domain"], got["record_type"]
        try:
            res = await asyncio.wait_for(pl.lookup(domain, rtype), timeout=settings.global_timeout)
        except asyncio.TimeoutError:
            log.warning("global timeout", extra={"fields": {"event": "global_timeout", "domain": domain[:200]}})
            res = fallback_lookup(domain, rtype, "global_timeout")
        except Exception as exc:  # noqa: BLE001
            log.exception("unhandled error in /email-security")
            res = fallback_lookup(domain, rtype, f"exception:{type(exc).__name__}")
        return JSONResponse(_payload(res))

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error")
        if request.url.path.rstrip("/") == "/email-security":
            return JSONResponse(_payload(fallback_lookup("", "", "exception")))
        return JSONResponse({"status": "error"}, status_code=500)

    return app


app = create_app()
