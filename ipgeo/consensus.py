"""Reduce several providers' fixes to one answer.

Free geolocation databases disagree, sometimes by thousands of km on anycast and mobile addresses.
Telegraph scores an answer against reference sources, so the best single answer is the one the most
providers agree on, not any one provider's. Fixes within `radius_km` of each other form a cluster; the
largest cluster wins (a tie goes to the cluster holding the best-ranked provider) and its coordinates
are the per-axis median. A lone outlier therefore cannot move the answer.
"""
from __future__ import annotations

import math
import statistics
from collections import Counter
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from .models import GeoFix


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))


@dataclass(frozen=True)
class Merged:
    latitude: float
    longitude: float
    country: str
    city: str
    isp: str
    providers: tuple[str, ...]     # the providers that formed the winning cluster
    support: int
    total: int                     # fixes considered


def _clusters(fixes: Sequence[GeoFix], radius_km: float) -> list[list[GeoFix]]:
    return [[g for g in fixes
             if haversine_km(f.latitude, f.longitude, g.latitude, g.longitude) <= radius_km]
            for f in fixes]


def _majority(values: Sequence[str]) -> str:
    values = [v for v in values if v]
    return Counter(values).most_common(1)[0][0] if values else ""


def merge(fixes: Sequence[GeoFix], radius_km: float, rank: Mapping[str, int]) -> Optional[Merged]:
    if not fixes:
        return None
    best_rank = lambda c: min(rank.get(f.provider, 99) for f in c)   # noqa: E731
    cluster = max(_clusters(fixes, radius_km), key=lambda c: (len(c), -best_rank(c)))
    cluster = sorted(cluster, key=lambda f: rank.get(f.provider, 99))
    names = [f.country for f in cluster]
    return Merged(
        latitude=round(statistics.median(f.latitude for f in cluster), 6),
        longitude=round(statistics.median(f.longitude for f in cluster), 6),
        # Prefer a full country name; fall back to the ISO code only when no provider gave a name.
        country=_majority(names) or _majority([f.country_code for f in cluster]),
        city=_majority([f.city for f in cluster]),
        isp=next((f.isp for f in cluster if f.isp), ""),
        providers=tuple(f.provider for f in cluster),
        support=len(cluster),
        total=len(fixes),
    )
