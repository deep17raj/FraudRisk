"""The deterministic roll-up, covering all four verdicts from synthetic outcome sets.

`verdict` is the only field Telegraph records on-chain, so these are the tests that matter
most: they pin the precedence table in `audit/verdict.py` and prove identical inputs give
identical verdicts.
"""
from __future__ import annotations

import pytest

from audit.models import ContractFlags, Obligation, SourceDoc, Status, Verdict
from audit.verdict import decisive, roll_up, tally


def ob(status: Status, **kw) -> Obligation:
    return Obligation(party=kw.pop("party", "The Supplier"),
                      duty=kw.pop("duty", "deliver the Goods"), status=status, **kw)


def doc(text: str = "1.1 The Supplier shall deliver the Goods.", **kw) -> SourceDoc:
    return SourceDoc(text=text, **kw)


# --- the four verdicts ------------------------------------------------------------------

def test_all_met_is_compliant():
    verdict, rule = roll_up([ob(Status.MET), ob(Status.MET)], ContractFlags(), doc())
    assert verdict is Verdict.COMPLIANT and rule == "all_met"


def test_met_plus_unverifiable_is_partially_compliant():
    verdict, rule = roll_up([ob(Status.MET), ob(Status.UNVERIFIABLE)], ContractFlags(), doc())
    assert verdict is Verdict.PARTIAL and rule == "mixed"


def test_a_single_breach_is_non_compliant():
    verdict, rule = roll_up([ob(Status.MET), ob(Status.BREACHED)], ContractFlags(), doc())
    assert verdict is Verdict.NON_COMPLIANT and rule == "breach"


def test_a_contradiction_is_non_compliant_even_with_everything_met():
    verdict, rule = roll_up([ob(Status.MET)], ContractFlags(contradiction=True), doc())
    assert verdict is Verdict.NON_COMPLIANT and rule == "contradiction"


def test_all_unverifiable_is_unclear_not_compliant():
    """The honest answer when the text states duties but establishes none of them."""
    verdict, rule = roll_up([ob(Status.UNVERIFIABLE)] * 3, ContractFlags(), doc())
    assert verdict is Verdict.UNCLEAR and rule == "nothing_established"


def test_unsupplied_schedule_is_unclear():
    flags = ContractFlags(missing_documents=["Schedule A"])
    verdict, rule = roll_up([ob(Status.MET)], flags, doc())
    assert verdict is Verdict.UNCLEAR and rule == "missing_dependency"


def test_missing_dependency_flag_alone_is_enough():
    verdict, rule = roll_up([ob(Status.MET)], ContractFlags(missing_dependency=True), doc())
    assert verdict is Verdict.UNCLEAR and rule == "missing_dependency"


def test_no_text_is_unclear():
    verdict, rule = roll_up([], ContractFlags(), doc(text=""))
    assert verdict is Verdict.UNCLEAR and rule == "no_text"


def test_text_with_no_identifiable_obligation_is_unclear():
    verdict, rule = roll_up([], ContractFlags(), doc(text="Hello there."))
    assert verdict is Verdict.UNCLEAR and rule == "no_obligations"


def test_missing_key_terms_is_unclear():
    verdict, rule = roll_up([ob(Status.MET)], ContractFlags(missing_key_terms=True), doc())
    assert verdict is Verdict.UNCLEAR and rule == "missing_key_terms"


# --- precedence -------------------------------------------------------------------------

def test_a_breach_outranks_a_missing_schedule():
    """A contradiction visible in the supplied text is established; the missing document
    only limits what else could have been checked."""
    flags = ContractFlags(missing_dependency=True, missing_documents=["Schedule A"])
    verdict, rule = roll_up([ob(Status.BREACHED), ob(Status.UNVERIFIABLE)], flags, doc())
    assert verdict is Verdict.NON_COMPLIANT and rule == "breach"


def test_a_breach_outranks_missing_key_terms():
    flags = ContractFlags(missing_key_terms=True)
    verdict, _ = roll_up([ob(Status.BREACHED)], flags, doc())
    assert verdict is Verdict.NON_COMPLIANT


def test_no_text_outranks_everything():
    flags = ContractFlags(contradiction=True)
    verdict, rule = roll_up([ob(Status.BREACHED)], flags, doc(text="   "))
    assert verdict is Verdict.UNCLEAR and rule == "no_text"


