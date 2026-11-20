"""Stage 1 - subject classification and hint extraction.

Telegraph's `subject` is one free-text string. It can be a bare IP, a card BIN, an email, a
domain or URL, a crypto wallet - or, most often and most valuably, a described scenario such
as "new account, login from a Dutch VPN, first transfer of $4,200 to a new payee".

This stage returns a PRIMARY kind plus EVERY entity embedded in the string, because a
scenario can carry an IP and a BIN inside it and all of the relevant sources should run. A
single-label router would query one source per subject, which is no better than the
single-source miners this service competes with.

It also extracts structured hints with explicit negation handling: "not a new payee", "no
VPN" and "account is 5 years old" must not score like their positive counterparts. Every
tri-state hint is True when stated, False when denied, and None when simply not mentioned -
the rule library treats those three cases differently.
"""
from __future__ import annotations

import ipaddress
import re
from typing import Optional

from .geo import find_countries
from .models import Entity, EntityKind, Hints, ParsedSubject
from .scams import SCAM_CUES

# ---------------------------------------------------------------------------- entity regexes

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6 = re.compile(r"\b(?:[0-9a-f]{1,4}:){2,7}[0-9a-f]{1,4}\b", re.I)
_EMAIL = re.compile(r"\b[\w.+-]+@([\w-]+(?:\.[\w-]+)+)\b")
_URL = re.compile(r"\b(?:https?://|www\.)[^\s,;<>\"')]+", re.I)
#: A bare hostname. The TLD list stays broad but excludes bare numbers and single labels.
_DOMAIN = re.compile(r"\b((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,24})\b", re.I)
_BTC = re.compile(r"\b(?:bc1[a-z0-9]{25,62}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b")
_ETH = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
_TRON = re.compile(r"\bT[a-zA-Z0-9]{33}\b")

#: 13-19 consecutive digits (optionally space/dash grouped): a full card number (PAN).
_PAN = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_DIGITS_6_8 = re.compile(r"\b\d{6,8}\b")

#: Words that make a nearby 6-8 digit number a card BIN rather than an order or account id.
_BIN_CUE = re.compile(r"\b(bin|iin|card|prepaid|visa|mastercard|maestro|amex|issuer|pan)\b", re.I)
#: Words that make it something else entirely - never read these as a BIN.
_NOT_BIN_CUE = re.compile(r"\b(order|invoice|ticket|ref|reference|case|zip|postcode|phone|"
                          r"account\s*(?:no|number|#)|tracking|sku|employee|user\s*id)\b", re.I)

#: A subject with at least this many narrative words is treated as a described scenario even
#: when no cue matched: there is content to report on, we just did not recognise any of it.
DETAILED_SUBJECT_WORDS = 8

# ---------------------------------------------------------------------------- hint regexes

_NEGATORS = re.compile(
    r"\b(?:no|not|never|without|none|neither|nor|non|isn'?t|wasn'?t|aren'?t|weren'?t|"
    r"doesn'?t|didn'?t|don'?t|hasn'?t|haven'?t|cannot|can'?t|lacks|lacking|free\s+of|"
    r"rather\s+than|instead\s+of|no\s+sign\s+of|nothing)\b", re.I)

#: amounts: $4,200 | 4200 USD | USD 4,200 | EUR500 | 4.2k | £300
_AMOUNT = re.compile(
    r"(?:(?P<sym>[$€£¥])\s?(?P<n1>\d[\d,]*(?:\.\d+)?)\s?(?P<k1>[km]\b)?)"
    r"|(?:\b(?P<cur>usd|eur|gbp|jpy|chf|cad|aud|inr)\s?(?P<n2>\d[\d,]*(?:\.\d+)?)\s?(?P<k2>[km]\b)?)"
    r"|(?:\b(?P<n3>\d[\d,]*(?:\.\d+)?)\s?(?P<k3>[km])?\s?(?P<cur2>usd|eur|gbp|jpy|dollars?|euros?|pounds?)\b)",
    re.I)

