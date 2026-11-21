"""Shared helpers: fake sources/LLMs and builders for synthetic Agreement / evidence objects."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from factcheck.config import Settings
from factcheck.models import Agreement, Evidence, ParsedClaim, ScoredEvidence, Stance, Tier
from factcheck.sources.base import Source


def make_settings(**kw) -> Settings:
    base = dict(anthropic_api_key="", stance_mode="heuristic", global_timeout=3.0, retrieval_timeout=1.5,
                provider_timeout=1.0, stance_timeout=0.5, synthesis_timeout=0.5)
    base.update(kw)
    return Settings(**base)


class FakeSource(Source):
    """Returns canned evidence, optionally after a delay or raising an error."""

    def __init__(self, name="fake", evidence=None, delay=0.0, error: Exception | None = None, recency_only=False):
        super().__init__()
        self.name = name
        self._evidence = evidence or []
        self.delay = delay
        self.error = error
        self.recency_only = recency_only
        self.calls = 0

    def enabled(self) -> bool:
        return True

    async def search(self, claim, client, timeout):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        # fresh copies: the retriever assigns ids in place
        return [Evidence(**{**e.__dict__}) for e in self._evidence]


def ev(url, title="Title", snippet="snippet", provider="fake", **kw) -> Evidence:
    return Evidence(url=url, title=title, snippet=snippet, provider=provider, **kw)


def scored(stance: Stance, domain="example.org", tier=Tier.HIGH, relevance=0.9, conf=0.9, id=1, **kw) -> ScoredEvidence:
    e = Evidence(url=f"https://{domain}/a{id}", title="t", snippet="s", provider="fake", id=id, **kw)
    return ScoredEvidence(evidence=e, domain=domain, tier=tier, relevance=relevance, stance=stance, stance_confidence=conf)


def agreement(**kw) -> Agreement:
    """Build an Agreement with sensible derived defaults for tests of the verdict function."""
    a = Agreement(**kw)
    if "decisive_domains" not in kw:
        a.decisive_domains = max(a.support_domains, a.refute_domains, 1 if a.decisive_w > 0 else 0)
    if "credible_decisive" not in kw:
        a.credible_decisive = a.decisive_domains
    return a


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient() as c:
        yield c
