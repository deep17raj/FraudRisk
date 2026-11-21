"""FastAPI layer.

Guarantee: POST /audit ALWAYS answers HTTP 200 with exactly the four fields
{verdict, obligations, clauses, summary} - including for a malformed body, a timeout or an
internal error - because Telegraph revokes routing from miners that return errors, empty
bodies or nothing at all. `verdict` is always one of Telegraph's four allowed strings, and
`clauses` is never populated with anything but verbatim contract wording.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict

from .config import Settings
from .logging_setup import setup_logging
from .models import Audit, Verdict
from .pipeline import AuditPipeline, fallback_audit

log = logging.getLogger("audit.app")
VERSION = "0.1.0"

#: Field names a caller might use for the contract, in the order we trust them.
_INPUT_FIELDS = ("contract", "text", "document", "input", "query", "contract_text")
#: Hard ceiling on the raw request body, well above any real contract.
_MAX_BODY_BYTES = 12_000_000

EMPTY_SUMMARY = (
    "No contract was provided, so there is nothing to audit. Send a JSON body like "
    '{"contract": "<the contract text, or a link to it>"}.'
)


class AuditResponse(BaseModel):
    """Exactly the response fields of Telegraph's CONTRACT_OBLIGATION_AUDIT_GENERAL intent."""

    model_config = ConfigDict(extra="forbid")
    verdict: Verdict
    obligations: list[str]
    clauses: list[str]
    summary: str


def _payload(verdict: Verdict, obligations: list[str], clauses: list[str], summary: str) -> dict:
    """Final shaping and the last guard before anything leaves the process."""
    model = AuditResponse(
        verdict=verdict,
        obligations=[s.strip() for s in obligations if s and s.strip()],
        clauses=[c for c in clauses if c and c.strip()],
        summary=" ".join(summary.split()) or EMPTY_SUMMARY,
    )
    return {"verdict": model.verdict.value, "obligations": model.obligations,
            "clauses": model.clauses, "summary": model.summary}


def _from_audit(res: Audit) -> dict:
    return _payload(res.verdict, res.obligations, res.clauses, res.summary)


async def _read_contract(request: Request, settings: Settings) -> str:
    """Pull the contract out of the request, tolerating whatever shape the caller sent."""
    contract = ""
    try:
        raw = await request.body()
        if len(raw) <= _MAX_BODY_BYTES:
            import json

            body = json.loads(raw) if raw.strip() else None
            if isinstance(body, dict):
                for name in _INPUT_FIELDS:
                    value = body.get(name)
                    if isinstance(value, str) and value.strip():
                        contract = value
                        break
                    # Some routers wrap the params: {"params": {"contract": "..."}}
                    if isinstance(value, dict):
                        inner = value.get("contract")
                        if isinstance(inner, str) and inner.strip():
                            contract = inner
                            break
                if not contract and isinstance(body.get("params"), dict):
                    inner = body["params"].get("contract")
                    if isinstance(inner, str):
                        contract = inner
            elif isinstance(body, str):
                contract = body
    except Exception:
        contract = ""
    if not contract.strip():
        contract = request.query_params.get("contract") or ""
    return contract.strip()[: settings.max_contract_chars]


def create_app(settings: Optional[Settings] = None,
               pipeline: Optional[AuditPipeline] = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging(settings.log_level)
        client = httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent},
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=10),
            follow_redirects=False,  # fetch.py follows them itself, re-checking every hop
            timeout=httpx.Timeout(settings.fetch_timeout),
        )
        app.state.pipeline = pipeline or AuditPipeline(settings, client)
        pl: AuditPipeline = app.state.pipeline
        log.info("startup", extra={"fields": {
            "event": "startup", "version": VERSION, "llm": pl.uses_llm,
            "model": settings.model, "effort": settings.effort,
            "url_fetch": settings.enable_url_fetch}})
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(title="Telegraph Contract Obligation Auditor", version=VERSION,
                  lifespan=lifespan)

    @app.get("/healthz")
    async def healthz(request: Request):
        pl: AuditPipeline = request.app.state.pipeline
        return {"status": "ok", "version": VERSION, "llm_enabled": pl.uses_llm,
                "model": settings.model, "url_fetch": settings.enable_url_fetch,
                "cache_items": pl.cache.size()}

    @app.get("/")
    async def root():
        return {"service": "telegraph-contract-obligation-auditor",
                "intent": "CONTRACT_OBLIGATION_AUDIT_GENERAL",
                "endpoints": ["POST /audit", "GET /healthz", "GET /miner.yaml"]}

    @app.get("/miner.yaml")
    async def miner_yaml():
        """Serve the registration config so YAML_URL can point straight at this host.

        The bytes are returned exactly as they are on disk, unmodified: the SHA-256 recorded
        on-chain must match what this route serves, so nothing here may re-encode or
        reformat them. `register.sh` verifies the hash of this response against the file.
        """
        path = Path(__file__).resolve().parent.parent / "miner.yaml"
        try:
            raw = path.read_bytes()
        except OSError:
            return JSONResponse({"error": "miner.yaml is not bundled with this deployment"},
                                status_code=404)
        return Response(content=raw, media_type="application/yaml",
                        headers={"Cache-Control": "no-cache"})

    @app.post("/audit")
    async def audit(request: Request):
        pl: AuditPipeline = request.app.state.pipeline
        t0 = time.monotonic()
        contract = await _read_contract(request, settings)

        if not contract:
            return JSONResponse(_payload(Verdict.UNCLEAR, [], [], EMPTY_SUMMARY))

        try:
            res = await asyncio.wait_for(pl.audit(contract), timeout=settings.global_timeout)
        except asyncio.TimeoutError:
            log.warning("global timeout", extra={"fields": {
                "event": "global_timeout", "contract": contract[:300]}})
            res = fallback_audit("global_timeout")
        except Exception as exc:
            log.exception("unhandled error in /audit")
            res = fallback_audit(f"exception:{type(exc).__name__}")

        payload = _from_audit(res)
        log.info("audit", extra={"fields": {
            "event": "audit", "contract": contract[:300], "chars_in": len(contract),
            "verdict": payload["verdict"], "rule": res.rule,
            "obligations": payload["obligations"], "n_clauses": len(payload["clauses"]),
            "degraded": res.degraded, "diagnostics": res.diagnostics,
            "latency_ms": int((time.monotonic() - t0) * 1000)}})
        return JSONResponse(payload)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error")
        if request.url.path == "/audit":
            return JSONResponse(_payload(
                Verdict.UNCLEAR, [], [],
                "An internal error prevented this contract from being audited, so no verdict "
                "is asserted. The result is Unclear because the audit did not complete, not "
                "because the contract is deficient."))
        return JSONResponse({"status": "error"}, status_code=500)

    return app


app = create_app()
