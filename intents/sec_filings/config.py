from __future__ import annotations

from dataclasses import dataclass

from intentkit.settings import BaseSettings, env


@dataclass(frozen=True)
class Settings(BaseSettings):
    cache_ttl: int = env("CACHE_TTL_S", 900)
    cache_ttl_degraded: int = env("CACHE_TTL_DEGRADED_S", 20)
    provider_timeout: float = env("PROVIDER_TIMEOUT_S", 6.0)
    global_timeout: float = env("GLOBAL_TIMEOUT_S", 12.0)
    tickers_ttl: int = env("TICKERS_TTL_S", 86400)           # the company list changes slowly
    max_listed: int = env("MAX_LISTED", 25)                  # titles and URLs returned; the count is complete
    # SEC asks automated clients to identify themselves; add a contact address here in production.
    sec_user_agent: str = env("SEC_USER_AGENT", "TelegraphIntentMiner/0.1 (hackathon project; automated client)")
