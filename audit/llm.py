"""Thin async wrapper around the Anthropic SDK for the single extraction call.

Design notes
------------
* `thinking` is never sent. On current models it cannot be disabled (Opus 5.5 and Sonnet 5.5
  return HTTP 400 for `{"type": "disabled"}` at every effort level), so depth is controlled
  with `output_config.effort` instead.
* Structured output (`output_config.format`) is what keeps the model inside the contract: it
  returns obligations with a status drawn from a closed set and quotes as plain strings. It
  has no field in which it could return a verdict or a summary - those are computed in code
  from what it returns, so the model cannot talk the service past its own roll-up.
* SDK retries are disabled: this service owns its latency budget and prefers a deterministic
  fallback to a late answer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from intentkit.model_fallback import GroqFallback

from .config import Settings

log = logging.getLogger("audit.llm")

#: Appended on the retry that runs WITHOUT structured output, so the reply is still parseable.
_JSON_INSTRUCTION = (
    "\n\nReply with a single JSON object and nothing else - no prose, no code fence. Shape:\n"
    '{"obligations": [{"party": "<who owes it>", "duty": "<what they owe>", '
    '"deadline": "<when, or empty>", "clause_ref": "<e.g. Clause 4.1, or empty>", '
    '"quote": "<the clause wording, copied exactly from the contract>", '
    '"status": "met"|"breached"|"unverifiable", "reason": "<<=15 words>"}], '
    '"contract_flags": {"missing_dependency": true|false, "contradiction": true|false, '
    '"missing_key_terms": true|false, "note": "<<=20 words>"}, '
    '"overall_quotes": ["<the clause wording that most decides the audit, copied exactly>"]}'
)


class LLMUnavailable(Exception):
    """Raised for every reason the model could not answer: no key, timeout, refusal, bad JSON."""


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a reply (tolerates code fences and chatter)."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start : end + 1])


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = None
        self.groq = GroqFallback(settings.groq_api_key, settings.groq_models)

    @property
    def available(self) -> bool:
        return bool(self.settings.anthropic_api_key) or self.groq.available

    def _get(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic(
                api_key=self.settings.anthropic_api_key,
                max_retries=0,
                timeout=self.settings.llm_timeout + 2,
            )
        return self._client

    async def complete_json(self, system: str, user: str, schema: dict, *,
                            max_tokens: int, timeout: float) -> dict:
        """One call, JSON-schema constrained. Raises LLMUnavailable on any failure."""
        if not self.settings.anthropic_api_key:
            return await self._groq_json(system, user, max_tokens=max_tokens, timeout=timeout)
        deadline = time.monotonic() + timeout
        try:
            return await self._anthropic_json(system, user, schema, max_tokens=max_tokens, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - any Anthropic failure may still fall to Groq
            if not self.groq.available:
                raise
            left = deadline - time.monotonic()
            log.warning("anthropic extraction failed (%s: %s); trying groq with %.1fs left",
                        type(exc).__name__, exc, left)
            return await self._groq_json(system, user, max_tokens=max_tokens, timeout=left)

    async def _groq_json(self, system: str, user: str, *, max_tokens: int, timeout: float) -> dict:
        """Groq has no structured-output mode, so the shape is asked for in the prompt instead."""
        data = await self.groq.complete_json(system, user + _JSON_INSTRUCTION,
                                             max_tokens=max_tokens, timeout=timeout)
        if not isinstance(data, dict):
            raise LLMUnavailable("no model answered (no Anthropic key; groq unavailable or unparseable)")
        return data

    async def _anthropic_json(self, system: str, user: str, schema: dict, *,
                              max_tokens: int, timeout: float) -> dict:
        import anthropic

        client = self._get()
        base = dict(
            model=self.settings.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        output_config = {"effort": self.settings.effort,
                         "format": {"type": "json_schema", "schema": schema}}

        # A safety classifier may decline a request (HTTP 200, stop_reason "refusal"). The
        # server-side fallback re-runs it on another model inside the same call rather than
        # losing the extraction step. `fallbacks` and its beta flag are only accepted on the
        # BETA messages endpoint.
        use_beta = bool(self.settings.enable_fallbacks)

        async def go(**extra):
            if use_beta:
                return await client.beta.messages.create(
                    **base, betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default", **extra)
            return await client.messages.create(**base, **extra)

        async def go_plain():
            """Last resort: no effort, no structured output, no beta - JSON asked for in
            the prompt, which `extract_json` can still recover."""
            return await client.messages.create(
                model=base["model"], max_tokens=base["max_tokens"], system=system,
                messages=[{"role": "user", "content": user + _JSON_INSTRUCTION}])

        try:
            try:
                resp = await asyncio.wait_for(go(output_config=output_config), timeout)
            except (anthropic.BadRequestError, TypeError) as exc:
                # The model or the installed SDK rejected effort, structured output or the
                # fallback beta. Retry once with nothing but the prompt.
                log.warning("request params rejected (%s: %s); retrying plainly",
                            type(exc).__name__, exc)
                resp = await asyncio.wait_for(go_plain(), timeout)
        except asyncio.TimeoutError as exc:
            raise LLMUnavailable(f"timeout after {timeout:.1f}s") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Every remaining failure - API error, transport, SDK signature mismatch,
            # anything unforeseen - becomes LLMUnavailable so the caller falls back to the
            # deterministic reader instead of failing the audit.
            raise LLMUnavailable(f"{type(exc).__name__}: {exc}") from exc

        stop = getattr(resp, "stop_reason", None)
        if stop == "refusal":
            raise LLMUnavailable("model refused")
        if stop == "max_tokens":
            # A truncated reply is unparseable JSON; fail over explicitly rather than let
            # extract_json raise a confusing error on a half-written object.
            raise LLMUnavailable("response truncated at max_tokens")

        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        if not text.strip():
            raise LLMUnavailable("empty model response")
        try:
            data = extract_json(text)
        except (ValueError, json.JSONDecodeError) as exc:
            raise LLMUnavailable(f"unparseable model output: {exc}") from exc
        if not isinstance(data, dict):
            raise LLMUnavailable("model output was not a JSON object")
        return data
