"""The shared machinery every intents/ package relies on: settings, the service contract, the base pipeline."""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Optional

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from intentkit.pipeline import BasePipeline, Result, consensus, median
from intentkit.request import read_fields
from intentkit.service import Num, StrictModel, create_service_app
from intentkit.settings import BaseSettings, env
from intentkit.upstream import Upstream
from tests.intents.helpers import FakeHttp, StubPipeline, fast


class Out(StrictModel):
    value: Num = None
    label: str = ""


@dataclass(frozen=True)
class S(BaseSettings):
    flag: bool = env("FLAG", False)
    count: int = env("COUNT", 3)


def make_app(pipeline, **kw):
    return create_service_app(title="t", intent="X_INTENT", path="/x", version="0", fields={"q": ("query",)},
                              model=Out, settings=fast(S, global_timeout=0.4), pipeline=pipeline,
                              make_pipeline=lambda s, c: pipeline, **kw)


# ---- settings -------------------------------------------------------------------------------------------
def test_settings_read_typed_values_from_the_environment(monkeypatch):
    monkeypatch.setenv("GLOBAL_TIMEOUT_S", "2.5")
    monkeypatch.setenv("COUNT", "9")
    monkeypatch.setenv("FLAG", "yes")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    s = S.from_env()
    assert (s.global_timeout, s.count, s.flag, s.log_level) == (2.5, 9, True, "DEBUG")


def test_a_malformed_environment_value_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("COUNT", "not-a-number")
    monkeypatch.setenv("GLOBAL_TIMEOUT_S", "")
    s = S.from_env()
    assert s.count == 3 and s.global_timeout == 6.0


# ---- service contract -----------------------------------------------------------------------------------
def test_answer_has_exactly_the_model_fields():
    with TestClient(make_app(StubPipeline(Out, value=1.5, label="a"))) as c:
        r = c.post("/x", json={"q": "hi"})
    assert r.status_code == 200 and r.json() == {"value": 1.5, "label": "a"}


@pytest.mark.parametrize("kwargs", [
    dict(content=b"not json", headers={"content-type": "application/json"}),
    dict(content=b"", headers={}),
    dict(json=[1, 2, 3]),
    dict(json={"q": {"nested": "x"}}),
])
def test_malformed_bodies_still_get_a_200_with_the_exact_fields(kwargs):
    with TestClient(make_app(StubPipeline(Out))) as c:
        r = c.post("/x", **kwargs)
    assert r.status_code == 200 and set(r.json()) == {"value", "label"}


def test_pipeline_exception_becomes_the_honest_empty_answer():
    with TestClient(make_app(StubPipeline(Out, error=RuntimeError("boom")))) as c:
        r = c.post("/x", json={"q": "hi"})
    assert r.status_code == 200 and r.json() == {"value": None, "label": ""}


def test_global_timeout_becomes_the_honest_empty_answer():
    with TestClient(make_app(StubPipeline(Out, delay=2.0, value=9.0))) as c:
        r = c.post("/x", json={"q": "hi"})
    assert r.status_code == 200 and r.json() == {"value": None, "label": ""}


def test_an_answer_that_does_not_fit_the_model_is_replaced_by_the_fallback():
    class Bad(StubPipeline):
        async def answer(self, req):
            return {"value": "not a number", "extra": 1}

    with TestClient(make_app(Bad(Out))) as c:
        r = c.post("/x", json={"q": "hi"})
    assert r.status_code == 200 and r.json() == {"value": None, "label": ""}


def test_alias_and_query_string_reach_the_pipeline_and_the_trailing_slash_routes():
    stub = StubPipeline(Out)
    with TestClient(make_app(stub)) as c:
        c.post("/x", json={"query": "via alias"})
        c.post("/x?q=via+query+string", content=b"")
    assert [s["q"] for s in stub.seen] == ["via alias", "via query string"]


def test_healthz_root_and_miner_yaml():
    with TestClient(make_app(StubPipeline(Out))) as c:
        assert c.get("/healthz").json()["intent"] == "X_INTENT"
        assert "POST /x" in c.get("/").json()["endpoints"]
        assert c.get("/miner.yaml").status_code in (200, 404)


def test_the_model_is_exposed_for_the_spec_parity_tests():
    app = make_app(StubPipeline(Out))
    assert app.state.response_model is Out and app.state.intent == "X_INTENT"


