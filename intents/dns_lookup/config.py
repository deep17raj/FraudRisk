from __future__ import annotations

from dataclasses import dataclass

from intentkit.settings import BaseSettings, env


@dataclass(frozen=True)
class Settings(BaseSettings):
    cache_ttl: int = env("CACHE_TTL_S", 60)
    cache_ttl_degraded: int = env("CACHE_TTL_DEGRADED_S", 10)
    provider_timeout: float = env("PROVIDER_TIMEOUT_S", 3.0)
    global_timeout: float = env("GLOBAL_TIMEOUT_S", 6.0)
