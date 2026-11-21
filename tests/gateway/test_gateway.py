"""The combined app routes each path to the right intent package, unchanged, and hosts them
all in one process behind one /healthz and one byte-exact /miner.yaml."""
from __future__ import annotations

import hashlib
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from audit.app import create_app as create_audit
from audit.config import Settings as AuditSettings
from factcheck.app import create_app as create_factcheck
from factcheck.pipeline import FactCheckPipeline
from fraudrisk.app import create_app as create_fraud
from telegraphintent.app import TelegraphIntentApp, YAML_PATH, scoped_env
from tests.audit.conftest import make_settings as audit_settings
from tests.audit.test_api import StubPipeline as AuditStub
from tests.factcheck.conftest import FakeSource, ev, make_settings as fact_settings
from tests.fraudrisk.conftest import make_settings as fraud_settings
from tests.fraudrisk.test_api import StubPipeline as FraudStub

FACT_EV = [ev("https://en.wikipedia.org/wiki/Eiffel_Tower", "Eiffel Tower",
              "The Eiffel Tower is a wrought-iron lattice tower located in Paris, France.",
              provider="wikipedia")]


def combined() -> TelegraphIntentApp:
    fs = fact_settings()
    apps = {
        "fraudrisk": create_fraud(fraud_settings(), pipeline=FraudStub()),
        "factcheck": create_factcheck(fs, FactCheckPipeline(fs, httpx.AsyncClient(),
                                                            sources=[FakeSource("fake", FACT_EV)])),
        "audit": create_audit(audit_settings(), pipeline=AuditStub()),
    }
    return TelegraphIntentApp(apps=apps)


@pytest.fixture
def client():
    with TestClient(combined()) as c:
        yield c


def test_each_intent_answers_with_its_own_exact_fields(client):
    r = client.post("/fraud-risk", json={"subject": "203.0.113.9"})
    assert r.status_code == 200 and set(r.json()) == {"risk_level", "risk_score", "signals", "summary"}
    r = client.post("/fact-check", json={"claim": "The Eiffel Tower is located in Paris."})
    assert r.status_code == 200 and set(r.json()) == {"claim", "verdict", "reasoning", "sources"}
    r = client.post("/audit", json={"contract": "1.1 The Supplier shall deliver the Goods."})
    assert r.status_code == 200 and set(r.json()) == {"verdict", "obligations", "clauses", "summary"}


@pytest.mark.parametrize("path,fields", [
    ("/fraud-risk", {"risk_level", "risk_score", "signals", "summary"}),
    ("/fact-check", {"claim", "verdict", "reasoning", "sources"}),
    ("/audit", {"verdict", "obligations", "clauses", "summary"}),
])
def test_malformed_body_still_returns_200_with_the_intent_fields(client, path, fields):
    r = client.post(path, content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 200 and set(r.json()) == fields


def test_trailing_slash_still_routes(client):
    assert client.post("/fraud-risk/", json={"subject": "x"}).status_code == 200


def test_healthz_reports_all_three_intents(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert set(body["intents"]) == {"fraudrisk", "factcheck", "audit"}
    assert all(v["status"] == "ok" for v in body["intents"].values())


def test_miner_yaml_is_served_byte_exact(client):
    raw = YAML_PATH.read_bytes()
    r = client.get("/miner.yaml")
    assert r.status_code == 200 and r.content == raw and b"\r" not in r.content
    assert hashlib.sha256(r.content).hexdigest() == hashlib.sha256(raw).hexdigest()


def test_index_lists_every_intent(client):
    intents = client.get("/").json()["intents"]
    assert set(intents) == {"FRAUD_RISK_GENERAL", "FACT_CHECK_GENERAL",
                            "CONTRACT_OBLIGATION_AUDIT_GENERAL"}


def test_unknown_path_is_404_json(client):
    r = client.get("/nope")
    assert r.status_code == 404 and r.json() == {"status": "not_found"}


def test_every_yaml_endpoint_is_routed():
    import yaml
    doc = yaml.safe_load(YAML_PATH.read_bytes())
    app = combined()
    assert {ep["path"] for ep in doc["endpoints"]} == set(app.routes)


def test_real_module_level_app_builds_all_three_keyless():
    """The deployed entrypoint (no stubs) must construct and start with no API keys."""
    from telegraphintent.app import app
    assert set(app.apps) == {"fraudrisk", "factcheck", "audit"}


class TestScopedEnv:
    def test_prefixed_var_overrides_shared_only_inside_the_block(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_MODEL", "shared-model")
        monkeypatch.setenv("FACTCHECK_ANTHROPIC_MODEL", "fact-model")
        with scoped_env("FACTCHECK"):
            assert os.environ["ANTHROPIC_MODEL"] == "fact-model"
        assert os.environ["ANTHROPIC_MODEL"] == "shared-model"

    def test_var_that_was_unset_is_removed_afterwards(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_EFFORT", raising=False)
        monkeypatch.setenv("AUDIT_ANTHROPIC_EFFORT", "high")
        with scoped_env("AUDIT"):
            assert AuditSettings.from_env().effort == "high"
        assert "ANTHROPIC_EFFORT" not in os.environ

    def test_other_intents_prefix_does_not_leak(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_MODEL", "shared-model")
        monkeypatch.setenv("FRAUDRISK_ANTHROPIC_MODEL", "fraud-model")
        with scoped_env("AUDIT"):
            assert os.environ["ANTHROPIC_MODEL"] == "shared-model"
