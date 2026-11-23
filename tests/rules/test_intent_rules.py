"""docs/INTENT_RULES.md, enforced. A red test here means an intent breaks a rule; the message names it.

Everything is derived from `telegraphintent/registry.py` and the committed spec snapshots in
docs/intent_specs/, so a new intent is covered the moment it is registered.
"""
from __future__ import annotations

import ast
import importlib
import json
import re
from pathlib import Path

import httpx
import pytest

from telegraphintent.registry import BY_INTENT, IDENTITY, INTENTS

ROOT = Path(__file__).resolve().parents[2]
SPECS = ROOT / "docs" / "intent_specs"
ORIGINAL = {"FRAUD_RISK_GENERAL", "FACT_CHECK_GENERAL", "CONTRACT_OBLIGATION_AUDIT_GENERAL", "IP_GEOLOCATION_GENERAL",
            "EMAIL_SECURITY_GENERAL", "URL_SAFE_GENERAL", "MALWARE_DETECTION_HASH"}
#: The pre-existing packages build their own apps; their response models are found by name.
LEGACY_MODELS = {
    "fraudrisk": "FraudRiskResponse", "factcheck": "FactCheckResponse", "audit": "AuditResponse",
    "ipgeo": "IpGeoResponse", "emailsec": "EmailSecResponse", "urlsafe": "UrlSafeResponse",
    "malware": "MalwareResponse",
}
NEW = [s for s in INTENTS if s.package.startswith("intents.")]
YAML_TYPE = {"string": "string", "enum": "string", "integer": "integer", "number": "number"}


def spec_of(intent: str) -> dict:
    path = SPECS / f"{intent}.json"
    assert path.is_file(), f"rule B5: no spec snapshot for {intent}; run scripts/snapshot_spec.py {intent}"
    return json.loads(path.read_text(encoding="utf-8"))


def model_of(spec):
    if spec.package in LEGACY_MODELS:
        return getattr(importlib.import_module(f"{spec.package}.app"), LEGACY_MODELS[spec.package])
    app_mod = importlib.import_module(f"{spec.package}.app")
    settings = importlib.import_module(f"{spec.package}.config").Settings()
    return app_mod.create_app(settings).state.response_model


def each(items=INTENTS):
    return pytest.mark.parametrize("spec", items, ids=[s.intent for s in items])


# ---- A. identity ------------------------------------------------------------------------------------------
def test_identity_is_frozen():
    assert IDENTITY == {"id": 83159274, "slug": "telegraph-intent-miner", "name": "TelegraphIntent",
                        "base_url": "https://fraudrisk.onrender.com"}, "rule A1: the miner's identity must not change"


def test_the_original_seven_intents_are_still_served_at_their_original_paths():
    paths = {"FRAUD_RISK_GENERAL": "/fraud-risk", "FACT_CHECK_GENERAL": "/fact-check",
             "CONTRACT_OBLIGATION_AUDIT_GENERAL": "/audit", "IP_GEOLOCATION_GENERAL": "/ip-geolocation",
             "EMAIL_SECURITY_GENERAL": "/email-security", "URL_SAFE_GENERAL": "/url-safe",
             "MALWARE_DETECTION_HASH": "/malware-detection"}
    for intent, path in paths.items():
        assert BY_INTENT[intent].path == path, f"rule A2: {intent} must keep its path"


# ---- B. build only to the published spec ------------------------------------------------------------------
def test_registry_has_no_duplicates_and_well_formed_paths():
    assert len({s.intent for s in INTENTS}) == len(INTENTS)
    assert len({s.path for s in INTENTS}) == len(INTENTS)
    assert len({s.name for s in INTENTS}) == len(INTENTS)
    for s in INTENTS:
        assert re.fullmatch(r"/[a-z0-9-]+", s.path), s.path
        assert re.fullmatch(r"[A-Z][A-Z0-9_]+", s.intent), s.intent


@each()
def test_the_intent_is_active_with_a_published_request_and_response(spec):
    snap = spec_of(spec.intent)
    assert snap["intent_id"] == spec.intent
    assert snap["lifecycle_state"] == "active", "rule B4: only build intents that are active"
    assert snap["request"] and snap["response"], "rule B4: a registered intent has no response format"


@each()
def test_request_params_match_the_spec(spec):
    wanted = spec_of(spec.intent)["request"]["params"]
    assert [p.name for p in spec.params] == [p["name"] for p in wanted], "rule B5: request param names/order"
    for mine, theirs in zip(spec.params, wanted):
        assert mine.required == bool(theirs.get("required")), f"rule B5: {mine.name} required flag"
        assert mine.type == YAML_TYPE[theirs["type"]], f"rule B5: {mine.name} type"