_CURRENCY_BY_SYMBOL = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}

#: Account age. The number must attach DIRECTLY to an account noun through an age-bearing
#: construction - an earlier version allowed any digits within 24 characters of a cue word,
#: which read "customer called 2 days ago" as a two-day-old account and "14 declined attempts
#: within 3 minutes" as a three-minute-old one. Both were fabricated signals.
_AGE_NOUN = r"(?:account|profile|customer|client|user|member|relationship)"
_AGE_UNIT = r"(?:minute|hour|day|week|month|year)"
_AGE = re.compile(
    # "account is 5 years old" - the trailing "old" is REQUIRED here, otherwise
    # "customer is 30 days overdue" reads as a 30-day-old account
    rf"(?:{_AGE_NOUN}\s+(?:is|was|has\s+been)\s+(?:only|just|barely)?\s*"
    rf"(?P<n1>\d+(?:\.\d+)?)[\s-]*(?P<u1>{_AGE_UNIT})s?[\s-]*old)"
    # "customer of 5 years", "account aged 3 months" - an explicit age-bearing link
    rf"|(?:{_AGE_NOUN}\s+(?:of|aged|age\s+of|open\s+for|active\s+for)\s+"
    rf"(?:only|just|barely)?\s*(?P<n5>\d+(?:\.\d+)?)[\s-]*(?P<u5>{_AGE_UNIT})s?)"
    # "account created 2 days ago", "profile registered 6 months ago", "signed up 3 days ago"
    rf"|(?:(?:{_AGE_NOUN}|signup|sign[\s-]?up)\s+(?:was\s+|were\s+)?"
    rf"(?:created|opened|registered|established|made|added|signed\s*up)\s+"
    rf"(?:only|just|barely)?\s*(?P<n2>\d+(?:\.\d+)?)[\s-]*(?P<u2>{_AGE_UNIT})s?[\s-]*(?:ago|old|back))"
    # "3-day-old account"
    rf"|(?:(?P<n3>\d+(?:\.\d+)?)[\s-]*(?P<u3>{_AGE_UNIT})[\s-]*old\s+{_AGE_NOUN})"
    # "account age: 14 days"
    rf"|(?:{_AGE_NOUN}\s+age\W{{0,4}}(?P<n4>\d+(?:\.\d+)?)\s*(?P<u4>{_AGE_UNIT})s?)",
    re.I)

_UNIT_DAYS = {"minute": 1 / 1440, "hour": 1 / 24, "day": 1.0, "week": 7.0, "month": 30.0, "year": 365.0}

#: "22 consecutive declined authorisations", "14 attempts", "9 failed card payments".
#: Two guards keep money out of this: a currency symbol before the number disqualifies it
#: (so "$45 payment" is never read as 45 attempts), and the counted noun must be PLURAL,
#: because a count of one is not a velocity signal in the first place. Up to three
#: intervening adjectives are allowed so the wording does not have to be anticipated.
#: Nouns that are inherently retry-flavoured: a count of these IS the signal.
_RETRY_NOUNS = r"(?:attempts|tries|retries|declines|declinations)"
#: Nouns that are neutral in themselves - 40 payroll payments is not a velocity signal. A
#: count of these only counts as velocity alongside a failure word or a short time window.
_NEUTRAL_NOUNS = (r"(?:transactions|charges|authorisations|authorizations|auths|payments|"
                  r"swipes|logins|log[\s-]?ins|transfers|orders|requests)")
_FAILURE_WORD = r"(?:failed|declined|rejected|unsuccessful|reversed|blocked|refused|bounced)"

_VELOCITY_N = re.compile(
    r"(?<![$€£¥])(?<![$€£¥]\s)"
    rf"\b(?P<n>\d{{1,5}})\s+(?:[a-z][a-z-]*\s+){{0,3}}?"
    rf"(?:{_RETRY_NOUNS}|{_FAILURE_WORD}\s+{_NEUTRAL_NOUNS}|{_NEUTRAL_NOUNS})\b",
    re.I)

