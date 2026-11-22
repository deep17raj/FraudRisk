"""Find the URL (or bare domain) in whatever the caller sent."""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

_URL = re.compile(r"""(?i)\b(?:https?|ftp)://[^\s<>"']+""")
_LABEL = re.compile(r"^[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?$")
_TRAILING = ".,;:!?)]}>\"'"


@dataclass(frozen=True)
class Target:
    raw: str          # the URL as found in the request; echoed back as `url`
    url: str          # absolute URL used for lookups (scheme added when missing)
    host: str         # lower-case ASCII host, no port or userinfo
    is_ip: bool
    userinfo: bool    # the authority carries `user@`, a classic disguise for the real destination


def _valid_host(host: str) -> Optional[str]:
    host = host.strip().strip(".").lower()
    if not host:
        return None
    try:
        ipaddress.ip_address(host.strip("[]"))
        return host.strip("[]")
    except ValueError:
        pass
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return None
    if len(host) > 253 or "." not in host:
        return None
    return host if all(_LABEL.match(label) for label in host.split(".")) else None


def _target(candidate: str) -> Optional[Target]:
    candidate = candidate.strip().rstrip(_TRAILING)
    if not candidate:
        return None
    url = candidate if "://" in candidate else "http://" + candidate
    try:
        parts = urlsplit(url)
        host = _valid_host(parts.hostname or "")
    except ValueError:
        return None
    if host is None:
        return None
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False
    return Target(raw=candidate, url=url, host=host, is_ip=is_ip, userinfo="@" in parts.netloc)


def parse_target(text: str) -> Optional[Target]:
    """The first URL in `text`, or failing that the first token that looks like a host or host/path."""
    s = (text or "").strip()
    if not s:
        return None
    m = _URL.search(s)
    if m:
        return _target(m.group(0))
    for token in s.split():
        token = token.strip("<>()[]\"'")
        if "." in token.split("/", 1)[0]:
            t = _target(token)
            if t is not None:
                return t
    return None
