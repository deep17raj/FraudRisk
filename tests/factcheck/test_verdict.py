"""Unit tests for decide_verdict(): every one of the 7 enum values, rule boundaries and invariants."""
import itertools

import pytest

from factcheck.models import Verdict
from factcheck.verdict import DEFAULT_THRESHOLDS as T, Thresholds, decide_verdict, support_share
from tests.factcheck.conftest import agreement


# ---------------------------------------------------------------- one test per enum value
def test_true_multi_source_uncontested_support():
    a = agreement(support_w=1.8, support_domains=2, decisive_domains=2, support_high=True)
    d = decide_verdict(a)
    assert d.verdict == Verdict.TRUE and d.rule == "R4_true"


def test_mostly_true_single_source_support():
    a = agreement(support_w=1.0, support_domains=1, decisive_domains=1, support_high=True)
    d = decide_verdict(a)
    assert d.verdict == Verdict.MOSTLY_TRUE and d.rule == "R5_mostly_true_thin_or_qualified"


def test_mostly_true_with_caveat_from_two_sources():
    # right direction but a qualified/imprecise figure: caveat_w is 50% of positive mass
    a = agreement(support_w=0.7, caveat_w=0.7, support_domains=2, decisive_domains=2)
    d = decide_verdict(a)
    assert d.verdict == Verdict.MOSTLY_TRUE


def test_mostly_true_with_minor_contrary_evidence():
    # share = 1.5 / (1.5 + 0.5) = 0.75
    a = agreement(support_w=1.5, refute_w=0.5, support_domains=2, refute_domains=1, decisive_domains=3)
    d = decide_verdict(a)
    assert d.verdict == Verdict.MOSTLY_TRUE and d.rule == "R7_mostly_true"


def test_mixed_balanced_support_and_refutation():
    a = agreement(support_w=1.0, refute_w=1.0, support_domains=1, refute_domains=1, decisive_domains=2)
    d = decide_verdict(a)
    assert d.verdict == Verdict.MIXED and d.rule == "R8_mixed"


def test_mixed_when_high_tier_sources_contest_even_if_lopsided():
    # share 0.8 would be Mostly True, but a HIGH-tier source on each side => contested
    a = agreement(support_w=1.6, refute_w=0.4, support_domains=2, refute_domains=1, decisive_domains=3,
                  support_high=True, refute_high=True)
    d = decide_verdict(a)
    assert d.verdict == Verdict.MIXED and d.rule == "R6_contested_by_high_tier"


def test_misleading_true_facts_deceptive_framing():
    # sources confirm the literal numbers (support 0.6) but say the framing omits context (0.9)
    a = agreement(support_w=0.6, misleading_w=0.9, refute_w=0.1, support_domains=1, decisive_domains=2)
    d = decide_verdict(a)
    assert d.verdict == Verdict.MISLEADING and d.rule == "R3_misleading"


def test_misleading_beats_true_even_with_two_supporting_sources():
    a = agreement(support_w=1.4, misleading_w=0.8, support_domains=2, decisive_domains=3)
    assert decide_verdict(a).verdict == Verdict.MISLEADING


def test_mostly_false_mostly_refuted_small_true_element():
    # share = 0.3 / (0.3 + 1.0) ~= 0.23
    a = agreement(support_w=0.3, refute_w=1.0, support_domains=1, refute_domains=1, decisive_domains=2)
    d = decide_verdict(a)
    assert d.verdict == Verdict.MOSTLY_FALSE and d.rule == "R9_mostly_false"


def test_false_clear_strong_refutation():
    a = agreement(refute_w=1.8, refute_domains=2, decisive_domains=2, refute_high=True)
    d = decide_verdict(a)
    assert d.verdict == Verdict.FALSE and d.rule == "R10_false"


def test_false_from_one_high_tier_domain_needs_multiple_refuting_pages():
    from tests.factcheck.conftest import scored
    from factcheck.models import Stance
    a = agreement(refute_w=1.0, refute_domains=1, decisive_domains=1, refute_high=True)
    assert decide_verdict(a).verdict == Verdict.MOSTLY_FALSE            # a single page is not "clearly"
    a.refuting = [scored(Stance.REFUTES, id=1), scored(Stance.REFUTES, id=2)]
    assert decide_verdict(a).verdict == Verdict.FALSE


