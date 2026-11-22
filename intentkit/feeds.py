"""Bulk threat feeds, refreshed in the background rather than fetched per request.

A feed is a whole-file download of a few hundred KB at most. Doing that inside a request would spend
the latency budget, so each feed is loaded once, served from memory and refreshed by a single detached
task when it goes stale. A refresh failure keeps the previous copy and never fails a request.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

log = logging.getLogger("intentkit.feeds")


class BulkFeed:
    """A set of strings refreshed on a TTL. `ready` is False until the first load lands."""

    def __init__(self, name: str, url: str, ttl: float, timeout: float) -> None:
        self.name = name
        self.url = url
        self.ttl = ttl
        self.timeout = timeout
        self._items: frozenset[str] = frozenset()
        self._loaded_at: float = 0.0
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @property
    def ready(self) -> bool:
        return bool(self._items)

    @property
    def stale(self) -> bool:
        return (time.monotonic() - self._loaded_at) > self.ttl

    @property
    def size(self) -> int:
        return len(self._items)

    def __contains__(self, item: str) -> bool:
        return item in self._items

    def parse(self, body: str) -> frozenset[str]:
        return frozenset(ln.strip() for ln in body.splitlines() if ln.strip() and not ln.startswith("#"))

    async def _load(self, client: httpx.AsyncClient) -> None:
        async with self._lock:
            try:
                r = await client.get(self.url, timeout=self.timeout, follow_redirects=True)
                r.raise_for_status()
                items = self.parse(r.text)
                if items:
                    self._items = items
                    self._loaded_at = time.monotonic()
                    log.info("feed loaded", extra={"fields": {
                        "event": "feed_loaded", "feed": self.name, "items": len(items)}})
            except Exception as exc:  # noqa: BLE001 - keep whatever we already had
                log.warning("feed refresh failed", extra={"fields": {
                    "event": "feed_refresh_failed", "feed": self.name,
                    "error": f"{type(exc).__name__}: {exc}"}})

    def ensure_fresh(self, client: httpx.AsyncClient) -> None:
        """Kick off a refresh if needed. Never awaited by a request - fire and forget."""
        if self._task and not self._task.done():
            return
        if self.ready and not self.stale:
            return
        self._task = asyncio.ensure_future(self._load(client))
        self._task.add_done_callback(lambda t: t.cancelled() or t.exception())

    async def preload(self, client: httpx.AsyncClient) -> None:
        """Awaited once at startup so the first request already has the feed.

        A feed that is already loaded and still fresh is left alone, so a warm process does not refetch
        on restart (and an injected test feed is never overwritten).
        """
        if self.ready and not self.stale:
            return
        await self._load(client)
