"""Stage 4 - deterministic score mapping.

`compute_score` is a PURE function of a signal list and a `Thresholds` object. No model is
involved, so every score is reproducible, unit-testable and tunable. Identical signal sets
always yield an identical `risk_score`, and `risk_level` is derived from the *rounded* score
by the single function `level_for_score`, so the two fields can never disagree at a boundary.

The maths
---------
Each signal carries `severity` (how damning the fact is) and `reliability` (how much the
source that produced it is trusted); `Signal.weight` is their product after the reasoning
layer's strength multiplier, clamped to [0, 1].

1. Signals are grouped by `Family`. Within one family the weights are combined with a
   decaying noisy-OR: the strongest counts fully, the next at 45%, then 20%, then 10%.
   That caps stacking inside a family, so three phrasings of a single underlying fact
   (or one chatty provider) cannot climb the scale on their own.

       family_w = 1 - prod(1 - w_k * decay_k)      for w_k sorted descending

2. Families are then combined with a plain noisy-OR, which is monotone, bounded in [0, 1)
   and order-independent:

       raw = 1 - prod(1 - family_w)

3. Corroboration rule: if only ONE family contributed any weight, the result is capped at
   `single_family_cap` (0.78). A lone family can therefore reach HIGH but never CRITICAL -
   CRITICAL is reserved for subjects where independent kinds of evidence agree. This is the
   deliberate difference from a single-source lookup, which cannot corroborate anything.

   One narrow exemption: a signal the source marked `verified` - a directly checked fact
   about this exact entity - escapes the cap when it is ALSO at least `verified_min_severity`
   severe and from a source of at least `verified_reliability`. In practice that is a live
   phishing-feed hit on the exact URL (severity 0.92). Membership of the Tor exit list is
   equally verified but less severe (0.72), so it stays capped at HIGH - which also keeps it
   consistent with the keyless path, where no model is present to rate its strength.
   Inference stacking can never reach CRITICAL alone, however confident it is.

4. `score = round(raw, 6)` and `level = level_for_score(score)`.

Bands (fixed and documented; `risk_score` is a fraction in [0, 1])
------------------------------------------------------------------
    score < 0.25            -> LOW
    0.25 <= score < 0.50    -> MEDIUM
    0.50 <= score < 0.80    -> HIGH
    score >= 0.80           -> CRITICAL

Abstention uses exactly 0.0 (not a tiny float, which would serialise as 1e-06).
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import RiskLevel, Score, Signal

#: Weight multiplier for the k-th strongest signal within one family (index >= 3 uses the last).
WITHIN_FAMILY_DECAY: tuple[float, ...] = (1.0, 0.45, 0.20, 0.10)

SCORE_DECIMALS = 6


@dataclass(frozen=True)
class Thresholds:
    medium: float = 0.25
    high: float = 0.50
    critical: float = 0.80
    #: Hard ceiling when a single family is the only contributor (no corroboration).
    single_family_cap: float = 0.78
    #: A family needs at least this much weight to count as a contributor at all.
    min_family_weight: float = 0.01
    #: A `verified` signal claims the single-family exemption only if it is BOTH this severe
    #: and this reliable. The severity floor is what separates a live phishing-feed hit on
    #: the exact URL (0.92 - the page is harvesting credentials right now) from membership of
    #: the Tor exit list (0.72 - concealment, and plenty of legitimate privacy users). The
    #: latter stays capped at HIGH, which also keeps it consistent with the keyless path.
    verified_min_severity: float = 0.85
    verified_reliability: float = 0.90

    def __post_init__(self) -> None:
        if not 0.0 < self.medium < self.high < self.critical <= 1.0:
            raise ValueError("thresholds must satisfy 0 < medium < high < critical <= 1")
        if not self.medium <= self.single_family_cap < self.critical:
            raise ValueError("single_family_cap must sit below the CRITICAL threshold")


DEFAULT_THRESHOLDS = Thresholds()


def level_for_score(score: float, t: Thresholds = DEFAULT_THRESHOLDS) -> RiskLevel:
    """The ONLY place a level is derived from a score. Call with the already-rounded score."""
    if score >= t.critical:
        return RiskLevel.CRITICAL
    if score >= t.high:
        return RiskLevel.HIGH
    if score >= t.medium:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def _family_weight(weights: list[float]) -> float:
    """Decaying noisy-OR within one family: strongest counts fully, the rest taper off."""
    product = 1.0
    for i, w in enumerate(sorted(weights, reverse=True)):
        decay = WITHIN_FAMILY_DECAY[min(i, len(WITHIN_FAMILY_DECAY) - 1)]
        product *= 1.0 - max(0.0, min(1.0, w)) * decay
    return 1.0 - product


def family_weights(signals: list[Signal], t: Thresholds = DEFAULT_THRESHOLDS) -> dict[str, float]:
    """Per-family fused weight, for logs and for explaining a score. Pure."""
    grouped: dict[str, list[float]] = {}
    for s in signals:
        w = s.weight
        if w > 0.0:
            grouped.setdefault(s.family.value, []).append(w)
    out = {}
    for fam, ws in grouped.items():
        fw = _family_weight(ws)
        if fw >= t.min_family_weight:
            out[fam] = fw
    return out


def _has_verified_fact(signals: list[Signal], t: Thresholds) -> bool:
    """True when a contributing signal is a directly checked fact from a trusted source.

    `verified` is set by the source that looked the entity up, never inferred from how large
    a severity happens to be - otherwise a stack of confident *inferences* would buy itself
    the exemption that is meant only for checked facts.
    """
    return any(
        s.verified
        and s.weight > 0.0
        and s.severity >= t.verified_min_severity
        and s.reliability >= t.verified_reliability
        for s in signals
    )


def compute_score(signals: list[Signal], t: Thresholds = DEFAULT_THRESHOLDS) -> Score:
    """Fuse the applicable signals into a score in [0, 1] and its consistent risk level.

    Signals that are benign, marked not-applicable by the reasoning layer, or of zero weight
    contribute nothing. With no contributors at all the result is exactly 0.0 / LOW, which is
    the honest-abstention and the checked-and-clean answer alike (the pipeline words them
    differently - see `pipeline._low_summary`).
    """
    fams = family_weights(signals, t)

    if not fams:
        return Score(score=0.0, level=RiskLevel.LOW, rule="S0_no_applicable_signals",
                     families=[], corroborated=False)

    product = 1.0
    for fw in fams.values():
        product *= 1.0 - fw
    raw = 1.0 - product

    corroborated = len(fams) >= 2
    rule = "S2_fused_multi_family"
    if not corroborated:
        if _has_verified_fact(signals, t):
            rule = "S3_single_family_verified_fact"
        else:
            rule = "S1_single_family_capped" if raw > t.single_family_cap else "S1_single_family"
            raw = min(raw, t.single_family_cap)

    score = round(max(0.0, min(1.0, raw)), SCORE_DECIMALS)
    return Score(
        score=score,
        level=level_for_score(score, t),
        rule=rule,
        families=sorted(fams),
        corroborated=corroborated,
    )
