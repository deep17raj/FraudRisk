"""The domain types. `Verdict` is the on-chain enum and its spelling is load-bearing.

Telegraph encodes `verdict` on-chain as an enum with a closed allowed-list; the other three
response fields are off-chain. An unrecognised spelling is not a near miss, it is an invalid
answer, so the enum values here are the only place the four strings are written.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

#: A deadline phrase that carries its own preposition, so "by" must not be put in front of it.
_OPENS_WITH_TIME_WORD = re.compile(
    r"^(?:within|on|by|before|after|upon|at|in|during|from|until|till|through|throughout|per|each|every|"
    r"annually|monthly|quarterly|weekly|daily|no\s+later\s+than|not\s+later\s+than|prior\s+to|as\s+of|"
    r"immediately|promptly|forthwith|once|when|whenever|while|subject\s+to|following)\b", re.I)


class Verdict(str, Enum):
    """Exactly Telegraph's allowed values, including the capitalisation and the space."""

    COMPLIANT = "Compliant"
    PARTIAL = "Partially compliant"
    NON_COMPLIANT = "Non-compliant"
    UNCLEAR = "Unclear"


class Status(str, Enum):
    """How one obligation stands *on the face of the contract text we were given*.

    MET           the duty is fully specified and consistent, or the text itself records
                  performance (e.g. "the Deposit was paid on 1 March").
    BREACHED      the text shows the duty was not performed, or two terms contradict.
    UNVERIFIABLE  the text alone cannot establish it - a term is absent, or it defers to a
                  document that was not supplied. This is an honest answer, not a failure.
    """

    MET = "met"
    BREACHED = "breached"
    UNVERIFIABLE = "unverifiable"


_STATUS_LABEL = {
    Status.MET: "met",
    Status.BREACHED: "not met",
    Status.UNVERIFIABLE: "unverifiable from the contract text",
}


@dataclass
class Obligation:
    """One duty, plus the grounding result for its supporting quote.

    `quote` is what the model claimed. `cited` is the verbatim slice of the source document
    that was actually located - empty when the quote could not be found, in which case
    nothing is cited for this obligation. `ref_verified` records whether the clause label
    itself (e.g. "Clause 4.1") appears in the source; an unverified label is never printed.
    """

    party: str
    duty: str
    status: Status
    deadline: str = ""
    clause_ref: str = ""
    reason: str = ""
    quote: str = ""
    cited: str = ""
    ref_verified: bool = False

    def render(self) -> str:
        """The plain string that goes in the `obligations` list.

        Built in code from the structured fields, so the shape is uniform and the only
        free text is the model's wording of the duty itself.
        """
        head = f"{self.party}: {self.duty}".rstrip(" .;,")
        # The duty wording often already carries the deadline ("pay within thirty (30) days");
        # appending it again reads as "... within thirty (30) days by within thirty (30) days".
        if self.deadline and self.deadline.strip().lower() not in self.duty.lower():
            deadline = self.deadline.strip()
            # A deadline is often a phrase that already opens with its own preposition ("within 30
            # days of the Purchase Order", "on 60 days written notice"); "by" in front of one of
            # those reads as "deliver the Goods by within 30 days".
            joiner = "" if _OPENS_WITH_TIME_WORD.match(deadline) else "by "
            head += f" {joiner}{deadline}".rstrip(" .;,")
        if self.clause_ref and self.ref_verified:
            head += f" ({self.clause_ref})"
        out = f"{head} - {_STATUS_LABEL[self.status]}"
        if self.reason:
            out += f": {self.reason.rstrip(' .;,')}"
        return out


@dataclass
class ContractFlags:
    """Document-level findings that outrank any single obligation in the roll-up."""

    missing_dependency: bool = False   # depends on a schedule/exhibit that was not supplied
    contradiction: bool = False        # two terms cannot both hold
    missing_key_terms: bool = False    # no identifiable parties, consideration or duties
    note: str = ""
    #: Un-supplied documents the text depends on, found by regex in the source (evidence).
    missing_documents: list[str] = field(default_factory=list)


@dataclass
class SourceDoc:
    """The resolved contract: the text we audit, and where it came from."""

    text: str
    origin: str = "text"       # "text" | "url" | "empty"
    url: str = ""
    truncated: bool = False
    fetch_error: str = ""      # set when `origin` was a URL that could not be resolved

    @property
    def usable(self) -> bool:
        return bool(self.text.strip())


@dataclass
class Audit:
    """The finished audit. `to_payload` is the only thing the API layer serialises."""

    verdict: Verdict
    obligations: list[str]
    clauses: list[str]
    summary: str
    #: Everything below is for logs and tests only - it never reaches the response.
    rule: str = ""
    degraded: bool = False
    diagnostics: dict = field(default_factory=dict)

    def to_payload(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "obligations": self.obligations,
            "clauses": self.clauses,
            "summary": self.summary,
        }
