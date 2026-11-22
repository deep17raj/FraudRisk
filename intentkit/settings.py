"""Settings shared by every `intents/<package>`: all from environment variables, no secrets in code.

A subclass adds its own fields; any field declared with `env("NAME", default)` is read from that
environment variable (and a malformed value falls back to the default instead of crashing startup).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from typing import Any


def env(name: str, default: Any) -> Any:
    return field(default=default, metadata={"env": name})


def _coerce(raw: str, default: Any) -> Any:
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw


@dataclass(frozen=True)
class BaseSettings:
    global_timeout: float = env("GLOBAL_TIMEOUT_S", 6.0)       # hard cap per request
    provider_timeout: float = env("PROVIDER_TIMEOUT_S", 3.0)   # one upstream call
    cache_ttl: int = env("CACHE_TTL_S", 60)
    cache_ttl_degraded: int = env("CACHE_TTL_DEGRADED_S", 10)  # answers built on missing sources
    cache_max_items: int = env("CACHE_MAX_ITEMS", 5000)
    user_agent: str = env("HTTP_USER_AGENT", "TelegraphIntentMiner/0.1 (hackathon project; automated client)")
    log_level: str = env("LOG_LEVEL", "INFO")

    @classmethod
    def from_env(cls):
        kw: dict[str, Any] = {}
        for f in fields(cls):
            name = f.metadata.get("env")
            raw = os.environ.get(name) if name else None
            if raw is None or raw == "":
                continue
            try:
                kw[f.name] = _coerce(raw, f.default)
            except ValueError:
                continue
        if "log_level" in kw:
            kw["log_level"] = str(kw["log_level"]).upper()
        return cls(**kw)
