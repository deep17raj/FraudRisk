from __future__ import annotations

from dataclasses import dataclass

from intentkit.settings import BaseSettings, env


@dataclass(frozen=True)
class Settings(BaseSettings):
    cache_ttl: int = env("CACHE_TTL_S", 3600)
    cache_ttl_degraded: int = env("CACHE_TTL_DEGRADED_S", 20)
    provider_timeout: float = env("PROVIDER_TIMEOUT_S", 6.0)
    global_timeout: float = env("GLOBAL_TIMEOUT_S", 10.0)
    min_score: float = env("MIN_NAME_SCORE", 0.8)          # a candidate must resemble the name this much
