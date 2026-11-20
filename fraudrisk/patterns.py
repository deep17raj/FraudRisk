"""The closed set of fraud patterns the reasoning layer may name.

Naming the pattern is the thing a fraud analyst does that a feed lookup cannot. The set is
closed (an enum, validated in code) so the model cannot invent a pattern name, and each
pattern carries a gloss used when the LLM is unavailable or times out.
"""
from __future__ import annotations

from enum import Enum


class Pattern(str, Enum):
    ACCOUNT_TAKEOVER = "account takeover"
    CARD_TESTING = "card testing"
    SYNTHETIC_IDENTITY = "synthetic identity"
    PHISHING = "phishing"
    MULE_ACCOUNT = "money mule activity"
    FIRST_PARTY_FRAUD = "first-party fraud"
    SOCIAL_ENGINEERING = "authorised push payment scam"
    ANONYMISED_ACCESS = "anonymised access"
    NONE = "no identifiable pattern"


#: How each pattern reads inside a one-line fallback summary.
PATTERN_GLOSS: dict[Pattern, str] = {
    Pattern.ACCOUNT_TAKEOVER: "consistent with account takeover",
    Pattern.CARD_TESTING: "consistent with card testing",
    Pattern.SYNTHETIC_IDENTITY: "consistent with a synthetic identity",
    Pattern.PHISHING: "consistent with phishing",
    Pattern.MULE_ACCOUNT: "consistent with money mule activity",
    Pattern.FIRST_PARTY_FRAUD: "consistent with first-party fraud",
    Pattern.SOCIAL_ENGINEERING: "consistent with an authorised push payment scam",
    Pattern.ANONYMISED_ACCESS: "consistent with deliberately anonymised access",
    Pattern.NONE: "not matching one specific fraud pattern",
}


#: Vote weighting used when the deterministic path picks a pattern.
#: "Anonymised access" describes HOW a session reached us, not what scheme is being run, so
#: it is discounted: it wins only when nothing else is in the running (a bare Tor IP), and
#: steps aside for a real scheme (account takeover, card testing) when one is also indicated.
PATTERN_VOTE_WEIGHT: dict[Pattern, float] = {Pattern.ANONYMISED_ACCESS: 0.5}

#: Pattern affinity for signals that do NOT come from the behavioural rule library (feed
#: hits, BIN metadata, name heuristics). Without this the deterministic fallback summary
#: could name no pattern for a confirmed phishing URL, which is the one thing it should name.
SIGNAL_PATTERN_AFFINITY: dict[str, Pattern] = {
    "phish.feed_hit": Pattern.PHISHING,
    "domain.brand_lure": Pattern.PHISHING,
    "domain.brand_term": Pattern.PHISHING,
    "domain.punycode": Pattern.PHISHING,
    "domain.abused_tld": Pattern.PHISHING,
    "domain.many_hyphens": Pattern.PHISHING,
    "anon.tor_exit": Pattern.ANONYMISED_ACCESS,
    "anon.tor_vendor": Pattern.ANONYMISED_ACCESS,
    "anon.proxy_vendor": Pattern.ANONYMISED_ACCESS,
    "anon.vpn_asn": Pattern.ANONYMISED_ACCESS,
    "anon.hosting_asn": Pattern.ANONYMISED_ACCESS,
    "card.prepaid_bin": Pattern.CARD_TESTING,
    "card.country_mismatch": Pattern.CARD_TESTING,
    "identity.disposable_email": Pattern.SYNTHETIC_IDENTITY,
    "identity.generated_local_part": Pattern.SYNTHETIC_IDENTITY,
}


def parse_pattern(raw: str | None) -> Pattern:
    """Map a model-supplied string onto the closed set; anything unrecognised becomes NONE."""
    if not raw:
        return Pattern.NONE
    key = raw.strip().lower()
    for p in Pattern:
        if key == p.value:
            return p
    # tolerate the enum member name (CARD_TESTING) and minor wording drift
    compact = key.replace("-", " ").replace("_", " ").strip()
    for p in Pattern:
        if compact == p.name.lower().replace("_", " "):
            return p
    for p in Pattern:
        if p is not Pattern.NONE and (p.value in compact or compact in p.value):
            return p
    return Pattern.NONE
