"""Plugin interface for signal sources.

To add a source: subclass `Source`, declare the entity kinds it `handles`, implement
`enabled()` and `collect()`, and register it in `fraudrisk/sources/__init__.py::build_sources`.
Nothing in the core pipeline changes - routing is driven entirely by `handles`.

Every source returns normalized `Signal` objects: {id, text, severity 0-1, reliability 0-1}.
A source NEVER raises for "nothing found" (it returns an empty list); it raises only on
transport/HTTP failures, which the gatherer turns into a dropped signal via the circuit
breaker, never into a failed request.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import httpx

from ..config import Settings
from ..models import ParsedSubject, Signal
from ..resilience import CircuitBreaker


class Source(ABC):
    name: str = "source"
    #: entity kinds this source can say something about
    handles: tuple = ()
    #: how much the fusion stage trusts this source (0-1), before per-signal severity
    reliability: float = 0.7
    #: True when the source makes no network call (so it always runs, even in a timeout)
    offline: bool = False

    def __init__(self) -> None:
        self.breaker = CircuitBreaker()

    def relevant(self, parsed: ParsedSubject) -> bool:
        """Default routing: run when the subject carries an entity kind we handle."""
        return any(e.kind in self.handles for e in parsed.entities)

    @abstractmethod
    def enabled(self, settings: Settings) -> bool:
        """False when the source lacks credentials or is switched off; then it is skipped."""

    @abstractmethod
    async def collect(self, parsed: ParsedSubject, client: httpx.AsyncClient,
                      timeout: float, settings: Settings) -> list[Signal]:
        """Return normalized signals. Raise on transport/HTTP errors."""
