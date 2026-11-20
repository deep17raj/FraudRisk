"""Behavioural pattern rules over a described scenario - the source that carries most
subjects, because most callers describe a transaction rather than hand over an identifier.

Each rule is a small pure function of the extracted `Hints`. A rule fires only on an
EXPLICIT cue: `new_payee is True` fires, `new_payee is False` ("an existing payee") emits a
benign observation, and `new_payee is None` ("not mentioned") emits nothing at all. That
three-way discipline is what stops the service inventing red flags from silence, and it is
also what makes the deterministic path safe to fall back on when the LLM is unavailable.

Rules also vote for a fraud PATTERN. The votes are used for the templated summary when the
reasoning layer cannot run; when it can, the model names the pattern itself from the same
closed set.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from ..config import Settings
from ..models import EntityKind, Family, Hints, ParsedSubject, Signal
from ..patterns import PATTERN_VOTE_WEIGHT, SIGNAL_PATTERN_AFFINITY, Pattern
from .base import Source

RULES_RELIABILITY = 0.85

#: A transfer at or above this (in the stated currency) counts as high-value for a consumer.
HIGH_VALUE = 2_000.0
VERY_HIGH_VALUE = 10_000.0
#: An account younger than this is "new" for fraud purposes.
NEW_ACCOUNT_DAYS = 30.0
VERY_NEW_ACCOUNT_DAYS = 7.0
#: Attempt counts that read as automated testing rather than a fumbling customer.
VELOCITY_SUSPICIOUS = 5
VELOCITY_HIGH = 10


@dataclass
class Rule:
    id: str
    severity: float
    family: Family
    pattern: Optional[Pattern]
    fn: Callable[[Hints, ParsedSubject], Optional[str]]
    reliability: float = RULES_RELIABILITY
    benign: bool = False
    #: A few words naming the signal, so the templated summary can stay to one sentence.
    label: str = ""


def _money(h: Hints) -> str:
    amt = h.max_amount
    cur = h.currency or "USD"
    sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get(cur, "")
    return f"{sym}{amt:,.0f}" if sym else f"{amt:,.0f} {cur}"


def _age_phrase(days: float) -> str:
    if days < 1:
        return "less than a day"
    if days < 60:
        return f"{days:.0f} days"
    if days < 730:
        return f"{days / 30:.0f} months"
    return f"{days / 365:.0f} years"


# ------------------------------------------------------------------ rule bodies (risk)

def _r_very_new_account(h, p):
    if h.account_age_days is not None and h.account_age_days <= VERY_NEW_ACCOUNT_DAYS:
        return (f"The account is only {_age_phrase(h.account_age_days)} old, leaving no "
                f"behavioural history to judge this activity against.")
    if h.account_age_days is None and h.new_account is True:
        return ("The account is described as newly created, so there is no established "
                "behavioural history behind this activity.")
    return None


def _r_new_account(h, p):
    if (h.account_age_days is not None
            and VERY_NEW_ACCOUNT_DAYS < h.account_age_days <= NEW_ACCOUNT_DAYS):
        return (f"The account is {_age_phrase(h.account_age_days)} old, still inside the window "
                f"where fraudulent signups are typically monetised.")
    return None


def _r_first_transfer_new_payee(h, p):
    if h.new_payee is True and h.first_transfer is True:
        return ("This is a first transfer to a payee the account has never paid before - the "
                "single most common shape of both takeover cash-out and scam-induced payment.")
    return None


def _r_new_payee(h, p):
    if h.new_payee is True and h.first_transfer is not True:
        return "The funds are going to a payee not previously seen on this account."
    return None


def _r_first_transfer(h, p):
    if h.first_transfer is True and h.new_payee is not True:
        return "This is the first outbound payment on the account."
    return None


def _r_high_value(h, p):
    amt = h.max_amount
    if amt >= VERY_HIGH_VALUE:
        return (f"The amount, {_money(h)}, is far above routine consumer activity and would "
                f"clear most accounts in a single movement.")
    if amt >= HIGH_VALUE:
        return (f"The amount, {_money(h)}, is high-value for a consumer payment.")
    return None


def _r_velocity(h, p):
    n = h.velocity_count
    if n is None:
        return None
    if n == -1:
        return ("The subject describes repeated attempts in quick succession, a machine-driven "
                "rather than human cadence.")
    if n >= VELOCITY_HIGH:
        return (f"{n} attempts on one instrument or account is an automated cadence, not a "
                f"customer retrying a typo.")
    if n >= VELOCITY_SUSPICIOUS:
        return f"{n} successive attempts is well above a normal retry pattern."
    return None


def _r_card_testing_shape(h, p):
    if h.small_amounts and (h.velocity_count is not None):
        return ("Many low-value attempts in sequence is the signature of card testing: "
                "validating stolen card numbers with charges small enough to escape notice.")
    return None


def _r_anonymiser(h, p):
    if h.anonymiser_claimed is True:
        return ("The session is described as arriving over a VPN, Tor or proxy, so the true "
                "origin and any device/geo history are concealed.")
    return None


def _r_geo_mismatch(h, p):
    if h.geo_mismatch is True:
        where = ", ".join(c.upper() for c in h.countries[:3])
        tail = f" ({where})" if where else ""
        return (f"The locations involved do not agree{tail}: the connection, the account and the "
                f"payment instrument point to different places.")
    return None


def _r_device_change(h, p):
    if h.device_change is True:
        return ("The activity comes from a device the account has not used before, which is the "
                "usual first visible step of a takeover.")
    return None


def _r_credential_reset(h, p):
    if h.credential_reset is not True:
        return None
    # Only claim the cash-out sequence when money actually left. A password reset next to an
    # INBOUND payment is still a takeover indicator, but calling it a "lock-out step before
    # the cash-out" would assert an outbound movement the subject never described.
    outbound = (h.new_payee is True or h.first_transfer is True
                or h.third_party_payout is True or h.max_amount >= HIGH_VALUE)
    if outbound:
        return ("Credentials or contact details were changed shortly before the money moved - "
                "the classic lock-out step that precedes a takeover cash-out.")
    return ("Credentials or contact details were changed on this account, which is how an "
            "attacker locks out the genuine owner after gaining access.")


def _r_ato_combo(h, p):
    if h.credential_reset is True and h.device_change is True:
        return ("A credential change and an unrecognised device together, followed by an "
                "outbound payment, is the full account-takeover sequence rather than an "
                "isolated anomaly.")
    return None


def _r_third_party_payout(h, p):
    if h.third_party_payout is True:
        return ("Funds are being passed straight on to a third party rather than used by the "
                "account holder - the defining behaviour of a mule account.")
    return None


def _r_urgency(h, p):
    if h.urgency_pressure is True:
        return ("The request carries urgency, secrecy or pressure, which is how a victim is "
                "driven to authorise a payment they would otherwise question.")
    return None


def _r_chargebacks(h, p):
    if h.chargeback_history is True:
        return ("The account has prior chargebacks or disputes, raising the odds that this is "
                "first-party (friendly) fraud rather than a third-party attack.")
    return None


# ------------------------------------------------------------------ rule bodies (benign)

def _b_established_account(h, p):
    if h.account_age_days is not None and h.account_age_days > 365:
        return (f"The account has {_age_phrase(h.account_age_days)} of history, giving a "
                f"settled baseline to compare this activity against.")
    if h.new_account is False and h.account_age_days is None:
        return "The account is described as established rather than newly created."
    return None


def _b_known_payee(h, p):
    if h.new_payee is False:
        return "The payee is one the account has paid before, not a new destination."
    return None


def _b_returning_customer(h, p):
    if h.returning_customer is True:
        return "The subject is a returning customer with existing transaction history."
    return None


def _b_same_device(h, p):
    if h.device_change is False:
        return "The activity comes from a device already associated with the account."
    return None


def _b_no_chargebacks(h, p):
    if h.chargeback_history is False:
        return "The account has no prior chargebacks or disputes."
    return None


def _b_no_anonymiser(h, p):
    if h.anonymiser_claimed is False:
        return "The connection is described as direct, with no VPN, Tor or proxy in between."
    return None


def _b_geo_consistent(h, p):
    if h.geo_mismatch is False:
        return "The locations involved are consistent with one another."
    return None


def _b_routine_amount(h, p):
    if h.amounts and h.max_amount < 500 and h.velocity_count is None:
        return f"The amount, {_money(h)}, is routine for consumer activity."
    return None


RULES: list[Rule] = [
    # --- risk-bearing
    Rule("behav.very_new_account", 0.45, Family.BEHAVIOR, Pattern.SYNTHETIC_IDENTITY, _r_very_new_account, label="a newly created account with no history"),
    Rule("behav.new_account", 0.30, Family.BEHAVIOR, Pattern.SYNTHETIC_IDENTITY, _r_new_account, label="an account still inside the fraud-monetisation window"),
    Rule("behav.first_transfer_new_payee", 0.50, Family.BEHAVIOR, Pattern.ACCOUNT_TAKEOVER,
         _r_first_transfer_new_payee, label="a first transfer to a never-before-paid payee"),
    Rule("behav.new_payee", 0.32, Family.BEHAVIOR, Pattern.ACCOUNT_TAKEOVER, _r_new_payee, label="a payee not previously seen on the account"),
    Rule("behav.first_transfer", 0.25, Family.BEHAVIOR, None, _r_first_transfer, label="the account's first outbound payment"),
    Rule("behav.high_value", 0.35, Family.BEHAVIOR, None, _r_high_value, label="a high-value amount for a consumer payment"),
    Rule("behav.velocity", 0.48, Family.BEHAVIOR, Pattern.CARD_TESTING, _r_velocity, label="an automated attempt cadence"),
    Rule("behav.card_testing_shape", 0.55, Family.BEHAVIOR, Pattern.CARD_TESTING, _r_card_testing_shape, label="many low-value attempts in sequence"),
    Rule("behav.anonymiser", 0.45, Family.ANONYMITY, Pattern.ANONYMISED_ACCESS, _r_anonymiser, label="a session arriving over a VPN, Tor or proxy"),
    Rule("behav.geo_mismatch", 0.38, Family.BEHAVIOR, None, _r_geo_mismatch, label="locations that do not agree"),
    Rule("behav.device_change", 0.40, Family.BEHAVIOR, Pattern.ACCOUNT_TAKEOVER, _r_device_change, label="an unrecognised device"),
    Rule("behav.credential_reset", 0.52, Family.BEHAVIOR, Pattern.ACCOUNT_TAKEOVER, _r_credential_reset, label="a credential change just before the money moved"),
    Rule("behav.ato_sequence", 0.66, Family.BEHAVIOR, Pattern.ACCOUNT_TAKEOVER, _r_ato_combo, label="a credential change and an unrecognised device together"),
    Rule("behav.third_party_payout", 0.58, Family.BEHAVIOR, Pattern.MULE_ACCOUNT, _r_third_party_payout, label="funds passed straight on to a third party"),
    Rule("behav.urgency", 0.46, Family.BEHAVIOR, Pattern.SOCIAL_ENGINEERING, _r_urgency, label="urgency, secrecy or pressure on the payer"),
    Rule("behav.chargebacks", 0.40, Family.BEHAVIOR, Pattern.FIRST_PARTY_FRAUD, _r_chargebacks, label="prior chargebacks or disputes"),
    # --- benign observations: reported, never scored
    Rule("behav.established_account", 0.0, Family.BEHAVIOR, None, _b_established_account, benign=True, label="an account with settled history"),
    Rule("behav.known_payee", 0.0, Family.BEHAVIOR, None, _b_known_payee, benign=True, label="a payee the account has paid before"),
    Rule("behav.returning_customer", 0.0, Family.BEHAVIOR, None, _b_returning_customer, benign=True, label="a returning customer"),
    Rule("behav.same_device", 0.0, Family.BEHAVIOR, None, _b_same_device, benign=True, label="a device already linked to the account"),
    Rule("behav.no_chargebacks", 0.0, Family.BEHAVIOR, None, _b_no_chargebacks, benign=True, label="no prior chargebacks"),
    Rule("behav.no_anonymiser", 0.0, Family.ANONYMITY, None, _b_no_anonymiser, benign=True, label="a direct connection with no anonymiser"),
    Rule("behav.geo_consistent", 0.0, Family.BEHAVIOR, None, _b_geo_consistent, benign=True, label="consistent locations"),
    Rule("behav.routine_amount", 0.0, Family.BEHAVIOR, None, _b_routine_amount, benign=True, label="a routine amount"),
]


def evaluate_rules(parsed: ParsedSubject) -> list[Signal]:
    """Pure: hints in, signals out. Used directly by tests and by the offline fallback path."""
    out: list[Signal] = []
    for rule in RULES:
        text = rule.fn(parsed.hints, parsed)
        if not text:
            continue
        out.append(Signal(
            id=rule.id, text=text, label=rule.label, severity=rule.severity,
            reliability=rule.reliability, source="behaviour_rules", family=rule.family,
            benign=rule.benign))
    return out


def vote_pattern(signals: list[Signal]) -> Pattern:
    """Pick the fraud pattern the gathered signals collectively point at.

    Covers both the rule library and the lookup sources (feed hits, BIN metadata, name
    heuristics), so the deterministic fallback can still name the pattern for, say, a
    confirmed phishing URL where no behavioural rule fired at all.
    """
    by_id = {r.id: r for r in RULES}
    tally: dict[Pattern, float] = {}
    for s in signals:
        if s.benign or s.weight <= 0:
            continue
        rule = by_id.get(s.id)
        pattern = rule.pattern if rule is not None else SIGNAL_PATTERN_AFFINITY.get(s.id)
        if pattern is None:
            continue
        tally[pattern] = tally.get(pattern, 0.0) + s.weight * PATTERN_VOTE_WEIGHT.get(pattern, 1.0)
    if not tally:
        return Pattern.NONE
    # highest accumulated weight wins; ties break on the pattern's declaration order
    best = max(tally.items(), key=lambda kv: (kv[1], -list(Pattern).index(kv[0])))
    return best[0]


class BehaviourRuleSource(Source):
    """The rule library as a Source. Offline, so it always runs - even when the network is down."""

    name = "behaviour_rules"
    handles = (EntityKind.SCENARIO,)
    reliability = RULES_RELIABILITY
    offline = True

    def enabled(self, settings: Settings) -> bool:
        return True

    def relevant(self, parsed: ParsedSubject) -> bool:
        # Hints can be present even when the primary kind is an identifier
        # ("457173, 14 declined attempts"), so route on the hints, not the label.
        return True

    async def collect(self, parsed, client, timeout, settings=None):
        return evaluate_rules(parsed)


# ------------------------------------------------------------------ named scam archetypes
# These read the cues extracted by fraudrisk/scams.py. Each is a well-documented fraud
# typology, so the severities are higher than the generic cues: "the supplier emailed new
# bank details" is not a weak hint, it is the whole of an invoice-redirection attack.

def _r_impersonation(h, p):
    if h.impersonation_of_authority is True:
        return ("Someone is impersonating a bank, tax authority or other trusted body to "
                "direct this payment - the opening move of a safe-account or authority scam.")
    return None


def _r_payee_details_changed(h, p):
    if h.payee_details_changed is True:
        return ("Bank or payee details were changed shortly before payment, which is the "
                "mechanism of invoice redirection and payroll diversion fraud.")
    return None


def _r_otp_lost(h, p):
    if h.otp_or_number_lost is True:
        return ("Control of the phone number or one-time passcodes has moved to someone else, "
                "which defeats SMS-based authentication and is the hallmark of a SIM swap.")
    return None


def _r_romance(h, p):
    if h.romance_or_grooming is True:
        return ("The payment is going to someone known only through an online relationship "
                "who has avoided verification - the grooming pattern behind romance fraud.")
    return None


def _r_refund_other_instrument(h, p):
    if h.refund_to_other_instrument is True:
        return ("A refund of an overpayment is being directed to a different instrument from "
                "the one that paid, which launders value out of a stolen payment method.")
    return None


def _r_mule_recruitment(h, p):
    if h.mule_recruitment is True:
        return ("The account holder was recruited to receive and pass on other people's funds "
                "for a cut, which is how mule networks are staffed.")
    return None


def _r_untraceable_demand(h, p):
    if h.untraceable_payment_demand is True:
        return ("Payment is demanded in gift cards, vouchers or crypto - instruments chosen "
                "because they are irreversible and untraceable, never by a genuine creditor.")
    return None


def _r_coached(h, p):
    if h.coached_against_bank is True:
        return ("The payer has been coached on what to tell the bank, which is deliberate "
                "defeat of fraud controls and strongly indicates a scam in progress.")
    return None


def _r_disputed_but_consistent(h, p):
    if h.disputed_but_consistent is True:
        return ("The transaction is disputed although the device, location or behaviour match "
                "the customer's established pattern, which points to first-party rather than "
                "third-party fraud.")
    return None


def _r_stolen_identity(h, p):
    if h.stolen_or_synthetic_identity is True:
        return ("The identity behind this subject is reported as stolen, fabricated or part "
                "real and part invented, which is the construction of a synthetic identity.")
    return None


def _r_inbound_then_onward(h, p):
    if h.inbound_then_onward is True:
        return ("Funds were received and then moved straight on rather than used, which is "
                "the pass-through behaviour that defines a mule account.")
    return None


RULES.extend([
    Rule("scam.impersonation", 0.62, Family.BEHAVIOR, Pattern.SOCIAL_ENGINEERING,
         _r_impersonation, label="impersonation of a bank or authority"),
    Rule("scam.payee_details_changed", 0.58, Family.BEHAVIOR, Pattern.SOCIAL_ENGINEERING,
         _r_payee_details_changed, label="payment details changed just before payment"),
    Rule("scam.sim_swap", 0.64, Family.BEHAVIOR, Pattern.ACCOUNT_TAKEOVER,
         _r_otp_lost, label="loss of control of the phone number or passcodes"),
    Rule("scam.romance", 0.56, Family.BEHAVIOR, Pattern.SOCIAL_ENGINEERING,
         _r_romance, label="an unverified online relationship behind the payment"),
    Rule("scam.refund_other_instrument", 0.52, Family.BEHAVIOR, Pattern.FIRST_PARTY_FRAUD,
         _r_refund_other_instrument, label="a refund directed to a different instrument"),
    Rule("scam.mule_recruitment", 0.62, Family.BEHAVIOR, Pattern.MULE_ACCOUNT,
         _r_mule_recruitment, label="recruitment to receive and pass on funds"),
    Rule("scam.untraceable_demand", 0.60, Family.BEHAVIOR, Pattern.SOCIAL_ENGINEERING,
         _r_untraceable_demand, label="payment demanded in gift cards or crypto"),
    Rule("scam.coached", 0.66, Family.BEHAVIOR, Pattern.SOCIAL_ENGINEERING,
         _r_coached, label="the payer coached on what to tell the bank"),
    Rule("scam.disputed_but_consistent", 0.50, Family.BEHAVIOR, Pattern.FIRST_PARTY_FRAUD,
         _r_disputed_but_consistent, label="a dispute despite matching device and behaviour"),
    Rule("scam.stolen_identity", 0.60, Family.IDENTITY, Pattern.SYNTHETIC_IDENTITY,
         _r_stolen_identity, label="a stolen or fabricated identity"),
    Rule("scam.inbound_then_onward", 0.58, Family.BEHAVIOR, Pattern.MULE_ACCOUNT,
         _r_inbound_then_onward, label="funds received and immediately passed on"),
])