def test_nan_and_infinity_are_not_valid_numbers():
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            Out(value=bad)


def test_response_models_reject_undeclared_fields():
    with pytest.raises(ValidationError):
        Out(value=1, surprise=True)


# ---- request reading ------------------------------------------------------------------------------------
def test_read_fields_reads_json_numbers_only_when_asked_to():
    app = FastAPI()
    got = {}

    @app.post("/r")
    async def r(request: Request):
        got["off"] = await read_fields(request, {"n": (), "f": (), "b": ()})
        got["on"] = await read_fields(request, {"n": (), "f": (), "b": ()}, accept_numbers=True)
        return {}

    with TestClient(app) as c:
        c.post("/r", json={"n": 24, "f": 2.5, "b": True})
    assert got["off"] == {"n": "", "f": "", "b": ""}          # the original intents' behaviour, unchanged
    assert got["on"] == {"n": "24", "f": "2.5", "b": ""}       # booleans are never numbers


def test_the_service_passes_numeric_params_through_to_the_pipeline():
    stub = StubPipeline(Out)
    with TestClient(make_app(stub)) as c:
        c.post("/x", json={"q": 24})
    assert stub.seen == [{"q": "24"}]


# ---- base pipeline --------------------------------------------------------------------------------------
class CountingPipeline(BasePipeline):
    intent = "X"

    def __init__(self, settings, client, degraded=False):
        super().__init__(settings, client)
        self.calls, self.degraded = 0, degraded

    def key(self, req) -> Optional[str]:
        return req.get("q") or None

    def empty(self, req, reason) -> BaseModel:
        return Out()

    async def compute(self, req) -> Result:
        self.calls += 1
        await asyncio.sleep(0.05)
        return Result(Out(value=float(self.calls)), degraded=self.degraded)


async def test_identical_concurrent_requests_share_one_computation_and_are_cached():
    pl = CountingPipeline(fast(S), httpx.AsyncClient())
    a = await asyncio.gather(*(pl.answer({"q": "same"}) for _ in range(5)))
    assert pl.calls == 1 and {m.value for m in a} == {1.0}
    await pl.answer({"q": "same"})
    assert pl.calls == 1 and pl.cache.size() == 1


async def test_a_request_with_no_key_is_never_cached():
    pl = CountingPipeline(fast(S), httpx.AsyncClient())
    await pl.answer({"q": ""})
    await pl.answer({"q": ""})
    assert pl.calls == 2 and pl.cache.size() == 0


async def test_degraded_answers_are_cached_for_the_short_ttl_only():
    pl = CountingPipeline(fast(S, cache_ttl=60, cache_ttl_degraded=0), httpx.AsyncClient(), degraded=True)
    await pl.answer({"q": "k"})
    await pl.answer({"q": "k"})
    assert pl.calls == 2          # ttl 0 means "do not cache"


def test_consensus_drops_a_wild_outlier_but_not_when_there_is_nothing_to_out_vote():
    assert consensus([100.0, 100.2, 99.9, 150.0], 0.03) == pytest.approx(100.0)
    assert consensus([100.0, 150.0], 0.03) == 125.0
    assert consensus([], 0.03) is None and median([3, 1, 2]) == 2


# ---- upstream -------------------------------------------------------------------------------------------
async def test_upstream_tells_not_found_from_unreachable():
    fake = FakeHttp({"https://a.test/ok": {"x": 1}, "https://a.test/missing": 404, "https://a.test/boom": 500,
                     "https://a.test/net": httpx.ConnectError("down")})
    up = Upstream(fake.client(), timeout=1.0)
    assert await up.fetch("https://a.test/ok") == (200, {"x": 1})
    assert await up.fetch("https://a.test/missing") == (404, None)    # the source answered: it does not exist
    assert await up.fetch("https://a.test/boom") is None               # we could not ask
    assert await up.fetch("https://a.test/net") is None
    assert await up.get_json("https://a.test/missing") is None


async def test_upstream_circuit_opens_after_repeated_outages_then_fails_fast():
    fake = FakeHttp({"https://b.test/": 503})
    up = Upstream(fake.client(), timeout=1.0, failure_threshold=2, cooldown_s=60)
    for _ in range(4):
        assert await up.fetch("https://b.test/x") is None
    assert len(fake.calls) == 2           # the breaker opened after two failures
