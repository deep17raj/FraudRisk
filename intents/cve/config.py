from __future__ import annotations

from dataclasses import dataclass

from intentkit.settings import BaseSettings, env


@dataclass(frozen=True)
class Settings(BaseSettings):
    cache_ttl: int = env("CACHE_TTL_S", 21600)              # a published CVSS score rarely changes
    cache_ttl_degraded: int = env("CACHE_TTL_DEGRADED_S", 60)
    provider_timeout: float = env("PROVIDER_TIMEOUT_S", 6.0)  # NVD is slow, and rate-limits without a key
    global_timeout: float = env("GLOBAL_TIMEOUT_S", 12.0)
    nvd_api_key: str = env("NVD_API_KEY", "")                # optional: lifts NVD's keyless rate limit
