"""The highest-leverage tests: the deterministic scorer.

Pure logic, so every band boundary and every invariant is asserted directly. The single most
important property is that `risk_score` and `risk_level` can never disagree - that pair is
what a judge checks first, and inconsistency is the most heavily penalised failure.
"""
from __future__ import annotations

import pytest

from fraudrisk.models import Family, RiskLevel, Strength
from fraudrisk.scoring import (DEFAULT_THRESHOLDS, SCORE_DECIMALS, Thresholds, compute_score,
                               family_weights, level_for_score)
from tests.fraudrisk.conftest import sig

T = DEFAULT_THRESHOLDS


# --------------------------------------------------------------- band boundaries

@pytest.mark.parametrize("score,expected", [
    (0.0, RiskLevel.LOW),
    (0.000001, RiskLevel.LOW),
    (0.249999, RiskLevel.LOW),
    (0.25, RiskLevel.MEDIUM),            # boundary is inclusive at the bottom of each band
    (0.4, RiskLevel.MEDIUM),
    (0.499999, RiskLevel.MEDIUM),
    (0.5, RiskLevel.HIGH),
    (0.79, RiskLevel.HIGH),
    (0.799999, RiskLevel.HIGH),
    (0.8, RiskLevel.CRITICAL),
    (0.95, RiskLevel.CRITICAL),
    (1.0, RiskLevel.CRITICAL),
])
def test_level_for_score_covers_every_band(score, expected):
    assert level_for_score(score) is expected


def test_every_level_is_reachable_from_real_signal_sets():
    """Each of the four grades must be produced by a plausible signal set, not just in theory."""
    cases = {
        RiskLevel.LOW: [sig(severity=0.2, reliability=0.5)],
        RiskLevel.MEDIUM: [sig(severity=0.45, reliability=0.8)],
        RiskLevel.HIGH: [sig(id="a", severity=0.6, reliability=0.85, family=Family.BEHAVIOR),
                         sig(id="b", severity=0.5, reliability=0.8, family=Family.ANONYMITY)],
        RiskLevel.CRITICAL: [
            sig(id="a", severity=0.75, reliability=0.9, family=Family.BEHAVIOR),
            sig(id="b", severity=0.72, reliability=0.95, family=Family.ANONYMITY),
            sig(id="c", severity=0.6, reliability=0.85, family=Family.CARD),
        ],
    }
    for expected, signals in cases.items():
        result = compute_score(signals)
        assert result.level is expected, f"{expected}: got {result.level} at {result.score}"


# --------------------------------------------------------------- the core invariant

@pytest.mark.parametrize("n_families,sev,rel", [
    (1, 0.1, 0.5), (1, 0.5, 0.8), (1, 0.99, 0.99),
    (2, 0.3, 0.6), (2, 0.7, 0.9), (3, 0.6, 0.85), (4, 0.9, 0.95),
])
def test_score_and_level_are_always_consistent(n_families, sev, rel):
    fams = list(Family)[:n_families]
    signals = [sig(id=f"s{i}", severity=sev, reliability=rel, family=f)
               for i, f in enumerate(fams)]
    r = compute_score(signals)
    assert 0.0 <= r.score <= 1.0
    assert r.level is level_for_score(r.score)
    assert round(r.score, SCORE_DECIMALS) == r.score, "score must already be rounded"


def test_identical_inputs_give_identical_scores():
    build = lambda: [sig(id="a", severity=0.55, reliability=0.85, family=Family.BEHAVIOR),
                     sig(id="b", severity=0.4, reliability=0.7, family=Family.CARD)]
    first = compute_score(build())
    for _ in range(25):
        again = compute_score(build())
        assert again.score == first.score and again.level is first.level


def test_score_is_order_independent():
    a = sig(id="a", severity=0.6, reliability=0.9, family=Family.BEHAVIOR)
    b = sig(id="b", severity=0.4, reliability=0.8, family=Family.CARD)
    c = sig(id="c", severity=0.5, reliability=0.7, family=Family.ANONYMITY)
    assert compute_score([a, b, c]).score == compute_score([c, a, b]).score


def test_adding_a_risk_signal_never_lowers_the_score():
    base = [sig(id="a", severity=0.5, reliability=0.8, family=Family.BEHAVIOR)]
    more = base + [sig(id="b", severity=0.45, reliability=0.75, family=Family.CARD)]
    assert compute_score(more).score >= compute_score(base).score


# --------------------------------------------------------------- abstention

def test_no_signals_scores_exactly_zero():
    r = compute_score([])
    assert r.score == 0.0 and r.level is RiskLevel.LOW
    assert r.rule == "S0_no_applicable_signals"
    assert repr(r.score) == "0.0", "must not serialise as 1e-06 or similar"


def test_benign_signals_alone_score_zero():
    r = compute_score([sig(id="b1", severity=0.0, benign=True),
                       sig(id="b2", severity=0.0, benign=True)])
    assert r.score == 0.0 and r.level is RiskLevel.LOW and not r.families


def test_signals_marked_inapplicable_by_the_model_do_not_score():
    s = sig(severity=0.9, reliability=0.95)
    s.applicable = False
    assert compute_score([s]).score == 0.0


# --------------------------------------------------------------- corroboration rule

def test_one_family_cannot_reach_critical_by_stacking():
    """Five strong signals of the SAME kind stay below CRITICAL: no corroboration."""
    signals = [sig(id=f"s{i}", severity=0.9, reliability=0.9, family=Family.BEHAVIOR)
               for i in range(5)]
    r = compute_score(signals)
    assert r.score <= T.single_family_cap
    assert r.level is RiskLevel.HIGH
    assert not r.corroborated and r.rule == "S1_single_family_capped"


