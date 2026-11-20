"""Source registry. `build_sources` is the ONLY place the plugin list is declared.

Adding a signal source means appending one line here; the gatherer, scorer, reasoning layer
and HTTP layer are untouched, because routing comes from each source's `handles` tuple.
"""
from __future__ import annotations

from ..config import Settings
from .base import Source
from .card_bin import CardBinSource
from .feeds import FeedRegistry
from .ip_rep import IpReputationSource, TorExitSource, select_adapter
from .phish import DomainHeuristicSource, EmailHygieneSource, PhishFeedSource
from .rules import BehaviourRuleSource

__all__ = ["build_sources", "FeedRegistry", "Source"]


def build_sources(settings: Settings, feeds: FeedRegistry) -> list[Source]:
    sources: list[Source] = [
        # offline sources first: they always run, even when the network is unavailable
        BehaviourRuleSource(),
        DomainHeuristicSource(),
        EmailHygieneSource(),
    ]

    adapter = select_adapter(settings)
    if adapter is not None:
        sources.append(IpReputationSource(adapter, feeds))
    if settings.enable_tor_feed and feeds.tor is not None:
        sources.append(TorExitSource(feeds))
    if settings.enable_phish_feed and feeds.phish is not None:
        sources.append(PhishFeedSource(feeds))
    if settings.enable_bin_lookup:
        sources.append(CardBinSource())

    return [s for s in sources if s.enabled(settings)]
