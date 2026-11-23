from __future__ import annotations

from dataclasses import dataclass

from intentkit.settings import BaseSettings, env


@dataclass(frozen=True)
class Settings(BaseSettings):
    cache_ttl: int = env("CACHE_TTL_S", 21600)               # a policy changes rarely; also keeps requests to a shop few
    cache_ttl_degraded: int = env("CACHE_TTL_DEGRADED_S", 120)
    provider_timeout: float = env("PROVIDER_TIMEOUT_S", 5.0)
    global_timeout: float = env("GLOBAL_TIMEOUT_S", 12.0)
    max_pages: int = env("MAX_PAGES", 8)                      # policy pages fetched per retailer
    allow_private_hosts: bool = env("ALLOW_PRIVATE_HOSTS", False)   # leave OFF: it is the SSRF guard
