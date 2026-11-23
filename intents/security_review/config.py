from __future__ import annotations

from dataclasses import dataclass

from intentkit.groq import LlmSettings
from intentkit.settings import env


@dataclass(frozen=True)
class Settings(LlmSettings):
    cache_ttl: int = env("CACHE_TTL_S", 900)
    cache_ttl_degraded: int = env("CACHE_TTL_DEGRADED_S", 60)
    provider_timeout: float = env("PROVIDER_TIMEOUT_S", 5.0)
    global_timeout: float = env("GLOBAL_TIMEOUT_S", 13.5)
    max_packages: int = env("MAX_PACKAGES", 150)
    max_detail_lookups: int = env("MAX_DETAIL_LOOKUPS", 40)     # OSV advisories fetched for severity and aliases
    max_issues_listed: int = env("MAX_ISSUES_LISTED", 25)
    allow_private_hosts: bool = env("ALLOW_PRIVATE_HOSTS", False)   # leave OFF: it is the SSRF guard