@each()
def test_response_fields_match_the_spec_in_registry_and_model(spec):
    wanted = [f["name"] for f in spec_of(spec.intent)["response"]["fields"]]
    assert list(spec.fields) == wanted, "rule B5/C8: registry response fields"
    assert list(model_of(spec).model_fields) == wanted, "rule C8: the response model's fields"


# ---- C. the response contract -----------------------------------------------------------------------------
@each()
def test_response_models_forbid_extra_fields(spec):
    assert model_of(spec).model_config.get("extra") == "forbid", "rule C7: extra='forbid'"


@each()
def test_enum_fields_only_allow_the_specs_values(spec):
    model = model_of(spec)
    for f in spec_of(spec.intent)["response"]["fields"]:
        if f["type"] == "enum":
            ann = str(model.model_fields[f["name"]].annotation)
            for v in f["allowed"]:
                assert v in ann or v.lower() in ann.lower() or "Enum" in ann or "Verdict" in ann or "Level" in ann, \
                    f"rule C7: {f['name']} must allow {v}"


@each(NEW)
def test_every_response_field_has_an_honest_default(spec):
    empty = model_of(spec)()                  # raises if any field is required
    for name, value in empty.model_dump().items():
        assert value in (None, "", [], False, "unresolved"), f"rule D9: {name} defaults to {value!r}, not an honest empty value"


@each(NEW)
def test_the_fallback_is_valid_and_honestly_empty(spec):
    app_mod = importlib.import_module(f"{spec.package}.app")
    settings = importlib.import_module(f"{spec.package}.config").Settings()
    app = app_mod.create_app(settings)
    pipeline = app.state.make_pipeline(settings, httpx.AsyncClient())
    blank = {name: "" for name in app.state.fields}
    got = pipeline.fallback(blank, "test")
    assert got.model_dump() == app.state.response_model().model_dump(), "rule D9/E14: fallback must be the empty answer"


@each(NEW)
def test_the_service_serves_the_registered_intent_at_the_registered_path(spec):
    app_mod = importlib.import_module(f"{spec.package}.app")
    app = app_mod.create_app(importlib.import_module(f"{spec.package}.config").Settings())
    assert app.state.intent == spec.intent
    assert any(getattr(r, "path", "") == spec.path and "POST" in getattr(r, "methods", ()) for r in app.routes), \
        f"rule F18: {spec.intent} is not served at POST {spec.path}"


@each(NEW)
def test_request_field_aliases_never_shadow_a_documented_param(spec):
    app_mod = importlib.import_module(f"{spec.package}.app")
    app = app_mod.create_app(importlib.import_module(f"{spec.package}.config").Settings())
    fields = app.state.fields
    for p in spec.params:
        assert p.name in fields, f"rule E15: the documented param {p.name} must be read by name"
    for name, aliases in fields.items():
        for a in aliases:
            assert a not in {p.name for p in spec.params} - {name}, f"rule E15: alias {a} is another param's name"


# ---- D. honesty and fair play -----------------------------------------------------------------------------
def test_language_models_are_limited_to_groq_through_the_shared_client():
    """Rule D11: Groq only, only via intentkit.groq; no other provider, no raw call to Groq from an intent."""
    banned = re.compile(r"(anthropic|openai|cohere|mistral|ANTHROPIC_API_KEY|OPENAI_API_KEY)", re.I)
    # code that calls the API or reads the key itself (a docstring that merely names the variable is fine)
    raw_groq = re.compile(r"""api\.groq\.com|chat/completions|(?:environ|getenv|env)\s*[(\[]\s*["']GROQ""")
    for path in (ROOT / "intents").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not banned.search(text), f"rule D11: {path.relative_to(ROOT)} uses a model provider other than Groq"
        assert not raw_groq.search(text), f"rule D11: {path.relative_to(ROOT)} must call Groq through intentkit.groq"
    for path in (ROOT / "intentkit").rglob("*.py"):
        if path.name != "groq.py":
            assert "api.groq.com" not in path.read_text(encoding="utf-8"), f"rule D11: {path.name} talks to Groq directly"


def test_new_intents_do_not_hard_code_question_keys_as_answers():
    """Rule D10: a literal pair like {"BTC": 60000} or {"CVE-2021-44228": 10.0} would be a canned answer."""
    for path in (ROOT / "intents").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if (isinstance(k, ast.Constant) and isinstance(k.value, str) and isinstance(v, ast.Constant)
                            and isinstance(v.value, (int, float)) and not isinstance(v.value, bool)):
                        keys = [q for s in INTENTS for q in (spec_of(s.intent)["evaluation"].get("comparator") or {}).get("question_keys") or []]
                        assert k.value not in keys, f"rule D10: {path.relative_to(ROOT)} maps question key {k.value!r} to a number"