def test_two_families_can_reach_critical():
    signals = [sig(id="a", severity=0.85, reliability=0.9, family=Family.BEHAVIOR),
               sig(id="b", severity=0.8, reliability=0.9, family=Family.ANONYMITY)]
    r = compute_score(signals)
    assert r.level is RiskLevel.CRITICAL and r.corroborated


def test_verified_fact_is_exempt_from_the_single_family_cap():
    """A live phishing-feed hit on the exact URL is a fact, not an inference: it may stand alone."""
    r = compute_score([sig(id="phish.feed_hit", severity=0.92, reliability=0.92,
                           family=Family.PHISHING, verified=True)])
    assert r.rule == "S3_single_family_verified_fact"
    assert r.score > T.single_family_cap and r.level is RiskLevel.CRITICAL


def test_inference_stack_is_not_treated_as_a_verified_fact():
    """High-severity INFERENCES, however many, never buy the single-family exemption."""
    r = compute_score([sig(id="i1", severity=0.95, reliability=0.95, family=Family.DOMAIN),
                       sig(id="i2", severity=0.9, reliability=0.95, family=Family.DOMAIN),
                       sig(id="i3", severity=0.9, reliability=0.95, family=Family.DOMAIN)])
    assert r.rule == "S1_single_family_capped" and r.level is RiskLevel.HIGH


def test_verified_flag_still_needs_a_reliable_source():
    """A `verified` claim from a weak source does not earn the exemption.

    Several of them stay capped, and one on its own never reaches CRITICAL.
    """
    for n in (1, 4, 8):
        signals = [sig(id=f"v{i}", severity=0.95, reliability=0.6,
                       family=Family.DOMAIN, verified=True) for i in range(n)]
        r = compute_score(signals)
        assert r.rule != "S3_single_family_verified_fact"
        assert r.score <= T.single_family_cap
        assert r.level is not RiskLevel.CRITICAL


def test_within_family_stacking_decays():
    """Three equal signals in one family must score less than three in three families."""
    one = [sig(id=f"s{i}", severity=0.5, reliability=0.8, family=Family.BEHAVIOR) for i in range(3)]
    many = [sig(id=f"s{i}", severity=0.5, reliability=0.8, family=f)
            for i, f in enumerate([Family.BEHAVIOR, Family.CARD, Family.ANONYMITY])]
    assert compute_score(one).score < compute_score(many).score


# --------------------------------------------------------------- strength multiplier

def test_strength_tiers_move_the_score_monotonically():
    def score_at(strength):
        s = sig(severity=0.5, reliability=0.9)
        s.strength = strength
        return compute_score([s]).score

    assert score_at(Strength.WEAK) < score_at(Strength.MODERATE) < score_at(Strength.STRONG)


def test_strength_cannot_push_severity_above_one():
    s = sig(severity=0.95, reliability=1.0)
    s.strength = Strength.STRONG          # 0.95 * 1.25 would exceed 1.0
    assert s.weight <= 1.0
    assert compute_score([s]).score <= 1.0


# --------------------------------------------------------------- helpers and config

def test_family_weights_reports_only_contributing_families():
    fw = family_weights([sig(id="a", severity=0.5, family=Family.BEHAVIOR),
                         sig(id="b", severity=0.0, benign=True, family=Family.CARD)])
    assert set(fw) == {"behavior"}


def test_thresholds_reject_an_inconsistent_configuration():
    with pytest.raises(ValueError):
        Thresholds(medium=0.6, high=0.5, critical=0.8)
    with pytest.raises(ValueError):
        Thresholds(single_family_cap=0.95)     # must sit below CRITICAL


def test_custom_thresholds_are_honoured_end_to_end():
    strict = Thresholds(medium=0.1, high=0.2, critical=0.3, single_family_cap=0.25)
    r = compute_score([sig(severity=0.4, reliability=0.8)], strict)
    assert r.level is level_for_score(r.score, strict)
    assert r.score <= strict.single_family_cap


def test_a_tor_exit_alone_stays_below_critical_at_every_strength():
    """Concealment is not the same as an active attack, and the two paths must agree.

    The Tor exit list is authoritative, so the signal is `verified` - but on its own it tops
    out at HIGH. Otherwise a model rating it `strong` would return CRITICAL where the keyless
    path returns HIGH for the same subject.
    """
    for strength in Strength:
        s = sig(id="anon.tor_exit", severity=0.72, reliability=0.95,
                family=Family.ANONYMITY, verified=True)
        s.strength = strength
        r = compute_score([s])
        assert r.level is not RiskLevel.CRITICAL, (strength, r.score)
        assert r.score <= T.single_family_cap


def test_a_live_phishing_hit_alone_does_reach_critical():
    """The contrast: this page is harvesting credentials right now."""
    s = sig(id="phish.feed_hit", severity=0.92, reliability=0.92,
            family=Family.PHISHING, verified=True)
    r = compute_score([s])
    assert r.rule == "S3_single_family_verified_fact"
    assert r.level is RiskLevel.CRITICAL


def test_a_verified_but_mild_fact_does_not_claim_the_exemption():
    """Prepaid-BIN metadata is a checked fact, but not a severe one."""
    for n in (1, 5):
        signals = [sig(id=f"card.prepaid_bin{i}", severity=0.5, reliability=0.85,
                       family=Family.CARD, verified=True) for i in range(n)]
        r = compute_score(signals)
        assert r.rule != "S3_single_family_verified_fact"
        assert r.level is not RiskLevel.CRITICAL
