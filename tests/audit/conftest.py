"""Shared fixtures: settings with no network, and a stub model that answers from a script.

The stub matters more than it looks. It lets every test below exercise the *real* pipeline -
grounding, status demotion, roll-up, summary - against model output chosen by the test,
including output that is wrong on purpose (fabricated quotes, invented clause numbers, a
"met" with no evidence). That is the only way to prove the grounding rules actually bite.
"""
from __future__ import annotations

import json

import pytest

from audit.config import Settings
from audit.llm import LLMUnavailable
from audit.pipeline import AuditPipeline


def make_settings(**over) -> Settings:
    base = dict(
        anthropic_api_key="test-key",
        extraction_mode="llm",
        enable_url_fetch=False,
        global_timeout=10.0,
        fetch_timeout=1.0,
        llm_timeout=5.0,
        cache_ttl=0,            # tests assert on fresh work unless they opt into caching
        cache_ttl_degraded=0,
        max_contract_chars=120_000,
    )
    base.update(over)
    return Settings(**base)


class StubLLM:
    """Stands in for `LLMClient`. Returns queued payloads, or raises to force the fallback."""

    def __init__(self, *payloads, available: bool = True, raises: Exception | None = None):
        self.payloads = list(payloads)
        self._available = available
        self.raises = raises
        self.calls: list[dict] = []

    @property
    def available(self) -> bool:
        return self._available

    async def complete_json(self, system, user, schema, *, max_tokens, timeout):
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens})
        if self.raises is not None:
            raise self.raises
        if not self.payloads:
            raise LLMUnavailable("stub exhausted")
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        # Round-trip through JSON so a test payload cannot smuggle in a non-JSON type.
        return json.loads(json.dumps(payload))


def extraction(obligations=(), *, missing_dependency=False, contradiction=False,
               missing_key_terms=False, note="", overall_quotes=()) -> dict:
    """Build a model payload in the shape `extract.SCHEMA` constrains the real one to."""
    return {
        "obligations": [
            {
                "party": o.get("party", "The Supplier"),
                "duty": o.get("duty", "deliver the Goods"),
                "deadline": o.get("deadline", ""),
                "clause_ref": o.get("clause_ref", ""),
                "quote": o.get("quote", ""),
                "status": o.get("status", "unverifiable"),
                "reason": o.get("reason", ""),
            }
            for o in obligations
        ],
        "contract_flags": {
            "missing_dependency": missing_dependency,
            "contradiction": contradiction,
            "missing_key_terms": missing_key_terms,
            "note": note,
        },
        "overall_quotes": list(overall_quotes),
    }


def make_pipeline(*payloads, settings: Settings | None = None, **stub_kw) -> AuditPipeline:
    pl = AuditPipeline(settings or make_settings(), client=None)
    pl.llm = StubLLM(*payloads, **stub_kw)
    return pl


@pytest.fixture
def settings() -> Settings:
    return make_settings()


# --- sample contracts -------------------------------------------------------------------

CLEAN = """SUPPLY AGREEMENT

1.1 The Supplier shall deliver 500 units of Product X to the Buyer's warehouse by 1 March 2026.
1.2 The Buyer shall pay GBP 10,000 within thirty (30) days of invoice.
1.3 The Supplier shall maintain product liability insurance of GBP 2,000,000 for the term.
"""

BREACH = """CONSULTANCY AGREEMENT

3.1 The Consultant shall deliver the final report by 1 October 2025.
3.2 The parties record that the final report was delivered on 15 November 2025.
3.3 The Client shall pay the Fee of GBP 5,000 within fourteen (14) days of delivery.
"""

DEFERS = """SERVICES AGREEMENT

2.1 The Supplier shall provide the Services as set out in Schedule A.
2.2 The Client shall pay the Fees in accordance with Schedule B.
"""
