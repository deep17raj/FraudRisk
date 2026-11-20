"""Shared data types for the fraud-risk pipeline.

Dependency-free (plain dataclasses/enums) so every stage can be unit-tested in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class RiskLevel(str, Enum):
    """The exact, uppercase enum Telegraph's FRAUD_RISK_GENERAL intent accepts."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class EntityKind(str, Enum):
    """What an extracted entity is. Sources subscribe to these kinds."""

    IP = "ip"
    DOMAIN = "domain"
    URL = "url"
    EMAIL = "email"
    CARD_BIN = "card_bin"
    WALLET = "wallet"
    SCENARIO = "scenario"          # free-text described behaviour
    UNKNOWN = "unknown"


class Family(str, Enum):
    """Signal provenance group.

    Fusion caps stacking WITHIN a family, so three phrasings of one underlying fact cannot
    inflate the score; corroboration ACROSS families is what pushes a subject into the top band.
    """

    IP_REPUTATION = "ip_reputation"
    ANONYMITY = "anonymity"          # Tor / VPN / proxy / hosting range
    PHISHING = "phishing"
    DOMAIN = "domain"
    CARD = "card"
    BEHAVIOR = "behavior"            # rule library over a described scenario
    IDENTITY = "identity"            # email / wallet hygiene


class Strength(str, Enum):
    """How strongly a signal applies IN CONTEXT. The LLM may only pick from this closed set."""

    WEAK = "weak"
    MODERATE = "moderate"
    STRONG = "strong"


#: Multiplier applied to a signal severity for each strength tier. Bounded, so an LLM
#: annotation can temper or sharpen a signal but never invent magnitude beyond its source.
STRENGTH_MULTIPLIER: dict[Strength, float] = {
    Strength.WEAK: 0.6,
    Strength.MODERATE: 1.0,
    Strength.STRONG: 1.25,
}


@dataclass
class Signal:
    """One normalized piece of evidence from one source.

    `id` is stable and source-assigned (e.g. anon.tor_exit); the reasoning layer may only
    refer to signals by these ids, which is what stops it inventing evidence.
    """

    id: str
    text: str                        # specific, evidence-naming, safe to show a caller
    severity: float                  # 0..1 how damning this fact is
    reliability: float               # 0..1 how much we trust the source that produced it
    source: str                      # provider name, e.g. ipwho
    family: Family = Family.BEHAVIOR
    #: A few words naming this signal, for composing a one-sentence summary. Falls back to
    #: the first clause of `text` when a source does not set one.
    label: str = ""
    #: benign observations (account 4 years old) carry severity 0: reported, never scored.
    benign: bool = False
    #: True only for a DIRECTLY CHECKED fact about this exact entity - membership of a
    #: published feed, or an explicit vendor flag - as opposed to an inference from shape,
    #: wording or network ownership. Set by the source, never by the model. The scorer uses
    #: it for one narrow purpose: such a fact may reach CRITICAL without corroboration,
    #: because there is nothing speculative left to corroborate.
    verified: bool = False
    #: set by the reasoning layer
    applicable: bool = True
    strength: Strength = Strength.MODERATE
    note: str = ""                   # short contextual annotation (logged, not returned)

    @property
    def short(self) -> str:
        """A compact phrase for this signal, for use inside a one-sentence summary."""
        if self.label:
            return self.label
        first = self.text.split(" - ")[0].split(", which")[0].split("; ")[0].rstrip(".")
        if len(first) > 90:
            first = first[:87].rsplit(" ", 1)[0] + "..."
        return first[0].lower() + first[1:] if first else ""

    @property
    def weight(self) -> float:
        """Effective risk weight: severity x reliability, after the strength multiplier."""
        if not self.applicable or self.benign:
            return 0.0
        sev = min(1.0, self.severity * STRENGTH_MULTIPLIER[self.strength])
        return max(0.0, min(1.0, sev * self.reliability))