def test_missing_key_terms_outranks_a_missing_schedule():
    flags = ContractFlags(missing_key_terms=True, missing_documents=["Schedule A"])
    _, rule = roll_up([ob(Status.UNVERIFIABLE)], flags, doc())
    assert rule == "missing_key_terms"


def test_truncation_downgrades_compliant_to_partial():
    """With later terms unread, "every obligation is met" is not a claim we can make."""
    verdict, rule = roll_up([ob(Status.MET)], ContractFlags(), doc(truncated=True))
    assert verdict is Verdict.PARTIAL and rule == "truncated"


def test_truncation_does_not_rescue_a_breach():
    verdict, _ = roll_up([ob(Status.BREACHED)], ContractFlags(), doc(truncated=True))
    assert verdict is Verdict.NON_COMPLIANT


# --- totality and determinism -----------------------------------------------------------

@pytest.mark.parametrize("n_met", range(3))
@pytest.mark.parametrize("n_breached", range(3))
@pytest.mark.parametrize("n_unver", range(3))
@pytest.mark.parametrize("flagged", [None, "missing_dependency", "contradiction",
                                     "missing_key_terms"])
def test_every_combination_yields_one_allowed_verdict(n_met, n_breached, n_unver, flagged):
    """Total function: no outcome set can produce an invalid or missing verdict."""
    obligations = ([ob(Status.MET)] * n_met + [ob(Status.BREACHED)] * n_breached
                   + [ob(Status.UNVERIFIABLE)] * n_unver)
    flags = ContractFlags(**({flagged: True} if flagged else {}))
    verdict, rule = roll_up(obligations, flags, doc())
    assert verdict in set(Verdict) and rule
    assert verdict.value in ("Compliant", "Partially compliant", "Non-compliant", "Unclear")


def test_identical_inputs_give_identical_verdicts():
    args = ([ob(Status.MET), ob(Status.UNVERIFIABLE)],
            ContractFlags(missing_documents=[]), doc())
    assert roll_up(*args) == roll_up(*args) == roll_up(*args)


def test_roll_up_does_not_mutate_its_inputs():
    obligations = [ob(Status.MET), ob(Status.BREACHED)]
    flags = ContractFlags(missing_documents=["Schedule A"])
    before = [(o.party, o.duty, o.status) for o in obligations]
    roll_up(obligations, flags, doc())
    assert [(o.party, o.duty, o.status) for o in obligations] == before
    assert flags.missing_documents == ["Schedule A"]


# --- helpers ----------------------------------------------------------------------------

def test_tally_counts_each_status():
    counts = tally([ob(Status.MET), ob(Status.MET), ob(Status.BREACHED)])
    assert counts == {Status.MET: 2, Status.BREACHED: 1, Status.UNVERIFIABLE: 0}


def test_decisive_names_the_breached_obligation():
    good, bad = ob(Status.MET, duty="pay the Fee"), ob(Status.BREACHED, duty="deliver late")
    assert decisive([good, bad], "breach") is bad


def test_decisive_names_an_unverifiable_obligation_when_mixed():
    good, unknown = ob(Status.MET), ob(Status.UNVERIFIABLE, duty="insure the Goods")
    assert decisive([good, unknown], "mixed") is unknown


def test_decisive_is_safe_on_an_empty_list():
    assert decisive([], "no_obligations") is None


# --- how an obligation reads -------------------------------------------------------------

def test_a_deadline_carrying_its_own_preposition_is_not_given_another_one():
    """The model returns deadlines as phrases; "by within 30 days" would be the giveaway of a blind join."""
    assert "within 30 days of the Purchase Order -" in ob(Status.UNVERIFIABLE,
                                                          deadline="within 30 days of the Purchase Order").render()
    assert " by within" not in ob(Status.UNVERIFIABLE, deadline="within 30 days").render()
    assert " by on " not in ob(Status.UNVERIFIABLE, duty="terminate", deadline="on 60 days written notice").render()
    assert "upon completion" in ob(Status.UNVERIFIABLE, deadline="upon completion").render()


def test_a_bare_date_still_gets_by():
    assert "deliver the Goods by 31 March 2027" in ob(Status.MET, deadline="31 March 2027").render()
    assert "pay rent by the first day of each month" in ob(Status.MET, duty="pay rent",
                                                           deadline="the first day of each month").render()


def test_a_deadline_already_inside_the_duty_is_not_repeated():
    rendered = ob(Status.MET, duty="pay each invoice within thirty (30) days", deadline="within thirty (30) days").render()
    assert rendered.count("within thirty (30) days") == 1
