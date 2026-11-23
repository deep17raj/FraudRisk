from __future__ import annotations

from dataclasses import dataclass

from intentkit.groq import LlmSettings
from intentkit.settings import env


@dataclass(frozen=True)
class Settings(LlmSettings):
    cache_ttl: int = env("CACHE_TTL_S", 900)
    cache_ttl_degraded: int = env("CACHE_TTL_DEGRADED_S", 60)
    provider_timeout: float = env("PROVIDER_TIMEOUT_S", 5.0)
    global_timeout: float = env("GLOBAL_TIMEOUT_S", 12.5)
    max_sources: int = env("MAX_SOURCES", 6)
    openalex_api_key: str = env("OPENALEX_API_KEY", "")        # optional: OpenAlex's free daily budget is shared per IP
