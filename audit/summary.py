"""Stage 5 - the summary, written in code after the verdict is known.

The summary is templated rather than generated for one reason: a sentence the model writes
in the same call that produced the obligations can contradict the verdict this service then
computes, and a summary arguing for "Compliant" above the word "Unclear" is worse than a
plain one. So the wording is assembled from the verdict, the rule that fired and the
decisive obligation's own reason - which keeps it grounded in the cited clauses and
guarantees it agrees with the field beside it.
"""
from __future__ import annotations

from .models import ContractFlags, Obligation, SourceDoc, Status, Verdict
from .verdict import decisive, tally

#: What `Compliant` and friends actually rest on. Stated once so no verdict overclaims.
_BASIS = ("This audits the contract text itself; no external evidence of performance was "
          "available.")


def _name(ob: Obligation | None, limit: int = 110) -> str:
    """Name an obligation compactly: the party, the duty, and a verified clause reference."""
    if ob is None:
        return "the contract's obligations"
    duty = " ".join(ob.duty.split())
    if len(duty) > limit:
        cut = duty[:limit].rsplit(" ", 1)[0]
        duty = cut + "..."
    out = f"{ob.party}'s obligation to {duty}".replace("'s obligation to shall ", "'s obligation to ")
    if ob.clause_ref and ob.ref_verified:
        out += f" ({ob.clause_ref})"
    return out


def _docs(flags: ContractFlags) -> tuple[str, str]:
    """The missing document(s) named, plus the verb that agrees with them."""
    docs = flags.missing_documents
    if not docs:
        return "a document that was not supplied", "was"
    if len(docs) == 1:
        return docs[0], "was"
    return ", ".join(docs[:-1]) + f" and {docs[-1]}", "were"


def compose(verdict: Verdict, rule: str, obligations: list[Obligation], flags: ContractFlags,
            doc: SourceDoc, *, n_clauses: int = 0, degraded: bool = False) -> str:
    """One or two sentences naming the decisive obligation and why the verdict follows."""
    counts = tally(obligations)
    lead = decisive(obligations, rule)
    total = len(obligations)

    if rule == "no_text":
        if doc.fetch_error:
            return (f"The contract could not be audited because the linked document could not "
                    f"be read: {doc.fetch_error}. Send the contract text itself, or a link "
                    f"that resolves to a readable document.")
        return ("No contract text was supplied, so there is nothing to audit. Send the "
                "contract text, or a link to it, in the `contract` field.")

    if rule == "breach":
        reason = lead.reason or "the text shows it was not performed"
        text = f"Non-compliant: {_name(lead)} is not met - {reason.rstrip('.')}."
        if total > 1:
            text += (f" {counts[Status.MET]} of {total} identified obligations are met and "
                     f"{counts[Status.UNVERIFIABLE]} cannot be verified from the text.")
        return text

    if rule == "contradiction":
        note = flags.note or "two of its terms cannot both be satisfied"
        return (f"Non-compliant: the contract contradicts itself - {note.rstrip('.')} - so its "
                f"obligations as written cannot all be met. {_BASIS}")

    if rule == "no_obligations":
        return ("The supplied text was read but no obligation on any party could be "
                "identified in it, so no compliance verdict is defensible. Send the operative "
                "clauses of the contract rather than a summary or a title page.")

    if rule == "missing_key_terms":
        note = flags.note or "it names no parties, consideration or operative duties"
        return (f"Unclear: the text is not a complete contract - {note.rstrip('.')} - so its "
                f"obligations cannot be established, let alone judged.")

    if rule == "missing_dependency":
        names, verb = _docs(flags)
        return (f"Unclear: the contract's operative terms are deferred to {names}, which "
                f"{verb} not supplied, so {_name(lead)} cannot be judged from the text alone. "
                f"Supply the referenced document(s) to obtain a verdict.")

    if rule == "nothing_established":
        return (f"Unclear: all {total} identified obligation(s) are stated but none can be "
                f"verified from the text alone, including {_name(lead)}. {_BASIS}")

    if rule == "truncated":
        return (f"Partially compliant: every one of the {total} obligations visible in the text "
                f"is fully specified, but the contract was truncated at this service's input "
                f"limit, so later terms were not audited.")

    if rule == "all_met":
        tail = (f" {_BASIS}" if not degraded else "")
        return (f"Compliant: all {total} identified obligation(s) are completely and "
                f"consistently specified, including {_name(lead)}"
                f"{f', cited from {n_clauses} clause(s) of the contract' if n_clauses else ''}."
                f"{tail}")

    # mixed
    reason = lead.reason if lead and lead.reason else "the text does not establish it"
    return (f"Partially compliant: {counts[Status.MET]} of {total} identified obligations are "
            f"met, while {counts[Status.UNVERIFIABLE]} cannot be verified from the contract "
            f"text - notably {_name(lead)}, where {reason.rstrip('.')}.")


def with_notes(summary: str, doc: SourceDoc, *, dropped: int = 0, degraded: str = "") -> str:
    """Append the honest caveats: truncation, a degraded path, discarded citations."""
    extra: list[str] = []
    if doc.truncated and "truncated" not in summary:
        extra.append("The contract exceeded this service's input limit and was truncated, so "
                     "later terms were not audited")
    if degraded:
        extra.append(f"This audit used the deterministic reader rather than the model "
                     f"({degraded}), so obligations are read from the contract's own wording")
    if dropped:
        extra.append(f"{dropped} candidate quote(s) were discarded because they did not appear "
                     f"verbatim in the contract")
    if not extra:
        return summary
    return summary.rstrip() + " " + ". ".join(extra) + "."
