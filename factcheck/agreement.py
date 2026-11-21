"""Stage 4 - cross-source agreement analysis.

Turns per-snippet stance labels into the weighted `Agreement` summary consumed by the verdict
function. Rules that keep the numbers honest:
  * weight = tier weight x relevance x stance confidence x freshness
  * one domain can't dominate: each (domain, stance-bucket) is capped at that tier's weight, so
    five Wikipedia articles do not count as five independent sources
  * neutral / irrelevant snippets never count as support or refutation
"""
from __future__ import annotations

import datetime as _dt
import re
from collections import defaultdict

from .credibility import TIER_WEIGHT
from .models import Agreement, Evidence, ParsedClaim, ScoredEvidence, Stance, Tier
from .stance import MIN_RELEVANCE

MIN_CONFIDENCE = 0.2
STALE_DAYS = 60

_BUCKET = {
    Stance.SUPPORTS: "support",
    Stance.CAVEATED: "caveat",
    Stance.MIXED: "mixed",
    Stance.MISLEADING: "misleading",
    Stance.REFUTES: "refute",
}

_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _parse_date(s: str | None) -> _dt.date | None:
    if not s:
        return None
    m = _DATE_RE.search(s)
    if m:
        try:
            return _dt.date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    m = re.search(r"(\d+)\s+(hour|day|week|month|year)s?\s+ago", s, re.I)
    if m:
        n, unit = int(m[1]), m[2].lower()
        days = {"hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}[unit] * n
        return _dt.date.today() - _dt.timedelta(days=days)
    return None


def freshness(ev: Evidence, claim: ParsedClaim, today: _dt.date | None = None) -> float:
    """1.0 for atemporal claims. For time-sensitive claims undated/old evidence counts for less."""
    if not claim.time_sensitive:
        return 1.0
    today = today or _dt.date.today()
    d = _parse_date(ev.published)
    if d is None:
        return 0.6
    return 1.0 if (today - d).days <= STALE_DAYS else 0.4


def analyze(claim: ParsedClaim, scored: list[ScoredEvidence], *, today: _dt.date | None = None) -> Agreement:
    a = Agreement()
    # (domain, bucket) -> list of items; cap the sum per group
    groups: dict[tuple[str, str], list[ScoredEvidence]] = defaultdict(list)
    for it in scored:
        it.weight = 0.0
        decisive = (
            it.stance != Stance.NEUTRAL
            and it.relevance >= MIN_RELEVANCE
            and it.stance_confidence >= MIN_CONFIDENCE
            and it.tier != Tier.REJECTED
        )
        if not decisive:
            if it.relevance >= MIN_RELEVANCE and it.tier != Tier.REJECTED:
                a.neutral.append(it)
                a.related_count += 1
            continue
        it.weight = (
            TIER_WEIGHT[it.tier] * it.relevance * it.stance_confidence * freshness(it.evidence, claim, today)
        )
        groups[(it.domain, _BUCKET[it.stance])].append(it)

    totals = defaultdict(float)
    decisive_domains: set[str] = set()
    credible_domains: set[str] = set()
    support_domains: set[str] = set()
    refute_domains: set[str] = set()

    for (domain, bucket), items in groups.items():
        cap = TIER_WEIGHT[items[0].tier]
        w = min(sum(i.weight for i in items), cap)
        totals[bucket] += w
        decisive_domains.add(domain)
        if items[0].tier >= Tier.MEDIUM:
            credible_domains.add(domain)
        if bucket in ("support", "caveat"):
            support_domains.add(domain)
            if items[0].tier == Tier.HIGH:
                a.support_high = True
        if bucket == "refute":
            refute_domains.add(domain)
            if items[0].tier == Tier.HIGH:
                a.refute_high = True
        for i in items:
            if bucket in ("support",):
                a.supporting.append(i)
            elif bucket == "refute":
                a.refuting.append(i)
            else:
                a.qualifying.append(i)

    a.heuristic_only = not any(
        i.labeller in ("llm", "source") for i in (a.supporting + a.refuting + a.qualifying)
    )
    a.support_w = totals["support"]
    a.caveat_w = totals["caveat"]
    a.mixed_w = totals["mixed"]
    a.misleading_w = totals["misleading"]
    a.refute_w = totals["refute"]
    a.decisive_domains = len(decisive_domains)
    a.credible_decisive = len(credible_domains)
    a.support_domains = len(support_domains)
    a.refute_domains = len(refute_domains)
    for lst in (a.supporting, a.refuting, a.qualifying):
        lst.sort(key=lambda i: -i.weight)

    s = a.support_w + a.caveat_w + 0.5 * a.mixed_w
    r = a.refute_w + 0.5 * a.mixed_w
    if a.decisive_w < 0.5:
        a.level = "insufficient"
    elif r == 0 and a.misleading_w == 0:
        a.level = "consensus_support"
    elif s == 0 and a.misleading_w == 0:
        a.level = "consensus_refute"
    elif s + r > 0 and min(s, r) / (s + r) >= 0.3:
        a.level = "contradiction"
    else:
        a.level = "partial"
    return a
