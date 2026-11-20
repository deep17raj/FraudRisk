"""Regression suite over real subjects with expected risk bands.

Every case runs TWICE: once on the deterministic path (no model) and once with a fake LLM
that behaves like a cooperative model. Both runs must land in the same allowed band - that is
the property which keeps answers stable when the model is slow, missing or degraded.

The suite is hermetic: the bulk feeds are pre-populated in memory and the BIN lookup answers
from a local table, so no test touches the network.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from fraudrisk.models import EntityKind, RiskLevel
from fraudrisk.scoring import level_for_score
from fraudrisk.pipeline import FraudRiskPipeline
from fraudrisk.sources.card_bin import CardBinSource
from fraudrisk.sources.feeds import FeedRegistry
from fraudrisk.sources.phish import DomainHeuristicSource, EmailHygieneSource, PhishFeedSource
from fraudrisk.sources.ip_rep import TorExitSource
from fraudrisk.sources.rules import BehaviourRuleSource
from tests.conftest import echo_llm, make_settings, scripted_llm

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "subjects.json").read_text("utf-8"))
CASES = FIXTURES["cases"]

TOR_EXITS = {"185.220.101.1", "171.25.193.25"}
PHISH_ENTRIES = {
    "http://paypal-verify.secure-login.tk/account",
    "paypal-verify.secure-login.tk",
}

#: Local stand-in for binlist.net, keyed by BIN.
BIN_TABLE = {
    "517805": {"scheme": "mastercard", "type": "prepaid", "brand": "Mastercard Prepaid",
               "prepaid": True, "country": {"alpha2": "GB", "name": "United Kingdom"},
               "bank": {"name": "Example Prepaid Ltd"}},
    "457173": {"scheme": "visa", "type": "debit", "brand": "Visa Classic/Dankort",
               "prepaid": False, "country": {"alpha2": "DK", "name": "Denmark"},
               "bank": {"name": "Jyske Bank A/S"}},
}


class StubBinSource(CardBinSource):
    """Same normalisation as the real source, answering from `BIN_TABLE` instead of HTTP."""

    async def collect(self, parsed, client, timeout, settings=None):
        ents = parsed.entities_of(EntityKind.CARD_BIN)
        if not ents:
            return []
        data = BIN_TABLE.get(ents[0].value[:6])
        if not data:
            return []
        return self._to_signals(ents[0].value[:6], data, parsed)


def stub_feeds() -> FeedRegistry:
    feeds = FeedRegistry(ttl=3600, timeout=1.0, tor=True, phish=True)
    now = time.monotonic()
    feeds.tor._items = frozenset(TOR_EXITS)
    feeds.tor._loaded_at = now
    feeds.phish._items = frozenset(PHISH_ENTRIES)
    feeds.phish._loaded_at = now
    return feeds


def build_pipeline(client: httpx.AsyncClient, llm=None) -> FraudRiskPipeline:
    settings = make_settings(reasoning_mode="auto" if llm else "off")
    feeds = stub_feeds()
    sources = [
        BehaviourRuleSource(),
        DomainHeuristicSource(),
        EmailHygieneSource(),
        TorExitSource(feeds),
        PhishFeedSource(feeds),
        StubBinSource(),
    ]
    return FraudRiskPipeline(settings, client, sources=sources, llm=llm, feeds=feeds)


def signal_ids(assessment) -> set[str]:
    return {s["id"] for s in assessment.debug.get("signals", []) if s.get("applicable")}


def check_case(case: dict, res) -> None:
    allowed = {RiskLevel(x) for x in case["allow"]}
    name = case["name"]

    assert res.level in allowed, f"{name}: {res.level.value} ({res.score}) not in {case['allow']}"
    assert 0.0 <= res.score <= 1.0, name
    assert res.summary.strip(), f"{name}: empty summary"

    from fraudrisk.scoring import level_for_score
    assert level_for_score(res.score) is res.level, f"{name}: score/level disagree"

    if case.get("must_abstain"):
        assert res.score == 0.0, f"{name}: abstention must score exactly 0.0, got {res.score}"
        assert res.signals == [], f"{name}: abstention must cite no signals, got {res.signals}"
        lowered = res.summary.lower()
        assert any(p in lowered for p in ("insufficient information", "no checkable",
                                          "nothing to assess")), f"{name}: {res.summary}"

    ids = signal_ids(res)
    for required in case.get("require_signal_ids", []):
        assert required in ids, f"{name}: expected signal {required}, got {sorted(ids)}"
    for forbidden in case.get("forbid_signal_ids", []):
        assert forbidden not in ids, f"{name}: fabricated signal {forbidden}"

    if "expect_pattern_in" in case:
        assert res.debug.get("pattern") in case["expect_pattern_in"], \
            f"{name}: pattern {res.debug.get('pattern')} not in {case['expect_pattern_in']}"
    if case.get("expect_corroborated"):
        assert res.debug.get("corroborated") is True, f"{name}: expected >= 2 signal families"


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
async def test_deterministic_path(case, http_client):
    """No model at all: rules, feeds and lookups only."""
    pl = build_pipeline(http_client)
    check_case(case, await pl.assess(case["subject"]))


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
async def test_with_model_in_the_loop(case, http_client):
    """A cooperative model annotating the same signals must not move the band."""
    pattern = (case.get("expect_pattern_in") or ["no identifiable pattern"])[0]
    pl = build_pipeline(http_client, llm=echo_llm(
        pattern=pattern, summary="The gathered signals are consistent with one another."))
    check_case(case, await pl.assess(case["subject"]))


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
async def test_both_paths_agree_on_the_band(case, http_client):
    pl_det = build_pipeline(http_client)
    pl_llm = build_pipeline(http_client, llm=echo_llm(
        pattern=(case.get("expect_pattern_in") or ["no identifiable pattern"])[0]))
    det = await pl_det.assess(case["subject"])
    llm = await pl_llm.assess(case["subject"])
    assert det.level is llm.level, \
        f"{case['name']}: deterministic {det.level.value} vs model {llm.level.value}"


async def test_repeated_assessment_is_stable(http_client):
    """Same subject, fresh pipeline each time: identical score. No drift, no randomness."""
    subject = CASES[0]["subject"]
    scores = set()
    for _ in range(5):
        pl = build_pipeline(http_client)
        res = await pl.assess(subject)
        scores.add(res.score)
    assert len(scores) == 1, scores


async def test_cache_returns_the_same_answer(http_client):
    pl = build_pipeline(http_client)
    subject = "Prepaid card BIN 517805, 14 declined attempts of $1.00 each within three minutes"
    first = await pl.assess(subject)
    second = await pl.assess(subject)
    assert second.debug.get("cache_hit") is True
    assert (second.level, second.score, second.summary) == (first.level, first.score, first.summary)


async def test_every_risk_level_appears_somewhere_in_the_suite(http_client):
    """Guards against a scorer that has collapsed onto one or two grades."""
    seen = set()
    for case in CASES:
        pl = build_pipeline(http_client)
        seen.add((await pl.assess(case["subject"])).level)
    assert {RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL} <= seen, seen


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
async def test_band_is_stable_across_model_behaviour(case, http_client):
    """Across the full range the schema permits, the grade must stay defensible.

    `test_both_paths_agree_on_the_band` compares the deterministic path against a model that
    returns source defaults, which on its own is close to tautological. This test instead
    drives the reasoning layer to its extremes - every signal strong, every signal weak,
    every signal dismissed - and asserts the answer stays sane: an all-strong model may raise
    a case by at most one band, an all-weak one may lower it by at most one, a must-abstain
    case abstains whatever the model says, and the score/level pair never disagrees.
    """
    order = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]
    pattern = (case.get("expect_pattern_in") or ["no identifiable pattern"])[0]

    results = {}
    for name, llm in (
        ("strong", scripted_llm(strength="strong", pattern=pattern)),
        ("weak", scripted_llm(strength="weak", pattern=pattern)),
        ("dismiss_all", scripted_llm(applies=False, pattern=pattern)),
    ):
        pl = build_pipeline(http_client, llm=llm)
        results[name] = await pl.assess(case["subject"])

    for name, res in results.items():
        assert res.level in order, name
        assert 0.0 <= res.score <= 1.0, f"{case['name']}/{name}"
        assert res.summary.strip(), f"{case['name']}/{name}: empty summary"
        assert level_for_score(res.score) is res.level, f"{case['name']}/{name}: pair disagrees"
        if case.get("must_abstain"):
            assert res.score == 0.0 and res.signals == [], f"{case['name']}/{name}"

    allowed = {RiskLevel(x) for x in case["allow"]}
    lo = min(order.index(x) for x in allowed)
    hi = max(order.index(x) for x in allowed)
    assert order.index(results["strong"].level) <= hi + 1, (
        f"{case['name']}: all-strong model inflated to {results['strong'].level.value}")
    assert order.index(results["weak"].level) >= lo - 1, (
        f"{case['name']}: all-weak model deflated to {results['weak'].level.value}")


async def test_a_model_cannot_dismiss_a_verified_fact(http_client):
    """A live phishing-feed hit is a checked fact: the model may rank it weak, not delete it."""
    pl = build_pipeline(http_client, llm=scripted_llm(applies=False, pattern="phishing"))
    res = await pl.assess("http://paypal-verify.secure-login.tk/account")
    assert res.score > 0.0, "a verified feed hit was scored away entirely"
    feed = [s for s in res.debug["signals"] if s["id"] == "phish.feed_hit"]
    assert feed and feed[0]["applicable"] is True
    assert feed[0]["strength"] == "weak", feed


async def test_a_model_may_dismiss_an_inferred_signal(http_client):
    """The counterpart: inferences are exactly what the model is there to rule out."""
    pl = build_pipeline(http_client, llm=scripted_llm(applies=False, pattern="phishing"))
    res = await pl.assess("secure-paypal-login-verify.tk")
    inferred = [s for s in res.debug["signals"] if s["id"].startswith("domain.")]
    assert inferred, "expected domain-shape inferences on this host"
    assert all(s["applicable"] is False for s in inferred), inferred
    assert res.score == 0.0 and res.level is RiskLevel.LOW
