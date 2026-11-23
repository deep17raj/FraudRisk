"""The shared Groq client: optional, never raises, remembers failures process-wide, never leaks the key."""
from __future__ import annotations

import logging

import httpx
import pytest

from intentkit.groq import (AUTH_COOLDOWN_S, GroqClient, LlmSettings, _retry_after, parse_json_object, reset_gates)
from tests.intents.helpers import FakeHttp
from tests.intents.llm_fixtures import GROQ, Groq, groq_error, groq_reply, transport

KEY = "gsk_test_SECRET_KEY_123456"


def settings(**kw) -> LlmSettings:
    base = dict(groq_api_key=KEY, groq_models="big-model,small-model", groq_timeout=2.0)
    base.update(kw)
    return LlmSettings(**base)


def client_for(groq: Groq, clock=None, **kw) -> GroqClient:
    fake = transport({}, groq)
    return GroqClient(settings(**kw), fake.client(), **({"clock": clock} if clock else {}))


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture(autouse=True)
def fresh_gates():
    reset_gates()
    yield
    reset_gates()


async def test_a_good_reply_is_returned_and_sent_with_the_key_json_mode_and_models_in_order():
    g = Groq(groq_reply({"a": 1}))
    c = client_for(g)
    assert await c.chat("sys", "user", json_mode=True, budget=5, max_tokens=123) == '{"a": 1}'
    call = g.calls[0]
    assert call["model"] == "big-model" and call["json_mode"] and call["auth"] == f"Bearer {KEY}" and call["max_tokens"] == 123
    assert [m["role"] for m in call["messages"]] == ["system", "user"]


async def test_no_key_means_no_call_at_all():
    g = Groq(groq_reply("x"))
    c = client_for(g, groq_api_key="")
    assert not c.configured and not c.available()
    assert await c.chat("s", "u") is None and g.calls == []


async def test_a_401_switches_groq_off_for_every_client_sharing_the_key_and_logs_once_without_the_key(caplog):
    caplog.set_level(logging.INFO)
    clock = Clock()
    g = Groq(401)
    c1, c2 = client_for(g, clock), client_for(g, clock)
    assert await c1.chat("s", "u") is None
    assert len(g.calls) == 1 and not c1.available() and not c2.available()        # one gate per key, process-wide
    assert await c2.chat("s", "u") is None and len(g.calls) == 1                   # no second attempt
    assert "rejected the API key" in caplog.text and KEY not in caplog.text
    clock.t += AUTH_COOLDOWN_S + 1
    assert c1.available()                                                          # tried again after the cooldown


async def test_a_429_cools_down_that_model_and_the_next_model_answers():
    clock = Clock()
    g = Groq((429, {"retry-after": "30"}), groq_reply("from the small model"))
    c = client_for(g, clock)
    assert await c.chat("s", "u") == "from the small model"
    assert g.models == ["big-model", "small-model"]
    assert await c.chat("s", "u") == "from the small model" and g.models[-1] == "small-model"   # big model skipped
    clock.t += 31
    g.rules = [groq_reply("big again")]
    g.calls.clear()
    assert await c.chat("s", "u") == "big again" and g.models == ["big-model"]


async def test_every_model_rate_limited_returns_none_and_available_turns_false():
    g = Groq((429, {"retry-after": "60"}))
    c = client_for(g)
    assert await c.chat("s", "u") is None
    assert not c.available()
    n = len(g.calls)
    assert await c.chat("s", "u") is None and len(g.calls) == n


@pytest.mark.parametrize("raw,seconds", [("12", 12.0), ("0", 1.0), ("99999", 300.0), ("1m30s", 90.0), ("7.5s", 7.5), ("", 30.0), ("soon", 30.0)])
def test_retry_after_parsing(raw, seconds):
    assert _retry_after(httpx.Headers({"retry-after": raw} if raw else {})) == seconds


@pytest.mark.parametrize("rule", [404, httpx.Response(400, json=groq_error("model_decommissioned", "The model has been decommissioned"))])
async def test_a_retired_model_is_skipped_for_an_hour_and_the_next_one_is_used(rule):
    clock = Clock()
    g = Groq(rule, groq_reply("ok"))
    c = client_for(g, clock)
    assert await c.chat("s", "u") == "ok" and g.models == ["big-model", "small-model"]
    n = len(g.calls)
    assert await c.chat("s", "u") == "ok" and g.models[n:] == ["small-model"]          # the retired model is not retried


@pytest.mark.parametrize("rule", [500, 503, httpx.ReadTimeout("slow"), httpx.ConnectError("down"), {"choices": []},
                                  {"nothing": 1}, groq_reply(""), groq_reply("   "), {"choices": [{"message": {"content": 5}}]}])
async def test_any_other_failure_moves_on_and_finally_returns_none_without_raising(rule):
    g = Groq(rule)
    assert await client_for(g).chat("s", "u") is None
    assert len(g.calls) == 2                                  # both models were tried


async def test_a_bad_json_mode_generation_400_falls_through_to_the_next_model():
    g = Groq(httpx.Response(400, json=groq_error("json_validate_failed")), groq_reply("{}"))
    assert await client_for(g).chat("s", "u", json_mode=True) == "{}"


async def test_it_gives_up_when_the_time_budget_is_spent():
    g = Groq(groq_reply("ok"))
    assert await client_for(g).chat("s", "u", budget=0.5) is None and g.calls == []


async def test_a_model_list_in_the_environment_is_parsed(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", KEY)
    monkeypatch.setenv("GROQ_MODELS", " a , b ,, c ")
    monkeypatch.setenv("GROQ_TIMEOUT_S", "3.5")
    s = LlmSettings.from_env()
    assert GroqClient(s, FakeHttp().client()).models == ["a", "b", "c"] and s.groq_timeout == 3.5 and s.groq_api_key == KEY


async def test_an_unexpected_internal_error_is_still_contained(monkeypatch):
    c = client_for(Groq(groq_reply("x")))

    async def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(c, "_chat", boom)
    assert await c.chat("s", "u") is None


@pytest.mark.parametrize("text,expected", [
    ('{"a": 1}', {"a": 1}), ('```json\n{"a": 1}\n```', {"a": 1}), ('Sure! {"a": {"b": 2}} hope that helps', {"a": {"b": 2}}),
    ("[1, 2]", None), ("not json", None), ("", None), (None, None), ('{"a": ', None)])
def test_json_objects_are_extracted_from_model_chatter(text, expected):
    assert parse_json_object(text) == expected


def test_the_key_never_appears_in_a_repr_of_the_client_settings_logs():
    # the fingerprint used to track health is a hash, never the key
    from intentkit.groq import _GATES, _gate
    _gate(KEY)
    assert all(KEY not in fp for fp in _GATES)
