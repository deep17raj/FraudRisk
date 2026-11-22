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


def _b(name: str, default: bool) -> bool:
    return os.environ.get(name, "1" if default else "0") not in ("0", "false", "False", "")


@dataclass(frozen=True)
class Settings:
    # --- Sources -------------------------------------------------------------------------
    enable_feed: bool = True        # OpenPhish live feed
    enable_dns: bool = True         # Cloudflare security resolver
    feed_ttl: int = 3600            # the community feed refreshes about hourly
    feed_timeout: float = 8.0       # downloading the feed (startup and background refresh)

    # --- Verdict -------------------------------------------------------------------------
    # Structural red flags in the address (brand lure, punycode, ...) add up to a weight; at or above
    # this the address is Suspicious even though no list names it.
    suspicious_weight: float = 0.6

    # --- Time budgets (seconds) ----------------------------------------------------------
    global_timeout: float = 6.0
    provider_timeout: float = 3.0

    # --- Cache ---------------------------------------------------------------------------
    cache_ttl: int = 600
    cache_ttl_degraded: int = 15
    cache_max_items: int = 5000

    # --- Misc ----------------------------------------------------------------------------
    user_agent: str = "TelegraphUrlSafeMiner/0.1 (hackathon project; automated URL reputation client)"
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        return cls(
            enable_feed=_b("ENABLE_PHISH_FEED", cls.enable_feed),
            enable_dns=_b("ENABLE_DNS_BLOCKLIST", cls.enable_dns),
            feed_ttl=_i("FEED_TTL_S", cls.feed_ttl),
            feed_timeout=_f("FEED_TIMEOUT_S", cls.feed_timeout),
            suspicious_weight=_f("SUSPICIOUS_WEIGHT", cls.suspicious_weight),
            global_timeout=_f("GLOBAL_TIMEOUT_S", cls.global_timeout),
            provider_timeout=_f("PROVIDER_TIMEOUT_S", cls.provider_timeout),
            cache_ttl=_i("CACHE_TTL_S", cls.cache_ttl),
            cache_ttl_degraded=_i("CACHE_TTL_DEGRADED_S", cls.cache_ttl_degraded),
            cache_max_items=_i("CACHE_MAX_ITEMS", cls.cache_max_items),
            user_agent=e("HTTP_USER_AGENT", cls.user_agent),
            log_level=e("LOG_LEVEL", cls.log_level).upper(),
        )
