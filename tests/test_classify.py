"""Subject classification, entity extraction and the negation guards.

The negation tests matter most: on the deterministic path the rules alone decide the grade,
so "not a new payee" scoring like "new payee" would be a fabricated red flag.
"""
from __future__ import annotations

import pytest

from fraudrisk.classify import classify
from fraudrisk.models import EntityKind
from fraudrisk.sources.rules import evaluate_rules


def kinds(subject: str) -> set[str]:
    return {e.kind.value for e in classify(subject).entities}


def values(subject: str, kind: EntityKind) -> list[str]:
    return [e.value for e in classify(subject).entities_of(kind)]


# --------------------------------------------------------------- primary routing

@pytest.mark.parametrize("subject,expected", [
    ("8.8.8.8", EntityKind.IP),
    ("457173", EntityKind.CARD_BIN),
    ("user@example.com", EntityKind.EMAIL),
    ("example.com", EntityKind.DOMAIN),
    ("https://example.com/login", EntityKind.URL),
    ("0x742d35Cc6634C0532925a3b844Bc454e4438f44e", EntityKind.WALLET),
    ("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", EntityKind.WALLET),
    ("A new customer made a large first transfer to a new payee", EntityKind.SCENARIO),
])
def test_primary_kind_routing(subject, expected):
    assert classify(subject).primary_kind is expected


def test_scenario_still_exposes_every_embedded_entity():
    """The whole point of multi-signal routing: one subject can feed several sources."""
    parsed = classify("Login from 185.220.101.1 using card BIN 517805 to pay "
                      "steal-your-login.tk from user@mailinator.com")
    found = {e.kind for e in parsed.entities}
    assert {EntityKind.IP, EntityKind.CARD_BIN, EntityKind.DOMAIN, EntityKind.EMAIL} <= found
    assert parsed.primary_kind is EntityKind.SCENARIO


# --------------------------------------------------------------- BIN discipline

def test_money_is_not_read_as_a_bin():
    assert not values("Transfer of $450000 to a new payee", EntityKind.CARD_BIN)
    assert not values("Invoice for 250000 USD", EntityKind.CARD_BIN)


def test_order_and_reference_numbers_are_not_read_as_a_bin():
    for subject in ("Order 1234567 shipped to the usual address",
                    "Invoice 998877 disputed by the customer",
                    "Ticket 445566 raised about the account",
                    "Account number 7654321 under review"):
        assert not values(subject, EntityKind.CARD_BIN), subject


def test_bin_is_found_when_a_card_cue_is_present():
    assert values("BIN 517805 declined repeatedly", EntityKind.CARD_BIN) == ["517805"]
    assert values("prepaid visa 457173", EntityKind.CARD_BIN) == ["457173"]


def test_full_card_number_yields_its_first_six_digits():
    assert values("card 4571736152364123", EntityKind.CARD_BIN) == ["457173"]


def test_private_and_documentation_ips_are_ignored():
    for subject in ("10.0.0.1", "192.168.1.1", "127.0.0.1", "203.0.113.9"):
        assert not values(subject, EntityKind.IP), subject


# --------------------------------------------------------------- hint extraction

def test_amounts_and_currency():
    h = classify("First transfer of $4,200 to a new payee").hints
    assert h.max_amount == 4200.0 and h.currency == "USD"
    assert classify("wire of 3.5k GBP").hints.max_amount == 3500.0
    assert classify("payment of EUR 900").hints.currency == "EUR"


@pytest.mark.parametrize("subject,days", [
    ("account created 2 days ago", 2.0),
    ("3-day-old account", 3.0),
    ("the account is 5 years old", 1825.0),
    ("profile registered 6 months ago", 180.0),
])
def test_account_age(subject, days):
    assert classify(subject).hints.account_age_days == pytest.approx(days)


def test_velocity_and_small_amounts():
    h = classify("14 declined attempts of $1.00 each").hints
    assert h.velocity_count == 14 and h.small_amounts is True
    assert classify("many repeated attempts in quick succession").hints.velocity_count == -1


def test_geo_mismatch_from_two_countries():
    assert classify("card issued in Denmark, shipping to Nigeria").hints.geo_mismatch is True


# --------------------------------------------------------------- negation guards

@pytest.mark.parametrize("field,positive,negative", [
    ("new_payee", "transfer to a new payee", "transfer to an existing payee"),
    ("new_payee", "paying a new beneficiary", "not a new payee"),
    ("anonymiser_claimed", "login over a VPN", "no VPN was used"),
    ("anonymiser_claimed", "connected through Tor", "not using a proxy"),
    ("device_change", "from a new device", "from the same device"),
    ("chargeback_history", "two prior chargebacks", "no chargebacks"),
    ("first_transfer", "their first payment", "a recurring payment"),
    ("new_account", "a brand new account", "a long-standing account"),
    ("credential_reset", "password was changed", "no password reset"),
    ("geo_mismatch", "country mismatch on the order", "same country throughout"),
])
def test_tristate_hints_distinguish_stated_from_denied(field, positive, negative):
    assert getattr(classify(positive).hints, field) is True, positive
    assert getattr(classify(negative).hints, field) is False, negative


def test_unmentioned_hints_stay_none():
    """Silence is not a negative, and it is certainly not a positive."""
    h = classify("A payment of $50 was made").hints
    for field in ("new_payee", "first_transfer", "device_change", "credential_reset",
                  "anonymiser_claimed", "chargeback_history", "returning_customer"):
        assert getattr(h, field) is None, field


def test_a_clause_break_cancels_a_negation():
    """"no VPN. first transfer to a new payee" - the negation must not leak across the stop."""
    h = classify("There was no VPN. The first transfer went to a new payee.").hints
    assert h.anonymiser_claimed is False
    assert h.new_payee is True


def test_denied_cues_produce_no_risk_signals():
    parsed = classify("Account is 5 years old, not a new payee, no VPN used, "
                      "same device, no chargebacks")
    risky = [s for s in evaluate_rules(parsed) if s.weight > 0]
    assert risky == [], [s.id for s in risky]


def test_stated_cues_do_produce_risk_signals():
    parsed = classify("Brand new account, VPN login, first transfer of $8,000 to a new payee")
    risky = [s for s in evaluate_rules(parsed) if s.weight > 0]
    assert {"behav.first_transfer_new_payee", "behav.anonymiser"} <= {s.id for s in risky}


# --------------------------------------------------------------- abstention

@pytest.mark.parametrize("subject", [
    "check this person", "is this safe?", "assess the customer", "review this account",
    "John Smith", "", "   ", "?", "this one",
])
def test_vague_subjects_are_not_assessable(subject):
    parsed = classify(subject)
    assert parsed.assessable is False
    assert parsed.vague_reason or not subject.strip()


@pytest.mark.parametrize("subject", [
    "8.8.8.8", "BIN 457173", "example.com", "a first transfer to a new payee",
    "14 declined attempts", "user@example.com",
])
def test_concrete_subjects_are_assessable(subject):
    assert classify(subject).assessable is True


def test_classification_is_pure_and_deterministic():
    subject = "New account, VPN login from 185.220.101.1, $4,200 to a new payee"
    first = classify(subject)
    for _ in range(5):
        again = classify(subject)
        assert again.primary_kind is first.primary_kind
        assert [e.value for e in again.entities] == [e.value for e in first.entities]
        assert again.hints == first.hints


def test_oversized_subject_is_truncated_safely():
    parsed = classify("x" * 9000, max_chars=2000)
    assert len(parsed.original) == 2000