#: A burst window, which makes even a count of ordinary payments a velocity signal.
_BURST_WINDOW = re.compile(
    r"\b(?:in|within|over|across|during)\s+(?:the\s+)?(?:a|an|one|two|three|four|five|\d+)?\s*"
    r"(?:second|minute|hour)s?\b"
    r"|\b(?:in\s+quick\s+succession|back[\s-]to[\s-]back|rapid(?:ly)?|in\s+a\s+burst|"
    r"same\s+(?:second|minute)|seconds\s+apart|minutes\s+apart)\b", re.I)

_PCT = re.compile(r"\b\d{1,3}\s?%")


def _norm_amount(raw: str, mult: Optional[str]) -> float:
    n = float(raw.replace(",", ""))
    if mult:
        n *= 1_000 if mult.lower() == "k" else 1_000_000
    return n


def _window_negated(text: str, start: int, span: int = 32) -> bool:
    """True when a negator sits just before `start` (same clause)."""
    lo = max(0, start - span)
    window = text[lo:start]
    # a clause break cancels the negation ("no VPN. first transfer to a new payee")
    window = re.split(r"[.;!?]|\bbut\b|\bhowever\b|\balthough\b|\bthough\b|\byet\b", window)[-1]
    return bool(_NEGATORS.search(window))


def _tristate(text: str, positive: str, negative: str = "") -> Optional[bool]:
    """Resolve a cue to True (stated), False (denied) or None (not mentioned).

    Order matters. An UN-NEGATED positive wins over a matching negative pattern, because
    subjects routinely state a baseline and then the deviation from it:

        "normally pays an existing payee from the same device, but today sent $5,000
         to a new payee from a new device"

    Letting the negative win there produced "the payee is one the account has paid before"
    and "the activity comes from a device already associated with the account" - two benign
    claims flatly contradicting the subject, which is as dishonest as a fabricated red flag.
    The deviation is what is being asked about, so it takes precedence.

    A positive that IS negated in its own clause ("not a new payee") falls through to the
    negative pattern, so explicit denials still read as denials.
    """
    for m in re.finditer(positive, text, re.I):
        if not _window_negated(text, m.start()):
            return True
    if negative and re.search(negative, text, re.I):
        return False
    # every positive occurrence was negated in its clause
    if re.search(positive, text, re.I):
        return False
    return None


# ---------------------------------------------------------------------------- entity extraction


def _valid_ip(raw: str) -> Optional[str]:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return None
    if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return None
    return str(ip)


def _extract_bin(text: str, consumed: set[str]) -> Optional[str]:
    """Find a card BIN, refusing to read money, order ids or postcodes as one.

    Accepts: an explicit cue nearby ("BIN 457173", "prepaid card 517805"), a full PAN
    (first 6 digits are the BIN), or a subject that is nothing but a 6-8 digit number.
    """
    if _NOT_BIN_CUE.search(text) and not _BIN_CUE.search(text):
        return None

    pan = _PAN.search(text)
    if pan:
        digits = re.sub(r"\D", "", pan.group(0))
        if 13 <= len(digits) <= 19:
            return digits[:6]

    stripped = text.strip()
    if _DIGITS_6_8.fullmatch(stripped):
        return stripped

    for m in _DIGITS_6_8.finditer(text):
        token = m.group(0)
        if token in consumed:
            continue
        # a currency symbol or amount word immediately before means it is money, not a BIN
        before = text[max(0, m.start() - 12):m.start()]
        after = text[m.end():m.end() + 12]
        if re.search(r"[$€£¥]\s?$", before):
            continue
        if re.search(r"^\s*(?:usd|eur|gbp|dollars?|euros?|pounds?)\b", after, re.I):
            continue
        if re.search(r"[.,]\d*$", before) or re.search(r"^[.,]?\d", after):
            continue   # part of a longer number
        # require a card cue somewhere in a sentence that also holds other words
        if _BIN_CUE.search(text):
            return token
    return None


