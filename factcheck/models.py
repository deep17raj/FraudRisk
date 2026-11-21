"""Shared data types for the fact-checking pipeline.

Kept dependency-free (plain dataclasses/enums) so every stage can be unit-tested in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Optional


class Verdict(str, Enum):
    """The exact, case-sensitive enum Telegraph's FACT_CHECK_GENERAL intent accepts."""

    TRUE = "True"
    MOSTLY_TRUE = "Mostly True"
    MIXED = "Mixed"
    MISLEADING = "Misleading"
    MOSTLY_FALSE = "Mostly False"
    FALSE = "False"
    UNVERIFIED = "Unverified"


class Stance(str, Enum):
    """How one piece of evidence relates to the claim."""

    SUPPORTS = "supports"          # confirms the claim as stated
    CAVEATED = "caveated"          # confirms the core but with an imprecision / qualification
    MIXED = "mixed"                # evidence itself says "half true" / partly right
    MISLEADING = "misleading"      # literally accurate (or partly) but missing context / deceptive framing
    REFUTES = "refutes"            # contradicts the claim
    NEUTRAL = "neutral"            # related but does not bear on truth of the claim


class Tier(IntEnum):
    REJECTED = 0   # known misinformation / satire: dropped entirely
    LOW = 1        # unknown or user-generated domains
    MEDIUM = 2     # established mainstream media / reputable publishers
    HIGH = 3       # gov / edu / wire services / fact-checkers / reference works


@dataclass
class Evidence:
    """A single retrieved snippet. `id` is assigned by the retrieval stage (1-based)."""

    url: str
    title: str
    snippet: str
    provider: str                       # which Source produced it, e.g. "wikipedia"
    published: Optional[str] = None     # ISO-ish date string if the provider gave one
    id: int = 0
    # Set by sources that already carry a stance (e.g. Google Fact Check textualRating).
    preset_stance: Optional[Stance] = None
    preset_confidence: float = 0.0
    fact_check_rating: Optional[str] = None


@dataclass
class ParsedClaim:
    original: str
    text: str                           # cleaned, checkable assertion
    checkable: bool
    reject_reason: str = ""             # human-readable reason when checkable is False
    entities: list[str] = field(default_factory=list)
    numbers: list[str] = field(default_factory=list)
    years: list[int] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    negated: bool = False
    time_sensitive: bool = False
    search_queries: list[str] = field(default_factory=list)


@dataclass
class ScoredEvidence:
    evidence: Evidence
    domain: str
    tier: Tier
    relevance: float = 0.0              # 0..1: how directly the snippet addresses the claim
    stance: Stance = Stance.NEUTRAL
    stance_confidence: float = 0.0      # 0..1
    note: str = ""                      # short reason for the stance (used in logs)
    weight: float = 0.0                 # final decisive weight, filled by agreement analysis
    labeller: str = "heuristic"         # "heuristic" | "llm" | "source" (a published fact-check rating)

    @property
    def id(self) -> int:
        return self.evidence.id


@dataclass
class Agreement:
    """Output of cross-source agreement analysis; the sole input to decide_verdict()."""

    support_w: float = 0.0
    caveat_w: float = 0.0
    mixed_w: float = 0.0
    misleading_w: float = 0.0
    refute_w: float = 0.0
    decisive_domains: int = 0           # distinct domains with a non-neutral, relevant stance
    credible_decisive: int = 0          # decisive items from tier >= MEDIUM
    support_domains: int = 0
    refute_domains: int = 0
    support_high: bool = False          # a HIGH-tier source supports
    refute_high: bool = False           # a HIGH-tier source refutes
    related_count: int = 0              # relevant-but-neutral items
    level: str = "insufficient"         # consensus_support | consensus_refute | contradiction | partial | insufficient
    #: True when NO decisive item was labelled by a model or by a published fact-check rating, i.e. the
    #: stance labels are all keyword heuristics. Such evidence cannot reach the strongest verdicts.
    heuristic_only: bool = True
    supporting: list[ScoredEvidence] = field(default_factory=list)
    refuting: list[ScoredEvidence] = field(default_factory=list)
    qualifying: list[ScoredEvidence] = field(default_factory=list)   # caveated / mixed / misleading
    neutral: list[ScoredEvidence] = field(default_factory=list)

    @property
    def decisive_w(self) -> float:
        return self.support_w + self.caveat_w + self.mixed_w + self.misleading_w + self.refute_w


@dataclass
class VerdictDecision:
    verdict: Verdict
    rule: str                           # stable id of the rule that fired, e.g. "R6_mostly_true"
    explanation: str                    # one-line, human-readable
    support_share: float = 0.0


@dataclass
class RetrievalResult:
    evidence: list[Evidence]
    provider_status: dict[str, str]     # name -> ok | empty | timeout | error | circuit_open | skipped
    elapsed_ms: int = 0

    @property
    def degraded(self) -> bool:
        """True when we cannot trust 'no evidence' to mean 'nothing exists' (provider failures)."""
        bad = {"timeout", "error", "circuit_open"}
        return any(s in bad for s in self.provider_status.values())

    @property
    def all_failed(self) -> bool:
        ran = [s for s in self.provider_status.values() if s != "skipped"]
        return bool(ran) and all(s in {"timeout", "error", "circuit_open"} for s in ran)


@dataclass
class CheckResult:
    """What the pipeline hands back to the HTTP layer."""

    claim: str
    verdict: Verdict
    reasoning: str
    sources: list[str]
    cacheable: bool = True
    cache_ttl: Optional[int] = None
    debug: dict = field(default_factory=dict)
