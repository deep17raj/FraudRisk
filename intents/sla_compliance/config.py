from __future__ import annotations

from dataclasses import dataclass

from intentkit.settings import BaseSettings, env


@dataclass(frozen=True)
class Settings(BaseSettings):
    cache_ttl: int = env("CACHE_TTL_S", 300)
    cache_ttl_degraded: int = env("CACHE_TTL_DEGRADED_S", 20)
    provider_timeout: float = env("PROVIDER_TIMEOUT_S", 5.0)
    global_timeout: float = env("GLOBAL_TIMEOUT_S", 10.0)
    # Public Upptime repositories (owner/repo, comma separated) whose published history/summary.json is read.
    upptime_repos: str = env("UPPTIME_REPOS", "upptime/upptime")
