from __future__ import annotations

from dataclasses import dataclass

from intentkit.groq import LlmSettings
from intentkit.settings import env


@dataclass(frozen=True)
class Settings(LlmSettings):
    cache_ttl: int = env("CACHE_TTL_S", 300)
    cache_ttl_degraded: int = env("CACHE_TTL_DEGRADED_S", 45)   # a keyless answer when the key is set but failed
    provider_timeout: float = env("PROVIDER_TIMEOUT_S", 4.5)
    global_timeout: float = env("GLOBAL_TIMEOUT_S", 12.5)
    max_sources: int = env("MAX_SOURCES", 6)
    tavily_api_key: str = env("TAVILY_API_KEY", "")             # optional, lifts quality when set
    brave_api_key: str = env("BRAVE_API_KEY", "")
    serper_api_key: str = env("SERPER_API_KEY", "")
