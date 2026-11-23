"""CHAT_COMPLETION_GENERAL: a direct reply to a general-knowledge, reasoning or conversational message.

With `GROQ_API_KEY` the reply is written by Groq. Without it, or whenever Groq is rate-limited, rejects the key,
errors or is too slow, the reply comes from deterministic helpers instead, in this order: arithmetic, the date and
time, small talk, then a reference lookup (Wikipedia / DuckDuckGo) for "what is / explain / why" questions. A "will X
happen" question gets `verdict` Likely / Unlikely / Uncertain and a reply that leads with that word; without a model
the honest verdict is Uncertain. A reply is never empty.
"""
from __future__ import annotations

import re
from typing import Literal, Optional

from fastapi import FastAPI
from pydantic import BaseModel

from intentkit.answers import extractive
from intentkit.groq import parse_json_object
from intentkit.llm_pipeline import UNTRUSTED_NOTE, LlmPipeline, today
from intentkit.pipeline import Req, Result
from intentkit.retrieval import dedupe_rank, duckduckgo, gather_docs, search_phrase, wikipedia
from intentkit.service import StrictModel, create_service_app
from intentkit.text import brief, clip

from .config import Settings
from .helpers import PREDICTION, arithmetic, date_reply, smalltalk, topic_of

VERSION = "0.1.0"
FIELDS = {"message": ("prompt", "text", "question", "query", "input", "q", "content")}
Verdict = Literal["Likely", "Unlikely", "Uncertain"]
_VERDICTS = ("Likely", "Unlikely", "Uncertain")

SYSTEM = ("You are a concise, accurate assistant. Answer the user's message directly in at most 6 sentences, in plain "
          "text. If you are unsure or the question needs live data you do not have, say so instead of guessing. If the "
          "message asks whether something WILL happen, begin the reply with exactly one of the words Likely, Unlikely or "
          'Uncertain and set "verdict" to that word; otherwise set "verdict" to null. ' + UNTRUSTED_NOTE)


class ChatCompletionResponse(StrictModel):
    """Exactly the response fields of Telegraph's CHAT_COMPLETION_GENERAL intent. Nothing else."""

    reply: str = ""
    verdict: Optional[Verdict] = None
    brief: str = ""


def _clean_verdict(value: object) -> Optional[str]:
    return next((v for v in _VERDICTS if isinstance(value, str) and value.strip().capitalize() == v), None)


def _lead(reply: str, verdict: str) -> str:
    """Make the reply lead with the verdict word, as the spec describes."""
    return reply if reply.lower().startswith(verdict.lower()) else f"{verdict}. {reply}"


class ChatCompletionPipeline(LlmPipeline):
    intent = "CHAT_COMPLETION_GENERAL"
    WARM = ("https://en.wikipedia.org/w/api.php?action=query&meta=siteinfo&format=json",
            "https://api.duckduckgo.com/?q=a&format=json")

    def key(self, req: Req) -> Optional[str]:
        m = re.sub(r"\s+", " ", req.get("message", "")).strip().lower()
        return f"cc:{m}" if m else None

    def empty(self, req: Req, reason: str) -> BaseModel:
        m = req.get("message", "").strip()
        if not m:
            return ChatCompletionResponse()
        text = f"I couldn't produce a reply to that right now ({reason}). Please try again in a moment."
        return ChatCompletionResponse(reply=text, brief=brief(text))

    @staticmethod
    def _model(reply: str, verdict: Optional[str], limit: int) -> ChatCompletionResponse:
        reply = clip(reply, limit)
        return ChatCompletionResponse(reply=reply, verdict=verdict, brief=brief(reply))  # type: ignore[arg-type]

    async def compute(self, req: Req) -> Result:
        s: Settings = self.settings
        msg = re.sub(r"\s+", " ", req.get("message", "")).strip()
        if not msg:
            return Result(self._model("Please send a message to reply to.", None, s.max_reply_chars), degraded=True)
        predict = bool(PREDICTION.match(msg))
        deadline = self.deadline()

        # Fast deterministic answers need no model and are exact, so they never wait for one.
        exact = arithmetic(msg) or date_reply(msg) or smalltalk(msg)
        if exact and not predict:
            return Result(self._model(exact, None, s.max_reply_chars), ttl=0 if date_reply(msg) else None)

        # The keyless reply: built first, so something complete exists whatever the model does.
        keyless = await self._keyless(msg, predict, deadline)
        used_llm = False
        reply, verdict = keyless
        if self.groq.available() and self.left(deadline) > 1.0:
            user = (f"Today's date: {today()}\nMessage: <<<{msg}>>>\n"
                    'Return JSON: {"reply": "<your reply>", "verdict": "Likely|Unlikely|Uncertain|null"}')
            raw = await self.groq.chat(SYSTEM, user, json_mode=True, budget=self.left(deadline) - 0.3, max_tokens=500,
                                       temperature=0.3)
            obj = parse_json_object(raw)
            text = obj.get("reply") if obj else None
            if isinstance(text, str) and len(text.strip()) >= 2:
                reply, used_llm = text.strip(), True
                verdict = _clean_verdict(obj.get("verdict")) if predict else None
                if predict and verdict is None:
                    verdict = next((v for v in _VERDICTS if reply.lower().startswith(v.lower())), "Uncertain")
        if predict and verdict:
            reply = _lead(reply, verdict)
        return Result(self._model(reply, verdict, s.max_reply_chars), degraded=not used_llm and self.groq.configured,
                      ttl=self.llm_ttl(used_llm))

    async def _keyless(self, msg: str, predict: bool, deadline: float) -> tuple[str, Optional[str]]:
        """A reply from helpers and reference sources only. Always non-empty and honest about what it is."""
        topic = topic_of(msg)
        query = topic or (msg if len(msg) <= 160 else "")
        docs = []
        if query:
            phrase = search_phrase(query)
            found = await gather_docs([duckduckgo(self.up, phrase), wikipedia(self.up, phrase, 2)],
                                      timeout=min(self.settings.provider_timeout, max(0.5, self.left(deadline) - 0.5)),
                                      soft=2.0)
            docs = dedupe_rank(found, query, 3, min_overlap=0.3 if topic else 0.5)
        context, _ = extractive(query, docs, max_docs=2, max_chars=600) if docs else ("", [])
        if predict:
            base = ("I can't reliably predict that, and the outcome depends on facts I can't verify right now. "
                    "Treat anything said about it as uncertain.")
            return (f"{base} For background: {context}" if context else base), "Uncertain"
        if context:
            return (f"Here is what a reference source says: {context}" if topic else
                    f"The closest reference I found says: {context}"), None
        return ("I don't have enough reliable information to answer that confidently without my language model. "
                "Could you rephrase it or ask something more specific?"), None


def create_app(settings: Optional[Settings] = None, pipeline: Optional[ChatCompletionPipeline] = None) -> FastAPI:
    return create_service_app(
        title="Telegraph Chat Completion Miner", intent="CHAT_COMPLETION_GENERAL", path="/chat-completion",
        version=VERSION, fields=FIELDS, model=ChatCompletionResponse, settings=settings or Settings.from_env(),
        pipeline=pipeline, make_pipeline=ChatCompletionPipeline, max_len=2000)
