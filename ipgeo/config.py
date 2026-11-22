"""Runtime configuration, all from environment variables (no secrets in code)."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # --- Providers -----------------------------------------------------------------------
    # Optional: an ipinfo.io token lifts that provider's keyless rate limit. Everything works without.
    ipinfo_token: str = ""
    # "all", or a comma-separated subset such as "ipinfo,ipwho".
    providers: str = "all"

    # --- Time budgets (seconds) ----------------------------------------------------------
    global_timeout: float = 6.0     # hard cap per request; on expiry we answer with what we have
    provider_timeout: float = 3.0   # one provider call
    soft_deadline: float = 1.2      # once any provider has answered, wait at most this long (from the start) for more

    # --- Consensus -----------------------------------------------------------------------
    # Providers disagree by tens of km on city-level fixes (and by hundreds on anycast addresses).
    # Fixes within this radius of each other count as agreeing.
    cluster_km: float = 100.0

    # --- Cache ---------------------------------------------------------------------------
    cache_ttl: int = 21600          # a location rarely changes within hours
    cache_ttl_degraded: int = 20    # "no provider answered" is cached only briefly
    cache_max_items: int = 5000

    # --- Misc ----------------------------------------------------------------------------
    user_agent: str = "TelegraphIPGeoMiner/0.1 (hackathon project; automated IP geolocation client)"
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        return cls(
            ipinfo_token=e("IPINFO_TOKEN", ""),
            providers=e("GEO_PROVIDERS", cls.providers).lower(),
            global_timeout=_f("GLOBAL_TIMEOUT_S", cls.global_timeout),
            provider_timeout=_f("PROVIDER_TIMEOUT_S", cls.provider_timeout),
            soft_deadline=_f("GEO_SOFT_DEADLINE_S", cls.soft_deadline),
            cluster_km=_f("GEO_CLUSTER_KM", cls.cluster_km),
            cache_ttl=_i("CACHE_TTL_S", cls.cache_ttl),
            cache_ttl_degraded=_i("CACHE_TTL_DEGRADED_S", cls.cache_ttl_degraded),
            cache_max_items=_i("CACHE_MAX_ITEMS", cls.cache_max_items),
            user_agent=e("HTTP_USER_AGENT", cls.user_agent),
            log_level=e("LOG_LEVEL", cls.log_level).upper(),
        )
