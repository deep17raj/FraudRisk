"""Phishing-feed membership and domain-shape reputation.

Two independent things live here, deliberately in two families:
  * `PhishFeedSource` (PHISHING) - is this exact URL or host on OpenPhish's live feed? A fact.
  * `DomainHeuristicSource` (DOMAIN) - does the name itself look like a credential-harvesting
    lure: a trusted brand glued to an unrelated domain, a free/abused TLD, punycode, an
    excess of hyphens or a raw IP in place of a hostname? Inference, scored lower and worded
    as a description of the name, not a verdict about the site.
"""
from __future__ import annotations

import re

from ..config import Settings
from ..models import EntityKind, Family, Signal
from .base import Source
from .feeds import FeedRegistry

#: Actual BRAND names impersonated in payment and account-takeover phishing. Generic security
#: words do not belong here: with "gov", "bank", "secure", "login" and "account" in this list,
#: the source flagged usa.gov, wellsfargo.com, amazon.de and secure.example.com as brand
#: impersonation - four fabricated signals against entirely legitimate names.
_BRANDS = re.compile(
    r"(paypal|stripe|binance|coinbase|kraken|metamask|blockchain|revolut|monzo|starling|"
    r"wise|venmo|cashapp|zelle|chase|hsbc|barclays|santander|citibank|wellsfargo|natwest|"
    r"lloyds|amex|americanexpress|visa|mastercard|apple|icloud|microsoft|office365|outlook|"
    r"google|gmail|amazon|netflix|spotify|dhl|fedex|usps|hmrc|dvla|linkedin|instagram|"
    r"facebook|whatsapp|steam|binance)", re.I)

#: Words that are suspicious only IN COMBINATION with a brand or an abused TLD. On their own
#: they are ordinary English used by countless legitimate hosts.
_LURE_WORDS = re.compile(
    r"\b(secure|security|verify|verification|login|signin|sign-in|account|update|confirm|"
    r"validate|auth|authenticate|recover|unlock|support|billing|payment|alert|suspended|"
    r"wallet|bank|gov)\b", re.I)

#: TLDs that are free or cheap to register and heavily abused for throwaway phishing hosts.
_ABUSED_TLDS = {"tk", "ml", "ga", "cf", "gq", "top", "xyz", "club", "work", "click", "link",
                "country", "stream", "download", "loan", "icu", "rest", "cyou", "sbs", "lol"}

#: Hosts that are legitimately brand-adjacent; never flag the brand's own domain.
_LEGIT_SUFFIXES = (
    "paypal.com", "stripe.com", "binance.com", "coinbase.com", "revolut.com", "wise.com",
    "chase.com", "hsbc.com", "barclays.co.uk", "apple.com", "icloud.com", "microsoft.com",
    "live.com", "outlook.com", "google.com", "gmail.com", "amazon.com", "netflix.com",
    "dhl.com", "fedex.com", "ups.com", "usps.com", "gov.uk", "irs.gov", "visa.com",
    "mastercard.com", "americanexpress.com", "metamask.io",
)


#: Two-label public suffixes. Without these, `_registrable("google.co.uk")` returns "co.uk"
#: and the second-level label looks like "co", so Google's own UK domain reads as a brand
#: planted outside its registrable domain.
_MULTI_SUFFIXES = {
    "co.uk", "org.uk", "gov.uk", "ac.uk", "me.uk", "net.uk", "sch.uk",
    "com.au", "net.au", "org.au", "gov.au", "edu.au", "co.nz", "org.nz", "govt.nz",
    "co.za", "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp", "com.br", "com.mx", "com.ar",
    "com.tr", "com.cn", "com.hk", "com.sg", "com.my", "co.in", "co.id", "co.kr",
    "com.ph", "com.vn", "com.pk", "com.ng", "com.gh", "co.ke", "com.eg", "com.sa",
    "gov.in", "nic.in", "com.pl", "com.ua", "com.ru", "co.il", "com.tw", "co.th",
}


def _split_host(host: str) -> tuple[str, str, str]:
    """Return (registrable_domain, second_level_label, effective_tld)."""
    parts = host.lower().strip(".").split(".")
    if len(parts) < 2:
        return host.lower(), host.lower(), ""
    if len(parts) >= 3 and ".".join(parts[-2:]) in _MULTI_SUFFIXES:
        suffix = ".".join(parts[-2:])
        return ".".join(parts[-3:]), parts[-3], suffix
    return ".".join(parts[-2:]), parts[-2], parts[-1]


