"""Ask Google and Cloudflare DNS-over-HTTPS and report whether the answer holds a record of the asked type.

`has_record` is true when the resolver answers NOERROR *and* the answer section contains at least one record
of the requested type. That is stricter than a NOERROR status: a name that exists but has no record of that
type (NODATA), or an A query that only returns a CNAME, is false. Google is preferred; Cloudflare answers
only if Google could not be asked.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from pydantic import BaseModel

from emailsec.names import normalize_domain, normalize_record_type
from intentkit.doh import DnsAnswer, DohClient
from intentkit.pipeline import BasePipeline, Req, Result
from intentkit.service import StrictModel

from .config import Settings

TYPE_CODES = {"A": 1, "NS": 2, "CNAME": 5, "SOA": 6, "PTR": 12, "MX": 15, "TXT": 16, "AAAA": 28, "SRV": 33,
              "CAA": 257, "DS": 43, "DNSKEY": 48, "NAPTR": 35, "HTTPS": 65, "SVCB": 64, "TLSA": 52, "SPF": 99}
PREFERENCE = ("google", "cloudflare")


class DnsResponse(StrictModel):
    """Exactly the response field of Telegraph's DNS_RECORD_LOOKUP_GENERAL intent. Nothing else."""

    has_record: bool = False


class DnsPipeline(BasePipeline):
    intent = "DNS_RECORD_LOOKUP_GENERAL"

    def __init__(self, settings: Settings, client, doh: Optional[DohClient] = None) -> None:
        super().__init__(settings, client)
        self.doh = doh or DohClient(client, timeout=settings.provider_timeout)

    def _parse(self, req: Req) -> Optional[tuple[str, str]]:
        name = normalize_domain(req.get("name", ""))
        rtype = normalize_record_type(req.get("record_type", "") or "A")
        return (name, rtype) if name and rtype else None

    def key(self, req: Req) -> Optional[str]:
        p = self._parse(req)
        return f"dns:{p[0]}:{p[1]}" if p else None

    def empty(self, req: Req, reason: str) -> BaseModel:
        return DnsResponse(has_record=False)

    @staticmethod
    def _has(answer: DnsAnswer, rtype: str) -> bool:
        if not answer.ok:
            return False
        code = TYPE_CODES.get(rtype) if not rtype.isdigit() else int(rtype)
        return bool(answer.data(code)) if code is not None else bool(answer.records)

    async def compute(self, req: Req) -> Result:
        p = self._parse(req)
        if p is None:
            return Result(self.empty(req, "not a DNS name or record type"))
        name, rtype = p
        answers = await asyncio.gather(*(self.doh.query(prov, name, rtype) for prov in PREFERENCE))
        answer = next((a for a in answers if a is not None), None)
        if answer is None:
            return Result(self.empty(req, "no resolver answered"), degraded=True)
        return Result(DnsResponse(has_record=self._has(answer, rtype)))
