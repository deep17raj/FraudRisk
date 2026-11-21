"""Source registry. Add new evidence sources here."""
from __future__ import annotations

from ..config import Settings
from .base import NewsSearchSource, SearchProvider, Source, WebSearchSource
from .factcheck_api import GoogleFactCheckSource
from .search_providers import BraveProvider, SerperProvider, TavilyProvider
from .wikipedia import WikipediaSource

__all__ = ["Source", "SearchProvider", "build_sources", "pick_search_provider"]


def pick_search_provider(s: Settings) -> SearchProvider | None:
    providers = {
        "tavily": TavilyProvider(s.tavily_api_key),
        "serper": SerperProvider(s.serper_api_key),
        "brave": BraveProvider(s.brave_api_key),
    }
    if s.search_provider == "none":
        return None
    if s.search_provider in providers:
        p = providers[s.search_provider]
        return p if p.configured() else None
    for p in providers.values():          # auto: first one with a key
        if p.configured():
            return p
    return None


def build_sources(s: Settings) -> list[Source]:
    """Plugin list. Order = priority when trimming evidence. Disabled sources are filtered out."""
    sources: list[Source] = [GoogleFactCheckSource(s.google_factcheck_api_key)]
    provider = pick_search_provider(s)
    if provider:
        sources.append(WebSearchSource(provider))
        if s.enable_news:
            sources.append(NewsSearchSource(provider))
    sources.append(WikipediaSource(s.user_agent))
    return [src for src in sources if src.enabled()]
