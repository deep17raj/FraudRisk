"""Ask public DNS-over-HTTPS resolvers for the record and report whether the lookup succeeded.

`lookup_ok` is true exactly when the resolver's DNS status is 0 (NOERROR). That includes a name that
exists but has no record of the requested type (NODATA), because that is what status 0 means; an
NXDOMAIN, SERVFAIL, REFUSED or a malformed query is false.

Google's resolver is preferred and Cloudflare's is the fallback. Both are asked at once so the common
case costs one round trip; if only the fallback answers quickly, the preferred resolver gets a short
grace period to catch up so that any disagreement is settled the same way every time.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from intentkit.cache import TTLCache
from intentkit.doh import RCODE_FORMERR, DnsAnswer, DohClient

from .config import Settings
from .names import normalize_domain, normalize_record_type

log = logging.getLogger("emailsec.pipeline")

#: Resolvers in order of preference.
PREFERENCE = ("google", "cloudflare")
DEFAULT_RECORD_TYPE = "MX"   # if the caller names none: the record that makes a domain mail-capable


@dataclass(frozen=True)
class Lookup:
    domain: str
    record_type: str
    ok: bool
    status: Optional[int]          # DNS RCODE; None when no resolver could be asked
    provider: str = ""
    degraded: bool = False         # True when no resolver answered
    note: str = ""


def fallback_lookup(domain: str, record_type: str, reason: str) -> Lookup:
    return Lookup(domain, record_type, ok=False, status=None, degraded=True, note=reason)


class EmailSecPipeline:
    def __init__(self, settings: Settings, client: httpx.AsyncClient, doh: Optional[DohClient] = None) -> None:
        self.settings = settings
        self.doh = doh or DohClient(client, timeout=settings.provider_timeout)
        self.cache = TTLCache(max_items=settings.cache_max_items)

    async def lookup(self, domain_raw: str, record_type_raw: str) -> Lookup:
        domain = normalize_domain(domain_raw)
        rtype = normalize_record_type(record_type_raw or DEFAULT_RECORD_TYPE)
        if domain is None or rtype is None:
            # No resolver would accept this query; on the wire that is a format error (status 1).
            why = "not a valid DNS name" if domain is None else "not a DNS record type"
            return Lookup(domain_raw.strip()[:253], (record_type_raw or "").strip()[:16],
                          ok=False, status=RCODE_FORMERR, note=why)
        key = f"{domain}|{rtype}"
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        return await self.cache.single_flight(key, lambda: self._resolve(domain, rtype, key))

    async def _resolve(self, domain: str, rtype: str, key: str) -> Lookup:
        s = self.settings
        t0 = time.monotonic()
        answer = await self._ask(domain, rtype)
        if answer is None:
            res = fallback_lookup(domain, rtype, "no resolver answered")
            self.cache.set(key, res, s.cache_ttl_degraded)
        else:
            res = Lookup(domain, rtype, ok=answer.ok, status=answer.status, provider=answer.provider)
            self.cache.set(key, res, s.cache_ttl)
        log.info("lookup", extra={"fields": {
            "event": "lookup", "domain": domain, "type": rtype, "status": res.status, "ok": res.ok,
            "provider": res.provider, "degraded": res.degraded, "ms": int((time.monotonic() - t0) * 1000)}})
        return res

    async def _ask(self, domain: str, rtype: str) -> Optional[DnsAnswer]:
        s = self.settings
        loop = asyncio.get_running_loop()
        deadline = loop.time() + s.provider_timeout + 0.5
        pending = {asyncio.ensure_future(self.doh.query(p, domain, rtype)) for p in PREFERENCE}
        answers: dict[str, DnsAnswer] = {}
        grace_until: Optional[float] = None
        try:
            while pending:
                limit = deadline if grace_until is None else min(deadline, grace_until)
                wait = limit - loop.time()
                if wait <= 0:
                    break
                done, pending = await asyncio.wait(pending, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    break
                for task in done:
                    ans = task.result()
                    if ans is not None:
                        answers[ans.provider] = ans
                if PREFERENCE[0] in answers:
                    break
                if answers and grace_until is None:
                    grace_until = loop.time() + s.grace
        finally:
            for task in pending:
                task.cancel()
        return next((answers[p] for p in PREFERENCE if p in answers), None)
