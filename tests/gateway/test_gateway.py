"""The combined app routes each path to the right intent package, unchanged, and hosts them
all in one process behind one /healthz and one byte-exact /miner.yaml.

Everything here is derived from `telegraphintent/registry.py`, so adding an intent to the registry extends
these tests with no edit to this file.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from audit.app import create_app as create_audit
from audit.config import Settings as AuditSettings
from emailsec.app import create_app as create_emailsec
from factcheck.app import create_app as create_factcheck
from factcheck.pipeline import FactCheckPipeline
from fraudrisk.app import create_app as create_fraud
from ipgeo.app import create_app as create_ipgeo
from malware.app import create_app as create_malware
from telegraphintent.app import INTENTS, TelegraphIntentApp, YAML_PATH, scoped_env
from telegraphintent.registry import INTENTS as REGISTRY
from tests.audit.conftest import make_settings as audit_settings
from tests.audit.test_api import StubPipeline as AuditStub
from tests.emailsec.conftest import make_settings as emailsec_settings
from tests.emailsec.test_api import StubPipeline as EmailSecStub
from tests.factcheck.conftest import FakeSource, ev, make_settings as fact_settings
from tests.fraudrisk.conftest import make_settings as fraud_settings
from tests.fraudrisk.test_api import StubPipeline as FraudStub
from tests.intents.helpers import StubPipeline
from tests.ipgeo.conftest import make_settings as ipgeo_settings
from tests.ipgeo.test_api import StubPipeline as IpGeoStub
from tests.malware.conftest import make_settings as malware_settings
from tests.malware.test_api import StubPipeline as MalwareStub
from tests.urlsafe.conftest import make_settings as urlsafe_settings
from tests.urlsafe.test_api import StubPipeline as UrlSafeStub
from urlsafe.app import create_app as create_urlsafe

FACT_EV = [ev("https://en.wikipedia.org/wiki/Eiffel_Tower", "Eiffel Tower",
              "The Eiffel Tower is a wrought-iron lattice tower located in Paris, France.",
              provider="wikipedia")]

ALL_NAMES = {s.name for s in REGISTRY}
ALL_INTENTS = {s.intent for s in REGISTRY}
ORIGINAL = ("fraudrisk", "factcheck", "audit", "ipgeo", "emailsec", "urlsafe", "malware")


def stub_app(spec):
    """A real intents/<package> app whose pipeline is replaced by a stub answering the empty model."""
    app_mod = importlib.import_module(f"{spec.package}.app")
    settings = importlib.import_module(f"{spec.package}.config").Settings()
    model = app_mod.create_app(settings).state.response_model
    return app_mod.create_app(settings, pipeline=StubPipeline(model))


def combined() -> TelegraphIntentApp:
    fs = fact_settings()
    apps = {
        "fraudrisk": create_fraud(fraud_settings(), pipeline=FraudStub()),
        "factcheck": create_factcheck(fs, FactCheckPipeline(fs, httpx.AsyncClient(),
                                                            sources=[FakeSource("fake", FACT_EV)])),
        "audit": create_audit(audit_settings(), pipeline=AuditStub()),
        "ipgeo": create_ipgeo(ipgeo_settings(), pipeline=IpGeoStub()),
        "emailsec": create_emailsec(emailsec_settings(), pipeline=EmailSecStub()),
        "urlsafe": create_urlsafe(urlsafe_settings(), pipeline=UrlSafeStub()),
        "malware": create_malware(malware_settings(), pipeline=MalwareStub()),
    }
    for spec in REGISTRY:
        if spec.name not in apps:
            apps[spec.name] = stub_app(spec)
    return TelegraphIntentApp(apps=apps)


@pytest.fixture(scope="module")
def client():
    with TestClient(combined()) as c:
        yield c


def by_intent(param="spec"):
    return pytest.mark.parametrize(param, REGISTRY, ids=[s.intent for s in REGISTRY])


@by_intent()
def test_each_intent_answers_with_its_own_exact_fields(client, spec):
    r = client.post(spec.path, json=json.loads(spec.probe))
    assert r.status_code == 200 and set(r.json()) == set(spec.fields), r.text


@by_intent()
def test_malformed_body_still_returns_200_with_the_intent_fields(client, spec):
    r = client.post(spec.path, content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 200 and set(r.json()) == set(spec.fields), r.text


@by_intent()
def test_trailing_slash_still_routes(client, spec):
    assert client.post(spec.path + "/", json=json.loads(spec.probe)).status_code == 200


@by_intent()
def test_a_cors_preflight_on_every_intent_path_succeeds(client, spec):
    r = client.options(spec.path, headers={"Origin": "https://anjoman.ai", "Access-Control-Request-Method": "POST"})
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == "*"
    assert "POST" in r.headers["access-control-allow-methods"]
    assert r.content == b""


@by_intent()
def test_the_real_response_also_carries_cors_headers(client, spec):
    r = client.post(spec.path, json=json.loads(spec.probe))
    assert r.headers["access-control-allow-origin"] == "*"
    assert r.headers["vary"].lower() == "origin"


@pytest.mark.parametrize("path", ["/healthz", "/miner.yaml", "/", "/nope"])
def test_cors_preflight_and_headers_cover_the_non_intent_routes_too(client, path):
    pre = client.options(path, headers={"Origin": "https://anjoman.ai", "Access-Control-Request-Method": "GET"})
    assert pre.status_code == 204 and pre.headers["access-control-allow-origin"] == "*"
    real = client.get(path)
    assert real.headers["access-control-allow-origin"] == "*"


def test_healthz_reports_every_intent(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert set(body["intents"]) == ALL_NAMES
    assert all(v["status"] == "ok" for v in body["intents"].values())


def test_miner_yaml_is_served_byte_exact(client):
    raw = YAML_PATH.read_bytes()
    r = client.get("/miner.yaml")
    assert r.status_code == 200 and r.content == raw and b"\r" not in r.content
    assert hashlib.sha256(r.content).hexdigest() == hashlib.sha256(raw).hexdigest()


def test_index_lists_every_intent(client):
    intents = client.get("/").json()["intents"]
    assert set(intents) == ALL_INTENTS


def test_unknown_path_is_404_json(client):
    r = client.get("/nope")
    assert r.status_code == 404 and r.json() == {"status": "not_found"}


def test_every_yaml_endpoint_is_routed():
    import yaml
    doc = yaml.safe_load(YAML_PATH.read_bytes())
    app = combined()
    assert {ep["path"] for ep in doc["endpoints"]} == set(app.routes)


def test_real_module_level_app_builds_every_intent_keyless():
    """The deployed entrypoint (no stubs) must construct and start with no API keys."""
    from telegraphintent.app import app
    assert set(app.apps) == ALL_NAMES


def test_the_router_table_matches_the_registry_and_has_unique_paths():
    assert {name for name, *_ in INTENTS} == ALL_NAMES
    assert {intent for _, intent, _, _ in INTENTS} == ALL_INTENTS
    paths = [p for _, _, p, _ in INTENTS]
    assert len(paths) == len(set(paths))
    assert len(ALL_NAMES) == len(REGISTRY), "two intents share a package name"


@by_intent()
def test_each_intent_can_be_configured_on_its_own_with_a_prefixed_variable(monkeypatch, spec):
    monkeypatch.setenv("GLOBAL_TIMEOUT_S", "7.5")
    monkeypatch.setenv(f"{spec.prefix}_GLOBAL_TIMEOUT_S", "2.5")
    settings_cls = importlib.import_module(f"{spec.package}.config").Settings
    with scoped_env(spec.prefix):
        assert settings_cls.from_env().global_timeout == 2.5
    assert settings_cls.from_env().global_timeout == 7.5


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
