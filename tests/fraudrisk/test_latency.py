"""Latency and resilience: the service must never hang, never return empty, and must stay
well inside the global timeout even when providers misbehave.

Telegraph revokes routing from miners that time out or answer with nothing, so these are
contract tests, not performance nice-to-haves.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from fraudrisk.models import RiskLevel
from fraudrisk.pipeline import FraudRiskPipeline
from fraudrisk.resilience import CircuitBreaker, CircuitOpen, call_with_retry
from tests.fraudrisk.conftest import FakeSource, make_settings, sig
from tests.fraudrisk.test_regression import CASES, build_pipeline

SUBJECTS = [c["subject"] for c in CASES]


async def test_p95_latency_is_well_inside_the_budget(http_client):
    """The hermetic path is pure CPU: p95 must be a tiny fraction of the 11.5s budget."""
    settings = make_settings()
    timings = []
    for subject in SUBJECTS * 3:
        pl = build_pipeline(http_client)       # fresh pipeline: no cache hits
        t0 = time.monotonic()
        res = await pl.assess(subject)
        timings.append(time.monotonic() - t0)
        assert res.summary.strip() and res.level in set(RiskLevel)

    timings.sort()
    p95 = timings[int(len(timings) * 0.95) - 1]
    assert p95 < settings.global_timeout / 3, \
        f"p95={p95:.3f}s (max={max(timings):.3f}s) too close to {settings.global_timeout}s"
    assert max(timings) < settings.global_timeout


async def test_no_subject_ever_returns_an_empty_answer(http_client):
    for subject in SUBJECTS + ["", "   ", "?", "x" * 5000, "\u0000\u0001", "SELECT 1"]:
        pl = build_pipeline(http_client)
        res = await pl.assess(subject)
        assert res.summary.strip(), f"empty summary for {subject[:40]!r}"
        assert res.level in set(RiskLevel)
        assert 0.0 <= res.score <= 1.0


async def test_a_hanging_source_does_not_delay_the_answer(http_client):
    """A provider that never returns must cost at most the gather budget."""
    settings = make_settings(gather_timeout=0.4, global_timeout=3.0)
    slow = FakeSource(name="slow", delay=30.0, signals=[sig(id="never.arrives")])
    fast = FakeSource(name="fast", offline=True,
                      signals=[sig(id="behav.velocity", severity=0.5, reliability=0.85)])
    pl = FraudRiskPipeline(settings, http_client, sources=[slow, fast])

    t0 = time.monotonic()
    res = await pl.assess("14 declined attempts of $1 each")
    elapsed = time.monotonic() - t0

    assert elapsed < 1.5, f"took {elapsed:.2f}s"
    assert res.debug["sources"]["slow"] == "timeout"
    assert res.debug["sources"]["fast"] == "ok"
    assert res.score > 0, "the healthy source's signal must still count"


async def test_a_failing_source_is_dropped_not_fatal(http_client):
    broken = FakeSource(name="broken", error=httpx.ConnectError("refused"))
    healthy = FakeSource(name="healthy", offline=True,
                         signals=[sig(id="behav.velocity", severity=0.5, reliability=0.85)])
    pl = FraudRiskPipeline(make_settings(), http_client, sources=[broken, healthy])
    res = await pl.assess("22 declined authorisations")
    assert res.debug["sources"]["broken"] == "error"
    assert res.score > 0 and res.summary.strip()


async def test_all_sources_failing_yields_an_honest_degraded_answer(http_client):
    broken = [FakeSource(name=f"b{i}", error=httpx.ConnectError("refused")) for i in range(3)]
    pl = FraudRiskPipeline(make_settings(), http_client, sources=broken)
    res = await pl.assess("185.220.101.1")
    assert res.level is RiskLevel.LOW and res.score == 0.0
    assert "could not be reached" in res.summary.lower()
    assert res.cacheable is False, "a could-not-check answer must not be cached"
    # and it really is absent from the cache, so recovery is picked up on the next call
    again = await pl.assess("185.220.101.1")
    assert again.debug.get("cache_hit") is not True
    assert pl.cache.size() == 0


async def test_concurrent_identical_requests_share_one_computation(http_client):
    counting = FakeSource(name="counted", offline=True,
                          signals=[sig(id="behav.velocity", severity=0.5)])
    pl = FraudRiskPipeline(make_settings(), http_client, sources=[counting])
    results = await asyncio.gather(*(pl.assess("22 declined authorisations") for _ in range(8)))
    assert counting.calls == 1, f"single-flight failed: {counting.calls} calls"
    assert len({(r.level, r.score) for r in results}) == 1


async def test_throughput_under_concurrency(http_client):
    pl = build_pipeline(http_client)
    t0 = time.monotonic()
    results = await asyncio.gather(*(pl.assess(s) for s in SUBJECTS))
    elapsed = time.monotonic() - t0
    assert all(r.summary.strip() for r in results)
    assert elapsed < 5.0, f"{len(SUBJECTS)} concurrent assessments took {elapsed:.2f}s"


# --------------------------------------------------------------- circuit breaker

async def test_breaker_opens_after_repeated_failures_and_then_recovers():
    clock = {"t": 0.0}
    breaker = CircuitBreaker(failure_threshold=2, cooldown_s=10.0, clock=lambda: clock["t"])

    async def failing():
        raise httpx.ConnectError("refused")

    for _ in range(2):
        with pytest.raises(httpx.ConnectError):
            await call_with_retry(failing, breaker, retries=0)
    assert breaker.state == "open"

    with pytest.raises(CircuitOpen):
        await call_with_retry(failing, breaker, retries=0)

    clock["t"] += 11.0
    assert breaker.state == "half_open"

    async def ok():
        return ["recovered"]

    assert await call_with_retry(ok, breaker, retries=0) == ["recovered"]
    assert breaker.state == "closed"


async def test_a_client_error_does_not_trip_the_breaker():
    """A 4xx is our bug, not a provider outage."""
    breaker = CircuitBreaker(failure_threshold=2)
    request = httpx.Request("GET", "https://example.test")

    async def bad_request():
        raise httpx.HTTPStatusError(
            "bad", request=request, response=httpx.Response(400, request=request))

    for _ in range(5):
        with pytest.raises(httpx.HTTPStatusError):
            await call_with_retry(bad_request, breaker, retries=0)
    assert breaker.state == "closed"
