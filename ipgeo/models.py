from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class GeoFix:
    """One provider's answer, normalised."""

    provider: str
    latitude: float
    longitude: float
    country: str = ""         # full name when the provider gives one
    country_code: str = ""
    city: str = ""
    isp: str = ""


@dataclass(frozen=True)
class GeoResult:
    """What the endpoint reports. `latitude`/`longitude` are None when the address could not be located."""

    ip: str
    country: str = ""
    city: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    isp: str = ""
    providers: tuple[str, ...] = ()
    degraded: bool = False    # True when providers were unreachable (as opposed to "address has no location")
    note: str = ""
