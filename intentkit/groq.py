"""The one place any intent talks to a language model: Groq's OpenAI-compatible chat API.

Design contract (docs/INTENT_RULES.md, rule 11):
  * Groq is OPTIONAL. With no `GROQ_API_KEY` every call returns None immediately and the intent answers
    from its keyless path.
  * `GroqClient.chat` NEVER raises and never blocks past the budget it is given. A missing key, a 401, a 429,
    a decommissioned model, a 5xx, a timeout, a network error, an empty or malformed completion all end the same
    way: None, so the caller falls back to the answer it already built without the model.
  * Failures are remembered process-wide (one key means one organisation-wide rate limit shared by every
    intent): a 401 switches the model off for a while, a 429 cools down just that model (Groq rate-limits per
    model, so the next model in `GROQ_MODELS` may still answer), a missing model is skipped for an hour.
  * The key is only ever sent in the Authorization header and is never logged.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import httpx

from .settings import BaseSettings, env

log = logging.getLogger("intentkit.groq")

#: Defaults are Groq's general-purpose production chat models at the time of writing. Model ids are retired from
#: time to time, so they are configurable (GROQ_MODELS, comma separated, best first) and a retired id is skipped.
DEFAULT_MODELS = "openai/gpt-oss-120b,openai/gpt-oss-20b"
#: gpt-oss models think before answering and the thinking counts against `max_tokens`, so they get a low reasoning effort
#: and extra headroom; otherwise a short cap is used up by the thinking and the reply comes back empty.
REASONING_HEADROOM = 800
AUTH_COOLDOWN_S = 600.0
MODEL_GONE_COOLDOWN_S = 3600.0
DEFAULT_RATE_COOLDOWN_S = 30.0
MAX_RATE_COOLDOWN_S = 300.0


@dataclass(frozen=True)
class LlmSettings(BaseSettings):
    """Settings of an intent that may use Groq. All have safe defaults; only the key is needed to turn it on."""

    groq_api_key: str = env("GROQ_API_KEY", "")
    groq_models: str = env("GROQ_MODELS", DEFAULT_MODELS)
    groq_base_url: str = env("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
    groq_timeout: float = env("GROQ_TIMEOUT_S", 6.0)        # one attempt; also capped by the time left
    groq_max_tokens: int = env("GROQ_MAX_TOKENS", 700)
    llm_margin: float = env("LLM_MARGIN_S", 1.5)            # time kept back to build the keyless answer


class _Gate:
    """What this process has learned about one key's health. Shared by every intent using that key."""

    def __init__(self) -> None:
        self.disabled_until = 0.0
        self.model_until: dict[str, float] = {}
        self.auth_logged = False


_GATES: dict[str, _Gate] = {}


def _gate(key: str) -> _Gate:
    fp = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]      # a fingerprint, never the key itself
    return _GATES.setdefault(fp, _Gate())


def reset_gates() -> None:
    """Forget everything learned about key health (tests, and a deploy with a new key)."""
    _GATES.clear()


def _retry_after(headers: httpx.Headers) -> float:
    raw = headers.get("retry-after", "")
    try:
        return min(max(float(raw), 1.0), MAX_RATE_COOLDOWN_S)
    except ValueError:
        m = re.fullmatch(r"(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", raw.strip())   # "1m30s" / "7.5s" style
        if m and (m.group(1) or m.group(2)):
            secs = int(m.group(1) or 0) * 60 + float(m.group(2) or 0)
            return min(max(secs, 1.0), MAX_RATE_COOLDOWN_S)
        return DEFAULT_RATE_COOLDOWN_S


