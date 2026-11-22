"""FastAPI layer. Guarantee: POST /ip-geolocation ALWAYS answers HTTP 200 with exactly the six fields
{ip, country, city, latitude, longitude, isp} - including for malformed bodies, timeouts and internal
errors - because Telegraph revokes miners that return errors or go silent."""
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
from .models import GeoResult
from .pipeline import IpGeoPipeline, fallback_result

log = logging.getLogger("ipgeo.app")
VERSION = "0.1.0"
FIELDS = {"ip": ("address", "host", *GENERIC_ALIASES)}


class IpGeoResponse(BaseModel):
    """Exactly the response fields of Telegraph's IP_GEOLOCATION_GENERAL intent. Nothing else."""

    model_config = ConfigDict(extra="forbid")
    ip: str
    country: str
    city: str
    latitude: Optional[float]
    longitude: Optional[float]
    isp: str


def _payload(res: GeoResult) -> dict:
    m = IpGeoResponse(ip=res.ip, country=res.country, city=res.city, latitude=res.latitude,
                      longitude=res.longitude, isp=res.isp)
    return m.model_dump()


def create_app(settings: Optional[Settings] = None, pipeline: Optional[IpGeoPipeline] = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging(settings.log_level)
        client = httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent},
            limits=httpx.Limits(max_connections=60, max_keepalive_connections=20),
            follow_redirects=True,
        )
        app.state.pipeline = pipeline if pipeline is not None else IpGeoPipeline(settings, client)
        log.info("startup", extra={"fields": {"providers": [p.name for p in app.state.pipeline.providers]}})
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(title="Telegraph IP Geolocation Miner", version=VERSION, lifespan=lifespan)

    @app.get("/healthz")
    async def healthz(request: Request):
        pl: IpGeoPipeline = request.app.state.pipeline
        return {"status": "ok", "version": VERSION, "providers": [p.name for p in pl.providers],
                "cache_items": pl.cache.size()}

    @app.get("/miner.yaml")
    async def miner_yaml():
        """Serve the registration YAML verbatim: the on-chain SHA-256 covers exactly these bytes."""
        path = Path(__file__).resolve().parent.parent / "miner.yaml"
        if not path.is_file():
            return JSONResponse({"status": "not_found"}, status_code=404)
        return Response(content=path.read_bytes(), media_type="application/yaml")

    @app.get("/")
    async def root():
        return {"service": "telegraph-ip-geolocation-miner", "intent": "IP_GEOLOCATION_GENERAL",
                "endpoints": ["POST /ip-geolocation", "GET /healthz"]}

    @app.post("/ip-geolocation")
    async def ip_geolocation(request: Request):
        pl: IpGeoPipeline = request.app.state.pipeline
        raw = (await read_fields(request, FIELDS))["ip"]
        try:
            res = await asyncio.wait_for(pl.locate(raw), timeout=settings.global_timeout)
        except asyncio.TimeoutError:
            log.warning("global timeout", extra={"fields": {"event": "global_timeout", "ip": raw[:64]}})
            res = fallback_result(raw, "global_timeout")
        except Exception as exc:  # noqa: BLE001
            log.exception("unhandled error in /ip-geolocation")
            res = fallback_result(raw, f"exception:{type(exc).__name__}")
        return JSONResponse(_payload(res))

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error")
        if request.url.path.rstrip("/") == "/ip-geolocation":
            return JSONResponse(_payload(GeoResult(ip="", degraded=True)))
        return JSONResponse({"status": "error"}, status_code=500)

    return app


app = create_app()