def _extract_entities(text: str) -> tuple[list[Entity], set[str]]:
    entities: list[Entity] = []
    consumed: set[str] = set()

    for m in _URL.finditer(text):
        raw = m.group(0).rstrip(".,)")
        entities.append(Entity(kind=EntityKind.URL, value=raw, raw=raw))
        consumed.add(raw)
        host = re.sub(r"^(?:https?://)?(?:www\.)?", "", raw, flags=re.I).split("/")[0].split(":")[0]
        if host and not _valid_ip(host):
            entities.append(Entity(kind=EntityKind.DOMAIN, value=host.lower(), raw=raw))
            consumed.add(host)

    for m in _EMAIL.finditer(text):
        addr = m.group(0)
        entities.append(Entity(kind=EntityKind.EMAIL, value=addr.lower(), raw=addr))
        consumed.add(addr)
        consumed.add(m.group(1))
        entities.append(Entity(kind=EntityKind.DOMAIN, value=m.group(1).lower(), raw=addr))

    for m in _IPV4.finditer(text):
        ip = _valid_ip(m.group(0))
        if ip:
            entities.append(Entity(kind=EntityKind.IP, value=ip, raw=m.group(0)))
        consumed.add(m.group(0))
    for m in _IPV6.finditer(text):
        ip = _valid_ip(m.group(0))
        if ip:
            entities.append(Entity(kind=EntityKind.IP, value=ip, raw=m.group(0)))
            consumed.add(m.group(0))

    for rx in (_ETH, _TRON, _BTC):
        for m in rx.finditer(text):
            val = m.group(0)
            if val in consumed:
                continue
            entities.append(Entity(kind=EntityKind.WALLET, value=val, raw=val))
            consumed.add(val)

    # bare domains not already picked up via a URL or email
    for m in _DOMAIN.finditer(text):
        host = m.group(1).lower()
        if host in consumed or any(host == e.value for e in entities):
            continue
        if host.endswith("."):
            continue
        entities.append(Entity(kind=EntityKind.DOMAIN, value=host, raw=m.group(0)))
        consumed.add(host)

    bin_value = _extract_bin(text, consumed)
    if bin_value:
        entities.append(Entity(kind=EntityKind.CARD_BIN, value=bin_value, raw=bin_value))

    # de-duplicate on (kind, value), keeping first occurrence
    seen: set[tuple[str, str]] = set()
    unique: list[Entity] = []
    for e in entities:
        key = (e.kind.value, e.value)
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique, consumed


# ---------------------------------------------------------------------------- hints


