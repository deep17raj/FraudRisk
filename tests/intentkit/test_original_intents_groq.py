"""The three original intents reach Groq when Anthropic cannot answer, and nothing else about them changes.

FRAUD_RISK, FACT_CHECK and CONTRACT_OBLIGATION_AUDIT keep Anthropic as their primary model. These tests pin the
three states that matter: no key at all (deterministic, no network), a Groq key only (Groq answers), and an
Anthropic key whose call fails (Groq picks up what is left of the budget).
"""
from __future__ import annotations

import httpx
import pytest

from intentkit.groq import reset_gates
from intentkit.model_fallback import GroqFallback
from tests.intents.helpers import FakeHttp
from tests.intents.llm_fixtures import GROQ, Groq, groq_reply, transport

KEY = "gsk_test_SECRET_KEY_123456"


@pytest.fixture(autouse=True)
def fresh_gates():
    reset_gates()
    yield
    reset_gates()


def with_groq(client, groq: Groq) -> None:
    """Point a package's LLMClient at a scripted Groq."""
    client.groq = GroqFallback(KEY, client=transport({}, groq).client())


# ---- availability ------------------------------------------------------------------------------------------
def test_each_client_reports_a_model_when_either_key_is_set():
    from audit.llm import LLMClient as AuditLLM
    from factcheck.llm import LLMClient as FactLLM
    from fraudrisk.llm import LLMClient as FraudLLM
    from tests.audit.conftest import make_settings as audit_settings
    from tests.factcheck.conftest import make_settings as fact_settings
    from tests.fraudrisk.conftest import make_settings as fraud_settings

    for cls, settings in ((AuditLLM, audit_settings), (FactLLM, fact_settings), (FraudLLM, fraud_settings)):
        assert not cls(settings(anthropic_api_key="", groq_api_key="")).available
        assert cls(settings(anthropic_api_key="", groq_api_key=KEY)).available
        assert cls(settings(anthropic_api_key="sk-ant-x", groq_api_key="")).available


# ---- fact check --------------------------------------------------------------------------------------------
async def test_fact_check_uses_groq_when_there_is_no_anthropic_key():
    from factcheck.llm import LLMClient, LLMUnavailable
    from tests.factcheck.conftest import make_settings

    g = Groq(groq_reply({"reasoning": "ok", "source_ids": [1]}))
    llm = LLMClient(make_settings(anthropic_api_key="", groq_api_key=KEY))
    with_groq(llm, g)
    assert '"reasoning"' in await llm.complete("sys", "usr", max_tokens=400, timeout=5)
    assert g.calls[0]["messages"][0]["role"] == "system"

    silent = LLMClient(make_settings(anthropic_api_key="", groq_api_key=KEY))
    with_groq(silent, Groq(500))
    with pytest.raises(LLMUnavailable):
        await silent.complete("sys", "usr", max_tokens=400, timeout=5)


async def test_fact_check_makes_no_call_when_neither_key_is_set():
    from factcheck.llm import LLMClient, LLMUnavailable
    from tests.factcheck.conftest import make_settings

    fake = FakeHttp({GROQ: groq_reply("x")})
    llm = LLMClient(make_settings(anthropic_api_key="", groq_api_key=""))
    llm.groq = GroqFallback("", client=fake.client())
    with pytest.raises(LLMUnavailable):
        await llm.complete("sys", "usr", max_tokens=100, timeout=5)
    assert fake.calls == []


# ---- contract audit ----------------------------------------------------------------------------------------
async def test_audit_extraction_falls_to_groq_and_asks_for_the_json_shape():
    from audit.llm import LLMClient
    from tests.audit.conftest import make_settings

    payload = {"obligations": [{"party": "Supplier", "duty": "deliver", "deadline": "30 days", "clause_ref": "",
                                "quote": "The Supplier shall deliver the Goods within 30 days.",
                                "status": "unverifiable", "reason": "not evidenced"}],
               "contract_flags": {"missing_dependency": False, "contradiction": False,
                                  "missing_key_terms": False, "note": ""},
               "overall_quotes": ["The Supplier shall deliver the Goods within 30 days."]}
    g = Groq(groq_reply(payload))
    llm = LLMClient(make_settings(anthropic_api_key="", groq_api_key=KEY))
    with_groq(llm, g)
    data = await llm.complete_json("sys", "usr", {"type": "object"}, max_tokens=900, timeout=5)
    assert data == payload
    # Groq has no structured-output mode, so the shape has to travel in the prompt.
    assert "obligations" in g.calls[0]["messages"][1]["content"] and g.calls[0]["json_mode"]


async def test_audit_reports_no_model_rather_than_inventing_one():
    from audit.llm import LLMClient, LLMUnavailable
    from tests.audit.conftest import make_settings

    llm = LLMClient(make_settings(anthropic_api_key="", groq_api_key=KEY))
    with_groq(llm, Groq(groq_reply("not json at all")))
    with pytest.raises(LLMUnavailable):
        await llm.complete_json("sys", "usr", {"type": "object"}, max_tokens=900, timeout=5)


# ---- fraud risk --------------------------------------------------------------------------------------------
async def test_fraud_risk_reasoning_falls_to_groq():
    from fraudrisk.llm import LLMClient
    from tests.fraudrisk.conftest import make_settings

    payload = {"signals": [{"id": "x.test", "applies": True, "strength": "strong", "note": "n"}],
               "pattern": "none", "summary": "A summary."}
    g = Groq(groq_reply(payload))
    llm = LLMClient(make_settings(anthropic_api_key="", groq_api_key=KEY))
    with_groq(llm, g)
    assert await llm.complete_json("sys", "usr", {"type": "object"}, max_tokens=700, timeout=5) == payload
    assert "signals" in g.calls[0]["messages"][1]["content"]


async def test_a_failing_anthropic_call_falls_through_to_groq():
    from fraudrisk.llm import LLMClient
    from tests.fraudrisk.conftest import make_settings

    llm = LLMClient(make_settings(anthropic_api_key="sk-ant-broken", groq_api_key=KEY))
    with_groq(llm, Groq(groq_reply({"pattern": "none", "summary": "s", "signals": []})))

    async def boom(*a, **kw):
        raise httpx.ConnectError("anthropic unreachable")

    llm._anthropic_json = boom
    out = await llm.complete_json("sys", "usr", {"type": "object"}, max_tokens=700, timeout=5)
    assert out["summary"] == "s"


async def test_anthropic_failing_with_no_groq_key_still_raises_for_the_deterministic_path():
    from fraudrisk.llm import LLMClient, LLMUnavailable
    from tests.fraudrisk.conftest import make_settings

    llm = LLMClient(make_settings(anthropic_api_key="sk-ant-broken", groq_api_key=""))

    async def boom(*a, **kw):
        raise LLMUnavailable("timeout")

    llm._anthropic_json = boom
    with pytest.raises(LLMUnavailable):
        await llm.complete_json("sys", "usr", {"type": "object"}, max_tokens=700, timeout=5)
