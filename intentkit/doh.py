"""DNS-over-HTTPS (JSON) lookups through three keyless public resolvers.

  google               dns.google            plain recursive resolver
  cloudflare           cloudflare-dns.com    plain recursive resolver
  cloudflare_security  security.cloudflare-dns.com
                       the same resolver with Cloudflare's malware/phishing blocklist applied: a
                       blocked name is answered with 0.0.0.0 / :: instead of its real address.

`DohClient.query` never raises. It returns None when the provider could not be reached or replied
with something unusable, so a caller can tell "the resolver said NXDOMAIN" (a DnsAnswer) apart from
"we could not ask" (None).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from .resilience import CircuitBreaker, call_with_retry

log = logging.getLogger("intentkit.doh")

ENDPOINTS: dict[str, str] = {
    "google": "https://dns.google/resolve",
    "cloudflare": "https://cloudflare-dns.com/dns-query",
    "cloudflare_security": "https://security.cloudflare-dns.com/dns-query",
}

RCODE_NOERROR = 0
RCODE_FORMERR = 1
RCODE_NXDOMAIN = 3


@dataclass(frozen=True)
class DnsAnswer:
    provider: str
    status: int                              # DNS RCODE: 0 NOERROR, 1 FORMERR, 2 SERVFAIL, 3 NXDOMAIN ...
    records: tuple[tuple[int, str], ...] = ()  # (record type, data) for each answer record
    comment: str = ""

    @property
    def ok(self) -> bool:
        return self.status == RCODE_NOERROR

    def data(self, rtype: Optional[int] = None) -> list[str]:
        return [d for t, d in self.records if rtype is None or t == rtype]


class DohClient:
    def __init__(self, client: httpx.AsyncClient, timeout: float = 3.0,
                 failure_threshold: int = 4, cooldown_s: float = 30.0) -> None:
        self.client = client
        self.timeout = timeout
        self._breakers = {name: CircuitBreaker(failure_threshold, cooldown_s) for name in ENDPOINTS}

    async def query(self, provider: str, name: str, rtype: str = "A",
                    *, timeout: Optional[float] = None) -> Optional[DnsAnswer]:
        breaker = self._breakers[provider]
        url = ENDPOINTS[provider]
        t = timeout or self.timeout

        async def fn() -> DnsAnswer:
            # httpx timeouts apply per network phase, not to the whole call, so cap the total as well.
            r = await asyncio.wait_for(
                self.client.get(url, params={"name": name, "type": rtype},
                                headers={"accept": "application/dns-json"}, timeout=t), timeout=t + 0.5)
            if r.status_code == 400:
                # Resolvers answer 400 for a name they will not resolve (e.g. a label over 63
                # bytes). On the wire that is a format error, so report it as one.
                return DnsAnswer(provider, RCODE_FORMERR)
            r.raise_for_status()
            return _parse(provider, r.json())

        try:
            return await call_with_retry(fn, breaker, retries=0)
        except Exception as exc:  # noqa: BLE001 - a failed lookup is "unknown", never an error
            log.debug("doh lookup failed", extra={"fields": {
                "provider": provider, "name": name, "type": rtype, "error": f"{type(exc).__name__}: {exc}"}})
            return None


def _parse(provider: str, d: object) -> DnsAnswer:
    if not isinstance(d, dict) or not isinstance(d.get("Status"), int):
        raise ValueError("not a DNS JSON response")
    records: list[tuple[int, str]] = []
    for rec in d.get("Answer") or []:
        if isinstance(rec, dict) and isinstance(rec.get("type"), int) and isinstance(rec.get("data"), str):
            records.append((rec["type"], rec["data"]))
    comment = d.get("Comment")
    if isinstance(comment, list):
        comment = " ".join(str(c) for c in comment)
    return DnsAnswer(provider, d["Status"], tuple(records), comment if isinstance(comment, str) else "")