def _extract_hints(text: str, consumed: set[str]) -> Hints:
    h = Hints()

    # --- money. Skip digits already claimed by an IP/BIN/wallet.
    for m in _AMOUNT.finditer(text):
        raw = m.group("n1") or m.group("n2") or m.group("n3")
        if not raw:
            continue
        if any(raw in c for c in consumed):
            continue
        mult = m.group("k1") or m.group("k2") or m.group("k3")
        try:
            val = _norm_amount(raw, mult)
        except ValueError:
            continue
        h.amounts.append(val)
        if not h.currency:
            sym = m.group("sym")
            cur = (m.group("cur") or m.group("cur2") or "").upper()
            if sym:
                h.currency = _CURRENCY_BY_SYMBOL.get(sym, "")
            elif cur:
                h.currency = {"DOLLAR": "USD", "DOLLARS": "USD", "EURO": "EUR", "EUROS": "EUR",
                              "POUND": "GBP", "POUNDS": "GBP"}.get(cur, cur[:3])

    # --- account age
    am = _AGE.search(text)
    if am:
        n = (am.group("n1") or am.group("n2") or am.group("n3") or am.group("n4")
             or am.group("n5"))
        unit = (am.group("u1") or am.group("u2") or am.group("u3") or am.group("u4")
                or am.group("u5") or "").lower()
        if n and unit in _UNIT_DAYS:
            h.account_age_days = float(n) * _UNIT_DAYS[unit]
    # The denial must attach to an ACCOUNT noun. Bare "existing" used to match "an existing
    # payee" and silently cancel "brand new account" - erasing a real signal and asserting a
    # benign one ("the account is described as established") that the subject never said.
    h.new_account = _tristate(
        text,
        r"\b(?:brand[\s-]?new|newly[\s-]?(?:created|opened|registered)|just\s+(?:created|opened|"
        r"registered|signed\s*up)|fresh(?:ly\s+made)?)\s*(?:account|profile|customer|user)?\b"
        r"|\bnew\s+(?:account|profile|customer|signup|sign[\s-]?up)\b"
        # "a week-old account" is NEW. Bare `old` in the negative below matched the "old
        # account" inside it and asserted the opposite - a false benign claim.
        r"|\b(?:minute|hour|day|week)[\s-]old\s+(?:account|profile|customer|user)\b",
        r"\b(?:long[\s-]?standing|established|existing|legacy|mature|"
        r"(?:year|month|decade)s?[\s-]old)\s+"
        r"(?:account|profile|customer|client|user|member|relationship)\b"
        r"|\bnot\s+a\s+new\s+(?:account|profile|customer|client|user)\b"
        r"|\b(?:account|profile|customer)\s+(?:is|was)\s+(?:not\s+new|established|"
        r"long[\s-]?standing|old)\b",
    )
    if h.account_age_days is not None and h.account_age_days > 180:
        h.new_account = False

    # --- payee / transfer
    h.new_payee = _tristate(
        text,
        r"\bnew\s+(?:payee|beneficiary|recipient|counterparty|destination\s+account)\b"
        r"|\b(?:payee|beneficiary|recipient)\s+(?:is|was)\s+new\b"
        r"|\bfirst[\s-]time\s+(?:payee|beneficiary|recipient)\b"
        r"|\b(?:unrecognis|unrecogniz|unknown)\w*\s+(?:payee|beneficiary|recipient)\b",
        r"\b(?:existing|known|established|recurring|regular|usual|same|trusted|trust|verified|"
        r"trusted)\s+(?:payee|beneficiary|recipient)\b"
        r"|\b(?:payee|beneficiary|recipient)\s+(?:is|was)\s+(?:not\s+new|known|existing|established)\b"
        r"|\bnot\s+a\s+new\s+(?:payee|beneficiary|recipient)\b",
    )
    h.first_transfer = _tristate(
        text,
        r"\bfirst\s+(?:transfer|payment|transaction|wire|order|purchase|withdrawal)\b"
        r"|\b(?:transfer|payment|transaction|wire|purchase)\s+is\s+(?:their|his|her|the)\s+first\b",
        r"\b(?:regular|routine|recurring|repeat|habitual|usual)\s+"
        r"(?:transfer|payment|transaction|wire|order|purchase)\b",
    )

    # --- velocity
    vm = _VELOCITY_N.search(text)
    if vm:
        matched = vm.group(0)
        retry_flavoured = bool(re.search(_RETRY_NOUNS, matched, re.I)
                               or re.search(_FAILURE_WORD, matched, re.I))
        # A plain count of ordinary payments needs a burst window before it means anything:
        # "40 payments to the same employees as last month" is a payroll run, not velocity.
        if retry_flavoured or _BURST_WINDOW.search(text):
            try:
                h.velocity_count = int(vm.group("n"))
            except ValueError:
                pass
    if h.velocity_count is None and re.search(
        r"\b(?:many|numerous|repeated|dozens\s+of|rapid|in\s+quick\s+succession|back[\s-]to[\s-]back|"
        r"burst\s+of|a\s+series\s+of)\s+\w*\s*"
        r"(?:attempts?|transactions?|charges?|declines?|payments?|authorisations?|authorizations?)\b",
        text, re.I,
    ):
        h.velocity_count = -1   # "many", count unknown: treated as a qualitative cue

    h.small_amounts = bool(
        re.search(r"\b(?:small|tiny|micro|low[\s-]value|\$?0?\.\d\d|1\s*(?:dollar|usd|cent))\b", text, re.I)
        and (h.velocity_count is not None or len(h.amounts) > 1)
    ) or bool(h.amounts and h.velocity_count is not None and max(h.amounts) <= 5.0)

    # --- geography (ISO codes, so one country named twice is not read as two)
    h.countries = find_countries(text)
    h.geo_mismatch = _tristate(
        text,
        r"\b(?:geo|country|location|ip|billing|shipping|address)\w*\s*(?:and|vs\.?|versus|/)?\s*"
        r"\w*\s*mismatch\b|\bmismatch(?:ed|ing)?\s+(?:country|countries|geo|location|address)\b"
        r"|\b(?:different|another|two\s+different|separate)\s+countr(?:y|ies)\b"
        r"|\bbilling\s+(?:in|is|address)\b[^.;]{0,48}\bshipping\s+(?:to|in|address)\b"
        # "card issued in Denmark, shipping to Nigeria" - an instrument registered in one
        # place being used or delivered to another is the real mismatch construction
        r"|\b(?:issued|registered|billed|based|opened|held)\s+in\b[^.;]{0,48}"
        r"\b(?:shipping|shipped|delivered|sent|used|spent|accessed)\s+(?:to|in|from)\b"
        r"|\b(?:shipping|shipped|delivered)\s+to\b[^.;]{0,48}"
        r"\b(?:issued|registered|billed|billing|based)\s+in\b"
        r"|\b(?:logged?\s*in|login|signed\s*in|connecting|accessed)\s+from\s+\w+[^.;]{0,40}"
        r"\b(?:but|while|whereas)\b[^.;]{0,48}\b(?:account|card|customer|registered|issued)\b",
        r"\b(?:same|matching|consistent)\s+(?:country|countries|geo|location|region)\b"
        r"|\bcountry\s+matches\b|\bno\s+(?:geo|country|location)\s*\w*\s*mismatch\b",
    )
    # Deliberately NO "two countries named => mismatch" inference: that fabricated a signal
    # for "American Express card used in Germany" and for ordinary cross-border trade.

    # --- anonymisers claimed in the text
    h.anonymiser_claimed = _tristate(
        text,
        r"\b(?:vpn|tor\b|proxy|proxies|anonymis\w+|anonymiz\w+|datacent(?:er|re)\s+ip|"
        r"hosting\s+ip|residential\s+proxy)\b",
        r"\b(?:no|without|not\s+(?:using|behind|on))\s+(?:a\s+)?(?:vpn|tor|proxy|anonymiser|anonymizer)\b"
        r"|\bnot\s+a\s+(?:vpn|proxy|tor)\b|\bno\s+anonymis\w+\b",
    )

    # --- named scam archetypes (see fraudrisk/scams.py). Resolved with the same tri-state
    # discipline as every other cue, so a denial still reads as a denial.
    for field_name, positive, negative in SCAM_CUES:
        setattr(h, field_name, _tristate(text, positive, negative))

    # --- device / credentials / customer standing
    h.device_change = _tristate(
        text,
        r"\b(?:new|unrecognis\w+|unrecogniz\w+|unknown|different|unfamiliar)\s+"
        r"(?:device|phone|handset|browser|user\s*agent|fingerprint|machine)\b"
        r"|\bdevice\s+(?:change|changed|swap|swapped|mismatch)\b",
        r"\b(?:same|usual|known|recognis\w+|recogniz\w+|trusted|previously\s+seen)\s+"
        r"(?:device|phone|browser|machine)\b",
    )
    h.credential_reset = _tristate(
        text,
        r"\b(?:password|credential|pin|2fa|mfa|email\s+address|phone\s+number)\s*\w*\s*"
        r"(?:reset|change[d]?|update[d]?|rotation|rotated|removed|disabled)\b"
        r"|\b(?:reset|changed|disabled|removed)\s+(?:the\s+|their\s+)?"
        r"(?:password|2fa|mfa|pin|credentials?)\b",
        r"\bno\s+(?:password|credential|2fa|mfa)\s*\w*\s*(?:reset|change)\b",
    )
    h.returning_customer = _tristate(
        text,
        r"\b(?:returning|repeat|loyal|long[\s-]?standing|regular|existing)\s+"
        r"(?:customer|client|user|buyer|account\s+holder)\b"
        r"|\b(?:good|clean|unblemished|spotless)\s+(?:history|standing|record)\b"
        r"|\b(?:shopped|ordered|purchased|transacted)\s+(?:here\s+)?(?:many\s+times|regularly|before)\b",
        r"\b(?:first[\s-]time|brand[\s-]?new)\s+(?:customer|client|buyer|user)\b",
    )
    h.chargeback_history = _tristate(
        text,
        r"\b(?:chargeback|dispute|disputed\s+transaction|fraud\s+claim)s?\b",
        r"\bno\s+(?:chargeback|dispute|fraud\s+claim)s?\b|\bnever\s+(?:charged\s*back|disputed)\b",
    )
    # Only genuine pressure or secrecy cues. Bare "immediately" / "right away" describe how
    # fast something happened, not that anyone was pressured into it: "immediately forwarded
    # the funds" is mule behaviour, and claiming social engineering on top of it is invented.
    h.urgency_pressure = _tristate(
        text,
        r"\b(?:urgen\w+|pressur\w+|threaten\w+|coerc\w+|intimidat\w+|deadline|"
        r"final\s+notice|act\s+now|asap|as\s+soon\s+as\s+possible|last\s+chance|"
        r"told\s+(?:not\s+to\s+tell|to\s+keep\s+it)|keep\s+it\s+(?:secret|quiet|"
        r"confidential|between)|don'?t\s+tell|do\s+not\s+tell|in\s+secret|"
        r"sworn\s+to\s+secrecy|account\s+will\s+be\s+(?:closed|suspended|frozen))\b",
        "",
    )
    # The forwarding verb must govern MONEY. "looking forward to a holiday abroad" matched
    # `forward\w*` ... `abroad` and was read as mule pass-through behaviour.
    h.third_party_payout = _tristate(
        text,
        r"\b(?:forward\w*|pass\w*\s+on|relay\w*|transfer\w*\s+out|move[ds]?\s+on|sent\s+on|"
        r"withdraw\w*)\s+(?:the\s+|those\s+|all\s+(?:of\s+)?the\s+|most\s+of\s+the\s+)?"
        r"(?:money|funds?|payment|deposit|balance|cash|proceeds|amount|them|it)\b"
        r"[^.;]{0,45}"
        r"\b(?:third\s+party|another\s+account|someone\s+else|different\s+account|crypto|"
        r"cash|abroad|overseas|onward)\b"
        r"|\b(?:third[\s-]party|someone\s+else'?s?)\s+(?:account|payout|beneficiary|wallet)\b"
        r"|\breceived\s+(?:the\s+)?funds?\b[^.;]{0,45}"
        r"\b(?:forwarded|sent\s+(?:it|them)\s+on|passed\s+(?:it|them)\s+on|withdrew)\b",
        "",
    )
    return h


