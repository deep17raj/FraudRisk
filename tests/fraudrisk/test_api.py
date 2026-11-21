"""HTTP contract tests.

The response must carry EXACTLY the four fields of Telegraph's FRAUD_RISK_GENERAL intent,
with an exact uppercase enum and a numeric score in [0, 1] consistent with it - and it must
be HTTP 200 for every input, including a malformed body, a crash and a timeout, because
Telegraph revokes routing from miners that error or go silent.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from fraudrisk.app import create_app
from fraudrisk.models import Assessment, RiskLevel
from fraudrisk.scoring import level_for_score
from tests.fraudrisk.conftest import make_settings
from tests.fraudrisk.test_regression import build_pipeline, stub_feeds

EXPECTED_FIELDS = {"risk_level", "risk_score", "signals", "summary"}
LEVELS = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}


class StubPipeline:
    """Stands in for the real pipeline so the HTTP layer can be tested in isolation."""

    def __init__(self, result=None, error=None, delay=0.0):
        self.result = result
        self.error = error
        self.delay = delay
        self.feeds = stub_feeds()
        self.sources = []
        self.cache = type("C", (), {"size": staticmethod(lambda: 0)})()
        self.reasoner = type("R", (), {"uses_llm": False})()
        self.calls = 0

    async def assess(self, subject: str) -> Assessment:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.result or Assessment(
            subject=subject, level=RiskLevel.LOW, score=0.0, signals=[], summary="Nothing found.")


def client_for(pipeline) -> TestClient:
    return TestClient(create_app(make_settings(global_timeout=1.0), pipeline=pipeline))


def assert_contract(body: dict) -> None:
    assert set(body) == EXPECTED_FIELDS, f"unexpected field set: {sorted(body)}"
    assert body["risk_level"] in LEVELS
    assert isinstance(body["risk_score"], (int, float)) and not isinstance(body["risk_score"], bool)
    assert 0.0 <= body["risk_score"] <= 1.0
    assert isinstance(body["signals"], list)
    assert all(isinstance(s, str) and s for s in body["signals"])
    assert isinstance(body["summary"], str) and body["summary"].strip()
    assert level_for_score(body["risk_score"]).value == body["risk_level"], \
        "risk_score and risk_level must agree"


# --------------------------------------------------------------- the happy path

def test_response_carries_exactly_the_four_intent_fields():
    with client_for(StubPipeline()) as c:
        r = c.post("/fraud-risk", json={"subject": "8.8.8.8"})
    assert r.status_code == 200
    assert_contract(r.json())


def test_real_pipeline_through_http():
    """End to end over HTTP with the hermetic source set."""
    transport_client = httpx.AsyncClient()
    pipeline = build_pipeline(transport_client)
    with client_for(pipeline) as c:
        for subject in ("New account 2 days old, first transfer of $4,200 to a new payee",
                        "Returning customer of 5 years paying an existing payee $80",
                        "185.220.101.1", "check this person", "BIN 457173"):
            body = c.post("/fraud-risk", json={"subject": subject}).json()
            assert_contract(body)


def test_score_is_serialised_as_a_plain_number():
    """0.0 must not go out as 1e-06 or as a string."""
    with client_for(StubPipeline()) as c:
        raw = c.post("/fraud-risk", json={"subject": "check this"}).text
    assert '"risk_score":0.0' in raw.replace(" ", "")


# --------------------------------------------------------------- hostile input

@pytest.mark.parametrize("payload", [
    {}, {"subject": ""}, {"subject": "   "}, {"subject": None}, {"subject": 42},
    {"subject": []}, {"wrong_key": "x"}, {"subject": {"nested": "x"}},
])
def test_malformed_bodies_still_answer_the_contract(payload):
    with client_for(StubPipeline()) as c:
        r = c.post("/fraud-risk", json=payload)
    assert r.status_code == 200
    assert_contract(r.json())


def test_invalid_json_body_still_answers_the_contract():
    with client_for(StubPipeline()) as c:
        r = c.post("/fraud-risk", content=b"{not json at all",
                   headers={"Content-Type": "application/json"})
    assert r.status_code == 200
    assert_contract(r.json())


def test_empty_body_still_answers_the_contract():
    with client_for(StubPipeline()) as c:
        r = c.post("/fraud-risk", content=b"")
    assert r.status_code == 200
    assert_contract(r.json())


def test_subject_may_arrive_as_a_query_parameter():
    with client_for(StubPipeline()) as c:
        r = c.post("/fraud-risk?subject=8.8.8.8", content=b"")
    assert_contract(r.json())


@pytest.mark.parametrize("alias", ["query", "input", "text"])
def test_common_field_aliases_are_accepted(alias):
    """Callers and gateways do not always send the field named `subject`."""
    stub = StubPipeline()
    with client_for(stub) as c:
        r = c.post("/fraud-risk", json={alias: "a first transfer to a new payee"})
    assert_contract(r.json())
    assert stub.calls == 1


def test_a_very_long_subject_is_accepted_and_truncated():
    stub = StubPipeline()
    with client_for(stub) as c:
        r = c.post("/fraud-risk", json={"subject": "x" * 50_000})
    assert r.status_code == 200
    assert_contract(r.json())


def test_unicode_subject_is_handled():
    with client_for(StubPipeline()) as c:
        r = c.post("/fraud-risk", json={"subject": "转账 €4.200 → new payee \U0001f3e6"})
    assert_contract(r.json())


# --------------------------------------------------------------- failure paths

def test_a_crashing_pipeline_still_answers_the_contract():
    with client_for(StubPipeline(error=RuntimeError("boom"))) as c:
        r = c.post("/fraud-risk", json={"subject": "8.8.8.8"})
    assert r.status_code == 200
    body = r.json()
    assert_contract(body)
    assert body["risk_level"] == "LOW" and body["risk_score"] == 0.0


def test_a_hanging_pipeline_is_cut_off_and_still_answers():
    with client_for(StubPipeline(delay=5.0)) as c:
        r = c.post("/fraud-risk", json={"subject": "8.8.8.8"})
    assert r.status_code == 200
    body = r.json()
    assert_contract(body)
    assert "could not be completed in time" in body["summary"]


def test_an_inconsistent_pipeline_result_is_corrected_at_the_boundary():
    """Belt and braces: if anything upstream ever disagreed, the HTTP layer fixes the pair."""
    bad = Assessment(subject="x", level=RiskLevel.CRITICAL, score=0.1,
                     signals=["something"], summary="A signal fired.")
    with client_for(StubPipeline(result=bad)) as c:
        body = c.post("/fraud-risk", json={"subject": "x"}).json()
    assert_contract(body)
    assert body["risk_level"] == "LOW"      # derived from the score, which is authoritative


def test_out_of_range_score_is_clamped():
    bad = Assessment(subject="x", level=RiskLevel.CRITICAL, score=4.7,
                     signals=["s"], summary="A signal fired.")
    with client_for(StubPipeline(result=bad)) as c:
        body = c.post("/fraud-risk", json={"subject": "x"}).json()
    assert body["risk_score"] == 1.0 and body["risk_level"] == "CRITICAL"


def test_empty_signal_strings_are_dropped():
    res = Assessment(subject="x", level=RiskLevel.LOW, score=0.0,
                     signals=["real signal", "", "   "], summary="ok")
    with client_for(StubPipeline(result=res)) as c:
        body = c.post("/fraud-risk", json={"subject": "x"}).json()
    assert body["signals"] == ["real signal"]


# --------------------------------------------------------------- liveness

def test_healthz_is_non_empty_and_cheap():
    with client_for(StubPipeline()) as c:
        r = c.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["version"]
    assert r.content.strip()


def test_root_describes_the_service():
    with client_for(StubPipeline()) as c:
        body = c.get("/").json()
    assert body["intent"] == "FRAUD_RISK_GENERAL"


def test_get_on_the_assessment_endpoint_does_not_crash_the_service():
    with client_for(StubPipeline()) as c:
        assert c.get("/fraud-risk").status_code in (405, 200)
        assert c.get("/healthz").status_code == 200      # still alive afterwards
