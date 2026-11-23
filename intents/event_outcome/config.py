from __future__ import annotations

from dataclasses import dataclass

from intentkit.groq import LlmSettings
from intentkit.settings import env


@dataclass(frozen=True)
class Settings(LlmSettings):
    cache_ttl: int = env("CACHE_TTL_S", 900)
    cache_ttl_degraded: int = env("CACHE_TTL_DEGRADED_S", 90)
    provider_timeout: float = env("PROVIDER_TIMEOUT_S", 5.0)
    global_timeout: float = env("GLOBAL_TIMEOUT_S", 12.5)
    market_match: float = env("MARKET_MATCH", 0.72)          # how closely a closed prediction-market question must match
