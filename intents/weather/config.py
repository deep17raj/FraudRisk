from __future__ import annotations

from dataclasses import dataclass

from intentkit.settings import BaseSettings, env


@dataclass(frozen=True)
class Settings(BaseSettings):
    cache_ttl: int = env("CACHE_TTL_S", 300)               # current conditions change slowly
    cache_ttl_degraded: int = env("CACHE_TTL_DEGRADED_S", 20)
    geocode_ttl: int = env("GEOCODE_TTL_S", 86400)          # a place does not move
    provider_timeout: float = env("PROVIDER_TIMEOUT_S", 3.5)
    global_timeout: float = env("GLOBAL_TIMEOUT_S", 7.0)
