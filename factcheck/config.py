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


@dataclass(frozen=True)
class Settings:
    # --- Anthropic ---------------------------------------------------------------------
    anthropic_api_key: str = ""
    # Fast model by default: the whole request must finish well inside Telegraph's patience.
    model: str = "claude-haiku-5-5"
    # auto = LLM stance labelling when a key exists, else heuristic. llm | heuristic force it.
    stance_mode: str = "auto"
    # --- Groq: the fallback model, used only when there is no Anthropic key or its call fails ---
    groq_api_key: str = ""
    groq_models: str = ""              # empty = intentkit.groq's own default list

    # --- Search providers (all optional; each self-disables when its key is absent) -------
    search_provider: str = "auto"       # auto | tavily | serper | brave | none
    tavily_api_key: str = ""
    serper_api_key: str = ""
    brave_api_key: str = ""
    google_factcheck_api_key: str = ""
    enable_news: bool = True

    # --- Time budgets (seconds) -----------------------------------------------------------
    global_timeout: float = 11.5        # hard cap per request; on expiry we answer Unverified
    retrieval_timeout: float = 6.0      # all sources in parallel share this
    provider_timeout: float = 4.5       # per-HTTP-call cap inside a source
    stance_timeout: float = 2.5
    synthesis_timeout: float = 3.0

    # --- Cache ------------------------------------------------------------------------
    cache_ttl: int = 3600
    cache_ttl_volatile: int = 300       # claims containing "today", "breaking", ...
    cache_ttl_degraded: int = 30        # partial provider failure: keep very short
    cache_max_items: int = 2000

    # --- Misc ---------------------------------------------------------------------------
    user_agent: str = "TelegraphFactCheckMiner/0.1 (hackathon project; automated fact-checking client)"
    log_level: str = "INFO"
    credibility_overrides_path: str = ""   # optional JSON file extending the domain lists

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        return cls(
            anthropic_api_key=e("ANTHROPIC_API_KEY", ""),
            groq_api_key=e("GROQ_API_KEY", ""),
            groq_models=e("GROQ_MODELS", ""),
            model=e("ANTHROPIC_MODEL", cls.model),
            stance_mode=e("STANCE_MODE", cls.stance_mode).lower(),
            search_provider=e("SEARCH_PROVIDER", cls.search_provider).lower(),
            tavily_api_key=e("TAVILY_API_KEY", ""),
            serper_api_key=e("SERPER_API_KEY", ""),
            brave_api_key=e("BRAVE_API_KEY", ""),
            google_factcheck_api_key=e("GOOGLE_FACTCHECK_API_KEY", ""),
            enable_news=e("ENABLE_NEWS", "1") not in ("0", "false", "False"),
            global_timeout=_f("GLOBAL_TIMEOUT_S", cls.global_timeout),
            retrieval_timeout=_f("RETRIEVAL_TIMEOUT_S", cls.retrieval_timeout),
            provider_timeout=_f("PROVIDER_TIMEOUT_S", cls.provider_timeout),
            stance_timeout=_f("STANCE_TIMEOUT_S", cls.stance_timeout),
            synthesis_timeout=_f("SYNTHESIS_TIMEOUT_S", cls.synthesis_timeout),
            cache_ttl=_i("CACHE_TTL_S", cls.cache_ttl),
            cache_ttl_volatile=_i("CACHE_TTL_VOLATILE_S", cls.cache_ttl_volatile),
            cache_ttl_degraded=_i("CACHE_TTL_DEGRADED_S", cls.cache_ttl_degraded),
            cache_max_items=_i("CACHE_MAX_ITEMS", cls.cache_max_items),
            user_agent=e("HTTP_USER_AGENT", cls.user_agent),
            log_level=e("LOG_LEVEL", cls.log_level).upper(),
            credibility_overrides_path=e("CREDIBILITY_OVERRIDES", ""),
        )
