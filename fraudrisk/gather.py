"""Stage 2 - parallel signal gathering under a hard deadline.

Every relevant source runs concurrently. Each one is wrapped in its own circuit breaker and
a single bounded retry, so a flaky provider degrades to a missing signal rather than a failed
request: the status map records *why* each source produced nothing, and the pipeline uses
that to decide whether "no signal" means "clean" or "we could not check".

Offline sources (the rule library, the name heuristics) cannot time out and are awaited
separately, which guarantees a scenario subject still gets a full behavioural assessment even
if every network provider is down.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from .config import Settings
from .models import GatherResult, ParsedSubject, Signal
from .resilience import CircuitOpen, call_with_retry
from .sources.base import Source

log = logging.getLogger("fraudrisk.gather")

#: Per-source cap on emitted signals, so one chatty provider cannot flood the signal list.
MAX_SIGNALS_PER_SOURCE = 6
MAX_SIGNALS_TOTAL = 14


class Gatherer:
    def __init__(self, sources: list[Source], client: httpx.AsyncClient, settings: Settings) -> None:
        self.sources = sources
        self.client = client
        self.s = settings

    def uncovered_kinds(self, parsed: ParsedSubject) -> list[str]:
        """Entity kinds in this subject that NO enabled source can say anything about.

        Needed for honesty: a bare wallet address matches no source, and reporting "the
        sources that apply returned no adverse finding" would claim a check that never
        happened. The pipeline words that case as insufficient information instead.
        """
        handled: set = set()
        for src in self.sources:
            handled.update(src.handles)
        kinds = {e.kind for e in parsed.entities}
        return sorted(k.value for k in kinds - handled)

    async def gather(self, parsed: ParsedSubject, budget: float) -> GatherResult:
        t0 = time.monotonic()
        status: dict[str, str] = {}
        tasks: dict[str, asyncio.Task] = {}
        deadline = t0 + max(0.1, budget)

        for src in self.sources:
            if not src.relevant(parsed):
                status[src.name] = "skipped"
                continue
            tasks[src.name] = asyncio.ensure_future(self._run_one(src, parsed, deadline))

        signals: list[Signal] = []
        if tasks:
            done = await asyncio.gather(*tasks.values(), return_exceptions=True)
            for name, result in zip(tasks.keys(), done):
                if isinstance(result, BaseException):
                    status[name] = _classify_error(result)
                    continue
                got, why = result
                status[name] = why
                signals.extend(got[:MAX_SIGNALS_PER_SOURCE])

        signals = _dedupe(signals)[:MAX_SIGNALS_TOTAL]
        return GatherResult(signals=signals, source_status=status,
                            elapsed_ms=int((time.monotonic() - t0) * 1000))

    async def _run_one(self, src: Source, parsed: ParsedSubject,
                       deadline: float) -> tuple[list[Signal], str]:
        remaining = deadline - time.monotonic()
        if remaining <= 0 and not src.offline:
            return [], "timeout"

        async def call() -> list[Signal]:
            timeout = min(self.s.provider_timeout, max(0.2, deadline - time.monotonic()))
            return await src.collect(parsed, self.client, timeout, self.s)

        try:
            if src.offline:
                got = await call()
            else:
                got = await asyncio.wait_for(
                    call_with_retry(call, src.breaker, retries=1, backoff_s=0.2, deadline=deadline),
                    timeout=max(0.2, deadline - time.monotonic()),
                )
        except CircuitOpen:
            return [], "circuit_open"
        except (asyncio.TimeoutError, TimeoutError):
            return [], "timeout"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("source failed", extra={"fields": {
                "event": "source_failed", "source": src.name,
                "error": f"{type(exc).__name__}: {exc}"}})
            return [], "error"
        return (got or []), ("ok" if got else "empty")


def _classify_error(exc: BaseException) -> str:
    if isinstance(exc, CircuitOpen):
        return "circuit_open"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    return "error"


def _dedupe(signals: list[Signal]) -> list[Signal]:
    """Drop repeats of the same signal id, and put risk-bearing signals before benign ones."""
    seen: set[str] = set()
    unique: list[Signal] = []
    for s in signals:
        if s.id in seen:
            continue
        seen.add(s.id)
        unique.append(s)
    unique.sort(key=lambda s: (s.benign, -s.weight))
    return unique
