"""Circuit breaker + bounded retry used around every external provider call."""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, TypeVar

import httpx

T = TypeVar("T")


class CircuitOpen(Exception):
    pass


class CircuitBreaker:
    """closed -> (N consecutive failures) -> open -> (cooldown) -> half-open -> closed | open.

    In half-open state exactly one probe call is let through; concurrent callers keep failing fast.
    """

    def __init__(self, failure_threshold: int = 4, cooldown_s: float = 30.0, clock: Callable[[], float] = time.monotonic):
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._probing = False

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self.cooldown_s:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        s = self.state
        if s == "closed":
            return True
        if s == "half_open" and not self._probing:
            self._probing = True
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._probing = False

    def record_failure(self) -> None:
        self._failures += 1
        self._probing = False
        if self._failures >= self.failure_threshold or self._opened_at is not None:
            self._opened_at = self._clock()


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TransportError, asyncio.TimeoutError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


async def call_with_retry(
    fn: Callable[[], Awaitable[T]],
    breaker: CircuitBreaker,
    *,
    retries: int = 1,
    backoff_s: float = 0.25,
    deadline: float | None = None,
) -> T:
    """Run `fn` through the breaker. Retries once on transient errors if time allows.

    `deadline` is an absolute `time.monotonic()` value; no retry is attempted if it would overrun.
    """
    attempt = 0
    while True:
        if not breaker.allow():
            raise CircuitOpen()
        try:
            result = await fn()
        except asyncio.CancelledError:
            breaker._probing = False
            raise
        except Exception as exc:
            transient = is_transient(exc)
            # Only transient failures count against the provider; a 4xx is our bug, not an outage.
            if transient:
                breaker.record_failure()
            else:
                breaker._probing = False
            attempt += 1
            out_of_time = deadline is not None and time.monotonic() + backoff_s + 0.5 > deadline
            if not transient or attempt > retries or out_of_time:
                raise
            await asyncio.sleep(backoff_s * attempt)
            continue
        breaker.record_success()
        return result