def test_mostly_false_when_refutation_is_thin():
    a = agreement(refute_w=0.6, refute_domains=1, decisive_domains=1)
    d = decide_verdict(a)
    assert d.verdict == Verdict.MOSTLY_FALSE and d.rule == "R11_mostly_false_thin_refutation"


def test_unverified_no_evidence():
    d = decide_verdict(agreement())
    assert d.verdict == Verdict.UNVERIFIED and d.rule == "R1_insufficient_evidence"


def test_unverified_evidence_too_thin():
    a = agreement(support_w=0.3, support_domains=1, decisive_domains=1)
    assert decide_verdict(a).verdict == Verdict.UNVERIFIED


def test_unverified_only_low_trust_sources():
    a = agreement(support_w=0.9, support_domains=1, decisive_domains=1, credible_decisive=0)
    d = decide_verdict(a)
    assert d.verdict == Verdict.UNVERIFIED and d.rule == "R2_only_low_trust_sources"


def test_low_trust_sources_suffice_only_with_enough_weight():
    a = agreement(support_w=1.2, support_domains=3, decisive_domains=3, credible_decisive=0)
    assert decide_verdict(a).verdict != Verdict.UNVERIFIED


# ---------------------------------------------------------------- boundaries & invariants
def test_true_requires_two_independent_domains():
    one = agreement(support_w=3.0, support_domains=1, decisive_domains=1)
    two = agreement(support_w=3.0, support_domains=2, decisive_domains=2)
    assert decide_verdict(one).verdict == Verdict.MOSTLY_TRUE
    assert decide_verdict(two).verdict == Verdict.TRUE


def test_true_requires_minimum_support_weight():
    a = agreement(support_w=0.8, support_domains=2, decisive_domains=2)
    assert decide_verdict(a).verdict == Verdict.MOSTLY_TRUE


def test_share_thresholds_are_respected_exactly():
    # support 9 vs refute 1 -> share 0.9 but 10% contrary weight is not 'uncontested' => Mostly True
    assert decide_verdict(agreement(support_w=9.0, refute_w=1.0, support_domains=2, refute_domains=1,
                                    decisive_domains=3)).verdict == Verdict.MOSTLY_TRUE
    assert decide_verdict(agreement(support_w=7.0, refute_w=3.0, support_domains=2, refute_domains=1,
                                    decisive_domains=3)).verdict == Verdict.MOSTLY_TRUE  # share 0.7
    assert decide_verdict(agreement(support_w=6.9, refute_w=3.1, support_domains=2, refute_domains=1,
                                    decisive_domains=3)).verdict == Verdict.MIXED        # share 0.69
    assert decide_verdict(agreement(support_w=3.5, refute_w=6.5, support_domains=2, refute_domains=2,
                                    decisive_domains=4)).verdict == Verdict.MIXED        # share 0.35
    assert decide_verdict(agreement(support_w=3.4, refute_w=6.6, support_domains=2, refute_domains=2,
                                    decisive_domains=4)).verdict == Verdict.MOSTLY_FALSE


def test_verdict_is_always_a_valid_enum_over_a_grid():
    vals = [0.0, 0.3, 0.8, 1.5]
    for s, c, x, m, r in itertools.product(vals, repeat=5):
        a = agreement(support_w=s, caveat_w=c, mixed_w=x, misleading_w=m, refute_w=r,
                      support_domains=1 if s + c else 0, refute_domains=1 if r else 0)
        assert isinstance(decide_verdict(a).verdict, Verdict)


def test_monotonic_more_refutation_never_improves_verdict():
    """Adding refuting weight must never move the verdict toward True."""
    order = [Verdict.FALSE, Verdict.MOSTLY_FALSE, Verdict.MIXED, Verdict.MOSTLY_TRUE, Verdict.TRUE]
    rank = {v: i for i, v in enumerate(order)}
    for s in (0.6, 1.0, 2.0, 3.0):
        prev = None
        for r in (0.0, 0.3, 0.8, 1.5, 3.0, 6.0):
            a = agreement(support_w=s, refute_w=r, support_domains=2, refute_domains=1 if r else 0, decisive_domains=3)
            v = decide_verdict(a).verdict
            if v not in rank:
                continue
            if prev is not None:
                assert rank[v] <= rank[prev], (s, r, prev, v)
            prev = v


