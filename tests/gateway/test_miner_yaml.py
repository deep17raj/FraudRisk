"""The single miner.yaml must satisfy Telegraph's closed schema and match all seven intents.

Registration is rejected outright on any unknown key, so these tests are the cheap way to
find that out before spending the MACHINA bond on a failing transaction.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
RAW = (ROOT / "miner.yaml").read_bytes()
DOC = yaml.safe_load(RAW)

TOP = {"version", "kind", "id", "slug", "protocol", "name", "description", "base_url",
       "input_schema", "output_schema", "intent_mapping", "polling", "cache_ttl_sec",
       "rate_limit_per_sec", "circuit_threshold", "circuit_cooldown_seconds", "docs",
       "limitations", "errors", "auth", "endpoints", "semantics"}
ENDPOINT = {"path", "external_path", "method", "description", "intents", "params",
            "endpoint_base_url", "content_type", "multipart_fields", "param_map"}
AUTH = {"type", "env_var", "header_name", "value_prefix", "inject"}
SIGNAL = {"confidence_field", "label_field", "reason_field"}
SEMANTICS = {"signal_mapping", "supported_intents"}
PARAM = {"name", "type", "intents", "description", "accepted_fields"}
REQUIRED = {"version", "kind", "id", "slug", "name", "base_url"}

#: path -> (intent, the intent's request params in order). Names are Telegraph's own, taken from
#: GET /engine/v1/intents/<INTENT> (`request.params`); the node builds each call from these.
EXPECTED = {
    "/fraud-risk": ("FRAUD_RISK_GENERAL", ("subject",)),
    "/fact-check": ("FACT_CHECK_GENERAL", ("claim",)),
    "/audit": ("CONTRACT_OBLIGATION_AUDIT_GENERAL", ("contract",)),
    "/ip-geolocation": ("IP_GEOLOCATION_GENERAL", ("ip",)),
    "/email-security": ("EMAIL_SECURITY_GENERAL", ("domain", "record_type")),
    "/url-safe": ("URL_SAFE_GENERAL", ("url",)),
    "/malware-detection": ("MALWARE_DETECTION_HASH", ("indicator",)),
}


def test_no_unknown_keys_anywhere():
    assert set(DOC) <= TOP, set(DOC) - TOP
    assert REQUIRED <= set(DOC)
    assert set(DOC["auth"]) <= AUTH and DOC["auth"]["type"] in {"bearer", "header", "none"}
    assert set(DOC["semantics"]) <= SEMANTICS
    assert set(DOC["semantics"].get("signal_mapping", {})) <= SIGNAL
    for ep in DOC["endpoints"]:
        assert set(ep) <= ENDPOINT, set(ep) - ENDPOINT
        assert "input_schema" not in ep and "output_schema" not in ep


def test_identity_fields():
    assert DOC["version"] == "1" and DOC["kind"] == "miner"
    assert DOC["protocol"] in {"generic", "bittensor"}
    assert isinstance(DOC["id"], int) and DOC["id"] > 0
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", DOC["slug"])
    assert DOC["base_url"].startswith(("http://", "https://"))
    assert DOC["description"].strip() and DOC["name"].strip()


def test_one_endpoint_per_intent_with_the_right_request_fields():
    eps = {ep["path"]: ep for ep in DOC["endpoints"]}
    assert set(eps) == set(EXPECTED), "miner.yaml must declare exactly the seven intent endpoints"
    for path, (intent, fields) in EXPECTED.items():
        ep = eps[path]
        assert ep["external_path"] == path and ep["method"] == "POST"
        assert ep["description"].strip() and ep["intents"] == [intent]
        required = ep["params"]["body"]["required"]
        assert tuple(p["name"] for p in required) == fields
        assert "optional" not in ep["params"]["body"], "every request param of these intents is required"
        for p in required:
            assert p["type"] == "string" and p["intents"] == ["*"] and p["description"]
            assert set(p) <= PARAM, set(p) - PARAM


def test_supported_intents_are_consistent():
    supported = set(DOC["semantics"]["supported_intents"])
    assert supported == {i for i, _ in EXPECTED.values()}
    assert all(set(ep["intents"]) <= supported for ep in DOC["endpoints"])


def test_signal_mapping_if_present_names_real_fields():
    sm = DOC["semantics"].get("signal_mapping")
    if not sm:
        return
    from audit.app import AuditResponse
    from factcheck.app import FactCheckResponse
    from fraudrisk.app import FraudRiskResponse

    shared = (set(AuditResponse.model_fields) & set(FactCheckResponse.model_fields)
              & set(FraudRiskResponse.model_fields))
    for key, value in sm.items():
        assert value in shared, f"{key}={value} is not returned by all three intents"


def test_file_is_lf_only_and_utf8():
    """The on-chain sha256 must match the bytes served at YAML_URL byte for byte."""
    assert b"\r" not in RAW
    assert not RAW.startswith(b"\xef\xbb\xbf"), "a UTF-8 BOM would change the hash"
    RAW.decode("utf-8")


def test_register_script_covers_every_intent():
    script = (ROOT / "register.sh").read_text(encoding="utf-8")
    assert "REPLACE-WITH" in script, "the guard that blocks a placeholder base_url is missing"
    for intent, _ in EXPECTED.values():
        assert intent in script
    assert "sha256" in script.lower()
    for path in EXPECTED:
        assert f"probe {path} " in script, f"register.sh does not smoke-test {path}"
    for secret in ("MINER_PRIVATE_KEY=0x", "0x1234"):
        assert f"\n{secret}" not in script, "no real or dummy key may be hardcoded"


def test_dockerfile_ships_every_package():
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for pkg in ("telegraphintent", "fraudrisk", "factcheck", "audit", "intentkit", "ipgeo", "emailsec",
                "urlsafe", "malware"):
        assert f"COPY {pkg} ./{pkg}" in docker
    assert "COPY miner.yaml" in docker and "telegraphintent.app:app" in docker
