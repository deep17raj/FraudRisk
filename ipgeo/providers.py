"""Keyless IP geolocation providers, each reduced to a `GeoFix`.

Every parser is a pure function of the provider's JSON, so it is tested against recorded payloads.
`rank` only breaks ties between equally supported clusters; it is not a claim about accuracy.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Callable, Optional

from .config import Settings
from .models import GeoFix


def _num(v: object) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _s(v: object) -> str:
    return v.strip() if isinstance(v, str) else ""


def _fix(provider: str, lat: object, lon: object, **kw) -> Optional[GeoFix]:
    la, lo = _num(lat), _num(lon)
    if la is None or lo is None or not (-90 <= la <= 90 and -180 <= lo <= 180):
        return None
    if la == 0 and lo == 0:      # "Null Island": providers use it for "no data", not as a place
        return None
    return GeoFix(provider=provider, latitude=la, longitude=lo, **kw)


def parse_ipinfo(d: object) -> Optional[GeoFix]:
    if not isinstance(d, dict) or d.get("bogon") or d.get("error"):
        return None
    loc = _s(d.get("loc"))
    if "," not in loc:
        return None
    lat, _, lon = loc.partition(",")
    org = _s(d.get("org"))
    m = re.match(r"AS\d+\s+(.*)", org)
    return _fix("ipinfo", lat, lon, country_code=_s(d.get("country")).upper(),
                city=_s(d.get("city")), isp=m.group(1) if m else org)


def parse_ipwho(d: object) -> Optional[GeoFix]:
    if not isinstance(d, dict) or not d.get("success"):
        return None
    conn = d.get("connection") if isinstance(d.get("connection"), dict) else {}
    return _fix("ipwho", d.get("latitude"), d.get("longitude"), country=_s(d.get("country")),
                country_code=_s(d.get("country_code")).upper(), city=_s(d.get("city")),
                isp=_s(conn.get("isp")) or _s(conn.get("org")))


def parse_ipapi_co(d: object) -> Optional[GeoFix]:
    if not isinstance(d, dict) or d.get("error"):
        return None
    return _fix("ipapi_co", d.get("latitude"), d.get("longitude"), country=_s(d.get("country_name")),
                country_code=_s(d.get("country_code")).upper(), city=_s(d.get("city")), isp=_s(d.get("org")))


def parse_freeipapi(d: object) -> Optional[GeoFix]:
    if not isinstance(d, dict):
        return None
    return _fix("freeipapi", d.get("latitude"), d.get("longitude"), country=_s(d.get("countryName")),
                country_code=_s(d.get("countryCode")).upper(), city=_s(d.get("cityName")),
                isp=_s(d.get("asnOrganization")))


def parse_geojs(d: object) -> Optional[GeoFix]:
    if not isinstance(d, dict):
        return None
    return _fix("geojs", d.get("latitude"), d.get("longitude"), country=_s(d.get("country")),
                country_code=_s(d.get("country_code")).upper(), city=_s(d.get("city")),
                isp=_s(d.get("organization_name")))


@dataclass(frozen=True)
class Provider:
    name: str
    rank: int                                                  # lower wins a tie
    url: Callable[[str, Settings], tuple[str, dict]]           # (url, query params) for an IP
    parse: Callable[[object], Optional[GeoFix]]


def _ipinfo_url(ip: str, s: Settings) -> tuple[str, dict]:
    return f"https://ipinfo.io/{ip}/json", ({"token": s.ipinfo_token} if s.ipinfo_token else {})


PROVIDERS: tuple[Provider, ...] = (
    Provider("ipinfo", 0, _ipinfo_url, parse_ipinfo),
    Provider("ipwho", 1, lambda ip, s: (f"https://ipwho.is/{ip}", {}), parse_ipwho),
    Provider("ipapi_co", 2, lambda ip, s: (f"https://ipapi.co/{ip}/json/", {}), parse_ipapi_co),
    Provider("freeipapi", 3, lambda ip, s: (f"https://free.freeipapi.com/api/json/{ip}", {}), parse_freeipapi),
    Provider("geojs", 4, lambda ip, s: (f"https://get.geojs.io/v1/ip/geo/{ip}.json", {}), parse_geojs),
)


def select_providers(settings: Settings) -> tuple[Provider, ...]:
    wanted = {n.strip() for n in settings.providers.split(",") if n.strip()}
    if not wanted or "all" in wanted:
        return PROVIDERS
    chosen = tuple(p for p in PROVIDERS if p.name in wanted)
    return chosen or PROVIDERS
