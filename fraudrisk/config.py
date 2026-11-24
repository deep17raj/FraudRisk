"""Runtime configuration, all from environment variables (no secrets in code)."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _b(name: str, default: bool) -> bool:
    return os.environ.get(name, "1" if default else "0") not in ("0", "false", "False", "")


@dataclass(frozen=True)
class Settings:
    # --- Anthropic -----------------------------------------------------------------------
    anthropic_api_key: str = ""
    # Opus 5.5 is the default. Set ANTHROPIC_MODEL=claude-haiku-5-5 if p95 latency is tight:
    # the whole request must finish inside Telegraph's patience and the reasoning stage owns
    # only `reasoning_timeout` seconds of the budget.
    model: str = "claude-opus-5-5"
    # low keeps the single reasoning call fast; it is a selection/annotation task, not research.
    effort: str = "low"
    # auto = use the LLM when a key exists, else deterministic. llm | off force it.
    reasoning_mode: str = "auto"
    # Server-side fallback on a safety-classifier refusal. Not available on every platform
    # (Bedrock/Vertex/Foundry use client-side middleware instead), so it is switchable.
    enable_fallbacks: bool = True
    # --- Groq: the fallback model, used only when there is no Anthropic key or its call fails ---
    groq_api_key: str = ""
    groq_models: str = ""              # empty = intentkit.groq's own default list

    # --- Signal providers (all optional; each self-disables without its key) ---------------
    ip_provider: str = "auto"          # auto | ipwho | ipinfo | ip2location | none
    ipinfo_token: str = ""
    ip2location_key: str = ""
    enable_tor_feed: bool = True
    enable_phish_feed: bool = True
    enable_bin_lookup: bool = True

    # --- Time budgets (seconds) ----------------------------------------------------------
    global_timeout: float = 11.5       # hard cap per request; on expiry we answer honestly
    gather_timeout: float = 4.5        # all signal sources in parallel share this
    provider_timeout: float = 3.0      # per-HTTP-call cap inside a source
    reasoning_timeout: float = 5.0     # the single LLM call
    feed_refresh_timeout: float = 20.0 # background bulk-feed refresh

    # --- Cache ---------------------------------------------------------------------------
    cache_ttl: int = 900               # assessments: subjects repeat across callers
    cache_ttl_degraded: int = 30       # partial provider failure: keep very short
    cache_max_items: int = 2000
    feed_ttl: int = 3600               # bulk feeds (Tor exits, phishing URLs)

    # --- Misc ----------------------------------------------------------------------------
    user_agent: str = "TelegraphFraudRiskMiner/0.1 (hackathon project; automated fraud-risk client)"
    log_level: str = "INFO"
    max_subject_chars: int = 2000

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        return cls(
            anthropic_api_key=e("ANTHROPIC_API_KEY", ""),
            model=e("ANTHROPIC_MODEL", cls.model),
            effort=e("ANTHROPIC_EFFORT", cls.effort).lower(),
            reasoning_mode=e("REASONING_MODE", cls.reasoning_mode).lower(),
            enable_fallbacks=_b("ENABLE_LLM_FALLBACKS", cls.enable_fallbacks),
            groq_api_key=e("GROQ_API_KEY", ""),
            groq_models=e("GROQ_MODELS", ""),
            ip_provider=e("IP_PROVIDER", cls.ip_provider).lower(),
            ipinfo_token=e("IPINFO_TOKEN", ""),
            ip2location_key=e("IP2LOCATION_KEY", ""),
            enable_tor_feed=_b("ENABLE_TOR_FEED", cls.enable_tor_feed),
            enable_phish_feed=_b("ENABLE_PHISH_FEED", cls.enable_phish_feed),
            enable_bin_lookup=_b("ENABLE_BIN_LOOKUP", cls.enable_bin_lookup),
            global_timeout=_f("GLOBAL_TIMEOUT_S", cls.global_timeout),
            gather_timeout=_f("GATHER_TIMEOUT_S", cls.gather_timeout),
            provider_timeout=_f("PROVIDER_TIMEOUT_S", cls.provider_timeout),
            reasoning_timeout=_f("REASONING_TIMEOUT_S", cls.reasoning_timeout),
            feed_refresh_timeout=_f("FEED_REFRESH_TIMEOUT_S", cls.feed_refresh_timeout),
            cache_ttl=_i("CACHE_TTL_S", cls.cache_ttl),
            cache_ttl_degraded=_i("CACHE_TTL_DEGRADED_S", cls.cache_ttl_degraded),
            cache_max_items=_i("CACHE_MAX_ITEMS", cls.cache_max_items),
            feed_ttl=_i("FEED_TTL_S", cls.feed_ttl),
            user_agent=e("HTTP_USER_AGENT", cls.user_agent),
            log_level=e("LOG_LEVEL", cls.log_level).upper(),
            max_subject_chars=_i("MAX_SUBJECT_CHARS", cls.max_subject_chars),
        )
