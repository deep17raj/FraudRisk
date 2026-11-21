"""Thin async wrapper around the Anthropic SDK used by stance labelling and synthesis."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from .config import Settings

log = logging.getLogger("factcheck.llm")


class LLMUnavailable(Exception):
    pass


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply (tolerates code fences / chatter)."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start : end + 1])


class LLMClient:
    #: Process-wide: set to False the first time a model 400s on `thinking: {"type": "disabled"}`
    #: (current Opus/Sonnet/Fable models reject it), so later calls skip the doomed attempt.
    _thinking_disabled_ok: bool = True

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self.settings.anthropic_api_key)

    def _get(self):
        if self._client is None:
            import anthropic

            # SDK retries disabled: we own the time budget and fall back instead of waiting.
            self._client = anthropic.AsyncAnthropic(
                api_key=self.settings.anthropic_api_key, max_retries=0, timeout=self.settings.synthesis_timeout + 2
            )
        return self._client

    async def complete(self, system: str, user: str, *, max_tokens: int, timeout: float) -> str:
        if not self.available:
            raise LLMUnavailable("ANTHROPIC_API_KEY not set")
        import anthropic

        client = self._get()
        kwargs = dict(
            model=self.settings.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )

        async def go(**extra):
            return await client.messages.create(**kwargs, **extra)

        # `effort: low` keeps latency inside our slice and is accepted by every current model.
        # `thinking: disabled` is NOT: Opus 5.5 / Sonnet 5.5 / Fable reject it with a 400, so the
        # first 400 switches this process to effort-only for every later call (no repeat round-trips).
        deadline = time.monotonic() + timeout
        effort = {"output_config": {"effort": "low"}}
        if LLMClient._thinking_disabled_ok:
            try:
                resp = await asyncio.wait_for(
                    go(thinking={"type": "disabled"}, extra_body=effort), timeout
                )
            except anthropic.BadRequestError as exc:
                LLMClient._thinking_disabled_ok = False
                remaining = deadline - time.monotonic()
                log.warning("model rejected thinking:disabled (%s); using effort-only from now on "
                            "(%.1fs left in this call's budget)", exc, remaining)
                if remaining < 0.5:
                    raise LLMUnavailable("no time left to retry after the 400") from exc
                # Retry stays inside the ORIGINAL budget and keeps effort: low.
                resp = await asyncio.wait_for(go(extra_body=effort), remaining)
        else:
            resp = await asyncio.wait_for(go(extra_body=effort), timeout)
        if getattr(resp, "stop_reason", None) == "refusal":
            raise LLMUnavailable("model refused")
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
