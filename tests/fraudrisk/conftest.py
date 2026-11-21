"""Shared fixtures: synthetic signals, fake sources and a fake LLM.

Every test here is hermetic - no test touches the network. The regression suite runs the
same fixtures twice (keyless, and with a fake LLM) to prove the band does not move when the
model is unavailable.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fraudrisk.config import Settings
from fraudrisk.llm import LLMClient
from fraudrisk.models import Family, ParsedSubject, Signal
from fraudrisk.sources.base import Source
from fraudrisk.sources.feeds import FeedRegistry

ROOT = Path(__file__).resolve().parents[2]


def make_settings(**kw) -> Settings:
    base = dict(
        anthropic_api_key="", reasoning_mode="off",
        global_timeout=4.0, gather_timeout=1.5, provider_timeout=1.0, reasoning_timeout=1.0,
        ip_provider="none", enable_tor_feed=False, enable_phish_feed=False,
        enable_bin_lookup=False,
    )
    base.update(kw)
    return Settings(**base)


def sig(id="x.test", severity=0.5, reliability=0.8, family=Family.BEHAVIOR,
        text="", benign=False, **kw) -> Signal:
    """Build a synthetic Signal for scorer tests."""
    return Signal(id=id, text=text or f"synthetic signal {id}", severity=severity,
                  reliability=reliability, source="test", family=family, benign=benign, **kw)


class FakeSource(Source):
    """Returns canned signals, optionally after a delay or by raising."""

    def __init__(self, name="fake", signals=None, handles=(), delay=0.0,
                 error: Exception | None = None, offline=False):
        super().__init__()
        self.name = name
        self.handles = handles
        self._signals = signals or []
        self.delay = delay
        self.error = error
        self.offline = offline
        self.calls = 0

    def relevant(self, parsed: ParsedSubject) -> bool:
        return True if not self.handles else super().relevant(parsed)

    def enabled(self, settings) -> bool:
        return True

    async def collect(self, parsed, client, timeout, settings=None):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return [Signal(**{**s.__dict__}) for s in self._signals]


class FakeLLM(LLMClient):
    """An LLMClient that answers from a canned dict (or raises) without any network call."""

    def __init__(self, response: dict | None = None, error: Exception | None = None,
                 delay: float = 0.0):
        super().__init__(make_settings(anthropic_api_key="test-key"))
        self.response = response
        self.error = error
        self.delay = delay
        self.calls = 0
        self.last_user_prompt = ""

    @property
    def available(self) -> bool:
        return True

    async def complete_json(self, system, user, schema, *, max_tokens, timeout):
        self.calls += 1
        self.last_user_prompt = user
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.response or {}


def echo_llm(pattern="account takeover", summary="The gathered signals point one way.",
             strength="moderate", extra_ids=()) -> FakeLLM:
    """A fake that marks every supplied signal applicable, as a well-behaved model would.

    `extra_ids` lets a test inject ids the model was never given, to prove they are dropped.
    """

    class EchoLLM(FakeLLM):
        async def complete_json(self, system, user, schema, *, max_tokens, timeout):
            self.calls += 1
            self.last_user_prompt = user
            ids = [ln.split("id=", 1)[1].split(" ", 1)[0]
                   for ln in user.splitlines() if "id=" in ln]
            items = [{"id": i, "applies": True, "strength": strength, "note": ""} for i in ids]
            items += [{"id": i, "applies": True, "strength": "strong", "note": "invented"}
                      for i in extra_ids]
            return {"signals": items, "pattern": pattern, "summary": summary}

    return EchoLLM()


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient() as c:
        yield c


@pytest.fixture
def offline_feeds() -> FeedRegistry:
    return FeedRegistry(ttl=3600, timeout=1.0, tor=False, phish=False)


def scripted_llm(*, strength="moderate", applies=True, pattern="account takeover",
                 summary="The gathered signals are consistent with one another.",
                 extra_ids=()) -> FakeLLM:
    """A fake model that answers every supplied signal with a FIXED strength/applies verdict.

    Used to prove the band is stable across the whole range of cooperative model behaviour -
    not just the one default the echo fake happens to produce.
    """

    class ScriptedLLM(FakeLLM):
        async def complete_json(self, system, user, schema, *, max_tokens, timeout):
            self.calls += 1
            self.last_user_prompt = user
            ids = [ln.split("id=", 1)[1].split(" ", 1)[0]
                   for ln in user.splitlines() if "id=" in ln]
            items = [{"id": i, "applies": applies, "strength": strength, "note": ""}
                     for i in ids]
            items += [{"id": i, "applies": True, "strength": "strong", "note": "invented"}
                      for i in extra_ids]
            return {"signals": items, "pattern": pattern, "summary": summary}

    return ScriptedLLM()
