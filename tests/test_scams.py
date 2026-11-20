"""The named scam archetypes (`fraudrisk/scams.py`).

Described scenarios are the most common and most valuable kind of subject, and before these
cues existed all fifteen classic scams below produced no signal at all - and were told they
contained "no concrete behavioural detail", which was false. Each case is written in wording
chosen BEFORE the patterns, then the patterns were written to match.

The denial half matters just as much: "the caller was verified by calling the bank back" must
not fire the impersonation rule.
"""
from __future__ import annotations

import pytest

from fraudrisk.classify import classify
from fraudrisk.models import RiskLevel
from fraudrisk.patterns import Pattern
from fraudrisk.scoring import compute_score, level_for_score
from fraudrisk.sources.rules import evaluate_rules, vote_pattern

#: (subject, hint that must fire, rule id that must fire, acceptable patterns)
SCENARIOS = [
    ("A caller claiming to be from the bank fraud team persuaded the customer to move their "
     "balance to a safe account",
     "impersonation_of_authority", "scam.impersonation",
     {Pattern.SOCIAL_ENGINEERING}),
    ("The supplier emailed new bank details just before the invoice was due and asked us to "
     "update the payee",
     "payee_details_changed", "scam.payee_details_changed",
     {Pattern.SOCIAL_ENGINEERING}),
    ("An employee changed the payroll bank details for a senior executive after an email request",
     "payee_details_changed", "scam.payee_details_changed",
     {Pattern.SOCIAL_ENGINEERING}),
    ("The customer lost access to their phone number and someone else is now receiving the "
     "one-time passcodes",
     "otp_or_number_lost", "scam.sim_swap",
     {Pattern.ACCOUNT_TAKEOVER}),
    ("She has been sending money to someone she met online who she has never video called",
     "romance_or_grooming", "scam.romance",
     {Pattern.SOCIAL_ENGINEERING}),
    ("A dating contact asked for money for an emergency medical bill abroad",
     "romance_or_grooming", "scam.romance",
     {Pattern.SOCIAL_ENGINEERING}),
    ("The buyer overpaid and is asking for the difference to be refunded to a different card",
     "refund_to_other_instrument", "scam.refund_other_instrument",
     {Pattern.FIRST_PARTY_FRAUD}),
    ("A recruiter asked the applicant to receive client payments into their own account and "
     "keep a commission",
     "mule_recruitment", "scam.mule_recruitment",
     {Pattern.MULE_ACCOUNT}),
    ("Someone called pretending to be from HMRC and demanded payment in gift cards",
     "untraceable_payment_demand", "scam.untraceable_demand",
     {Pattern.SOCIAL_ENGINEERING}),
    ("The customer was coached on what to tell the bank if asked about the transfer",
     "coached_against_bank", "scam.coached",
     {Pattern.SOCIAL_ENGINEERING}),
    ("Customer says they did not authorise the transaction but the device and location match "
     "their usual pattern",
     "disputed_but_consistent", "scam.disputed_but_consistent",
     {Pattern.FIRST_PARTY_FRAUD}),
    ("The merchant shipped goods then the cardholder claimed the card was stolen",
     "disputed_but_consistent", "scam.disputed_but_consistent",
     {Pattern.FIRST_PARTY_FRAUD}),
    ("Someone is using a stolen identity with a real name and a fabricated address to open "
     "accounts",
     "stolen_or_synthetic_identity", "scam.stolen_identity",
     {Pattern.SYNTHETIC_IDENTITY}),
    ("Funds arrived from an unknown sender and the customer was asked to send most of it onward",
     "inbound_then_onward", "scam.inbound_then_onward",
     {Pattern.MULE_ACCOUNT}),
    ("The invoice bank details were changed at the last minute by email",
     "payee_details_changed", "scam.payee_details_changed",
     {Pattern.SOCIAL_ENGINEERING}),
]


@pytest.mark.parametrize("subject,hint,rule_id,patterns",
                         SCENARIOS, ids=[s[1] + "|" + s[0][:28] for s in SCENARIOS])
def test_classic_scams_are_recognised(subject, hint, rule_id, patterns):
    parsed = classify(subject)
    assert getattr(parsed.hints, hint) is True, f"{hint} did not fire"

    fired = {s.id for s in evaluate_rules(parsed) if s.weight > 0}
    assert rule_id in fired, f"expected {rule_id}, got {sorted(fired)}"

    signals = evaluate_rules(parsed)
    assert vote_pattern(signals) in patterns, vote_pattern(signals)


@pytest.mark.parametrize("subject,hint,rule_id,patterns",
                         SCENARIOS, ids=[s[1] + "|" + s[0][:28] for s in SCENARIOS])
def test_classic_scams_are_graded_above_the_floor(subject, hint, rule_id, patterns):
    """A recognised scam must not come back LOW - that was the behaviour before these cues."""
    score = compute_score(evaluate_rules(classify(subject)))
    assert score.level is not RiskLevel.LOW, f"{subject[:40]!r} -> {score.score}"
    assert level_for_score(score.score) is score.level


@pytest.mark.parametrize("subject,denied_hint", [
    ("The customer verified the caller by calling the bank back on the number on their card",
     "impersonation_of_authority"),
    ("The supplier's bank details were verified by callback before payment",
     "payee_details_changed"),
])
def test_denials_do_not_fire_the_scam_rules(subject, denied_hint):
    assert getattr(classify(subject).hints, denied_hint) is False, subject


@pytest.mark.parametrize("subject", [
    "The customer paid the invoice on the usual bank details",
    "A returning customer bought a book for $14",
    "The supplier sent the invoice by email as they always do",
    "She video called her sister before sending her a birthday gift",
    "The applicant accepted a job offer and signed a contract",
    "The buyer paid the correct amount and the order shipped",
    "The customer asked for a refund to the card they paid with",
    "Payment was made by bank transfer from a long-standing account",
])
def test_ordinary_business_is_not_read_as_a_scam(subject):
    """The counterpart to the suite above: these share vocabulary but describe nothing wrong."""
    fired = [s.id for s in evaluate_rules(classify(subject)) if s.weight > 0]
    assert fired == [], f"fabricated scam signal for {subject!r}: {fired}"


def test_a_detailed_but_unrecognised_scenario_is_not_called_detail_free():
    """An honest limit, not a false claim that the caller said nothing."""
    subject = ("The warehouse team reported that the pallet arrived with a torn label and "
               "some damp packaging on the outer carton")
    parsed = classify(subject)
    assert parsed.assessable is True, "a detailed narrative is assessable; we just matched nothing"
    assert "no concrete behavioural detail" not in parsed.vague_reason


def test_short_vague_subjects_are_still_unassessable():
    for subject in ("check this person", "is this safe?", "John Smith", "thoughts?"):
        assert classify(subject).assessable is False, subject
