"""One HTTP client wrapper for every keyless data source the intents call.

`Upstream.fetch` never raises. It returns `None` when the source could not be asked (network error,
timeout, HTTP 429/5xx, open circuit), and `(status, data)` when the source answered, so a caller can tell
"the source says this does not exist" (a 404) from "we could not ask".
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from .resilience import CircuitBreaker, call_with_retry

log = logging.getLogger("intentkit.upstream")


class Upstream:
    def __init__(self, client: httpx.AsyncClient, timeout: float = 3.0,
                 failure_threshold: int = 4, cooldown_s: float = 30.0) -> None:
        self.client = client
        self.timeout = timeout
        self._threshold, self._cooldown = failure_threshold, cooldown_s
        self._breakers: dict[str, CircuitBreaker] = {}

    def breaker(self, key: str) -> CircuitBreaker:
        if key not in self._breakers:
            self._breakers[key] = CircuitBreaker(self._threshold, self._cooldown)
        return self._breakers[key]

    async def fetch(self, url: str, *, method: str = "GET", params: Optional[dict] = None,
                    json_body: Any = None, headers: Optional[dict] = None, timeout: Optional[float] = None,
                    retries: int = 0, key: Optional[str] = None, text: bool = False) -> Optional[tuple[int, Any]]:
        t = timeout or self.timeout
        breaker = self.breaker(key or urlsplit(url).netloc)

        async def fn() -> tuple[int, Any]:
            # httpx timeouts apply per network phase, so cap the whole call as well.
            r = await asyncio.wait_for(
                self.client.request(method, url, params=params, json=json_body, headers=headers, timeout=t),
                timeout=t + 0.5)
            if r.status_code == 429 or r.status_code >= 500:
                r.raise_for_status()
            if text:
                return r.status_code, r.text
            try:
                return r.status_code, r.json()
            except ValueError:
                return r.status_code, None

        try:
            return await call_with_retry(fn, breaker, retries=retries)
        except Exception as exc:  # noqa: BLE001 - an unreachable source is "unknown", never an error
            log.debug("upstream call failed", extra={"fields": {
                "url": url, "error": f"{type(exc).__name__}: {exc}"}})
            return None

    async def get_json(self, url: str, **kw) -> Optional[Any]:
        """Parsed JSON of a 200 answer; None for anything else (including a 404)."""
        res = await self.fetch(url, **kw)
        return res[1] if res is not None and res[0] == 200 else None
