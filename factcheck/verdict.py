"""Stage 5 - deterministic verdict decision.

`decide_verdict` is a pure function of an `Agreement` summary and a `Thresholds` object. No model
is involved, so every verdict can be reproduced, unit-tested and tuned. Rules fire in order; the
first match wins, and its id is logged and surfaced in the reasoning.

Inputs (all weights are already tier x relevance x confidence x freshness weighted, see agreement.py):
  S = support_w, C = caveat_w, X = mixed_w, M = misleading_w, R = refute_w, D = S+C+X+M+R
  s = S + 0.75*C + 0.5*X       (support-side mass; a caveated source mostly supports)
  r = R + 0.25*C + 0.5*X       (refute-side mass)
  share = s / (s + r)

Rules:
  R1  D < min_decisive_weight, or no decisive source           -> Unverified  (abstain)
  R2  only low-trust sources and D < min_weight_low_trust_only -> Unverified  (abstain)
  R2b disagreement only within a single domain (no 2nd domain) -> Unverified  (retrieval noise, not a real dispute)
  R3  M/D >= misleading_share and M >= R                        -> Misleading
  R4  share >= 0.90 and >=2 supporting domains, S+C+X >= 1.0,
      caveat ratio <= 0.30, M/D < 0.15, R/D <= 0.05              -> True
  R5  share >= 0.90 (anything short of R4)                       -> Mostly True
  R6  share >= 0.70 and the two sides are HIGH-tier contested    -> Mixed
  R7  share >= 0.70                                              -> Mostly True
  R8  share >= 0.35                                              -> Mixed
  R9  share >  0.10                                              -> Mostly False
  R10 share <= 0.10 and R >= 0.8 and (>=2 refuting domains, or a HIGH-tier domain with >=2 refuting
      pages)                                                  -> False
  R11 share <= 0.10 otherwise (thin refutation)                  -> Mostly False

`Misleading` (R3) is the "literally true but deceptive" bucket. It fires when evidence says the
claim's words are accurate or partly accurate but framed to deceive, and that is not outweighed by
outright refutation. Examples the stance step labels `misleading`:
  * "Crime fell 10% last month" - true, but sources show it is up year over year and last month was
    unusually high.
  * "Country X has the highest death toll" - true in absolute numbers, but sources show per-capita
    it is among the lowest.
  * An old study/figure presented as current.
A published fact-check rated "misleading"/"missing context" feeds the same bucket.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Agreement, Verdict, VerdictDecision


@dataclass(frozen=True)
class Thresholds:
    min_decisive_weight: float = 0.5
    min_weight_low_trust_only: float = 1.0
    misleading_share: float = 0.35
    true_share: float = 0.90
    mostly_true_share: float = 0.70
    mixed_share: float = 0.35
    false_share: float = 0.10
    true_min_support_w: float = 1.0
    true_min_support_domains: int = 2
    true_max_caveat_ratio: float = 0.30
    true_max_misleading_share: float = 0.15
    true_max_refute_share: float = 0.05     # 'uncontested': at most 5% of decisive weight refutes
    false_min_refute_w: float = 0.8
    false_min_refute_domains: int = 2
    contested_min_minority_share: float = 0.15


DEFAULT_THRESHOLDS = Thresholds()


def support_share(a: Agreement) -> float:
    s = a.support_w + 0.75 * a.caveat_w + 0.5 * a.mixed_w
    r = a.refute_w + 0.25 * a.caveat_w + 0.5 * a.mixed_w
    return s / (s + r) if (s + r) > 0 else 0.0


def decide_verdict(a: Agreement, t: Thresholds = DEFAULT_THRESHOLDS) -> VerdictDecision:
    D = a.decisive_w
    share = support_share(a)

    def out(v: Verdict, rule: str, why: str) -> VerdictDecision:
        return VerdictDecision(verdict=v, rule=rule, explanation=why, support_share=round(share, 3))

    if D < t.min_decisive_weight or a.decisive_domains < 1:
        return out(Verdict.UNVERIFIED, "R1_insufficient_evidence",
                   "Not enough relevant, credible evidence bears directly on this claim.")
    if a.credible_decisive == 0 and D < t.min_weight_low_trust_only:
        return out(Verdict.UNVERIFIED, "R2_only_low_trust_sources",
                   "The only evidence found comes from low-trust sources.")

    # Disagreement confined to ONE domain is retrieval noise, not "sources disagree": abstain.
    s0 = a.support_w + 0.75 * a.caveat_w + 0.5 * a.mixed_w
    r0 = a.refute_w + 0.25 * a.caveat_w + 0.5 * a.mixed_w
    if (a.decisive_domains < 2 and a.mixed_w == 0 and a.misleading_w == 0 and a.refute_w > 0
            and (a.support_w + a.caveat_w) > 0 and t.false_share < support_share(a) < t.mostly_true_share):
        return out(Verdict.UNVERIFIED, "R2b_single_domain_conflict",
                   "The only evidence comes from one domain and points in conflicting directions, so it cannot "
                   "settle the claim.")

    if a.misleading_w / D >= t.misleading_share and a.misleading_w >= a.refute_w:
        return out(Verdict.MISLEADING, "R3_misleading",
                   "Sources indicate the claim is literally accurate or partly accurate but framed in a way that "
                   "leaves a false impression (missing context).")

    s = a.support_w + 0.75 * a.caveat_w + 0.5 * a.mixed_w
    r = a.refute_w + 0.25 * a.caveat_w + 0.5 * a.mixed_w
    minority = min(s, r) / (s + r) if (s + r) > 0 else 0.0
    contested = a.support_high and a.refute_high and minority >= t.contested_min_minority_share

    if share >= t.true_share:
        pos = a.support_w + a.caveat_w + a.mixed_w
        caveat_ratio = (a.caveat_w + a.mixed_w) / pos if pos else 0.0
        if (
            a.support_domains >= t.true_min_support_domains
            and pos >= t.true_min_support_w
            and caveat_ratio <= t.true_max_caveat_ratio
            and a.misleading_w / D < t.true_max_misleading_share
            and a.refute_w / D <= t.true_max_refute_share
        ):
            return out(Verdict.TRUE, "R4_true",
                       "Multiple independent sources support the claim and none credibly contest it.")
        return out(Verdict.MOSTLY_TRUE, "R5_mostly_true_thin_or_qualified",
                   "The evidence supports the claim, but it is thin (few independent sources) or carries a "
                   "qualification.")
    if share >= t.mostly_true_share:
        if contested:
            return out(Verdict.MIXED, "R6_contested_by_high_tier",
                       "Reliable sources disagree: some support the claim while others contradict it.")
        return out(Verdict.MOSTLY_TRUE, "R7_mostly_true",
                   "Most evidence supports the claim, with some contrary or qualifying evidence.")
    if share >= t.mixed_share:
        return out(Verdict.MIXED, "R8_mixed", "Support and refutation are roughly balanced.")
    if share > t.false_share:
        return out(Verdict.MOSTLY_FALSE, "R9_mostly_false",
                   "Most evidence contradicts the claim, though part of it is supported.")
    if a.refute_w >= t.false_min_refute_w and (
        a.refute_domains >= t.false_min_refute_domains or (a.refute_high and len(a.refuting) >= 2)
    ):
        return out(Verdict.FALSE, "R10_false", "Credible sources clearly and strongly contradict the claim.")
    return out(Verdict.MOSTLY_FALSE, "R11_mostly_false_thin_refutation",
               "The available evidence contradicts the claim, but the refutation rests on limited sourcing.")


def decide(a: Agreement, t: Thresholds = DEFAULT_THRESHOLDS) -> VerdictDecision:
    """decide_verdict plus the evidence-quality cap.

    When every decisive stance label came from the keyword heuristic (no model labels, no published
    fact-check rating), the labels are not reliable enough for the two strongest verdicts: the
    heuristic cannot always tell "the source states X" from "the source reports that someone claims
    X". Such cases are capped at Mostly True / Mostly False.
    """
    d = decide_verdict(a, t)
    if a.heuristic_only and d.verdict in (Verdict.TRUE, Verdict.FALSE):
        softened = Verdict.MOSTLY_TRUE if d.verdict == Verdict.TRUE else Verdict.MOSTLY_FALSE
        return VerdictDecision(
            verdict=softened,
            rule=d.rule + "+heuristic_cap",
            explanation=d.explanation.replace(
                "Credible sources clearly and strongly contradict the claim.",
                "Credible sources contradict the claim.",
            ).replace(
                "Multiple independent sources support the claim and none credibly contest it.",
                "Multiple independent sources support the claim and none contest it.",
            ) + " Sources were matched on wording rather than read in full, so this stops one step "
                "short of the strongest verdict.",
            support_share=d.support_share,
        )
    return d
