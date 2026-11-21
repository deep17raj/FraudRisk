"""Stage 2 - evidence retrieval.

Runs every enabled source in parallel under one shared time budget. Slow sources are cancelled
rather than awaited, so one laggy provider can never stall the request; whatever finished in time
is used. Every call goes through that source's circuit breaker + a bounded retry.
"""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from .models import Evidence, ParsedClaim, RetrievalResult
from .resilience import CircuitOpen, call_with_retry
from .sources.base import Source

log = logging.getLogger("factcheck.retrieval")

MAX_EVIDENCE = 16
_TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid", "ref", "ref_src"}


def canonical_url(url: str) -> str:
    try:
        p = urlparse(url.strip())
    except ValueError:
        return url
    q = [(k, v) for k, v in parse_qsl(p.query) if k.lower() not in _TRACKING]
    host = p.netloc.lower().removeprefix("www.")
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower() or "https", host, path, "", urlencode(q), ""))


class Retriever:
    def __init__(self, sources: list[Source], client: httpx.AsyncClient, provider_timeout: float = 4.5) -> None:
        self.sources = sources
        self.client = client
        self.provider_timeout = provider_timeout

    def active_sources(self, claim: ParsedClaim) -> tuple[list[Source], dict[str, str]]:
        status: dict[str, str] = {}
        active: list[Source] = []
        for s in self.sources:
            if not s.enabled():
                continue
            if s.recency_only and not claim.time_sensitive:
                status[s.name] = "skipped"
                continue
            active.append(s)
        return active, status

    async def retrieve(self, claim: ParsedClaim, budget_s: float) -> RetrievalResult:
        t0 = time.monotonic()
        deadline = t0 + budget_s
        active, status = self.active_sources(claim)

        async def run(src: Source) -> list[Evidence]:
            remaining = max(0.5, deadline - time.monotonic())
            return await call_with_retry(
                lambda: src.search(claim, self.client, min(self.provider_timeout, remaining)),
                src.breaker,
                deadline=deadline,
            )

        tasks = {asyncio.create_task(run(s)): s for s in active}
        collected: dict[str, list[Evidence]] = {}
        if tasks:
            done, pending = await asyncio.wait(tasks.keys(), timeout=budget_s)
            for t in pending:
                t.cancel()
                status[tasks[t].name] = "timeout"
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for t in done:
                src = tasks[t]
                exc = t.exception()
                if exc is None:
                    items = t.result()
                    collected[src.name] = items
                    status[src.name] = "ok" if items else "empty"
                elif isinstance(exc, CircuitOpen):
                    status[src.name] = "circuit_open"
                elif isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)):
                    status[src.name] = "timeout"
                else:
                    status[src.name] = "error"
                    log.warning("source %s failed: %s: %s", src.name, type(exc).__name__, exc)

        # priority order = order in the sources list; dedupe by canonical URL
        seen: set[str] = set()
        merged: list[Evidence] = []
        for s in active:
            for ev in collected.get(s.name, []):
                key = canonical_url(ev.url)
                if key in seen or not ev.url.startswith(("http://", "https://")):
                    continue
                seen.add(key)
                merged.append(ev)
        merged = merged[:MAX_EVIDENCE]
        for i, ev in enumerate(merged, start=1):
            ev.id = i
        return RetrievalResult(evidence=merged, provider_status=status, elapsed_ms=int((time.monotonic() - t0) * 1000))