def _registrable(host: str) -> str:
    return _split_host(host)[0]


def _is_legit(host: str) -> bool:
    h = host.lower().strip(".")
    return any(h == s or h.endswith("." + s) for s in _LEGIT_SUFFIXES)


class PhishFeedSource(Source):
    """Exact membership of the OpenPhish live feed."""

    name = "openphish"
    handles = (EntityKind.URL, EntityKind.DOMAIN)
    reliability = 0.92

    def __init__(self, feeds: FeedRegistry) -> None:
        super().__init__()
        self.feeds = feeds

    def enabled(self, settings: Settings) -> bool:
        return self.feeds.phish is not None

    async def collect(self, parsed, client, timeout, settings=None):
        feed = self.feeds.phish
        if feed is None or not feed.ready:
            return []
        out: list[Signal] = []
        seen: set[str] = set()
        for ent in parsed.entities:
            if ent.kind not in (EntityKind.URL, EntityKind.DOMAIN):
                continue
            needle = ent.value.lower()
            if needle in seen:
                continue
            seen.add(needle)
            if needle in feed:
                what = "URL" if ent.kind == EntityKind.URL else "host"
                out.append(Signal(
                    id="phish.feed_hit",
                label="a live phishing-feed hit on this exact address",
                    text=f"The {what} {ent.value} appears on OpenPhish's live phishing feed as an "
                         f"active credential-harvesting page.",
                    severity=0.92, reliability=self.reliability, source=self.name,
                    family=Family.PHISHING, verified=True))
                break
        return out


class DomainHeuristicSource(Source):
    """Offline reputation of the NAME itself. Runs with no network call."""

    name = "domain_shape"
    handles = (EntityKind.URL, EntityKind.DOMAIN, EntityKind.EMAIL)
    reliability = 0.62
    offline = True

    def enabled(self, settings: Settings) -> bool:
        return True

    async def collect(self, parsed, client, timeout, settings=None):
        out: list[Signal] = []
        hosts: list[str] = []
        for ent in parsed.entities:
            if ent.kind in (EntityKind.DOMAIN,):
                hosts.append(ent.value.lower())
        for host in dict.fromkeys(hosts):
            out.extend(self._host_signals(host, parsed))
            break   # one host is enough; more would just stack the same family
        return out

    def _host_signals(self, host: str, parsed) -> list[Signal]:
        out: list[Signal] = []
        if _is_legit(host):
            out.append(Signal(
                id="domain.known_legit",
                label="a genuine provider domain",
                text=f"{host} is the genuine domain of a well-known provider.",
                severity=0.0, reliability=self.reliability, source=self.name,
                family=Family.DOMAIN, benign=True))
            return out

        reg, sld, tld = _split_host(host)
        brand = _BRANDS.search(host)
        abused_tld = tld in _ABUSED_TLDS
        lures = {m.group(0).lower() for m in _LURE_WORDS.finditer(host)}
        hyphens = host.count("-")

        if brand:
            name = brand.group(0).lower()
            if sld == name:
                # the brand name IS the registrable domain: amazon.de, wellsfargo.com,
                # google.co.uk - presumed genuine, no signal
                pass
            elif name not in sld:
                # the brand sits in a subdomain of someone else's domain, which is the
                # classic impersonation shape: paypal-verify.secure-login.tk
                out.append(Signal(
                    id="domain.brand_lure",
                label="a brand name planted outside its own domain",
                    text=f"{host} carries the brand name \"{brand.group(0)}\" while its "
                         f"registrable domain is {reg}, which that brand does not operate - "
                         f"the standard shape of a brand-impersonation phishing host.",
                    severity=0.62, reliability=self.reliability, source=self.name,
                    family=Family.DOMAIN))
            elif abused_tld or len(lures) >= 2 or hyphens >= 3:
                # the brand is inside a longer registrable domain AND something else is off.
                # Corroboration is required here on purpose: microsoftonline.com and
                # paypalobjects.com are genuine, so the brand alone cannot carry a signal.
                out.append(Signal(
                    id="domain.brand_term",
                label="a brand name embedded in an unrelated domain",
                    text=f"{host} embeds the brand name \"{brand.group(0)}\" in the unrelated "
                         f"registrable domain {reg}, alongside other throwaway-host traits.",
                    severity=0.45, reliability=self.reliability * 0.9, source=self.name,
                    family=Family.DOMAIN))

        # Lure wording is evidence only together with a throwaway TLD; on its own it is
        # ordinary English that legitimate hosts use constantly (secure.example.com).
        if abused_tld and len(lures) >= 2 and not brand:
            joined = ", ".join(sorted(lures)[:3])
            out.append(Signal(
                id="domain.lure_wording_on_throwaway_tld",
                label="security-prompt wording on a throwaway TLD",
                text=f"{host} combines security-prompt wording ({joined}) with the .{tld} "
                     f"top-level domain, a pairing typical of credential-harvesting hosts.",
                severity=0.42, reliability=self.reliability, source=self.name,
                family=Family.DOMAIN))

        if abused_tld:
            out.append(Signal(
                id="domain.abused_tld",
                label="a heavily abused top-level domain",
                text=f"{host} sits on the .{tld} top-level domain, which is free or near-free to "
                     f"register and is disproportionately used for short-lived phishing hosts.",
                severity=0.38, reliability=self.reliability, source=self.name, family=Family.DOMAIN))

        if host.startswith("xn--") or ".xn--" in host:
            out.append(Signal(
                id="domain.punycode",
                label="an internationalised look-alike domain",
                text=f"{host} is an internationalised (punycode) domain, a common way to render a "
                     f"look-alike of a familiar brand name.",
                severity=0.45, reliability=self.reliability, source=self.name, family=Family.DOMAIN))

        if hyphens >= 3:
            out.append(Signal(
                id="domain.many_hyphens",
                label="a generated-looking hostname",
                text=f"{host} contains {host.count('-')} hyphens, typical of generated "
                     f"throwaway phishing hostnames.",
                severity=0.28, reliability=self.reliability * 0.85, source=self.name,
                family=Family.DOMAIN))

        if not out:
            out.append(Signal(
                id="domain.unremarkable",
                label="a domain name with no structural red flags",
                text=f"{host} shows no brand impersonation, abused TLD or other structural red "
                     f"flag in its name.",
                severity=0.0, reliability=self.reliability, source=self.name,
                family=Family.DOMAIN, benign=True))
        return out


