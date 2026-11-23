"""Deterministic helpers that answer common messages with no model: arithmetic, the date, small talk, prediction."""
from __future__ import annotations

import ast
import operator
import re
from datetime import datetime, timezone
from typing import Optional

#: "Will X happen" style questions: the spec asks such a reply to lead with Likely / Unlikely / Uncertain.
PREDICTION = re.compile(
    r"^\s*(will|would|shall|is\s+.{1,80}?\s+(going|likely|expected|set|about)\s+to|are\s+.{1,80}?\s+(going|likely|expected)\s+to|"
    r"how\s+likely|what\s+(are|is)\s+the\s+(odds|chances|probability)|do\s+you\s+think\s+.{1,80}?\s+will|"
    r"can\s+you\s+predict|predict\s+whether)\b", re.I)

_GREET = re.compile(r"^\s*(hi|hello|hey|good\s+(morning|afternoon|evening)|greetings|yo)\b[\s!.,?]*$", re.I)
_THANKS = re.compile(r"^\s*(thanks|thank\s+you|thx|cheers)\b[\s!.,?a-z]*$", re.I)
_WHO_ARE_YOU = re.compile(r"^\s*(who|what)\s+are\s+you\b|^\s*what\s+can\s+you\s+do\b|^\s*what\s+is\s+your\s+name\b", re.I)
_DATE = re.compile(r"\b(what('?s| is)\s+(the\s+)?(today'?s\s+)?(date|day)|what\s+day\s+is\s+(it|today)|"
                   r"what\s+time\s+is\s+it|current\s+(date|time)|today'?s\s+date)\b", re.I)
_MATH_PREFIX = re.compile(r"^\s*(what('?s| is)|calculate|compute|evaluate|solve|how much is)\s+", re.I)
_MATH_WORDS = [(r"\bplus\b", "+"), (r"\bminus\b", "-"), (r"\b(times|multiplied by|x)\b", "*"),
               (r"\b(divided by|over)\b", "/"), (r"\bto the power of\b", "**"), (r"\bmod(ulo)?\b", "%"), (r"[×]", "*"),
               (r"[÷]", "/"), (r"\^", "**"), (r",", "")]
_MATH_OK = re.compile(r"^[\d\s+\-*/().%e]+$")
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}


def _eval(node: ast.AST, depth: int = 0) -> float:
    if depth > 20:
        raise ValueError("too deep")
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        v = _eval(node.operand, depth + 1)
        return v if isinstance(node.op, ast.UAdd) else -v
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        a, b = _eval(node.left, depth + 1), _eval(node.right, depth + 1)
        if isinstance(node.op, ast.Pow) and (abs(b) > 64 or abs(a) > 1e9):      # no 9**9**9 denial of service
            raise ValueError("exponent too large")
        return _OPS[type(node.op)](a, b)
    raise ValueError("not arithmetic")


def arithmetic(message: str) -> Optional[str]:
    """'What is 12 * (3 + 4)?' -> '12 * (3 + 4) = 84'. None unless the whole message is arithmetic."""
    s = _MATH_PREFIX.sub("", message.strip().rstrip("?=. ").lower())
    for pat, rep in _MATH_WORDS:
        s = re.sub(pat, rep, s)
    s = s.strip()
    if not s or not _MATH_OK.match(s) or not re.search(r"\d", s) or not re.search(r"[+\-*/%]", s.lstrip("+-")):
        return None
    try:
        v = _eval(ast.parse(s, mode="eval").body)
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError, RecursionError):
        return None
    if isinstance(v, float):
        v = round(v, 10)
        v = int(v) if v == int(v) and abs(v) < 1e15 else v
    expression = re.sub(r"\s+", " ", s)
    return f"{expression} = {v}"


def date_reply(message: str, now: Optional[datetime] = None) -> Optional[str]:
    if not _DATE.search(message):
        return None
    now = now or datetime.now(timezone.utc)
    return f"It is {now.strftime('%A, %d %B %Y')}, {now.strftime('%H:%M')} UTC."


def smalltalk(message: str) -> Optional[str]:
    if _GREET.match(message):
        return "Hello! Ask me a question or give me something to explain, and I'll do my best."
    if _THANKS.match(message):
        return "You're welcome!"
    if _WHO_ARE_YOU.match(message):
        return ("I'm an AI assistant that answers general-knowledge and reasoning questions. For live data such as "
                "prices or news I'd need to search, and I will say when I can't be sure.")
    return None


_TOPIC = [
    re.compile(r"^(?:what|who)\s+(?:is|are|was|were)\s+(?:an?\s+|the\s+)?(.+)$", re.I),
    re.compile(r"^(?:explain|define|describe|summari[sz]e|tell\s+me\s+about|what\s+does)\s+(?:what\s+|how\s+|the\s+|an?\s+)?(.+)$", re.I),
    re.compile(r"^(?:why|how)\s+(?:is|are|does|do|did|was|were)\s+(.+)$", re.I),
]


def topic_of(message: str) -> Optional[str]:
    """The subject of an explanatory question ('Why is the sky blue?' -> 'the sky blue'), or None."""
    s = message.strip().rstrip("?!. ")
    for pat in _TOPIC:
        m = pat.match(s)
        if m:
            t = re.sub(r"\b(work|works|mean|means|do|does)$", "", m.group(1).strip(), flags=re.I).strip()
            return t or None
    return None
