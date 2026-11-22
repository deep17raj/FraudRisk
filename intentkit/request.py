"""Lenient request reading shared by the intent endpoints.

Telegraph's node builds each call from the endpoint's declared params, but callers and gateways do not
always use the documented name, and a malformed body must still be answered (HTTP 200) rather than
rejected. Anything unusable simply reads as an empty string and the endpoint answers honestly.
"""
from __future__ import annotations

from typing import Mapping, Sequence

from fastapi import Request

#: Names callers commonly use for "the thing to check" when they do not use the documented one.
GENERIC_ALIASES: tuple[str, ...] = ("query", "input", "text", "question", "q")


async def read_fields(request: Request, fields: Mapping[str, Sequence[str]], *, max_len: int = 2000) -> dict[str, str]:
    """Return each wanted field as a stripped string ('' when absent or not a string).

    The JSON body is consulted first, then the query string. A field may arrive under any of its
    aliases; the documented name always wins.
    """
    try:
        body = await request.json()
    except Exception:
        body = None
    out: dict[str, str] = {}
    for name, aliases in fields.items():
        keys = (name, *aliases)
        value = ""
        if isinstance(body, dict):
            for key in keys:
                cand = body.get(key)
                if isinstance(cand, str) and cand.strip():
                    value = cand.strip()
                    break
        if not value:
            for key in keys:
                cand = request.query_params.get(key)
                if cand and cand.strip():
                    value = cand.strip()
                    break
        out[name] = value[:max_len]
    return out
