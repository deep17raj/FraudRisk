"""CORPORATE_REGISTRY_LOOKUP_GENERAL from GLEIF's public API (keyless).

GLEIF is the Global Legal Entity Identifier Foundation's registry. It holds entities that have an LEI, not every
company in every national register, so an unknown name is reported as not found rather than guessed at.
`active` is true when the entity's status is ACTIVE. A 20-character LEI in `company` is looked up directly.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel

from intentkit.ask import registry_company
from intentkit.names import normalize
from intentkit.pipeline import BasePipeline, Req, Result
from intentkit.request import GENERIC_ALIASES
from intentkit.service import StrictModel, create_service_app

from .config import Settings

VERSION = "0.1.0"
API = "https://api.gleif.org/api/v1/lei-records"
_LEI = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")
FIELDS = {"company": ("name", "entity", "organisation", "organization", "lei", *GENERIC_ALIASES),
          "jurisdiction": ("country", "state", "region", "registered_in")}

#: Country names -> ISO alpha-2, only to rank candidates by where they are registered.
COUNTRIES = {
    "united states": "US", "usa": "US", "us": "US", "america": "US", "united kingdom": "GB", "uk": "GB",
    "great britain": "GB", "britain": "GB", "england": "GB", "scotland": "GB", "wales": "GB", "germany": "DE",
    "france": "FR", "canada": "CA", "australia": "AU", "india": "IN", "china": "CN", "japan": "JP",
    "netherlands": "NL", "switzerland": "CH", "ireland": "IE", "singapore": "SG", "hong kong": "HK", "brazil": "BR",
    "mexico": "MX", "spain": "ES", "italy": "IT", "sweden": "SE", "norway": "NO", "denmark": "DK", "finland": "FI",
    "belgium": "BE", "austria": "AT", "luxembourg": "LU", "cayman islands": "KY", "british virgin islands": "VG",
    "united arab emirates": "AE", "uae": "AE", "saudi arabia": "SA", "south africa": "ZA", "south korea": "KR",
    "korea": "KR", "russia": "RU", "turkey": "TR", "poland": "PL", "israel": "IL", "new zealand": "NZ",
    "pakistan": "PK", "nigeria": "NG", "malta": "MT", "cyprus": "CY", "jersey": "JE", "guernsey": "GG",
    "bermuda": "BM", "portugal": "PT", "greece": "GR", "czech republic": "CZ", "hungary": "HU", "romania": "RO",
    "argentina": "AR", "chile": "CL", "colombia": "CO", "egypt": "EG", "indonesia": "ID", "malaysia": "MY",
    "thailand": "TH", "vietnam": "VN", "philippines": "PH", "taiwan": "TW", "ukraine": "UA", "kenya": "KE",
}
#: Abbreviated legal form -> the full spellings registries use for it.
FORMS = {"ag": ("Aktiengesellschaft",), "gmbh": ("Gesellschaft mit beschraenkter Haftung",), "ltd": ("Limited",),
         "corp": ("Corporation",), "inc": ("Incorporated",), "co": ("Company",), "sa": ("S.A.", "Societe Anonyme"),
         "nv": ("N.V.",), "bv": ("B.V.",), "spa": ("S.p.A.",), "ab": ("AB (publ)",), "kg": ("Kommanditgesellschaft",)}
US_STATES = {"delaware", "california", "new york", "texas", "nevada", "florida", "washington", "wyoming",
             "massachusetts", "illinois", "ohio", "georgia", "virginia", "colorado", "michigan"}


def country_code(raw: str) -> Optional[str]:
    s = (raw or "").strip().lower()
    if not s:
        return None
    if s in COUNTRIES:                      # first: "uk" is GB, not an ISO code of its own
        return COUNTRIES[s]
    if re.fullmatch(r"[a-z]{2}(-[a-z0-9]{1,3})?", s):
        return s[:2].upper()
    if s in US_STATES:
        return "US"
    for name, code in COUNTRIES.items():
        if len(name) > 3 and name in s:
            return code
    return None


class RegistryResponse(StrictModel):
    """Exactly the response fields of Telegraph's CORPORATE_REGISTRY_LOOKUP_GENERAL intent. Nothing else."""

    legal_name: str = ""
    registration_id: str = ""
    active: bool = False
    jurisdiction: str = ""
    registered_address: str = ""
    incorporated: str = ""


def _address(a: dict) -> str:
    parts = [*(a.get("addressLines") or []), a.get("city"), a.get("region"), a.get("postalCode"), a.get("country")]
    return ", ".join(str(p) for p in parts if p)


