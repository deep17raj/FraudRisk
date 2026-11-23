from __future__ import annotations

from dataclasses import dataclass

from intentkit.groq import LlmSettings
from intentkit.settings import env


@dataclass(frozen=True)
class Settings(LlmSettings):
    cache_ttl: int = env("CACHE_TTL_S", 600)
    cache_ttl_degraded: int = env("CACHE_TTL_DEGRADED_S", 45)
    provider_timeout: float = env("PROVIDER_TIMEOUT_S", 4.0)
    global_timeout: float = env("GLOBAL_TIMEOUT_S", 12.0)
    max_reply_chars: int = env("MAX_REPLY_CHARS", 1500)
