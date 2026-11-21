"""Small in-memory TTL cache with single-flight de-duplication.

Swap for Redis by implementing the same three methods (`get`, `set`, `size`) if you scale beyond one
process. In-memory is correct for the hackathon: one instance, claims repeat across callers.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Optional


class TTLCache:
    def __init__(self, max_items: int = 2000, clock: Callable[[], float] = time.monotonic) -> None:
        self.max_items = max_items
        self._clock = clock
        self._data: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
        self._inflight: dict[str, asyncio.Future] = {}

    def get(self, key: str) -> Optional[Any]:
        item = self._data.get(key)
        if item is None:
            return None
        expires, value = item
        if expires <= self._clock():
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        if ttl <= 0:
            return
        self._data[key] = (self._clock() + ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_items:
            self._data.popitem(last=False)

    def size(self) -> int:
        return len(self._data)

    async def single_flight(self, key: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        """Concurrent identical requests share one computation.

        The computation runs as a detached task: if a caller is cancelled (e.g. its request hit the
        global timeout) the work still finishes and warms the cache for the next caller.
        """
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(factory())
            self._inflight[key] = task
            task.add_done_callback(lambda t, k=key: self._inflight.pop(k, None))
            task.add_done_callback(lambda t: t.cancelled() or t.exception())  # silence "never retrieved"
        return await asyncio.shield(task)
