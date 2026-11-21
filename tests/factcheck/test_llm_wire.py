"""Exercise the real Anthropic SDK against a local mock Messages endpoint.

No API key and no network: a tiny HTTP server stands in for api.anthropic.com via ANTHROPIC_BASE_URL.
This is the only test that covers the actual wire path - request construction by the SDK, our params,
the `thinking: disabled` 400 fallback, response parsing, and the pipeline running in true `llm` mode.
It does NOT prove a real model answers well, only that the request we send is well-formed and that we
read the reply correctly.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from factcheck.llm import LLMClient
from factcheck.models import Stance, Verdict
from factcheck.pipeline import FactCheckPipeline
from tests.factcheck.conftest import FakeSource, ev, make_settings

REQUESTS: list[dict] = []
MODE = {"reject_thinking": False, "reply": "", "refuse": False}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        REQUESTS.append({"path": self.path, "body": body, "headers": dict(self.headers)})
        if MODE["reject_thinking"] and "thinking" in body:
            payload = {"type": "error", "error": {"type": "invalid_request_error",
                                                  "message": 'thinking: "disabled" is not supported for this model.'}}
            self._send(400, payload)
            return
        msg = {
            "id": "msg_test", "type": "message", "role": "assistant", "model": body.get("model", "m"),
            "content": [{"type": "text", "text": MODE["reply"]}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
        if MODE["refuse"]:      # safety classifier declined: HTTP 200 with stop_reason "refusal"
            msg.update(stop_reason="refusal", content=[],
                       stop_details={"type": "refusal", "category": "general_harms"})
        self._send(200, msg)

    def _send(self, code, payload):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def mock_api(monkeypatch):
    REQUESTS.clear()
    MODE.update(reject_thinking=False, reply="{}", refuse=False)
    # Threading matters: the SDK keeps connections alive, and a single-threaded server deadlocks.
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{srv.server_address[1]}")
    monkeypatch.setattr(LLMClient, "_thinking_disabled_ok", True)
    yield MODE
    srv.shutdown()


def _settings():
    return make_settings(anthropic_api_key="sk-ant-test", stance_mode="auto",
                         stance_timeout=5.0, synthesis_timeout=5.0, global_timeout=20.0)


EVIDENCE = [
    ev("https://www.reuters.com/a", "Eiffel Tower", "The Eiffel Tower is a tower located in Paris, France.",
       provider="tavily-general"),
    ev("https://www.britannica.com/b", "Eiffel Tower", "The Eiffel Tower stands in Paris, France.",
       provider="tavily-general"),
]


async def test_llm_request_shape_and_full_llm_mode_pipeline(mock_api):
    mock_api["reply"] = json.dumps({
        "labels": [{"id": 1, "stance": "supports", "relevance": 0.95, "confidence": 0.9, "note": "states it"},
                   {"id": 2, "stance": "supports", "relevance": 0.95, "confidence": 0.9, "note": "states it"}],
        "reasoning": "Both reuters.com and britannica.com state plainly that the tower stands in Paris, France. "
                     "No source contradicts this.",
        "source_ids": [1, 2],
    })
    s = _settings()
    async with httpx.AsyncClient() as client:
        pl = FactCheckPipeline(s, client, sources=[FakeSource("fake", EVIDENCE)])
        r = await pl.check("The Eiffel Tower is located in Paris.")

    assert r.debug["stance_mode"] == "llm" and r.debug["synthesis_mode"] == "llm"
    # Model labels are trusted, so the heuristic cap does not apply: True is reachable.
    assert r.verdict == Verdict.TRUE and r.debug["heuristic_only"] is False
    assert r.reasoning.startswith("Both reuters.com") and set(r.sources) == {e.url for e in EVIDENCE}

    assert len(REQUESTS) == 2, "expected one stance call and one synthesis call"
    for req in REQUESTS:
        b = req["body"]
        assert req["path"].endswith("/v1/messages")
        assert b["model"] == s.model and isinstance(b["max_tokens"], int)
        assert b["messages"] == [{"role": "user", "content": b["messages"][0]["content"]}]
        assert b["system"] and isinstance(b["system"], str)
        assert b["thinking"] == {"type": "disabled"}
        assert b["output_config"] == {"effort": "low"}          # extra_body is merged into the top level
        assert "x-api-key" in {k.lower() for k in req["headers"]}
        json.loads(b["messages"][0]["content"])                 # we always send JSON payloads


async def test_400_on_thinking_falls_back_within_budget_and_is_remembered(mock_api):
    """Current Opus/Sonnet models reject thinking:disabled; the retry must keep effort and not repeat."""
    mock_api["reject_thinking"] = True
    mock_api["reply"] = json.dumps({"labels": [{"id": 1, "stance": "refutes", "relevance": 0.9, "confidence": 0.9}],
                                    "reasoning": "reuters.com directly contradicts the claim in its report. "
                                                 "No other source supports it.",
                                    "source_ids": [1]})
    s = _settings()
    async with httpx.AsyncClient() as client:
        pl = FactCheckPipeline(s, client, sources=[FakeSource("fake", EVIDENCE)])
        r1 = await pl.check("The Eiffel Tower is located in Paris.")
        r2 = await pl.check("The Louvre is located in Lyon.")

    assert r1.debug["stance_mode"] == "llm" and r1.debug["synthesis_mode"] == "llm"
    assert LLMClient._thinking_disabled_ok is False
    rejected = [r for r in REQUESTS if "thinking" in r["body"]]
    accepted = [r for r in REQUESTS if "thinking" not in r["body"]]
    assert len(rejected) == 1, "the doomed parameter must be tried once per process, not per call"
    assert accepted and all(r["body"]["output_config"] == {"effort": "low"} for r in accepted), \
        "the retry must keep effort: low"
    assert r2.debug["stance_mode"] == "llm"


async def test_server_error_falls_back_to_heuristic_and_template(mock_api):
    mock_api["reply"] = "this is not json"
    s = _settings()
    async with httpx.AsyncClient() as client:
        pl = FactCheckPipeline(s, client, sources=[FakeSource("fake", EVIDENCE)])
        r = await pl.check("The Eiffel Tower is located in Paris.")
    assert r.debug["stance_mode"] == "heuristic(llm_failed)"
    assert r.debug["synthesis_mode"] == "template(llm_failed)"
    assert r.verdict in (Verdict.MOSTLY_TRUE, Verdict.TRUE) and r.sources
    assert r.debug["heuristic_only"] is True and "+heuristic_cap" in r.debug["rule"]


async def test_refusal_stop_reason_is_treated_as_unavailable(mock_api):
    """A safety refusal must degrade to heuristic/template, never surface as a verdict or an error."""
    mock_api["refuse"] = True
    mock_api["reply"] = "irrelevant"
    s = _settings()
    async with httpx.AsyncClient() as client:
        pl = FactCheckPipeline(s, client, sources=[FakeSource("fake", EVIDENCE)])
        r = await pl.check("The Eiffel Tower is located in Paris.")
    assert "llm_failed" in r.debug["stance_mode"] and "llm_failed" in r.debug["synthesis_mode"]
    assert r.verdict != Verdict.UNVERIFIED and r.sources
