"""HTTP-layer tests: the 4-field contract must hold on EVERY path, and the service must never hang."""
import json
import time

import pytest
from starlette.testclient import TestClient

from factcheck.app import create_app
from factcheck.models import Verdict
from factcheck.pipeline import FactCheckPipeline
from tests.factcheck.conftest import FakeSource, ev, make_settings

VERDICTS = {v.value for v in Verdict}
FIELDS = {"claim", "verdict", "reasoning", "sources"}

GOOD = [
    ev("https://en.wikipedia.org/wiki/Eiffel_Tower", "Eiffel Tower",
       "The Eiffel Tower is a wrought-iron lattice tower located in Paris, France.", provider="wikipedia"),
    ev("https://www.britannica.com/topic/Eiffel-Tower-Paris-landmark", "Eiffel Tower",
       "The Eiffel Tower is a landmark located in Paris, France.", provider="web"),
]


def client_for(sources, **settings_kw):
    settings = make_settings(**settings_kw)
    import httpx
    pipeline = FactCheckPipeline(settings, httpx.AsyncClient(), sources=sources)
    return TestClient(create_app(settings, pipeline)), pipeline


def assert_contract(body: dict):
    assert set(body) == FIELDS, body
    assert isinstance(body["claim"], str)
    assert body["verdict"] in VERDICTS                      # case-sensitive exact enum
    assert isinstance(body["reasoning"], str) and body["reasoning"].strip()
    assert isinstance(body["sources"], list) and all(isinstance(s, str) for s in body["sources"])


def test_happy_path_returns_exactly_four_fields_and_only_retrieved_urls():
    c, _ = client_for([FakeSource("fake", GOOD)])
    with c:
        r = c.post("/fact-check", json={"claim": "The Eiffel Tower is located in Paris."})
    assert r.status_code == 200
    body = r.json()
    assert_contract(body)
    assert body["claim"] == "The Eiffel Tower is located in Paris."
    assert body["verdict"] in {"True", "Mostly True"}
    assert body["sources"] and set(body["sources"]) <= {e.url for e in GOOD}


@pytest.mark.parametrize("payload,kw", [
    (None, {"content": b"not json", "headers": {"content-type": "application/json"}}),
    (None, {"content": b"", "headers": {"content-type": "application/json"}}),
    ({}, {}),
    ({"claim": ""}, {}),
    ({"claim": "   "}, {}),
    ({"claim": 123}, {}),
    ({"claim": None}, {}),
    ({"claim": ["a"]}, {}),
    ({"other": "x"}, {}),
    ([1, 2, 3], {}),
])
def test_malformed_or_missing_claim_still_returns_valid_four_fields(payload, kw):
    c, _ = client_for([FakeSource("fake", GOOD)])
    with c:
        r = c.post("/fact-check", json=payload, **kw) if payload is not None else c.post("/fact-check", **kw)
    assert r.status_code == 200
    body = r.json()
    assert_contract(body)
    assert body["verdict"] == "Unverified" and body["sources"] == []


def test_opinion_is_abstained_without_touching_sources():
    src = FakeSource("fake", GOOD)
    c, _ = client_for([src])
    with c:
        body = c.post("/fact-check", json={"claim": "Pineapple is the best pizza topping."}).json()
    assert_contract(body)
    assert body["verdict"] == "Unverified" and "subjective" in body["reasoning"] and src.calls == 0


def test_no_evidence_gives_unverified_with_no_sources():
    c, _ = client_for([FakeSource("fake", [])])
    with c:
        body = c.post("/fact-check", json={"claim": "The mayor of Tiny Falls approved a $41,200 bike lane budget."}).json()
    assert_contract(body)
    assert body["verdict"] == "Unverified" and body["sources"] == []


def test_global_timeout_returns_unverified_quickly():
    slow = FakeSource("slow", GOOD, delay=5.0)
    c, _ = client_for([slow], global_timeout=0.6, retrieval_timeout=0.4)
    with c:
        t0 = time.monotonic()
        r = c.post("/fact-check", json={"claim": "The Eiffel Tower is located in Paris."})
        dt = time.monotonic() - t0
    assert dt < 2.0, dt
    body = r.json()
    assert_contract(body)
    assert body["verdict"] == "Unverified"


def test_slow_provider_does_not_block_fast_one():
    fast = FakeSource("fast", GOOD)
    slow = FakeSource("slow", GOOD, delay=5.0)
    c, _ = client_for([slow, fast], retrieval_timeout=0.5, global_timeout=3.0)
    with c:
        t0 = time.monotonic()
        body = c.post("/fact-check", json={"claim": "The Eiffel Tower is located in Paris."}).json()
        dt = time.monotonic() - t0
    assert dt < 2.5 and body["verdict"] in {"True", "Mostly True"}