# ---------------------------------------------------------------------------- vagueness

_VAGUE_ONLY = re.compile(
    r"^(?:please\s+)?(?:can\s+you\s+)?(?:check|assess|verify|review|look\s+(?:at|into)|"
    r"investigate|rate|score|evaluate|is|are|should|do|does|tell\s+me\s+about)?\s*"
    r"(?:this|that|the|a|an|my|his|her|their|it)?\s*"
    r"(?:person|customer|user|account|guy|man|woman|entity|company|business|thing|one|"
    r"transaction|payment|transfer|order|client|individual|name)?\s*"
    r"(?:safe|risky|ok|okay|legit|legitimate|fraud|fraudulent|suspicious|trustworthy|good|bad)?"
    r"\s*[?.!]*$", re.I)

#: a subject with none of these and no entity carries nothing to reason from
_HINT_FIELDS = ("account_age_days", "new_account", "new_payee", "first_transfer", "velocity_count",
                "geo_mismatch", "anonymiser_claimed", "device_change", "credential_reset",
                "returning_customer", "chargeback_history", "urgency_pressure", "third_party_payout",
                "impersonation_of_authority", "payee_details_changed", "otp_or_number_lost",
                "romance_or_grooming", "refund_to_other_instrument", "mule_recruitment",
                "untraceable_payment_demand", "coached_against_bank", "disputed_but_consistent",
                "stolen_or_synthetic_identity", "inbound_then_onward")


