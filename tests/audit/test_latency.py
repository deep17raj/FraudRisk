"""Latency and never-empty guarantees under load.

Telegraph spot-checks miners and revokes routing from endpoints that time out or answer
emptily, so these tests assert the two properties that keep routing: the service stays well
inside its budget, and it always answers.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from audit.app import create_app
from audit.config import Settings
from audit.models import Verdict
from audit.pipeline import AuditPipeline
from tests.audit.conftest import CLEAN, extraction, make_settings
from tests.audit.fixtures import CASES


class SlowLLM:
    """A model that takes `delay` seconds - or never returns at all."""

    available = True

    def __init__(self, payload, delay: float = 0.0, hang: bool = False):
        self.payload = payload
        self.delay = delay
        self.hang = hang

    async def complete_json(self, system, user, schema, *, max_tokens, timeout):
        if self.hang:
            await asyncio.sleep(3600)
        await asyncio.sleep(self.delay)
        return self.payload


PAYLOAD = extraction([
    {"party": "The Supplier", "duty": "deliver 500 units of Product X", "status": "met",
     "clause_ref": "1.1",
     "quote": "The Supplier shall deliver 500 units of Product X to the Buyer's warehouse"},
])


def pipeline_with(llm, settings: Settings | None = None) -> AuditPipeline:
    pl = AuditPipeline(settings or make_settings(), client=None)
    pl.llm = llm
    return pl


# --- the pipeline's own overhead --------------------------------------------------------

async def test_pipeline_overhead_is_small_with_an_instant_model():
    pl = pipeline_with(SlowLLM(PAYLOAD, delay=0.0))
    t0 = time.monotonic()
    for _ in range(20):
        pl.cache._data.clear()
        await pl.audit(CLEAN)
    per_call = (time.monotonic() - t0) / 20
    assert per_call < 0.25, f"pipeline overhead is {per_call*1000:.0f} ms per audit"


async def test_grounding_stays_fast_on_a_long_contract():
    """The citation search must not scale badly: contracts run to tens of thousands of words."""
    long_contract = CLEAN + "\n".join(
        f"{i}.1 The Supplier shall perform obligation number {i} by 1 March 2026."
        for i in range(2, 1200))
    pl = pipeline_with(SlowLLM(PAYLOAD, delay=0.0))
    t0 = time.monotonic()
    res = await pl.audit(long_contract)
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, f"a long contract took {elapsed:.2f}s"
    assert res.clauses and all(c in long_contract for c in res.clauses)


async def test_a_pathological_fabricated_quote_cannot_burn_the_budget():
    """The salvage search is bounded, so a long invented quote cannot stall a request."""
    long_contract = CLEAN * 60
    payload = extraction([
        {"party": "X", "duty": "do things", "status": "met",
         "quote": " ".join(f"invented clause fragment number {i}" for i in range(400))},
    ])
    pl = pipeline_with(SlowLLM(payload, delay=0.0))
    t0 = time.monotonic()
    res = await pl.audit(long_contract)
    assert time.monotonic() - t0 < 2.0
    assert res.verdict is not Verdict.COMPLIANT, "an unlocatable quote cannot support `met`"


# --- concurrency ------------------------------------------------------------------------

async def test_many_concurrent_audits_all_answer_inside_the_budget():
    settings = make_settings(global_timeout=13.5)
    pl = pipeline_with(SlowLLM(PAYLOAD, delay=0.15), settings)
    contracts = [c["contract"] + f"\n\n(copy {i})" for i, c in enumerate(CASES)] * 4

    t0 = time.monotonic()
    results = await asyncio.gather(*(
        asyncio.wait_for(pl.audit(c), timeout=settings.global_timeout) for c in contracts))
    elapsed = time.monotonic() - t0

    assert len(results) == len(contracts)
    assert elapsed < 10.0, f"{len(contracts)} concurrent audits took {elapsed:.1f}s"
    for res in results:
        assert res.verdict in set(Verdict)
        assert res.summary.strip(), "no audit may answer with an empty summary"


async def test_identical_concurrent_contracts_share_one_model_call():
    """Single-flight de-duplication: a spot-check burst must not fan out to N calls."""
    calls = {"n": 0}

    class Counting(SlowLLM):
        async def complete_json(self, *a, **kw):
            calls["n"] += 1
            await asyncio.sleep(0.2)
            return self.payload

    pl = pipeline_with(Counting(PAYLOAD), make_settings(cache_ttl=60))
    await asyncio.gather(*(pl.audit(CLEAN) for _ in range(25)))
    assert calls["n"] == 1, f"expected one shared call, made {calls['n']}"


# --- timeouts never produce silence ------------------------------------------------------

async def test_a_hanging_model_still_answers_within_the_budget():
    settings = make_settings(global_timeout=1.0, llm_timeout=0.3)
    pl = pipeline_with(SlowLLM(PAYLOAD, hang=True), settings)
    t0 = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        # The pipeline's own LLM timeout should fire first; if it did not, this proves the
        # outer guard would still catch it.
        await asyncio.wait_for(pl.audit(CLEAN), timeout=settings.global_timeout)
    assert time.monotonic() - t0 < settings.global_timeout + 0.5


async def test_an_llm_timeout_degrades_instead_of_failing():
    class Timeout(SlowLLM):
        async def complete_json(self, *a, **kw):
            from audit.llm import LLMUnavailable
            raise LLMUnavailable("timeout after 8.0s")

    pl = pipeline_with(Timeout(PAYLOAD), make_settings())
    res = await pl.audit(CLEAN)
    assert res.verdict is Verdict.UNCLEAR and res.obligations
    assert all(c in CLEAN for c in res.clauses)


# --- through HTTP -----------------------------------------------------------------------

def test_the_endpoint_never_returns_empty_under_a_burst():
    settings = make_settings(global_timeout=5.0)
    pl = pipeline_with(SlowLLM(PAYLOAD, delay=0.05), settings)
    with TestClient(create_app(settings, pipeline=pl)) as client:
        for case in CASES:
            r = client.post("/audit", json={"contract": case["contract"]})
            assert r.status_code == 200
            body = r.json()
            assert body["verdict"] in {v.value for v in Verdict}
            assert body["summary"].strip()
            assert set(body) == {"verdict", "obligations", "clauses", "summary"}


def test_a_slow_pipeline_is_cut_off_and_still_answers():
    settings = make_settings(global_timeout=0.3)
    pl = pipeline_with(SlowLLM(PAYLOAD, delay=5.0), settings)
    with TestClient(create_app(settings, pipeline=pl)) as client:
        t0 = time.monotonic()
        r = client.post("/audit", json={"contract": CLEAN})
        elapsed = time.monotonic() - t0
    assert r.status_code == 200
    assert elapsed < 3.0, f"the endpoint took {elapsed:.1f}s despite a 0.3s budget"
    assert r.json()["verdict"] == "Unclear"
    assert r.json()["summary"].strip()


def test_healthz_answers_immediately():
    settings = make_settings()
    pl = pipeline_with(SlowLLM(PAYLOAD, hang=True), settings)
    with TestClient(create_app(settings, pipeline=pl)) as client:
        t0 = time.monotonic()
        r = client.get("/healthz")
        assert time.monotonic() - t0 < 1.0
    assert r.status_code == 200 and r.json()["status"] == "ok"