class EmailHygieneSource(Source):
    """Disposable/throwaway mailbox detection - a weak but real synthetic-identity cue."""

    name = "email_hygiene"
    handles = (EntityKind.EMAIL,)
    reliability = 0.7
    offline = True

    DISPOSABLE = {
        "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
        "temp-mail.org", "throwawaymail.com", "yopmail.com", "trashmail.com", "getnada.com",
        "dispostable.com", "maildrop.cc", "sharklasers.com", "mohmal.com", "fakeinbox.com",
        "tempinbox.com", "mintemail.com", "spamgourmet.com", "emailondeck.com",
    }

    def enabled(self, settings: Settings) -> bool:
        return True

    async def collect(self, parsed, client, timeout, settings=None):
        out: list[Signal] = []
        for ent in parsed.entities_of(EntityKind.EMAIL)[:1]:
            domain = ent.value.split("@")[-1]
            if domain in self.DISPOSABLE:
                out.append(Signal(
                    id="identity.disposable_email",
                label="a disposable mailbox provider",
                    text=f"{ent.value} uses {domain}, a disposable mailbox provider, so the "
                         f"address cannot be tied to a durable identity.",
                    severity=0.52, reliability=self.reliability, source=self.name,
                    family=Family.IDENTITY))
            elif re.search(r"\d{5,}@", ent.value):
                out.append(Signal(
                    id="identity.generated_local_part",
                label="a programmatically generated address",
                    text=f"The local part of {ent.value} is a long digit run, typical of "
                         f"programmatically generated accounts.",
                    severity=0.3, reliability=self.reliability * 0.8, source=self.name,
                    family=Family.IDENTITY))
            else:
                out.append(Signal(
                    id="identity.email_ordinary",
                label="an ordinary mail domain",
                    text=f"{ent.value} is on an ordinary mail domain with no disposable-provider "
                         f"or generated-address indicators.",
                    severity=0.0, reliability=self.reliability, source=self.name,
                    family=Family.IDENTITY, benign=True))
        return out