def _error_code(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error") or {}
        return f"{err.get('code', '')} {err.get('type', '')} {err.get('message', '')}".lower()
    except Exception:  # noqa: BLE001
        return ""


class GroqClient:
    def __init__(self, settings: LlmSettings, client: httpx.AsyncClient,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.settings, self.client, self._clock = settings, client, clock
        self.models = [m.strip() for m in settings.groq_models.split(",") if m.strip()]

    @property
    def configured(self) -> bool:
        return bool(self.settings.groq_api_key.strip())

    def available(self) -> bool:
        """True when a call is worth attempting right now (key set, not switched off, a model not cooling)."""
        if not self.configured or not self.models:
            return False
        g, now = _gate(self.settings.groq_api_key), self._clock()
        return now >= g.disabled_until and any(now >= g.model_until.get(m, 0.0) for m in self.models)

    async def chat(self, system: str, user: str, *, json_mode: bool = False, budget: float = 6.0,
                   max_tokens: Optional[int] = None, temperature: float = 0.2) -> Optional[str]:
        """The model's reply text, or None for any reason at all. `budget` is the most time this call may use."""
        try:
            return await self._chat(system, user, json_mode, budget, max_tokens, temperature)
        except Exception as exc:  # noqa: BLE001 - the model is an enhancement, never a point of failure
            log.warning("groq call failed unexpectedly", extra={"fields": {"error": f"{type(exc).__name__}: {exc}"}})
            return None

    async def _chat(self, system: str, user: str, json_mode: bool, budget: float,
                    max_tokens: Optional[int], temperature: float) -> Optional[str]:
        if not self.configured:
            return None
        s = self.settings
        gate = _gate(s.groq_api_key)
        started = self._clock()
        for model in self.models:
            left = budget - (self._clock() - started)
            now = self._clock()
            if now < gate.disabled_until:
                return None
            if now < gate.model_until.get(model, 0.0):
                continue
            if left < 0.8:                                   # not enough time for a useful round trip
                return None
            body: dict[str, Any] = {
                "model": model, "temperature": temperature, "max_tokens": max_tokens or s.groq_max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
            if "gpt-oss" in model and "safeguard" not in model:
                body["reasoning_effort"] = "low"
                body["max_tokens"] += REASONING_HEADROOM
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            timeout = min(s.groq_timeout, left)
            try:
                # httpx timeouts apply per network phase, so a response that trickles in could overrun the budget:
                # cap the whole call as well.
                resp = await asyncio.wait_for(
                    self.client.post(f"{s.groq_base_url.rstrip('/')}/chat/completions", json=body, timeout=timeout,
                                     headers={"Authorization": f"Bearer {s.groq_api_key.strip()}"}),
                    timeout=timeout + 0.3)
            except Exception as exc:  # noqa: BLE001 - timeout, DNS, TLS, connection reset ...
                log.info("groq request failed", extra={"fields": {"model": model, "error": type(exc).__name__}})
                continue
            text = self._handle(resp, gate, model)
            if text is not None:
                return text
            if self._clock() < gate.disabled_until:
                return None
        return None

    def _handle(self, resp: httpx.Response, gate: _Gate, model: str) -> Optional[str]:
        code, now = resp.status_code, self._clock()
        if code == 200:
            try:
                content = resp.json()["choices"][0]["message"]["content"]
            except Exception:  # noqa: BLE001
                return None
            if isinstance(content, str):
                content = _THINK.sub("", content).strip()      # some models put their reasoning in the reply
            return content if isinstance(content, str) and content else None
        info = _error_code(resp)
        if code in (401, 403):
            gate.disabled_until = now + AUTH_COOLDOWN_S
            if not gate.auth_logged:
                gate.auth_logged = True
                log.error("groq rejected the API key; the model is switched off for %d s and answers are keyless",
                          int(AUTH_COOLDOWN_S))
        elif code == 429:
            gate.model_until[model] = now + _retry_after(resp.headers)
            log.warning("groq rate limit", extra={"fields": {"model": model}})
        elif code == 404 or (code == 400 and any(w in info for w in ("model_decommissioned", "model_not_found",
                                                                       "does not exist", "decommissioned"))):
            gate.model_until[model] = now + MODEL_GONE_COOLDOWN_S
            log.error("groq model unavailable; set GROQ_MODELS from your Groq console",
                      extra={"fields": {"model": model}})
        else:                                               # 5xx, a failed JSON generation, anything else
            log.info("groq error", extra={"fields": {"model": model, "status": code}})
        return None


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)
_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


def parse_json_object(text: Optional[str]) -> Optional[dict]:
    """The first JSON object in a model reply (tolerating code fences and chatter), or None."""
    if not text:
        return None
    s = _FENCE.sub("", text.strip())
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else None
    except ValueError:
        pass
    start, end = s.find("{"), s.rfind("}")
    if 0 <= start < end:
        try:
            v = json.loads(s[start:end + 1])
            return v if isinstance(v, dict) else None
        except ValueError:
            return None
    return None
