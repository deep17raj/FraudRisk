"""Pull an IP address out of whatever the caller sent."""
from __future__ import annotations

import ipaddress
import re
from typing import Optional

# Any run of characters that could belong to an IPv4 or IPv6 literal; each run is then validated.
_CANDIDATE = re.compile(r"[0-9A-Fa-f:.]{2,45}")


def _canonical(token: str) -> Optional[str]:
    token = token.strip().strip("[]()<>\"'").rstrip(".,;:")
    if not token:
        return None
    try:
        addr = ipaddress.ip_address(token)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return str(addr)


def extract_ip(raw: str) -> Optional[str]:
    """The first valid IPv4/IPv6 address in `raw`, canonicalised; None when there is none.

    Accepts a bare address ("8.8.8.8"), a bracketed one ("[2001:db8::1]") and an address inside a
    sentence ("Where is the IP address 8.8.8.8 located?").
    """
    if not raw:
        return None
    whole = _canonical(raw)
    if whole:
        return whole
    for token in _CANDIDATE.findall(raw):
        ip = _canonical(token)
        if ip:
            return ip
    return None
