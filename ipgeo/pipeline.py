"""Locate an IP: ask every provider at once, collect what arrives by the soft deadline, answer with the consensus.

Stopping at the first two agreeing providers would be faster but makes the scored latitude depend on which
two happen to answer first (the same address came back as 37.67 and 37.38 in two live runs). Waiting for
all of them, up to `soft_deadline`, lets the consensus step reject outliers and gives a stable answer.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from typing import Optional

import httpx

from intentkit.cache import TTLCache
from intentkit.resilience import CircuitBreaker, call_with_retry

from .config import Settings
from .consensus import merge
from .models import GeoFix, GeoResult
from .parse import extract_ip
from .providers import PROVIDERS, Provider, select_providers

log = logging.getLogger("ipgeo.pipeline")


def fallback_result(raw: str, reason: str) -> GeoResult:
    """The honest answer when nothing could be established: the address, and no location."""
    return GeoResult(ip=(extract_ip(raw) or raw.strip())[:64], degraded=True, note=reason)


class IpGeoPipeline:
    def __init__(self, settings: Settings, client: httpx.AsyncClient,
                 providers: Optional[tuple[Provider, ...]] = None) -> None:
        self.settings = settings
        self.client = client
        self.providers = providers if providers is not None else select_providers(settings)
        self.rank = {p.name: p.rank for p in self.providers}
        self.cache = TTLCache(max_items=settings.cache_max_items)
        self._breakers = {p.name: CircuitBreaker() for p in self.providers}

    async def locate(self, raw: str) -> GeoResult:
        ip = extract_ip(raw)
        if ip is None:
            return GeoResult(ip=raw.strip()[:64], note="no IP address found in the request")
        if not ipaddress.ip_address(ip).is_global:
            # Private, loopback, link-local, documentation and other reserved ranges have no
            # location, and no provider will invent one. Do not spend network calls on them.
            return GeoResult(ip=ip, note="not a public address")
        hit = self.cache.get(ip)
        if hit is not None:
            return hit
        return await self.cache.single_flight(ip, lambda: self._resolve(ip))

    async def _fetch(self, provider: Provider, ip: str) -> Optional[GeoFix]:
        url, params = provider.url(ip, self.settings)

        async def fn():
            r = await self.client.get(url, params=params, timeout=self.settings.provider_timeout,
                                      follow_redirects=True)
            r.raise_for_status()
            return r.json()

        try:
            data = await call_with_retry(fn, self._breakers[provider.name], retries=0)
            return provider.parse(data)
        except Exception as exc:  # noqa: BLE001 - one provider failing is not an error
            log.debug("provider failed", extra={"fields": {
                "provider": provider.name, "ip": ip, "error": f"{type(exc).__name__}: {exc}"}})
            return None

    async def _resolve(self, ip: str) -> GeoResult:
        s = self.settings
        start = time.monotonic()
        pending = {asyncio.ensure_future(self._fetch(p, ip)) for p in self.providers}
        fixes: list[GeoFix] = []
        try:
            while pending:
                elapsed = time.monotonic() - start
                budget = s.provider_timeout + 0.5 - elapsed
                if fixes:
                    budget = min(budget, s.soft_deadline - elapsed)
                if budget <= 0:
                    break
                done, pending = await asyncio.wait(pending, timeout=budget,
                                                   return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    break
                for task in done:
                    fix = task.result()
                    if fix is not None:
                        fixes.append(fix)
        finally:
            for task in pending:
                task.cancel()

        merged = merge(fixes, s.cluster_km, self.rank)
        if merged is None:
            result = GeoResult(ip=ip, degraded=True, note="no provider could locate this address")
            self.cache.set(ip, result, s.cache_ttl_degraded)
        else:
            result = GeoResult(ip=ip, country=merged.country, city=merged.city, latitude=merged.latitude,
                               longitude=merged.longitude, isp=merged.isp, providers=merged.providers)
            self.cache.set(ip, result, s.cache_ttl)
        log.info("located", extra={"fields": {
            "event": "located", "ip": ip, "answered": [f.provider for f in fixes],
            "agreeing": list(result.providers), "lat": result.latitude, "lon": result.longitude,
            "country": result.country, "ms": int((time.monotonic() - start) * 1000)}})
        return result


__all__ = ["IpGeoPipeline", "fallback_result", "PROVIDERS"]
