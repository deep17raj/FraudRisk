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
    model: str = "claude-opus-5-5"
    # Obligation extraction is a close-reading task over supplied text, not research.
    # `low` keeps the single call inside the latency budget; raise it if quality needs it.
    effort: str = "low"
    # auto = use the model when a key exists, else the deterministic path. llm | off force it.
    extraction_mode: str = "auto"
    # Server-side fallback on a safety-classifier refusal. Not available on every platform
    # (Bedrock/Vertex/Foundry use client-side middleware instead), so it is switchable.
    enable_fallbacks: bool = True

    # --- Input resolution ----------------------------------------------------------------
    # `contract` may be a URL. Fetching one is core, not optional: Telegraph's on-chain
    # encoding caps the field at 256 bytes, so routed callers often send a link.
    enable_url_fetch: bool = True
    max_fetch_bytes: int = 8_000_000
    max_redirects: int = 3
    # Allow fetches to private/loopback addresses. OFF by default - this is the SSRF guard.
    allow_private_hosts: bool = False

    # --- Time budgets (seconds) ----------------------------------------------------------
    global_timeout: float = 13.5   # hard cap per request; on expiry we answer honestly
    fetch_timeout: float = 6.0     # resolving a linked document
    llm_timeout: float = 8.0       # the single extraction call

    # --- Cache ---------------------------------------------------------------------------
    cache_ttl: int = 1800          # audits: the same contract is re-sent across callers
    cache_ttl_degraded: int = 30   # a fetch failure or a timeout is cached only briefly
    cache_max_items: int = 500     # entries hold contract text, so keep the count modest

    # --- Limits --------------------------------------------------------------------------
    # Direct callers send whole contracts, so this is generous; it exists to bound latency
    # and tokens, and a truncated audit says so in its summary.
    max_contract_chars: int = 120_000
    max_obligations: int = 10
    max_clause_chars: int = 600    # a cited clause longer than this is trimmed at a boundary
    max_clauses: int = 12

    # --- Misc ----------------------------------------------------------------------------
    user_agent: str = "TelegraphContractAuditMiner/0.1 (hackathon project; automated contract-audit client)"
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        return cls(
            anthropic_api_key=e("ANTHROPIC_API_KEY", ""),
            model=e("ANTHROPIC_MODEL", cls.model),
            effort=e("ANTHROPIC_EFFORT", cls.effort).lower(),
            extraction_mode=e("EXTRACTION_MODE", cls.extraction_mode).lower(),
            enable_fallbacks=_b("ENABLE_LLM_FALLBACKS", cls.enable_fallbacks),
            enable_url_fetch=_b("ENABLE_URL_FETCH", cls.enable_url_fetch),
            max_fetch_bytes=_i("MAX_FETCH_BYTES", cls.max_fetch_bytes),
            max_redirects=_i("MAX_REDIRECTS", cls.max_redirects),
            allow_private_hosts=_b("ALLOW_PRIVATE_HOSTS", cls.allow_private_hosts),
            global_timeout=_f("GLOBAL_TIMEOUT_S", cls.global_timeout),
            fetch_timeout=_f("FETCH_TIMEOUT_S", cls.fetch_timeout),
            llm_timeout=_f("LLM_TIMEOUT_S", cls.llm_timeout),
            cache_ttl=_i("CACHE_TTL_S", cls.cache_ttl),
            cache_ttl_degraded=_i("CACHE_TTL_DEGRADED_S", cls.cache_ttl_degraded),
            cache_max_items=_i("CACHE_MAX_ITEMS", cls.cache_max_items),
            max_contract_chars=_i("MAX_CONTRACT_CHARS", cls.max_contract_chars),
            max_obligations=_i("MAX_OBLIGATIONS", cls.max_obligations),
            max_clause_chars=_i("MAX_CLAUSE_CHARS", cls.max_clause_chars),
            max_clauses=_i("MAX_CLAUSES", cls.max_clauses),
            user_agent=e("HTTP_USER_AGENT", cls.user_agent),
            log_level=e("LOG_LEVEL", cls.log_level).upper(),
        )
