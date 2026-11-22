"""Shared helpers. Every test here is hermetic: providers are replaced by httpx's MockTransport."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ipgeo.config import Settings  # noqa: E402
from ipgeo.models import GeoFix  # noqa: E402


def make_settings(**kw) -> Settings:
    base = dict(provider_timeout=1.0, soft_deadline=0.3, global_timeout=3.0,
                cache_ttl=60, cache_ttl_degraded=5)
    base.update(kw)
    return Settings(**base)


class Routes:
    """host -> payload | Exception | (payload, delay_seconds). Counts the calls each host receives."""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls: dict[str, int] = {}
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.calls[host] = self.calls.get(host, 0) + 1
        self.requests.append(request)
        spec = self.routes.get(host)
        delay = 0.0
        if isinstance(spec, tuple):
            spec, delay = spec
        if delay:
            await asyncio.sleep(delay)
        if isinstance(spec, Exception):
            raise spec
        if spec is None:
            return httpx.Response(404, json={"error": "unrouted"})
        return httpx.Response(200, json=spec)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self), follow_redirects=True)

    @property
    def total(self) -> int:
        return sum(self.calls.values())


def fix(provider="p", lat=37.4, lon=-122.1, country="United States", code="US", city="Mountain View",
        isp="Google LLC") -> GeoFix:
    return GeoFix(provider=provider, latitude=lat, longitude=lon, country=country, country_code=code,
                  city=city, isp=isp)
