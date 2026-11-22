"""Base class for an intent pipeline: caching, single-flight, short TTL when degraded, one log line.

A subclass implements:
    key(req)            cache key, or None for "do not cache" (for example an empty request)
    compute(req)        do the lookup and return `Result(model, degraded=..)`; may raise (the service
                        turns that into `fallback`)
    empty(req, reason)  the honest "could not establish" answer; always a valid response model
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Mapping, Optional

import httpx
from pydantic import BaseModel

from .cache import TTLCache
from .settings import BaseSettings
from .upstream import Upstream

Req = Mapping[str, str]


@dataclass(frozen=True)
class Result:
    model: BaseModel
    degraded: bool = False          # built on missing sources: cached briefly, logged
    ttl: Optional[float] = None     # overrides the settings' TTL for this answer


class BasePipeline:
    intent = ""

    def __init__(self, settings: BaseSettings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client
        self.up = Upstream(client, timeout=settings.provider_timeout)
        self.cache = TTLCache(max_items=settings.cache_max_items)
        self.log = logging.getLogger(f"intents.{self.intent.lower() or 'pipeline'}")

    def key(self, req: Req) -> Optional[str]:
        raise NotImplementedError

    async def compute(self, req: Req) -> Result:
        raise NotImplementedError

    def empty(self, req: Req, reason: str) -> BaseModel:
        raise NotImplementedError

    def fallback(self, req: Req, reason: str) -> BaseModel:
        return self.empty(req, reason)

    async def answer(self, req: Req) -> BaseModel:
        key = self.key(req)
        if key is None:
            return (await self._timed(req)).model
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        return await self.cache.single_flight(key, lambda: self._run(key, req))

    async def _run(self, key: str, req: Req) -> BaseModel:
        res = await self._timed(req)
        ttl = res.ttl if res.ttl is not None else (
            self.settings.cache_ttl_degraded if res.degraded else self.settings.cache_ttl)
        self.cache.set(key, res.model, ttl)
        return res.model

    async def _timed(self, req: Req) -> Result:
        t0 = time.monotonic()
        res = await self.compute(req)
        self.log.info("answered", extra={"fields": {
            "event": "answered", "intent": self.intent, "req": {k: v[:200] for k, v in req.items()},
            "degraded": res.degraded, "ms": int((time.monotonic() - t0) * 1000)}})
        return res


def median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def consensus(values: list[float], tolerance: float) -> Optional[float]:
    """Median of the values that sit within `tolerance` (a fraction) of the overall median.

    One wild reading must not decide a scored number, so with three or more readings the outliers are
    dropped before the final median; with fewer there is nothing to out-vote, so the plain median is used.
    """
    if not values:
        return None
    m = median(values)
    if len(values) < 3 or m == 0:
        return m
    kept = [v for v in values if abs(v - m) <= tolerance * abs(m)]
    return median(kept) if kept else m
