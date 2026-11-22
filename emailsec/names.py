"""Turn what a caller sent into a DNS name and record type, or decide it cannot be looked up."""
from __future__ import annotations

import re
from typing import Optional

#: Record types a public resolver will accept by mnemonic.
KNOWN_TYPES = frozenset({
    "A", "AAAA", "AFSDB", "APL", "CAA", "CDNSKEY", "CDS", "CERT", "CNAME", "CSYNC", "DHCID", "DLV",
    "DNAME", "DNSKEY", "DS", "EUI48", "EUI64", "HINFO", "HIP", "HTTPS", "IPSECKEY", "KEY", "KX", "LOC",
    "MX", "NAPTR", "NS", "NSEC", "NSEC3", "NSEC3PARAM", "OPENPGPKEY", "PTR", "RP", "RRSIG", "SIG",
    "SMIMEA", "SOA", "SPF", "SRV", "SSHFP", "SVCB", "TA", "TKEY", "TLSA", "TSIG", "TXT", "URI",
    "ZONEMD", "ANY",
})

# Underscores are legal in labels that carry mail policy (`_dmarc`, `selector._domainkey`).
_LABEL = re.compile(r"^[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?$")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)


def normalize_record_type(raw: str) -> Optional[str]:
    """A mnemonic ("mx" -> "MX") or a numeric type; None when no resolver would accept it."""
    t = (raw or "").strip().upper()
    if t.isdigit():
        return t if 0 < int(t) < 65536 else None
    return t if t in KNOWN_TYPES else None


def _normalize_one(raw: str) -> Optional[str]:
    s = raw.strip().strip("<>[]()\"'").strip()
    if "@" in s:
        s = s.rsplit("@", 1)[1]
    s = _SCHEME.sub("", s)
    s = re.split(r"[/?#]", s, maxsplit=1)[0]
    if s.count(":") == 1:                      # a port; an IPv6 literal is not a mail domain
        s = s.split(":")[0]
    s = s.strip(".").lower()
    if not s:
        return None
    try:
        s.encode("ascii")
    except UnicodeEncodeError:
        try:
            s = s.encode("idna").decode("ascii")
        except UnicodeError:
            return None
    if len(s) > 253:
        return None
    return s if all(_LABEL.match(label) for label in s.split(".")) else None


def normalize_domain(raw: str) -> Optional[str]:
    """A lower-case ASCII DNS name, or None when `raw` is not a valid name.

    Accepts a bare domain, an email address (the part after the last "@"), a URL, and a name inside
    a sentence ("Does example.com publish SPF?").
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if re.search(r"\s", raw):
        # A sentence: only a token that looks like a name ("example.com", "me@example.com") counts,
        # never a plain word such as "Does".
        for token in re.split(r"\s+", raw):
            if "." in token or "@" in token:
                name = _normalize_one(token.rstrip("?,;!"))
                if name:
                    return name
        return None
    return _normalize_one(raw)
