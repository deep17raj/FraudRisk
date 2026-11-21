"""The HTTP contract: exactly four fields, HTTP 200, on every path including the bad ones.

Telegraph revokes routing from miners that return errors, empty bodies or nothing at all, so
"the endpoint answers correctly even when everything has gone wrong" is a hard requirement
rather than a nicety.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from audit.app import create_app
from audit.models import Audit, Verdict
from tests.audit.conftest import CLEAN, extraction, make_pipeline, make_settings

ALLOWED = {"Compliant", "Partially compliant", "Non-compliant", "Unclear"}
FIELDS = {"verdict", "obligations", "clauses", "summary"}


class StubPipeline:
    """A pipeline stand-in for tests that care about the HTTP layer, not the audit."""

    def __init__(self, result: Audit | None = None, *, delay: float = 0.0,
                 raises: Exception | None = None):
        self.result = result or Audit(
            verdict=Verdict.COMPLIANT,
            obligations=["The Supplier: deliver the Goods by 1 March 2026 (1.1) - met"],
            clauses=["The Supplier shall deliver the Goods"],
            summary="Compliant: the single obligation is fully specified.")
        self.delay = delay
        self.raises = raises
        self.cache = type("C", (), {"size": staticmethod(lambda: 0)})()
        self.uses_llm = False
        self.calls: list[str] = []

    async def audit(self, contract: str) -> Audit:
        self.calls.append(contract)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises:
            raise self.raises
        return self.result


def client(pipeline=None, settings=None) -> TestClient:
    return TestClient(create_app(settings or make_settings(), pipeline=pipeline or StubPipeline()))


def check_shape(body: dict) -> None:
    assert set(body) == FIELDS, f"the response must carry exactly the four intent fields: {body}"
    assert body["verdict"] in ALLOWED
    assert isinstance(body["obligations"], list) and isinstance(body["clauses"], list)
    assert all(isinstance(x, str) for x in body["obligations"] + body["clauses"])
    assert isinstance(body["summary"], str) and body["summary"].strip()


# --- the happy path ---------------------------------------------------------------------

def test_a_normal_request_returns_exactly_the_four_fields():
    with client() as c:
        r = c.post("/audit", json={"contract": CLEAN})
    assert r.status_code == 200
    check_shape(r.json())


def test_the_enum_spelling_is_exact():
    """Telegraph encodes `verdict` on-chain against a closed allowed-list; a near miss is
    not a near miss, it is an invalid answer."""
    for verdict in Verdict:
        pipeline = StubPipeline(Audit(verdict=verdict, obligations=[], clauses=[],
                                      summary="A summary."))
        with client(pipeline) as c:
            body = c.post("/audit", json={"contract": "x"}).json()
        assert body["verdict"] == verdict.value and body["verdict"] in ALLOWED


def test_no_extra_fields_can_leak_into_the_response():
    with client() as c:
        body = c.post("/audit", json={"contract": CLEAN}).json()
    for leak in ("rule", "degraded", "diagnostics", "obligations_detail", "status"):
        assert leak not in body


# --- hostile and malformed input --------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {},                                   # no contract
    {"contract": ""},                     # empty contract
    {"contract": "   "},                  # whitespace only
    {"contract": None},                   # wrong type
    {"contract": 12345},                  # wrong type
    {"contract": []},                     # wrong type
    {"contract": {"nested": "object"}},   # wrong type
    {"wrong_field": "text"},              # unknown field
])
def test_a_malformed_body_still_returns_a_valid_answer(payload):
    with client() as c:
        r = c.post("/audit", json=payload)
    assert r.status_code == 200
    check_shape(r.json())


def test_invalid_json_still_returns_a_valid_answer():
    with client() as c:
        r = c.post("/audit", content=b"{not json at all",
                   headers={"Content-Type": "application/json"})
    assert r.status_code == 200
    check_shape(r.json())
    assert r.json()["verdict"] == "Unclear"


def test_an_empty_body_still_returns_a_valid_answer():
    with client() as c:
        r = c.post("/audit", content=b"")
    assert r.status_code == 200
    check_shape(r.json())


def test_a_bare_json_string_body_is_accepted():
    pipeline = StubPipeline()
    with client(pipeline) as c:
        r = c.post("/audit", json=CLEAN)
    assert r.status_code == 200
    check_shape(r.json())
    assert pipeline.calls, "a bare string body should still reach the pipeline"


@pytest.mark.parametrize("field", ["contract", "text", "document", "input", "query",
                                   "contract_text"])
def test_common_field_aliases_are_accepted(field):
    pipeline = StubPipeline()
    with client(pipeline) as c:
        r = c.post("/audit", json={field: CLEAN})
    assert r.status_code == 200 and pipeline.calls


def test_a_wrapped_params_object_is_accepted():
    pipeline = StubPipeline()
    with client(pipeline) as c:
        r = c.post("/audit", json={"params": {"contract": CLEAN}})
    assert r.status_code == 200 and pipeline.calls


def test_a_query_parameter_is_accepted():
    pipeline = StubPipeline()
    with client(pipeline) as c:
        r = c.post("/audit?contract=The%20Supplier%20shall%20deliver.", json={})
    assert r.status_code == 200 and pipeline.calls


def test_an_oversized_contract_is_capped_not_rejected():
    pipeline = StubPipeline()
    settings = make_settings(max_contract_chars=5_000)
    with client(pipeline, settings) as c:
        r = c.post("/audit", json={"contract": "The Supplier shall pay. " * 20_000})
    assert r.status_code == 200
    check_shape(r.json())
    assert len(pipeline.calls[0]) <= 5_000


def test_an_absurd_body_does_not_crash_the_endpoint():
    with client() as c:
        r = c.post("/audit", content=b"\x00\x01\x02 binary rubbish \xff",
                   headers={"Content-Type": "application/json"})
    assert r.status_code == 200
    check_shape(r.json())


# --- failure paths ----------------------------------------------------------------------

def test_a_pipeline_crash_returns_unclear_rather_than_a_500():
    pipeline = StubPipeline(raises=RuntimeError("the audit exploded"))
    with client(pipeline) as c:
        r = c.post("/audit", json={"contract": CLEAN})
    assert r.status_code == 200
    check_shape(r.json())
    assert r.json()["verdict"] == "Unclear"
    assert "did not complete" in r.json()["summary"]


def test_a_timeout_returns_unclear_inside_the_budget():
    settings = make_settings(global_timeout=0.2)
    pipeline = StubPipeline(delay=5.0)
    with client(pipeline, settings) as c:
        r = c.post("/audit", json={"contract": CLEAN})
    assert r.status_code == 200
    check_shape(r.json())
    assert r.json()["verdict"] == "Unclear"
    assert "time budget" in r.json()["summary"]


def test_a_blank_summary_from_the_pipeline_is_still_answered():
    pipeline = StubPipeline(Audit(verdict=Verdict.UNCLEAR, obligations=[], clauses=[],
                                  summary="   "))
    with client(pipeline) as c:
        body = c.post("/audit", json={"contract": "x"}).json()
    check_shape(body), "the summary must never be returned empty"


def test_blank_entries_are_stripped_from_the_lists():
    pipeline = StubPipeline(Audit(
        verdict=Verdict.COMPLIANT, obligations=["  ", "", "A real obligation - met"],
        clauses=["", "   ", "The Supplier shall deliver"], summary="A summary."))
    with client(pipeline) as c:
        body = c.post("/audit", json={"contract": "x"}).json()
    assert body["obligations"] == ["A real obligation - met"]
    assert body["clauses"] == ["The Supplier shall deliver"]


# --- the real pipeline through HTTP -----------------------------------------------------

def test_the_real_pipeline_answers_over_http_with_verbatim_clauses():
    payload = extraction([
        {"party": "The Supplier", "duty": "deliver 500 units of Product X", "status": "met",
         "clause_ref": "1.1",
         "quote": "The Supplier shall deliver 500 units of Product X to the Buyer's warehouse"},
    ])
    pipeline = make_pipeline(payload)
    with client(pipeline) as c:
        body = c.post("/audit", json={"contract": CLEAN}).json()
    check_shape(body)
    assert body["obligations"] and body["clauses"]
    for clause in body["clauses"]:
        assert clause in CLEAN, "a clause reached the wire that is not in the contract"


# --- the other routes -------------------------------------------------------------------

def test_healthz_is_live():
    with client() as c:
        r = c.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_root_describes_the_service():
    with client() as c:
        body = c.get("/").json()
    assert body["intent"] == "CONTRACT_OBLIGATION_AUDIT_GENERAL"
    assert "POST /audit" in body["endpoints"]
