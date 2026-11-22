"""Offline structural red flags in the address itself. Inference, never a listing.

Every flag carries a weight. They only ever raise a verdict to Suspicious (see `Settings.suspicious_weight`)
and are never counted as detections: a detection means a database actually lists the address.
Each rule is written to leave legitimate addresses alone, so a signal needs corroboration where the
evidence alone is ordinary (a brand name inside `microsoftonline.com` is genuine; a free TLD on its own
is common).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .parse import Target

#: Brands that phishing pages actually impersonate. Generic security words do not belong here.
_BRANDS = re.compile(
    r"(paypal|stripe|binance|coinbase|kraken|metamask|revolut|monzo|venmo|cashapp|zelle|chase|hsbc|"
    r"barclays|santander|citibank|wellsfargo|natwest|lloyds|amex|americanexpress|visa|mastercard|"
    r"apple|icloud|microsoft|office365|outlook|google|gmail|amazon|netflix|spotify|dhl|fedex|usps|"
    r"hmrc|dvla|linkedin|instagram|facebook|whatsapp|steam|dropbox|docusign|adobe)", re.I)

#: Wording that is suspicious only together with a brand or a throwaway TLD.
_LURE_WORDS = re.compile(
    r"\b(secure|security|verify|verification|login|signin|sign-in|account|update|confirm|validate|auth|"
    r"authenticate|recover|unlock|support|billing|payment|alert|suspended|wallet|bank)\b", re.I)

_ABUSED_TLDS = {"tk", "ml", "ga", "cf", "gq", "top", "xyz", "club", "work", "click", "link", "country",
                "stream", "download", "loan", "icu", "rest", "cyou", "sbs", "lol"}

#: A brand's own domains: never flag these.
_LEGIT_SUFFIXES = (
    "paypal.com", "stripe.com", "binance.com", "coinbase.com", "revolut.com", "chase.com", "hsbc.com",
    "barclays.co.uk", "apple.com", "icloud.com", "microsoft.com", "live.com", "outlook.com", "office.com",
    "google.com", "gmail.com", "amazon.com", "netflix.com", "dhl.com", "fedex.com", "ups.com", "usps.com",
    "gov.uk", "irs.gov", "visa.com", "mastercard.com", "americanexpress.com", "metamask.io",
    "linkedin.com", "facebook.com", "instagram.com", "dropbox.com", "adobe.com", "docusign.com",
)

#: Two-label public suffixes, so `google.co.uk` reads as registrable `google.co.uk`, not `co.uk`.
_MULTI_SUFFIXES = {
    "co.uk", "org.uk", "gov.uk", "ac.uk", "me.uk", "net.uk", "com.au", "net.au", "org.au", "gov.au",
    "co.nz", "co.za", "co.jp", "or.jp", "ne.jp", "com.br", "com.mx", "com.ar", "com.tr", "com.cn",
    "com.hk", "com.sg", "com.my", "co.in", "co.id", "co.kr", "com.ph", "com.vn", "com.pk", "com.ng",
    "co.ke", "com.eg", "com.sa", "com.pl", "com.ua", "com.ru", "co.il", "com.tw", "co.th",
}


@dataclass(frozen=True)
class Flag:
    id: str
    weight: float
    text: str


def split_host(host: str) -> tuple[str, str, str]:
    """(registrable domain, its second-level label, effective TLD)."""
    parts = host.lower().strip(".").split(".")
    if len(parts) < 2:
        return host, host, ""
    if len(parts) >= 3 and ".".join(parts[-2:]) in _MULTI_SUFFIXES:
        return ".".join(parts[-3:]), parts[-3], ".".join(parts[-2:])
    return ".".join(parts[-2:]), parts[-2], parts[-1]


def _is_legit(host: str) -> bool:
    return any(host == s or host.endswith("." + s) for s in _LEGIT_SUFFIXES)


def analyse(target: Target) -> list[Flag]:
    flags: list[Flag] = []
    host = target.host

    if target.userinfo:
        flags.append(Flag("userinfo", 0.5, "it puts a 'user@' prefix before the host, a trick used to disguise "
                                           "the real destination"))
    if target.is_ip:
        flags.append(Flag("ip_host", 0.35, f"it uses the raw IP address {host} instead of a domain name"))
        return flags
    if _is_legit(host):
        return flags

    reg, sld, tld = split_host(host)
    brand = _BRANDS.search(host)
    abused = tld.split(".")[-1] in _ABUSED_TLDS
    lures = {m.group(0).lower() for m in _LURE_WORDS.finditer(host)}
    hyphens = host.count("-")

    if brand:
        name = brand.group(0).lower()
        if sld == name:
            pass                                    # the brand IS the registrable domain (amazon.de)
        elif name not in sld:
            flags.append(Flag("brand_lure", 0.6, f'it carries the brand name "{brand.group(0)}" while its '
                                                 f"registrable domain is {reg}, which that brand does not operate"))
        elif abused or len(lures) >= 2 or hyphens >= 3:
            # microsoftonline.com and paypalobjects.com are genuine, so a brand inside a longer domain
            # needs corroboration before it counts.
            flags.append(Flag("brand_term", 0.45, f'it embeds the brand name "{brand.group(0)}" in the unrelated '
                                                  f"domain {reg} alongside other throwaway-host traits"))
    if abused:
        flags.append(Flag("abused_tld", 0.3, f"it sits on the .{tld} top-level domain, which is free or near-free "
                                             "to register and heavily used for short-lived phishing hosts"))
        if len(lures) >= 2 and not brand:
            flags.append(Flag("lure_on_tld", 0.3, "it combines security-prompt wording "
                                                  f"({', '.join(sorted(lures)[:3])}) with that throwaway TLD"))
    if host.startswith("xn--") or ".xn--" in host:
        flags.append(Flag("punycode", 0.4, "it is an internationalised (punycode) name, a common way to render "
                                           "a look-alike of a familiar brand"))
    if hyphens >= 3:
        flags.append(Flag("many_hyphens", 0.2, f"its hostname has {hyphens} hyphens, typical of generated "
                                               "throwaway hosts"))
    if host.count(".") >= 5:
        flags.append(Flag("deep_subdomains", 0.15, "it nests an unusually deep chain of subdomains"))
    return flags