def test_provider_failure_is_unverified_and_not_cached():
    import httpx
    bad = FakeSource("bad", error=httpx.ConnectError("down"))
    c, pl = client_for([bad])
    with c:
        b1 = c.post("/fact-check", json={"claim": "The Eiffel Tower is located in Paris."}).json()
        assert_contract(b1)
        assert b1["verdict"] == "Unverified" and "could not be reached" in b1["reasoning"]
        assert pl.cache.size() == 0                       # outage must not be cached as "no evidence"
        calls = bad.calls
        c.post("/fact-check", json={"claim": "The Eiffel Tower is located in Paris."})
        assert bad.calls > calls


def test_one_failing_provider_does_not_break_the_others():
    import httpx
    bad = FakeSource("bad", error=httpx.ConnectError("down"))
    good = FakeSource("good", GOOD)
    c, _ = client_for([bad, good])
    with c:
        body = c.post("/fact-check", json={"claim": "The Eiffel Tower is located in Paris."}).json()
    assert_contract(body)
    assert body["verdict"] in {"True", "Mostly True"}


def test_good_answers_are_cached_and_normalised():
    src = FakeSource("fake", GOOD)
    c, pl = client_for([src])
    with c:
        c.post("/fact-check", json={"claim": "The Eiffel Tower is located in Paris."})
        n = src.calls
        r2 = c.post("/fact-check", json={"claim": "  the EIFFEL tower is located in paris "})
        r3 = c.post("/fact-check", json={"claim": "Is it true that the Eiffel Tower is located in Paris?"})
    assert src.calls == n, "repeat/normalised claims must be served from cache"
    assert r2.json()["verdict"] == r3.json()["verdict"]
    assert r2.json()["claim"] == "the EIFFEL tower is located in paris"   # echoes the caller's claim, trimmed


def test_volatile_claims_get_short_ttl():
    src = FakeSource("fake", GOOD)
    c, pl = client_for([src], cache_ttl=3600, cache_ttl_volatile=300)
    from factcheck.claim_parser import parse_claim
    assert pl._ttl(parse_claim("The Eiffel Tower is in Paris."), False) == 3600
    assert pl._ttl(parse_claim("The Eiffel Tower is closed today."), False) == 300
    assert pl._ttl(parse_claim("The Eiffel Tower is in Paris."), True) == pl.s.cache_ttl_degraded


def test_pipeline_crash_is_contained():
    c, pl = client_for([FakeSource("fake", GOOD)])

    async def boom(claim):
        raise RuntimeError("kaboom")

    pl.check = boom
    with c:
        r = c.post("/fact-check", json={"claim": "The Eiffel Tower is located in Paris."})
    assert r.status_code == 200
    body = r.json()
    assert_contract(body)
    assert body["verdict"] == "Unverified"


def test_query_param_fallback_and_healthz():
    c, _ = client_for([FakeSource("fake", GOOD)])
    with c:
        body = c.post("/fact-check?claim=The%20Eiffel%20Tower%20is%20located%20in%20Paris.").json()
        assert_contract(body)
        h = c.get("/healthz")
    assert h.status_code == 200 and h.json()["status"] == "ok" and "fake" in h.json()["sources"]


def test_structured_log_record_is_emitted(capsys):
    c, _ = client_for([FakeSource("fake", GOOD)])
    with c:
        c.post("/fact-check", json={"claim": "The Eiffel Tower is located in Paris."})
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.startswith("{")]
    recs = [l for l in lines if l.get("event") == "fact_check"]
    assert recs, lines
    f = recs[-1]
    assert f["claim"] and f["verdict"] and f["latency_ms"] is not None and f["evidence"] and f["providers"] == {"fake": "ok"}


def test_miner_yaml_is_served_verbatim_for_the_onchain_hash():
    """YAML_URL may point at this route, so the bytes must equal the file exactly (hash must match)."""
    import hashlib
    from pathlib import Path
    c, _ = client_for([FakeSource("fake", GOOD)])
    with c:
        r = c.get("/miner.yaml")
    raw = (Path(__file__).resolve().parents[2] / "miner.yaml").read_bytes()
    assert r.status_code == 200 and r.content == raw
    assert b"\r" not in r.content
    assert hashlib.sha256(r.content).hexdigest() == hashlib.sha256(raw).hexdigest()