def _record_to_response(rec: dict) -> RegistryResponse:
    at = rec.get("attributes") or {}
    e = at.get("entity") or {}
    return RegistryResponse(
        legal_name=((e.get("legalName") or {}).get("name") or "")[:300], registration_id=at.get("lei") or "",
        active=e.get("status") == "ACTIVE", jurisdiction=e.get("jurisdiction") or "",
        registered_address=_address(e.get("legalAddress") or {})[:500],
        incorporated=(e.get("creationDate") or "")[:10])


class RegistryPipeline(BasePipeline):
    intent = "CORPORATE_REGISTRY_LOOKUP_GENERAL"

    def key(self, req: Req) -> Optional[str]:
        c = registry_company(req.get("company", "").strip()).casefold()
        return f"{c}|{(country_code(req.get('jurisdiction', '')) or '').lower()}" if c else None

    def empty(self, req: Req, reason: str) -> BaseModel:
        return RegistryResponse()

    @staticmethod
    def _variants(name: str) -> list[str]:
        """The name as given, plus the spellings of its legal form that GLEIF may hold it under."""
        out = [name]
        m = re.match(r"^(.*?)[\s,]+([A-Za-z.]+)$", name)
        if m:
            base, suffix = m.group(1), m.group(2).strip(".").lower()
            if suffix in FORMS:
                out += [f"{base} {full}" for full in FORMS[suffix]]
            if suffix in ("inc", "corp", "co", "ltd"):
                out.append(f"{base} {m.group(2).strip('.').title()}.")
        return list(dict.fromkeys(out))[:4]

    async def _query(self, param: str, name: str) -> Optional[list[dict]]:
        d = await self.up.get_json(API, params={param: name, "page[size]": 20})
        return [r for r in d.get("data") or [] if isinstance(r, dict)] if isinstance(d, dict) else None

    async def _search(self, name: str, want: Optional[str], min_score: float) -> Optional[list[dict]]:
        """Candidates from GLEIF's name filter (several spellings at once), then its full-text filter.

        None when GLEIF could not be reached at all.
        """
        batches = await asyncio.gather(*(self._query("filter[entity.legalName]", v) for v in self._variants(name)))
        reached = any(b is not None for b in batches)
        got = [r for b in batches if b for r in b]
        if not any(self._rank(name, want, r) >= min_score for r in got):
            more = await self._query("filter[fulltext]", name)
            reached = reached or more is not None
            got += more or []
        return got if reached else None

    def _rank(self, name: str, want: Optional[str], rec: dict) -> float:
        e = (rec.get("attributes") or {}).get("entity") or {}
        cand = normalize(((e.get("legalName") or {}).get("name")) or "")
        q = normalize(name)
        score = 1.0 if q == cand else (0.9 if len(q) >= 2 and set(q) < set(cand) and len(cand) <= len(q) + 2 else 0.0)
        if score == 0.0 and q and cand:
            from difflib import SequenceMatcher
            score = SequenceMatcher(None, " ".join(q), " ".join(cand)).ratio()
        country = ((e.get("legalAddress") or {}).get("country") or (e.get("jurisdiction") or "")[:2]).upper()
        if want and country == want:
            score += 0.05
        if e.get("status") == "ACTIVE":
            score += 0.02
        return score

    async def compute(self, req: Req) -> Result:
        s: Settings = self.settings
        name = registry_company(req.get("company", "").strip())
        if not name:
            return Result(self.empty(req, "no company"), degraded=True)
        if _LEI.match(name.upper()):
            d = await self.up.get_json(f"{API}/{name.upper()}")
            rec: Any = (d or {}).get("data") if isinstance(d, dict) else None
            if isinstance(rec, dict):
                return Result(_record_to_response(rec))
            return Result(self.empty(req, "LEI not found"), degraded=d is None)
        want = country_code(req.get("jurisdiction", ""))
        cands = await self._search(name, want, s.min_score)
        if cands is None:
            return Result(self.empty(req, "GLEIF unreachable"), degraded=True)
        ranked = sorted(((self._rank(name, want, r), r) for r in cands), key=lambda t: -t[0])
        if not ranked or ranked[0][0] < s.min_score:
            return Result(self.empty(req, "no matching entity"))
        return Result(_record_to_response(ranked[0][1]))


def create_app(settings: Optional[Settings] = None, pipeline: Optional[RegistryPipeline] = None) -> FastAPI:
    return create_service_app(
        title="Telegraph Corporate Registry Miner", intent="CORPORATE_REGISTRY_LOOKUP_GENERAL",
        path="/corporate-registry", version=VERSION, fields=FIELDS, model=RegistryResponse,
        settings=settings or Settings.from_env(), pipeline=pipeline, make_pipeline=RegistryPipeline, max_len=300)