def test_custom_thresholds_change_outcomes():
    a = agreement(support_w=1.0, support_domains=1, decisive_domains=1)
    strict = Thresholds(min_decisive_weight=1.5)
    assert decide_verdict(a).verdict == Verdict.MOSTLY_TRUE
    assert decide_verdict(a, strict).verdict == Verdict.UNVERIFIED


def test_support_share_edge_cases():
    assert support_share(agreement()) == 0.0
    assert support_share(agreement(support_w=1.0)) == 1.0
    assert support_share(agreement(refute_w=1.0)) == 0.0


def test_true_requires_uncontested_support():
    clean = agreement(support_w=9.0, refute_w=0.4, support_domains=2, refute_domains=1, decisive_domains=3)
    assert decide_verdict(clean).verdict == Verdict.TRUE            # 4% contrary weight
    contested = agreement(support_w=9.0, refute_w=0.6, support_domains=2, refute_domains=1, decisive_domains=3)
    assert decide_verdict(contested).verdict == Verdict.MOSTLY_TRUE  # ~6%


# ---------------------------------------------------------------- R2b: single-domain conflict
def test_single_domain_conflict_abstains():
    """Two pages of ONE domain pointing opposite ways is retrieval noise, not a real dispute."""
    a = agreement(support_w=0.7, refute_w=0.6, support_domains=1, refute_domains=1, decisive_domains=1)
    d = decide_verdict(a)
    assert d.verdict == Verdict.UNVERIFIED and d.rule == "R2b_single_domain_conflict"


def test_two_domains_in_conflict_is_mixed_not_abstention():
    a = agreement(support_w=0.7, refute_w=0.6, support_domains=1, refute_domains=1, decisive_domains=2)
    assert decide_verdict(a).verdict == Verdict.MIXED


def test_single_domain_without_conflict_still_decides():
    a = agreement(support_w=1.0, support_domains=1, decisive_domains=1)
    assert decide_verdict(a).verdict == Verdict.MOSTLY_TRUE
    b = agreement(refute_w=1.0, refute_domains=1, decisive_domains=1)
    assert decide_verdict(b).verdict == Verdict.MOSTLY_FALSE


def test_single_domain_lopsided_conflict_is_not_abstained():
    # share 0.95: a near-consensus inside one domain is still informative
    a = agreement(support_w=1.9, refute_w=0.1, support_domains=1, refute_domains=1, decisive_domains=1)
    assert decide_verdict(a).verdict != Verdict.UNVERIFIED


# ---------------------------------------------------------------- heuristic-only evidence cap
def test_heuristic_only_evidence_cannot_reach_true_or_false():
    from factcheck.verdict import decide
    t = agreement(support_w=1.8, support_domains=2, decisive_domains=2, support_high=True)
    f = agreement(refute_w=1.8, refute_domains=2, decisive_domains=2, refute_high=True)
    for a in (t, f):
        a.heuristic_only = True
    assert decide_verdict(t).verdict == Verdict.TRUE                      # raw rule
    assert decide(t).verdict == Verdict.MOSTLY_TRUE                       # capped
    assert decide(f).verdict == Verdict.MOSTLY_FALSE
    assert decide(t).rule == "R4_true+heuristic_cap"
    # the explanation must say WHY the strongest verdict was withheld, and must not contradict itself
    assert "matched on wording" in decide(t).explanation
    assert "clearly and strongly" not in decide(f).explanation


def test_model_or_source_labelled_evidence_is_not_capped():
    from factcheck.verdict import decide
    a = agreement(support_w=1.8, support_domains=2, decisive_domains=2, support_high=True)
    a.heuristic_only = False
    assert decide(a).verdict == Verdict.TRUE and decide(a).rule == "R4_true"


def test_cap_leaves_middle_verdicts_untouched():
    from factcheck.verdict import decide
    for kw in ({"support_w": 1.0, "refute_w": 1.0, "support_domains": 1, "refute_domains": 1, "decisive_domains": 2},
               {"support_w": 1.5, "refute_w": 0.5, "support_domains": 2, "refute_domains": 1, "decisive_domains": 3},
               {"support_w": 0.6, "misleading_w": 0.9, "support_domains": 1, "decisive_domains": 2}):
        a = agreement(**kw)
        a.heuristic_only = True
        assert decide(a).verdict == decide_verdict(a).verdict
