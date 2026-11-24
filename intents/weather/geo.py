"""Place name (or 'lat,lon') -> coordinates, through Open-Meteo's keyless geocoding API."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from intentkit.ask import place_of
from intentkit.cache import TTLCache
from intentkit.upstream import Upstream

GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
_LATLON = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*[,;\s]\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
_LEAD = re.compile(r"^(?:what(?:'s| is)? the )?(?:current |forecast |weather |temperature |air temperature )+(?:in|for|at)\s+",
                   re.I)


@dataclass(frozen=True)
class Place:
    label: str
    lat: float
    lon: float


def clean_query(raw: str) -> str:
    s = place_of((raw or "").strip()).strip().strip("?.! ")
    s = _LEAD.sub("", s)
    s = re.sub(r",?\s*in degrees (celsius|fahrenheit)$", "", s, flags=re.I)
    s = re.sub(r"\s+(?:right now|today|now|tomorrow)$", "", s, flags=re.I)
    return s.strip()


async def resolve(up: Upstream, cache: TTLCache, ttl: float, raw: str) -> Optional[Place]:
    text = clean_query(raw)
    if not text:
        return None
    m = _LATLON.match(text)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return Place(f"{lat:g},{lon:g}", lat, lon)
        return None
    key = "geo:" + text.lower()
    hit = cache.get(key)
    if hit is not None:
        return hit or None
    parts = [p.strip() for p in text.split(",") if p.strip()]
    name, hints = parts[0], [h.lower() for h in parts[1:]]
    d = await up.get_json(GEOCODE, params={"name": name, "count": 10, "language": "en", "format": "json"})
    results = d.get("results") if isinstance(d, dict) else None
    if d is None:
        return None                      # the geocoder could not be asked: do not cache a "not found"
    if not isinstance(results, list) or not results:
        cache.set(key, False, ttl)
        return None
    pick = results[0]
    for r in results:
        hay = " ".join(str(r.get(k, "")).lower() for k in ("country", "country_code", "admin1", "admin2"))
        if hints and all(h in hay for h in hints):
            pick = r
            break
    label = ", ".join(str(pick[k]) for k in ("name", "admin1", "country") if pick.get(k)) or name
    place = Place(label, float(pick["latitude"]), float(pick["longitude"]))
    cache.set(key, place, ttl)
    return place