def _has_any_hint(h: Hints) -> bool:
    if h.amounts or h.countries or h.small_amounts:
        return True
    return any(getattr(h, f) is not None for f in _HINT_FIELDS)


def _narrative_words(text: str, entities: list[Entity]) -> int:
    """Count words that are NOT part of an extracted identifier.

    Without this, "user@mail.example.com" looks like three words of narrative and would be
    routed as a scenario instead of an email.
    """
    stripped = text
    for e in sorted(entities, key=lambda x: -len(x.raw or x.value)):
        for token in (e.raw, e.value):
            if token:
                stripped = stripped.replace(token, " ")
    return len(re.findall(r"[a-z]{3,}", stripped, re.I))


def _primary_kind(entities: list[Entity], text: str, has_hints: bool) -> EntityKind:
    """The dominant kind. A bare identifier is that identifier; anything with surrounding
    narrative is a SCENARIO, so the behavioural rules run alongside the lookups."""
    words = _narrative_words(text, entities)
    for kind in (EntityKind.IP, EntityKind.CARD_BIN, EntityKind.URL, EntityKind.EMAIL,
                 EntityKind.WALLET, EntityKind.DOMAIN):
        matches = [e for e in entities if e.kind == kind]
        if matches and words <= 2 and len(entities) <= 2:
            return kind
    if has_hints or words >= 3:
        return EntityKind.SCENARIO
    if entities:
        return entities[0].kind
    return EntityKind.UNKNOWN


