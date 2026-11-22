from __future__ import annotations

from dataclasses import dataclass

from intentkit.settings import BaseSettings, env


@dataclass(frozen=True)
class Settings(BaseSettings):
    cache_ttl: int = env("CACHE_TTL_S", 120)
    cache_ttl_degraded: int = env("CACHE_TTL_DEGRADED_S", 15)
    provider_timeout: float = env("PROVIDER_TIMEOUT_S", 6.0)
    global_timeout: float = env("GLOBAL_TIMEOUT_S", 12.0)
    chains_ttl: int = env("CHAINS_TTL_S", 600)
    protocols_ttl: int = env("PROTOCOLS_TTL_S", 21600)      # the 10 MB list is only fetched for a fuzzy name
