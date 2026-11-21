"""Plugin interface for evidence sources.

To add a source: subclass `Source`, implement `enabled()` and `search()`, and register it in
`factcheck/sources/__init__.py::build_sources`. Nothing in the core pipeline needs to change.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import httpx

from ..models import Evidence, ParsedClaim
from ..resilience import CircuitBreaker


class Source(ABC):
    name: str = "source"
    #: sources flagged `recency_only` are only queried for time-sensitive claims (e.g. news APIs)
    recency_only: bool = False

    def __init__(self) -> None:
        self.breaker = CircuitBreaker()

    @abstractmethod
    def enabled(self) -> bool:
        """False when the source lacks credentials / is switched off; it is then skipped silently."""

    @abstractmethod
    async def search(self, claim: ParsedClaim, client: httpx.AsyncClient, timeout: float) -> list[Evidence]:
        """Return evidence snippets. Raise on transport/HTTP errors (the caller handles breaker+retry)."""


class SearchProvider(ABC):
    """Vendor adapter for a general web / news search API. Swap vendors via SEARCH_PROVIDER."""

    name: str = "provider"

    @abstractmethod
    def configured(self) -> bool: ...

    @abstractmethod
    async def web(self, query: str, client: httpx.AsyncClient, timeout: float) -> list[Evidence]: ...

    @abstractmethod
    async def news(self, query: str, client: httpx.AsyncClient, timeout: float) -> list[Evidence]: ...


class WebSearchSource(Source):
    def __init__(self, provider: SearchProvider) -> None:
        super().__init__()
        self.provider = provider
        self.name = f"{provider.name}-web"

    def enabled(self) -> bool:
        return self.provider.configured()

    async def search(self, claim, client, timeout):
        return await self.provider.web(claim.search_queries[0], client, timeout)


class NewsSearchSource(Source):
    recency_only = True

    def __init__(self, provider: SearchProvider) -> None:
        super().__init__()
        self.provider = provider
        self.name = f"{provider.name}-news"

    def enabled(self) -> bool:
        return self.provider.configured()

    async def search(self, claim, client, timeout):
        return await self.provider.news(claim.search_queries[0], client, timeout)