def classify(subject: str, max_chars: int = 2000) -> ParsedSubject:
    """Pure function: string in, routing decision + hints out. No I/O, no model."""
    original = (subject or "").strip()[:max_chars]
    normalized = re.sub(r"\s+", " ", original).strip().lower()

    if not original:
        return ParsedSubject(original=original, normalized=normalized,
                             primary_kind=EntityKind.UNKNOWN, assessable=False,
                             vague_reason="No subject was provided.")

    entities, consumed = _extract_entities(original)
    hints = _extract_hints(original, consumed)
    has_hints = _has_any_hint(hints)
    kind = _primary_kind(entities, original, has_hints)

    assessable = True
    vague = ""
    if not entities and not has_hints:
        # A subject with real narrative in it IS assessable - we simply recognised nothing in
        # it. Telling a detailed scenario that it "describes no behaviour" is false, and the
        # distinction matters: one answer is "you gave me nothing", the other is "I checked
        # and none of my indicators matched", which the pipeline words separately.
        words = _narrative_words(original, entities)
        if words >= DETAILED_SUBJECT_WORDS:
            vague = ""
        else:
            assessable = False
            if _VAGUE_ONLY.match(normalized) or len(normalized) < 12:
                vague = ("The subject names no checkable identifier (no IP, domain, card BIN, "
                         "email or wallet) and describes no behaviour that could be assessed.")
            else:
                vague = ("The subject is too short and unspecific to assess: it names no "
                         "checkable identifier and describes no particular behaviour.")

    return ParsedSubject(original=original, normalized=normalized, primary_kind=kind,
                         entities=entities, hints=hints, assessable=assessable, vague_reason=vague)


def cache_key(parsed: ParsedSubject) -> str:
    """Stable key for the assessment cache: the normalized subject text."""
    return normalized_key(parsed.normalized)


def normalized_key(normalized: str) -> str:
    return re.sub(r"[^\w@:./$-]+", " ", normalized).strip()
