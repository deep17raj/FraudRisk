"""Card BIN metadata (binlist.net).

A BIN on its own is not a fraud signal - it is issuer metadata. What matters is the
combination: a prepaid or anonymous-funding BIN is the classic instrument for card testing
because it carries no durable identity, and an issuer country that disagrees with the
geography described in the subject is the classic card-not-present mismatch. Both are
emitted as CARD-family signals; the behavioural rules supply the velocity half of the
pattern, and fusion needs both families to agree before the score reaches the top band.

binlist is rate-limited (roughly 5 requests/minute unauthenticated) and answers 404/400 for
an unknown BIN, so misses are "no data", not errors, and the breaker plus the assessment
cache keep us well inside the limit.
"""
from __future__ import annotations

import httpx

from ..config import Settings
from ..models import EntityKind, Family, Signal
from .base import Source



class CardBinSource(Source):
    name = "binlist"
    handles = (EntityKind.CARD_BIN,)
    reliability = 0.85

    def enabled(self, settings: Settings) -> bool:
        return settings.enable_bin_lookup

    async def collect(self, parsed, client, timeout, settings=None):
        ents = parsed.entities_of(EntityKind.CARD_BIN)
        if not ents:
            return []
        bin_value = ents[0].value[:8]
        try:
            r = await client.get(f"https://lookup.binlist.net/{bin_value}",
                                 headers={"Accept-Version": "3"}, timeout=timeout)
        except httpx.HTTPError:
            raise
        if r.status_code in (400, 404):
            return []                       # unknown BIN is "no data", not a failure
        r.raise_for_status()
        d = r.json()
        if not isinstance(d, dict) or not d:
            return []
        return self._to_signals(bin_value, d, parsed)

    def _to_signals(self, bin_value: str, d: dict, parsed) -> list[Signal]:
        out: list[Signal] = []
        scheme = (d.get("scheme") or "").title()
        ctype = (d.get("type") or "").lower()
        prepaid = bool(d.get("prepaid"))
        country = d.get("country") or {}
        cc = (country.get("alpha2") or "").upper()
        cname = country.get("name") or cc
        bank = (d.get("bank") or {}).get("name") or ""
        desc = " ".join(x for x in (scheme, (d.get("brand") or "")) if x).strip() or "card"

        if prepaid or ctype == "prepaid":
            out.append(Signal(
                id="card.prepaid_bin",
                label="a prepaid card BIN",
                text=f"BIN {bin_value} is a prepaid {desc} issued in {cname}"
                     + (f" by {bank}" if bank else "")
                     + " - prepaid instruments carry no durable identity and are the usual "
                       "vehicle for card testing.",
                severity=0.5, reliability=self.reliability, source=self.name,
                family=Family.CARD, verified=True))
        else:
            out.append(Signal(
                id="card.bin_metadata",
                label="standard card issuer metadata",
                text=f"BIN {bin_value} is a {ctype or 'standard'} {desc} issued in {cname}"
                     + (f" by {bank}" if bank else "") + ".",
                severity=0.0, reliability=self.reliability, source=self.name,
                family=Family.CARD, benign=True))

        stated = set(parsed.hints.countries)      # already ISO alpha-2
        if cc and stated and cc not in stated:
            named = ", ".join(sorted(stated))
            out.append(Signal(
                id="card.country_mismatch",
                label="an issuer country unlike the stated geography",
                text=f"The card is issued in {cname} ({cc}) while the subject describes activity "
                     f"in {named} - a cross-border card-not-present mismatch.",
                severity=0.4, reliability=self.reliability * 0.9, source=self.name,
                family=Family.CARD))
        return out
