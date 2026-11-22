"""Threat-list lookups used by both the URL and the malware intents.

Two keyless sources:
  * OpenPhish's community feed (a list of live phishing URLs), as `UrlFeed`;
  * Cloudflare's security resolver, which answers 0.0.0.0 for a name on its malware/phishing list.

Why the feed matches whole URLs and only SOME hosts
---------------------------------------------------
Phishing pages are often hosted on large multi-tenant platforms (docs.google.com, github.io,
blogspot.com). Treating "this hostname appears in the feed" as a hit would flag every legitimate page
on those platforms. So a URL matches the feed only when the whole URL is listed, and a bare host
matches only when the feed lists that host as a site in its own right (an entry with no path).
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

from .doh import DohClient
from .feeds import BulkFeed

OPENPHISH_URL = "https://openphish.com/feed.txt"
_BLOCKED_ADDRESSES = {"0.0.0.0", "::", "::0", "0:0:0:0:0:0:0:0"}


def _split(url: str):
    s = url.strip()
    if "://" not in s:
        s = "http://" + s
    try:
        return urlsplit(s)
    except ValueError:
        return None


def host_of(url: str) -> str:
    """Lower-case hostname without port, userinfo or a leading `www.`; '' when unparseable."""
    p = _split(url)
    host = ((p.hostname or "") if p else "").lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def url_key(url: str) -> str:
    """Scheme-, `www.`- and trailing-slash-insensitive identity of a URL (fragment dropped)."""
    p = _split(url)
    if p is None:
        return ""
    host = host_of(url)
    if not host:
        return ""
    return host + p.path.rstrip("/") + (("?" + p.query) if p.query else "")


class UrlFeed(BulkFeed):
    """OpenPhish: entries are indexed as `u:<url key>` and, for whole-site entries only, `h:<host>`."""

    def parse(self, body: str) -> frozenset[str]:
        out: set[str] = set()
        for line in body.splitlines():
            url = line.strip()
            if not url or url.startswith("#"):
                continue
            key = url_key(url)
            if not key:
                continue
            out.add("u:" + key)
            p = _split(url)
            if p is not None and p.path in ("", "/") and not p.query:
                out.add("h:" + host_of(url))
        return frozenset(out)

    def has_url(self, url: str) -> bool:
        key = url_key(url)
        return bool(key) and ("u:" + key) in self._items

    def has_host(self, host: str) -> bool:
        h = host.lower().strip(".")
        h = h[4:] if h.startswith("www.") else h
        return bool(h) and ("h:" + h) in self._items


async def dns_blocked(doh: DohClient, host: str) -> Optional[bool]:
    """True if Cloudflare's security resolver blocks `host`; None if it could not be asked."""
    ans = await doh.query("cloudflare_security", host, "A")
    if ans is None:
        return None
    return any(d.strip().lower() in _BLOCKED_ADDRESSES for d in ans.data())
