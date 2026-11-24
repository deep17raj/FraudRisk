"""Groq as the fallback model for the three original intents: used only when Anthropic cannot answer."""
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


def fallback(groq: Groq, key: str = KEY, models: str = "") -> GroqFallback:
    return GroqFallback(key, models, client=transport({}, groq).client())


async def test_with_no_key_nothing_is_called_at_all():
    fake = FakeHttp({GROQ: groq_reply({"a": 1})})
    fb = GroqFallback("", client=fake.client())
    assert not fb.available
    assert await fb.complete("s", "u", max_tokens=100, timeout=5) is None
    assert await fb.complete_json("s", "u", max_tokens=100, timeout=5) is None
    assert fake.calls == []


async def test_a_reply_is_returned_and_json_is_parsed():
    assert await fallback(Groq(groq_reply("hello"))).complete("s", "u", max_tokens=100, timeout=5) == "hello"
    fb = fallback(Groq(groq_reply({"labels": [{"id": 1}]})))
    assert await fb.complete_json("s", "u", max_tokens=100, timeout=5) == {"labels": [{"id": 1}]}


async def test_json_mode_is_asked_for_and_the_models_setting_is_honoured():
    g = Groq(groq_reply({"a": 1}))
    fb = fallback(g, models="first-model,second-model")
    await fb.complete_json("sys", "usr", max_tokens=321, timeout=5)
    assert g.calls[0]["json_mode"] and g.calls[0]["model"] == "first-model"
    assert g.calls[0]["max_tokens"] == 321 and g.calls[0]["auth"] == f"Bearer {KEY}"


async def test_an_unusable_or_slow_answer_is_none_never_an_exception():
    assert await fallback(Groq(500)).complete("s", "u", max_tokens=10, timeout=5) is None
    assert await fallback(Groq(groq_reply("not json"))).complete_json("s", "u", max_tokens=10, timeout=5) is None
    no_time = fallback(Groq(groq_reply("x")))
    assert await no_time.complete("s", "u", max_tokens=10, timeout=0.1) is None     # below a round trip


async def test_a_transport_error_is_none_not_a_raise():
    fb = GroqFallback(KEY, client=FakeHttp({GROQ: httpx.ConnectError("down")}).client())
    assert await fb.complete("s", "u", max_tokens=10, timeout=5) is None
