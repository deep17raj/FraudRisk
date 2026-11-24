"""Groq behind the Anthropic-shaped interface the three original intents already call.

FRAUD_RISK, FACT_CHECK and CONTRACT_OBLIGATION_AUDIT were built against the Anthropic API and keep it as their
primary model, so their behaviour with a key set is unchanged (docs/INTENT_RULES.md rule A2). Without
`ANTHROPIC_API_KEY`, or when an Anthropic call fails, the same prompt goes to Groq instead, so each intent reaches
its deterministic fallback only when no model at all can answer. Groq is reached only through `intentkit.groq`
(rule 11), and the model stays an optional extra: with neither key set, nothing here is called and every intent
answers from its own rules exactly as before.

Groq has no structured-output mode, so a caller that needs JSON passes the shape as prompt text (each package
already writes one for its own no-structured-output retry) and `json_mode` asks Groq for a JSON object.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

from .groq import GroqClient, LlmSettings, parse_json_object

log = logging.getLogger("intentkit.model_fallback")

#: Below this, a round trip cannot finish, so the caller's deterministic path is the better answer.
MIN_BUDGET_S = 0.8


class GroqFallback:
    """One Groq client per intent process, created on first use.

    `api_key` and `models` come from the calling package's own settings, so a test that builds those settings
    without a key gets `available is False` and no network call, whatever the machine's environment holds.
    """

    def __init__(self, api_key: str = "", models: str = "", client: Optional[httpx.AsyncClient] = None) -> None:
        self.api_key = (api_key or "").strip()
        self.models = (models or "").strip()
        self._client = client
        self._given = client is not None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._groq: Optional[GroqClient] = None

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _settings(self) -> LlmSettings:
        kw = {"groq_api_key": self.api_key}
        if self.models:
            kw["groq_models"] = self.models
        return LlmSettings(**kw)

    def _get(self) -> GroqClient:
        # An httpx client binds its connection pool to the loop that first uses it, so a new loop needs a new
        # client; in the service the loop never changes and this builds one client for the process.
        loop = asyncio.get_running_loop()
        if self._groq is None or (not self._given and self._loop is not loop):
            if not self._given:
                self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
            self._loop = loop
            self._groq = GroqClient(self._settings(), self._client)
        return self._groq

    async def complete(self, system: str, user: str, *, max_tokens: int, timeout: float) -> Optional[str]:
        """The model's reply text, or None for any reason at all (no key, no time, refusal, transport)."""
        if not self.available or timeout < MIN_BUDGET_S:
            return None
        groq = self._get()
        if not groq.available():
            return None
        return await groq.chat(system, user, json_mode=True, budget=timeout, max_tokens=max_tokens,
                               temperature=0.2)

    async def complete_json(self, system: str, user: str, *, max_tokens: int, timeout: float) -> Optional[dict]:
        """The first JSON object in the model's reply, or None when it could not produce one."""
        raw = await self.complete(system, user, max_tokens=max_tokens, timeout=timeout)
        return parse_json_object(raw)


__all__ = ["GroqFallback", "MIN_BUDGET_S"]
