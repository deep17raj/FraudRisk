from __future__ import annotations

from dataclasses import dataclass

from intentkit.settings import BaseSettings, env


@dataclass(frozen=True)
class Settings(BaseSettings):
    cache_ttl: int = env("CACHE_TTL_S", 5)                   # prices move; Telegraph tolerates about 1%
    cache_ttl_degraded: int = env("CACHE_TTL_DEGRADED_S", 3)
    provider_timeout: float = env("PROVIDER_TIMEOUT_S", 2.5)
    global_timeout: float = env("GLOBAL_TIMEOUT_S", 5.0)
    outlier_pct: float = env("OUTLIER_PCT", 3.0)             # a reading this far from the median is dropped
    soft_deadline: float = env("SOFT_DEADLINE_S", 1.2)       # stop waiting for stragglers after this long
