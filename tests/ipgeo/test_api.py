"""HTTP contract: exactly the six IP_GEOLOCATION_GENERAL fields, and HTTP 200 for every input."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from ipgeo.app import create_app
from ipgeo.models import GeoResult
from tests.ipgeo.conftest import make_settings

FIELDS = {"ip", "country", "city", "latitude", "longitude", "isp"}


class StubPipeline:
    def __init__(self, result=None, error=None, delay=0.0):
        self.result, self.error, self.delay = result, error, delay
        self.providers = []
        self.cache = type("C", (), {"size": staticmethod(lambda: 0)})()
        self.seen: list[str] = []

    async def locate(self, raw: str) -> GeoResult:
        self.seen.append(raw)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.result or GeoResult(ip=raw, country="United States", city="Mountain View",
                                        latitude=37.4225, longitude=-122.0842, isp="Google LLC")


def client_for(stub) -> TestClient:
    return TestClient(create_app(make_settings(global_timeout=0.5), pipeline=stub))


def assert_contract(body: dict) -> None:
    assert set(body) == FIELDS, sorted(body)
    assert isinstance(body["ip"], str) and isinstance(body["country"], str)
    assert isinstance(body["city"], str) and isinstance(body["isp"], str)
    for k, lo, hi in (("latitude", -90, 90), ("longitude", -180, 180)):
        v = body[k]
        assert v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi)


def test_response_carries_exactly_the_six_intent_fields():
    with client_for(StubPipeline()) as c:
        r = c.post("/ip-geolocation", json={"ip": "8.8.8.8"})
    assert r.status_code == 200
    body = r.json()
    assert_contract(body)
    assert body["latitude"] == 37.4225 and body["country"] == "United States"


def test_coordinates_are_plain_json_numbers():
    with client_for(StubPipeline()) as c:
        raw = c.post("/ip-geolocation", json={"ip": "8.8.8.8"}).text.replace(" ", "")
    assert '"latitude":37.4225' in raw and '"longitude":-122.0842' in raw


def test_an_unlocated_address_has_null_coordinates_not_zero():
    with client_for(StubPipeline(result=GeoResult(ip="10.0.0.1"))) as c:
        body = c.post("/ip-geolocation", json={"ip": "10.0.0.1"}).json()
    assert_contract(body)
    assert body["latitude"] is None and body["longitude"] is None


@pytest.mark.parametrize("payload", [
    {}, {"ip": ""}, {"ip": "   "}, {"ip": None}, {"ip": 8}, {"ip": []}, {"wrong_key": "x"}, {"ip": {"n": 1}},
])
def test_malformed_bodies_still_answer_the_contract(payload):
    with client_for(StubPipeline()) as c:
        r = c.post("/ip-geolocation", json=payload)
    assert r.status_code == 200
    assert_contract(r.json())


def test_invalid_json_and_empty_body_still_answer_the_contract():
    with client_for(StubPipeline()) as c:
        for content in (b"{not json", b""):
            r = c.post("/ip-geolocation", content=content, headers={"Content-Type": "application/json"})
            assert r.status_code == 200
            assert_contract(r.json())


def test_ip_may_arrive_as_a_query_parameter_or_under_a_common_alias():
    stub = StubPipeline()
    with client_for(stub) as c:
        c.post("/ip-geolocation?ip=1.1.1.1", content=b"")
        c.post("/ip-geolocation", json={"address": "2.2.2.2"})
        c.post("/ip-geolocation", json={"query": "Where is 3.3.3.3 located?"})
    assert stub.seen == ["1.1.1.1", "2.2.2.2", "Where is 3.3.3.3 located?"]


def test_the_documented_field_name_beats_an_alias():
    stub = StubPipeline()
    with client_for(stub) as c:
        c.post("/ip-geolocation", json={"query": "9.9.9.9", "ip": "8.8.8.8"})
    assert stub.seen == ["8.8.8.8"]


def test_a_very_long_input_is_accepted():
    with client_for(StubPipeline()) as c:
        r = c.post("/ip-geolocation", json={"ip": "x" * 50_000})
    assert r.status_code == 200
    assert_contract(r.json())


def test_a_crashing_pipeline_still_answers_the_contract():
    with client_for(StubPipeline(error=RuntimeError("boom"))) as c:
        r = c.post("/ip-geolocation", json={"ip": "8.8.8.8"})
    assert r.status_code == 200
    body = r.json()
    assert_contract(body)
    assert body["ip"] == "8.8.8.8" and body["latitude"] is None


def test_a_hanging_pipeline_is_cut_off_and_still_answers():
    with client_for(StubPipeline(delay=5.0)) as c:
        r = c.post("/ip-geolocation", json={"ip": "8.8.8.8"})
    assert r.status_code == 200
    assert_contract(r.json())


def test_liveness_and_index():
    with client_for(StubPipeline()) as c:
        h = c.get("/healthz")
        assert h.status_code == 200 and h.json()["status"] == "ok" and h.content.strip()
        assert c.get("/").json()["intent"] == "IP_GEOLOCATION_GENERAL"
        assert c.get("/ip-geolocation").status_code in (405, 200)
        assert c.get("/healthz").status_code == 200       # still alive afterwards
