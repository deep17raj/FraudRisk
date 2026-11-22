from __future__ import annotations

from dataclasses import dataclass

from intentkit.settings import BaseSettings, env


@dataclass(frozen=True)
class Settings(BaseSettings):
    cache_ttl: int = env("CACHE_TTL_S", 8)                  # a block or two; gas moves every few seconds
    cache_ttl_degraded: int = env("CACHE_TTL_DEGRADED_S", 4)
    provider_timeout: float = env("PROVIDER_TIMEOUT_S", 2.5)
    global_timeout: float = env("GLOBAL_TIMEOUT_S", 6.0)
