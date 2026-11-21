"""Distractor suite: ordinary sentences that must produce NO risk signal.

Every test above this one asserts that the right signal fires. These assert the opposite, and
they are deliberately written as prose a caller would plausibly send rather than as phrasings
chosen to match the regexes. Fabricated signals are the most heavily penalised failure mode
for a judgment intent, and each case below is a bug this suite actually caught:

  * "14 declined attempts within 3 minutes" read as a three-minute-old account
  * "customer called 2 days ago" read as a two-day-old account
  * "$45 payment" read as 45 attempts
  * "an existing payee" cancelling "brand new account", then asserting the account was
    established - a false BENIGN claim, which is just as dishonest as a false red flag
  * "German customer in Germany" read as a cross-border mismatch
  * "asked us to pay a supplier in France" reading the pronoun "us" as the United States
  * "immediately forwarded" read as social-engineering pressure
  * wellsfargo.com, amazon.de, usa.gov and google.co.uk read as brand impersonation
"""
from __future__ import annotations

import pytest

from fraudrisk.classify import classify
from fraudrisk.sources.phish import DomainHeuristicSource
from fraudrisk.sources.rules import evaluate_rules

#: Sentences with no fraud signal in them at all.
BENIGN = [
    "A $45 payment to an existing payee from the usual device",
    "Customer called 2 days ago about a credit on their statement",
    "The customer phoned 3 hours ago to update their address",
    "Refund of $20 issued 5 days ago for a returned item",
    "German customer in Germany buying from a German retailer",
    "We asked us to pay a supplier in France",
    "Supplier invoice settled within 30 days as agreed",
    "A subscription renewal of $9.99 charged to the card on file",
    "The account holder updated their marketing preferences",
    "Order delivered 2 days ago and marked complete",
    "A $12 top-up on a long-standing account",
    "Customer logged in from their usual device and browser",
    "Payroll run of 40 payments to the same employees as last month",
    "The buyer asked a question about delivery times",
    "Statement downloaded 4 hours ago by the account holder",
    "A transfer to the customer's own savings account",
    "Monthly rent payment of $1,200 to the same landlord as always",
    "The user enabled two-factor authentication on their account",
    "Card replaced 6 months ago after it expired",
    "A $3 coffee purchase at a local store",
]

#: Ordinary sentences that SHARE VOCABULARY with a named scam archetype but describe nothing
#: wrong. Every one of these fired a MEDIUM-or-HIGH scam signal in the first version of
#: `fraudrisk/scams.py`, which is why each cue there now needs two anchors in one clause.
SCAM_LOOKALIKES = [
    "The bank called the customer to confirm the transfer was genuine",
    "Customer logged in to their secure account and paid a bill",
    "Payment received and the order was sent to the customer",
    "The customer overpaid their bill and we refunded the original card",
    "The marketplace will take a commission on each sale",
    "Her employer processed the monthly salary payment",
    "Customer paid $9.99 for a dating app subscription",
    "Customer paid an unexpected medical bill at the hospital",
    "The customer asked for a gift card for their nephew",
    "The customer's account details were updated after they moved house",
    "The customer no longer has the order number",
    "The bank advised the customer how to respond to phishing emails",
    "The customer is looking forward to a holiday abroad",
    "The customer verified the caller by calling the bank back on the number on their card",
    "We processed a commission payment to the sales agent",
    "The supplier's invoice was paid to the details already on file",
]


@pytest.mark.parametrize("subject", SCAM_LOOKALIKES)
def test_scam_lookalikes_produce_no_signal(subject):
    parsed = classify(subject)
    fired = [(s.id, round(s.weight, 3)) for s in evaluate_rules(parsed) if s.weight > 0]
    assert fired == [], f"fabricated scam signal for {subject!r}: {fired}"


def test_a_credential_reset_does_not_assert_money_moved():
    """The signal is legitimate; the wording must not invent an outbound payment."""
    inbound = classify("Payment received and the customer reset their password")
    texts = [s.text for s in evaluate_rules(inbound) if s.id == "behav.credential_reset"]
    assert texts, "the credential-reset signal should still fire"
    assert "before the money moved" not in texts[0]
    assert "cash-out" not in texts[0]

    outbound = classify("Password was reset then $9,000 was sent to a new payee")
    texts = [s.text for s in evaluate_rules(outbound) if s.id == "behav.credential_reset"]
    assert texts and "before the money moved" in texts[0]


