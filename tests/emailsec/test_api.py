"""HTTP contract: exactly the one EMAIL_SECURITY_GENERAL field, and HTTP 200 for every input."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from emailsec.app import create_app
from emailsec.pipeline import Lookup
from tests.emailsec.conftest import make_settings


class StubPipeline:
    def __init__(self, ok=True, error=None, delay=0.0):
        self.ok, self.error, self.delay = ok, error, delay
        self.cache = type("C", (), {"size": staticmethod(lambda: 0)})()
        self.seen: list[tuple[str, str]] = []

    async def lookup(self, domain: str, rtype: str) -> Lookup:
        self.seen.append((domain, rtype))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return Lookup(domain, rtype, ok=self.ok, status=0 if self.ok else 3)


def client_for(stub) -> TestClient:
    return TestClient(create_app(make_settings(global_timeout=0.5), pipeline=stub))


def assert_contract(body: dict) -> None:
    assert set(body) == {"lookup_ok"}, sorted(body)
    assert isinstance(body["lookup_ok"], bool)


def test_true_and_false_are_json_booleans():
    with client_for(StubPipeline(ok=True)) as c:
        r = c.post("/email-security", json={"domain": "example.com", "record_type": "MX"})
    assert r.status_code == 200 and r.json() == {"lookup_ok": True}
    assert r.text.replace(" ", "") == '{"lookup_ok":true}'
    with client_for(StubPipeline(ok=False)) as c:
        assert c.post("/email-security", json={"domain": "x.example", "record_type": "MX"}).json() == {"lookup_ok": False}


def test_both_fields_reach_the_pipeline():
    stub = StubPipeline()
    with client_for(stub) as c:
        c.post("/email-security", json={"domain": "example.com", "record_type": "TXT"})
    assert stub.seen == [("example.com", "TXT")]


def test_aliases_and_query_parameters_are_accepted():
    stub = StubPipeline()
    with client_for(stub) as c:
        c.post("/email-security", json={"name": "a.example", "type": "MX"})
        c.post("/email-security?domain=b.example&record_type=TXT", content=b"")
        c.post("/email-security", json={"email": "me@c.example", "record": "A"})
        c.post("/email-security", json={"query": "d.example"})
    assert stub.seen == [("a.example", "MX"), ("b.example", "TXT"), ("me@c.example", "A"), ("d.example", "")]


@pytest.mark.parametrize("payload", [
    {}, {"domain": ""}, {"domain": None}, {"domain": 5}, {"domain": []}, {"record_type": "MX"},
    {"domain": "example.com", "record_type": None}, {"wrong": "x"}, {"domain": {"a": 1}},
])
def test_malformed_bodies_still_answer_the_contract(payload):
    with client_for(StubPipeline()) as c:
        r = c.post("/email-security", json=payload)
    assert r.status_code == 200
    assert_contract(r.json())


def test_invalid_json_and_empty_body_still_answer_the_contract():
    with client_for(StubPipeline()) as c:
        for content in (b"{not json", b""):
            r = c.post("/email-security", content=content, headers={"Content-Type": "application/json"})
            assert r.status_code == 200
            assert_contract(r.json())


def test_a_very_long_domain_is_accepted():
    with client_for(StubPipeline()) as c:
        r = c.post("/email-security", json={"domain": "a" * 50_000, "record_type": "MX"})
    assert r.status_code == 200
    assert_contract(r.json())


def test_a_crashing_pipeline_answers_false_with_http_200():
    with client_for(StubPipeline(error=RuntimeError("boom"))) as c:
        r = c.post("/email-security", json={"domain": "example.com", "record_type": "MX"})
    assert r.status_code == 200 and r.json() == {"lookup_ok": False}


def test_a_hanging_pipeline_is_cut_off_and_answers_false():
    with client_for(StubPipeline(delay=5.0)) as c:
        r = c.post("/email-security", json={"domain": "example.com", "record_type": "MX"})
    assert r.status_code == 200 and r.json() == {"lookup_ok": False}


def test_liveness_and_index():
    with client_for(StubPipeline()) as c:
        h = c.get("/healthz")
        assert h.status_code == 200 and h.json()["status"] == "ok"
        assert c.get("/").json()["intent"] == "EMAIL_SECURITY_GENERAL"
        assert c.get("/email-security").status_code in (405, 200)
        assert c.get("/healthz").status_code == 200
