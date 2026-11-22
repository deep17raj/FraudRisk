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
    # --- Time budgets (seconds) ----------------------------------------------------------
    global_timeout: float = 6.0     # hard cap per request
    provider_timeout: float = 3.0   # one resolver call
    # When the fallback resolver answers first, give the preferred one this long to catch up.
    grace: float = 0.4

    # --- Cache ---------------------------------------------------------------------------
    # DNS answers change; a short TTL keeps us in step with what a validator sees at question time.
    cache_ttl: int = 60
    cache_ttl_degraded: int = 10
    cache_max_items: int = 5000

    # --- Misc ----------------------------------------------------------------------------
    user_agent: str = "TelegraphEmailSecMiner/0.1 (hackathon project; automated DNS client)"
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        return cls(
            global_timeout=_f("GLOBAL_TIMEOUT_S", cls.global_timeout),
            provider_timeout=_f("PROVIDER_TIMEOUT_S", cls.provider_timeout),
            grace=_f("DNS_GRACE_S", cls.grace),
            cache_ttl=_i("CACHE_TTL_S", cls.cache_ttl),
            cache_ttl_degraded=_i("CACHE_TTL_DEGRADED_S", cls.cache_ttl_degraded),
            cache_max_items=_i("CACHE_MAX_ITEMS", cls.cache_max_items),
            user_agent=e("HTTP_USER_AGENT", cls.user_agent),
            log_level=e("LOG_LEVEL", cls.log_level).upper(),
        )