#: Legitimate hostnames, including brand-adjacent ones that are genuinely brand-operated.
LEGIT_HOSTS = [
    "paypal.com", "amazon.de", "google.co.uk", "wellsfargo.com", "usa.gov",
    "hmrc.gov.uk", "login.microsoftonline.com", "paypalobjects.com",
    "accounts.google.com", "secure.example.com", "support.apple.com",
    "my-bank-account.com", "example.org", "news.bbc.co.uk",
]


@pytest.mark.parametrize("subject", BENIGN)
def test_benign_sentences_produce_no_risk_signal(subject):
    parsed = classify(subject)
    fired = [(s.id, round(s.weight, 3)) for s in evaluate_rules(parsed) if s.weight > 0]
    assert fired == [], f"fabricated signal(s) for {subject!r}: {fired}"


@pytest.mark.parametrize("subject", BENIGN)
def test_benign_sentences_claim_no_unsupported_account_age(subject):
    """An account age must come from an age statement, not from any nearby number."""
    hints = classify(subject).hints
    if hints.account_age_days is not None:
        lowered = subject.lower()
        assert any(w in lowered for w in ("account", "profile", "customer", "client", "user",
                                          "member", "relationship")), subject
        assert any(w in lowered for w in ("old", "opened", "created", "registered",
                                          "signed up", "of ", "age")), subject


@pytest.mark.parametrize("subject", BENIGN)
def test_benign_sentences_claim_no_velocity(subject):
    """A velocity count must never come from a currency amount."""
    hints = classify(subject).hints
    if hints.velocity_count is not None:
        assert hints.velocity_count not in {int(a) for a in hints.amounts}, subject


def test_a_new_account_is_not_erased_by_an_existing_payee():
    """The bug that both hid a real signal AND asserted a false benign one."""
    parsed = classify("Brand new account paying an existing payee")
    assert parsed.hints.new_account is True
    assert parsed.hints.new_payee is False
    ids = {s.id for s in evaluate_rules(parsed)}
    assert "behav.very_new_account" in ids
    assert "behav.established_account" not in ids, "claimed an established account that is new"


def test_one_country_named_twice_is_not_a_mismatch():
    for subject in ("German customer in Germany",
                    "US customer with a United States billing address",
                    "British buyer in England"):
        hints = classify(subject).hints
        assert len(hints.countries) <= 1, (subject, hints.countries)
        assert hints.geo_mismatch is not True, subject


def test_the_pronoun_us_is_not_the_united_states():
    hints = classify("The seller asked us to pay a supplier in France").hints
    assert hints.countries == ["FR"], hints.countries
    assert hints.geo_mismatch is not True


def test_two_real_countries_still_mismatch():
    hints = classify("Card issued in Denmark, shipping to Nigeria").hints
    assert set(hints.countries) == {"DK", "NG"}
    assert hints.geo_mismatch is True


def test_speed_is_not_pressure():
    """"immediately forwarded" is mule behaviour, not social engineering."""
    hints = classify("Customer received funds then immediately forwarded them overseas").hints
    assert hints.third_party_payout is True
    assert hints.urgency_pressure is not True


def test_real_pressure_is_still_detected():
    for subject in ("Customer was told urgently to move the money and keep it confidential",
                    "The caller threatened that the account will be closed today",
                    "They were told not to tell anyone about the transfer"):
        assert classify(subject).hints.urgency_pressure is True, subject


@pytest.mark.parametrize("host", LEGIT_HOSTS)
async def test_legitimate_hosts_are_not_flagged(host, http_client):
    signals = await DomainHeuristicSource().collect(classify(host), http_client, 1.0, None)
    fired = [(s.id, round(s.weight, 3)) for s in signals if s.weight > 0]
    assert fired == [], f"smeared {host}: {fired}"


@pytest.mark.parametrize("host", [
    "paypal-verify.secure-login.tk",
    "secure-paypal-login-verify.tk",
    "account-verify-now.tk",
    "apple-icloud-locked.click",
    "paypal.verify-now.com",
    "binance-wallet-unlock.xyz",
])
async def test_phishing_shaped_hosts_are_still_flagged(host, http_client):
    signals = await DomainHeuristicSource().collect(classify(host), http_client, 1.0, None)
    assert any(s.weight > 0 for s in signals), f"missed {host}"


def test_a_vague_subject_never_acquires_signals():
    for subject in ("check this person", "is this ok?", "look into this account",
                    "assess", "the customer", "thoughts?"):
        parsed = classify(subject)
        assert parsed.assessable is False, subject
        assert evaluate_rules(parsed) == [], subject