@dataclass
class Entity:
    """A concrete thing found in the subject string, routed to the sources that handle it."""

    kind: EntityKind
    value: str
    raw: str = ""


@dataclass
class Hints:
    """Structured cues extracted from a described scenario.

    Every tri-state field is True when stated, False when explicitly denied, and None when
    not mentioned. Absence is NOT a negative: the rule library checks explicitly, which is
    what keeps "not a new payee" from scoring like "new payee".
    """

    amounts: list[float] = field(default_factory=list)
    currency: str = ""
    account_age_days: Optional[float] = None
    #: "a brand new account" with no number stated: newness without inventing a precise age.
    new_account: Optional[bool] = None
    new_payee: Optional[bool] = None
    first_transfer: Optional[bool] = None
    velocity_count: Optional[int] = None      # e.g. 12 attempts
    small_amounts: bool = False               # many low-value attempts
    geo_mismatch: Optional[bool] = None
    anonymiser_claimed: Optional[bool] = None  # VPN / Tor / proxy mentioned in the text
    device_change: Optional[bool] = None
    credential_reset: Optional[bool] = None
    returning_customer: Optional[bool] = None
    chargeback_history: Optional[bool] = None
    urgency_pressure: Optional[bool] = None   # social-engineering cue
    third_party_payout: Optional[bool] = None  # mule cue
    # --- named scam archetypes, recognised from their own wording
    impersonation_of_authority: Optional[bool] = None
    payee_details_changed: Optional[bool] = None
    otp_or_number_lost: Optional[bool] = None
    romance_or_grooming: Optional[bool] = None
    refund_to_other_instrument: Optional[bool] = None
    mule_recruitment: Optional[bool] = None
    untraceable_payment_demand: Optional[bool] = None
    coached_against_bank: Optional[bool] = None
    disputed_but_consistent: Optional[bool] = None
    stolen_or_synthetic_identity: Optional[bool] = None
    inbound_then_onward: Optional[bool] = None
    countries: list[str] = field(default_factory=list)

    @property
    def max_amount(self) -> float:
        return max(self.amounts) if self.amounts else 0.0


@dataclass
class ParsedSubject:
    original: str
    normalized: str                  # lowercased, whitespace-collapsed: the cache key basis
    primary_kind: EntityKind
    entities: list[Entity] = field(default_factory=list)
    hints: Hints = field(default_factory=Hints)
    assessable: bool = True          # False => honest abstention, no sources queried
    vague_reason: str = ""

    def entities_of(self, kind: EntityKind) -> list[Entity]:
        return [e for e in self.entities if e.kind == kind]


@dataclass
class GatherResult:
    signals: list[Signal]
    source_status: dict[str, str]    # name -> ok | empty | timeout | error | circuit_open | skipped
    elapsed_ms: int = 0

    @property
    def degraded(self) -> bool:
        """True when a source failed, so "no signal" cannot be read as "nothing found"."""
        bad = {"timeout", "error", "circuit_open"}
        return any(s in bad for s in self.source_status.values())

    @property
    def all_failed(self) -> bool:
        ran = [s for s in self.source_status.values() if s != "skipped"]
        return bool(ran) and all(s in {"timeout", "error", "circuit_open"} for s in ran)


@dataclass
class Score:
    """Output of the deterministic scorer.

    `score` is already rounded to 6 decimals and `level` is derived from that rounded value,
    so the two can never disagree at a boundary.
    """

    score: float
    level: RiskLevel
    rule: str                        # stable id of the mapping path taken, for logs
    families: list[str] = field(default_factory=list)
    corroborated: bool = False       # >= 2 independent families contributed weight


@dataclass
class Assessment:
    """What the pipeline hands back to the HTTP layer."""

    subject: str
    level: RiskLevel
    score: float
    signals: list[str]
    summary: str
    cacheable: bool = True
    cache_ttl: Optional[int] = None
    debug: dict = field(default_factory=dict)
