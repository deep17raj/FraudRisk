"""Stage 4 - the verdict roll-up. Pure, total, and deliberately not the model's decision.

`verdict` is the one field Telegraph records on-chain, so it is computed here from the
per-obligation statuses and the document-level flags by a fixed table rather than asked for
in prose. The same structure always produces the same verdict, which is what makes the audit
reproducible and the rules arguable.

Precedence, highest first. The first rule that matches decides:

    rule                  condition                                          verdict
    --------------------  -------------------------------------------------  -------------------
    no_text               nothing auditable was supplied or resolved         Unclear
    breach                an obligation is breached on the face of the text  Non-compliant
    contradiction         two terms cannot both hold                         Non-compliant
    no_obligations        text was read but creates no identifiable duty     Unclear
    missing_key_terms     no identifiable parties, consideration or duties   Unclear
    missing_dependency    the real terms sit in a document not supplied      Unclear
    nothing_established   every obligation is unverifiable from the text     Unclear
    truncated            all obligations met, but the text was cut short     Partially compliant
    all_met               every obligation is met and consistent             Compliant
    mixed                 some met, some unverifiable                        Partially compliant

A breach outranks a missing schedule: a contradiction visible in the supplied text is
established, whereas the missing document only limits what else could be checked.

Note what `Compliant` claims and what it does not. We audit the contract text, not the
world: it means every obligation found is completely and consistently specified, or the text
itself records performance. It never asserts that the parties have in fact performed, and the
summary says so.
"""
from __future__ import annotations

from .models import ContractFlags, Obligation, SourceDoc, Status, Verdict


def tally(obligations: list[Obligation]) -> dict[Status, int]:
    return {s: sum(1 for o in obligations if o.status is s) for s in Status}


def roll_up(obligations: list[Obligation], flags: ContractFlags,
            doc: SourceDoc | None = None) -> tuple[Verdict, str]:
    """Return (verdict, rule) for a set of per-obligation outcomes. Pure and total."""
    counts = tally(obligations)
    truncated = bool(doc and doc.truncated)

    if doc is not None and not doc.usable:
        return Verdict.UNCLEAR, "no_text"
    if counts[Status.BREACHED]:
        return Verdict.NON_COMPLIANT, "breach"
    if flags.contradiction:
        return Verdict.NON_COMPLIANT, "contradiction"
    if not obligations:
        return Verdict.UNCLEAR, "no_obligations"
    if flags.missing_key_terms:
        return Verdict.UNCLEAR, "missing_key_terms"
    if flags.missing_dependency or flags.missing_documents:
        return Verdict.UNCLEAR, "missing_dependency"
    if not counts[Status.MET]:
        return Verdict.UNCLEAR, "nothing_established"
    if not counts[Status.UNVERIFIABLE]:
        return (Verdict.PARTIAL, "truncated") if truncated else (Verdict.COMPLIANT, "all_met")
    return Verdict.PARTIAL, "mixed"


def decisive(obligations: list[Obligation], rule: str) -> Obligation | None:
    """The obligation the verdict turns on - what the summary should name."""
    if not obligations:
        return None
    if rule == "breach":
        return next((o for o in obligations if o.status is Status.BREACHED), obligations[0])
    if rule in ("mixed", "nothing_established"):
        return next((o for o in obligations if o.status is Status.UNVERIFIABLE), obligations[0])
    return obligations[0]