# ---- E/F. structure ---------------------------------------------------------------------------------------
def test_new_intents_live_under_intents_and_old_ones_are_untouched():
    for s in INTENTS:
        top = s.package.split(".")[0]
        assert (ROOT / top).is_dir(), s.package
        if s.intent in ORIGINAL:
            assert "." not in s.package, "rule A2: the original packages stay where they are"
        else:
            assert s.package.startswith("intents."), f"rule E13: {s.intent} belongs in intents/<package>"


@each(NEW)
def test_new_packages_follow_the_layout(spec):
    pkg = ROOT.joinpath(*spec.package.split("."))
    for name in ("__init__.py", "app.py", "config.py"):
        assert (pkg / name).is_file(), f"rule E13: {spec.package} is missing {name}"
    cfg = importlib.import_module(f"{spec.package}.config").Settings
    from intentkit.settings import BaseSettings
    if spec.package in ("intents.urlscan", "intents.threatintel"):       # views over urlsafe / malware settings
        assert hasattr(cfg, "from_env")
    else:
        assert issubclass(cfg, BaseSettings), f"rule E13: {spec.package}.config.Settings must extend BaseSettings"


# ---- G/H. tests, docs, process ----------------------------------------------------------------------------
@each(NEW)
def test_every_new_intent_has_unit_and_live_tests(spec):
    unit = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "tests" / "intents").glob("test_*.py")
                     if p.name != "test_live.py")
    live = (ROOT / "tests" / "intents" / "test_live.py").read_text(encoding="utf-8")
    assert spec.package in unit, f"rule G20: no unit test imports {spec.package}"
    assert spec.intent in live, f"rule G20: no live test for {spec.intent} in tests/intents/test_live.py"


LLM = [s for s in INTENTS if s.llm]


@each(LLM)
def test_an_llm_intent_extends_llm_settings_and_has_failure_mode_tests(spec):
    from intentkit.groq import LlmSettings
    assert issubclass(importlib.import_module(f"{spec.package}.config").Settings, LlmSettings),         f"rule D11: {spec.package}.config.Settings must extend intentkit.groq.LlmSettings"
    tests = (ROOT / "tests" / "intents" / "test_llm_failure_modes.py").read_text(encoding="utf-8")
    assert spec.package in tests, f"rule D11: tests/intents/test_llm_failure_modes.py must cover {spec.package} (no key, 401, 429, ...)"


def test_only_registered_llm_intents_import_the_groq_client():
    users = {p.parent.name for p in (ROOT / "intents").rglob("*.py")
             if re.search(r"intentkit\.(groq|llm_pipeline)", p.read_text(encoding="utf-8"))}
    assert users <= {s.name for s in LLM} | {"fx"}, f"rule D11: mark these llm=True in the registry: {users - {s.name for s in LLM}}"


def test_the_docs_list_every_new_intent():
    doc = (ROOT / "docs" / "MoreIntents.md").read_text(encoding="utf-8")
    for s in NEW:
        assert s.intent in doc, f"checklist step 7: add {s.intent} to docs/MoreIntents.md"


def test_the_rules_are_wired_into_every_session():
    assert (ROOT / "docs" / "INTENT_RULES.md").is_file()
    assert "docs/INTENT_RULES.md" in (ROOT / "CLAUDE.md").read_text(encoding="utf-8")


def test_rules_document_numbers_are_contiguous_and_referenced_rules_exist():
    text = (ROOT / "docs" / "INTENT_RULES.md").read_text(encoding="utf-8")
    numbered = [int(n) for n in re.findall(r"^(\d+)\. ", text, re.M)]
    assert numbered[:23] == list(range(1, 24)), "the rule numbers in docs/INTENT_RULES.md must run 1..23"


def test_env_example_documents_the_new_settings():
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    for needle in ("NVD_API_KEY", "SEC_USER_AGENT", "IPSUM_MIN_LISTS", "MATCH_THRESHOLD", "ALLOW_PRIVATE_HOSTS",
                   "GROQ_API_KEY", "GROQ_MODELS", "GROQ_TIMEOUT_S", "OPENALEX_API_KEY", "UPPTIME_REPOS"):
        assert needle in env, f"rule E17: document {needle} in .env.example"
