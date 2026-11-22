from __future__ import annotations

from dataclasses import dataclass

from intentkit.settings import BaseSettings, env


@dataclass(frozen=True)
class Settings(BaseSettings):
    cache_ttl: int = env("CACHE_TTL_S", 300)
    cache_ttl_degraded: int = env("CACHE_TTL_DEGRADED_S", 15)
    provider_timeout: float = env("PROVIDER_TIMEOUT_S", 5.0)  # connect + handshake
    global_timeout: float = env("GLOBAL_TIMEOUT_S", 9.0)
    allow_private_hosts: bool = env("ALLOW_PRIVATE_HOSTS", False)  # leave OFF: it is the SSRF guard
