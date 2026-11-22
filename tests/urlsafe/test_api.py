"""HTTP contract: exactly the four URL_SAFE_GENERAL fields, an exact verdict enum, HTTP 200 always."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from tests.urlsafe.conftest import make_settings
from urlsafe.app import create_app
from urlsafe.pipeline import UrlResult
from urlsafe.verdict import Verdict

FIELDS = {"url", "verdict", "detections", "summary"}
VERDICTS = {"Safe", "Suspicious", "Malicious", "Unknown"}


class StubPipeline:
    def __init__(self, result=None, error=None, delay=0.0):
        self.result, self.error, self.delay = result, error, delay
        self.feed = None
        self.cache = type("C", (), {"size": staticmethod(lambda: 0)})()
        self.seen: list[str] = []

    async def start(self):
        return None

    async def check(self, raw: str) -> UrlResult:
        self.seen.append(raw)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.result or UrlResult(url=raw, verdict=Verdict.SAFE, detections=0, summary="Not listed.")


def client_for(stub) -> TestClient:
    return TestClient(create_app(make_settings(global_timeout=0.5), pipeline=stub))


def assert_contract(body: dict) -> None:
    assert set(body) == FIELDS, sorted(body)
    assert isinstance(body["url"], str)
    assert body["verdict"] in VERDICTS
    assert isinstance(body["detections"], int) and not isinstance(body["detections"], bool)
    assert 0 <= body["detections"] <= 1000
    assert isinstance(body["summary"], str) and body["summary"].strip()


@pytest.mark.parametrize("verdict", list(Verdict))
def test_every_verdict_is_serialised_as_its_exact_string(verdict):
    res = UrlResult(url="http://x.example", verdict=verdict, detections=2, summary="s")
    with client_for(StubPipeline(result=res)) as c:
        body = c.post("/url-safe", json={"url": "http://x.example"}).json()
    assert_contract(body)
    assert body["verdict"] == verdict.value


def test_detections_is_a_plain_integer_and_is_clamped_to_the_documented_range():
    for given, expected in ((3, 3), (5000, 1000), (-4, 0)):
        res = UrlResult(url="u", verdict=Verdict.MALICIOUS, detections=given, summary="s")
        with client_for(StubPipeline(result=res)) as c:
            r = c.post("/url-safe", json={"url": "u"})
        assert f'"detections":{expected},' in r.text.replace(" ", "")


@pytest.mark.parametrize("payload", [
    {}, {"url": ""}, {"url": None}, {"url": 7}, {"url": []}, {"wrong": "x"}, {"url": {"a": 1}},
])
def test_malformed_bodies_still_answer_the_contract(payload):
    with client_for(StubPipeline()) as c:
        r = c.post("/url-safe", json=payload)
    assert r.status_code == 200
    assert_contract(r.json())


def test_invalid_json_and_empty_body_still_answer_the_contract():
    with client_for(StubPipeline()) as c:
        for content in (b"{not json", b""):
            r = c.post("/url-safe", content=content, headers={"Content-Type": "application/json"})
            assert r.status_code == 200
            assert_contract(r.json())


def test_aliases_and_query_parameters_are_accepted():
    stub = StubPipeline()
    with client_for(stub) as c:
        c.post("/url-safe?url=http://a.example", content=b"")
        c.post("/url-safe", json={"link": "http://b.example"})
        c.post("/url-safe", json={"domain": "c.example"})
        c.post("/url-safe", json={"query": "Is http://d.example safe?"})
    assert stub.seen == ["http://a.example", "http://b.example", "c.example", "Is http://d.example safe?"]


def test_a_very_long_input_is_accepted():
    with client_for(StubPipeline()) as c:
        r = c.post("/url-safe", json={"url": "http://x.example/" + "a" * 50_000})
    assert r.status_code == 200
    assert_contract(r.json())


def test_a_crashing_pipeline_answers_unknown_not_safe():
    with client_for(StubPipeline(error=RuntimeError("boom"))) as c:
        r = c.post("/url-safe", json={"url": "http://x.example"})
    body = r.json()
    assert r.status_code == 200
    assert_contract(body)
    assert body["verdict"] == "Unknown" and body["detections"] == 0


def test_a_hanging_pipeline_is_cut_off_and_answers_unknown():
    with client_for(StubPipeline(delay=5.0)) as c:
        r = c.post("/url-safe", json={"url": "http://x.example"})
    body = r.json()
    assert r.status_code == 200
    assert_contract(body)
    assert body["verdict"] == "Unknown"


def test_liveness_and_index():
    with client_for(StubPipeline()) as c:
        h = c.get("/healthz")
        assert h.status_code == 200 and h.json()["status"] == "ok"
        assert c.get("/").json()["intent"] == "URL_SAFE_GENERAL"
        assert c.get("/url-safe").status_code in (405, 200)
        assert c.get("/healthz").status_code == 200
