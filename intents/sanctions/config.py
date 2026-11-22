from __future__ import annotations

from dataclasses import dataclass

from intentkit.settings import BaseSettings, env


@dataclass(frozen=True)
class Settings(BaseSettings):
    cache_ttl: int = env("CACHE_TTL_S", 900)
    cache_ttl_degraded: int = env("CACHE_TTL_DEGRADED_S", 10)
    global_timeout: float = env("GLOBAL_TIMEOUT_S", 8.0)
    list_ttl: int = env("LIST_TTL_S", 21600)                 # re-download the lists every 6 hours
    list_timeout: float = env("LIST_TIMEOUT_S", 40.0)         # the SDN file is about 6 MB
    match_threshold: float = env("MATCH_THRESHOLD", 0.88)     # best score at or above this is a match
    max_matches: int = env("MAX_MATCHES", 5)
