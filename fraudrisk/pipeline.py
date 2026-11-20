"""Orchestration. Every stage lives in its own module; this file only wires and budgets them.

Stage order: classify -> gather (parallel, deadlined) -> reason (one LLM call) -> score
(deterministic) -> respond. The score is always computed AFTER the model has annotated the
signals and is never taken from the model, which is what keeps `risk_score` reproducible and
always consistent with `risk_level`.

Three outcomes are all LOW with score 0.0 and they are worded differently on purpose:
  * nothing assessable  - the subject carries no identifier and no behavioural detail;
  * checked and clean   - sources ran and returned only benign observations;
  * could not check     - every source failed, so absence of signal proves nothing (not cached).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

from .cache import TTLCache
from .classify import cache_key, classify
from .config import Settings
from .gather import Gatherer
from .llm import LLMClient
from .models import Assessment, GatherResult, ParsedSubject, RiskLevel
from .patterns import Pattern
from .reasoning import ReasoningService, template_summary
from .scoring import DEFAULT_THRESHOLDS, Thresholds, compute_score, family_weights
from .sources import FeedRegistry, build_sources
from .sources.base import Source

log = logging.getLogger("fraudrisk.pipeline")

#: Time kept back to build and return the response.
RESERVE_S = 0.35


def _low(subject: str, summary: str, *, signals: Optional[list[str]] = None,
         cacheable: bool = True, ttl: Optional[int] = None,
         debug: Optional[dict] = None) -> Assessment:
    return Assessment(subject=subject, level=RiskLevel.LOW, score=0.0,
                      signals=signals or [], summary=summary,
                      cacheable=cacheable, cache_ttl=ttl, debug=debug or {})


def fallback_assessment(subject: str, why: str) -> Assessment:
    """Used by the HTTP layer when the pipeline timed out or crashed: always valid and honest."""
    return _low(
        subject,
        "The assessment could not be completed in time, so no fraud signal was confirmed for "
        "this subject. It is graded at the floor on insufficient information rather than on any "
        "positive indication that it is safe.",
        cacheable=False, debug={"fallback": why, "rule": "F1_fallback"},
    )


class FraudRiskPipeline:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        sources: Optional[list[Source]] = None,
        llm: Optional[LLMClient] = None,
        cache: Optional[TTLCache] = None,
        feeds: Optional[FeedRegistry] = None,
        thresholds: Thresholds = DEFAULT_THRESHOLDS,
    ) -> None:
        self.s = settings
        self.client = client
        self.t = thresholds
        self.feeds = feeds if feeds is not None else FeedRegistry(
            settings.feed_ttl, settings.feed_refresh_timeout,
            tor=settings.enable_tor_feed, phish=settings.enable_phish_feed)
        self.sources = sources if sources is not None else build_sources(settings, self.feeds)
        self.llm = llm or LLMClient(settings)
        self.reasoner = ReasoningService(self.llm, settings.reasoning_mode)
        self.gatherer = Gatherer(self.sources, client, settings)
        self.cache = cache or TTLCache(settings.cache_max_items)

    # ------------------------------------------------------------------ public

    async def assess(self, subject: str) -> Assessment:
        t0 = time.monotonic()
        subject = (subject or "").strip()
        try:
            parsed = classify(subject, self.s.max_subject_chars)
            if not parsed.assessable:
                res = _low(subject,
                           template_summary([], Pattern.NONE, insufficient=True),
                           cacheable=False,
                           debug={"rule": "A1_not_assessable", "reason": parsed.vague_reason,
                                  "primary_kind": parsed.primary_kind.value})
            else:
                key = cache_key(parsed)
                cached: Optional[Assessment] = self.cache.get(key)
                if cached is not None:
                    res = Assessment(subject=subject, level=cached.level, score=cached.score,
                                     signals=list(cached.signals), summary=cached.summary,
                                     cacheable=True, cache_ttl=cached.cache_ttl,
                                     debug={**cached.debug, "cache_hit": True})
                else:
                    shared = await self.cache.single_flight(key, lambda: self._compute(parsed, key))
                    # carry the caching decision through: a could-not-check answer must keep
                    # saying so, since callers and tests read it off the returned object
                    res = Assessment(subject=subject, level=shared.level, score=shared.score,
                                     signals=list(shared.signals), summary=shared.summary,
                                     cacheable=shared.cacheable, cache_ttl=shared.cache_ttl,
                                     debug=dict(shared.debug))
        except asyncio.CancelledError:
            raise
        except Exception as exc:   # last line of defence: never surface a 500 to Telegraph
            log.exception("pipeline crashed")
            res = fallback_assessment(subject, f"exception:{type(exc).__name__}")

        res.debug["latency_ms"] = int((time.monotonic() - t0) * 1000)
        self._log(res)
        return res

    # ------------------------------------------------------------------ internals

    async def _compute(self, parsed: ParsedSubject, key: str) -> Assessment:
        t0 = time.monotonic()
        deadline = t0 + self.s.global_timeout - RESERVE_S
        timings: dict[str, int] = {}

        def remaining() -> float:
            return deadline - time.monotonic()

        # keep the bulk feeds warm without ever blocking this request on them
        self.feeds.ensure_fresh(self.client)

        # --- stage 2: gather
        budget = min(self.s.gather_timeout, max(0.5, remaining() - self.s.reasoning_timeout - 0.4))
        gr: GatherResult = await self.gatherer.gather(parsed, budget)
        timings["gather_ms"] = gr.elapsed_ms

        debug = {
            "primary_kind": parsed.primary_kind.value,
            "entities": [{"kind": e.kind.value, "value": e.value} for e in parsed.entities],
            "sources": gr.source_status,
            "n_signals": len(gr.signals),
            "degraded": gr.degraded,
        }

        if not gr.signals:
            return self._store(key, self._no_signal_result(parsed, gr, debug, timings))

        # --- stage 3: reasoning (annotate only; it cannot add evidence or set the score)
        ts = time.monotonic()
        r_budget = min(self.s.reasoning_timeout, max(0.0, remaining()))
        reasoning = await self.reasoner.reason(parsed, gr.signals, r_budget, degraded=gr.degraded)
        timings["reasoning_ms"] = int((time.monotonic() - ts) * 1000)

        # --- stage 4: deterministic scoring over the annotated signals
        score = compute_score(reasoning.signals, self.t)

        # --- stage 6: the returned signal list - concrete, applicable, risk-bearing first
        applicable = [s for s in reasoning.signals if s.applicable]
        signal_texts = [s.text for s in applicable]

        summary = reasoning.summary
        if not summary:
            summary = template_summary(applicable, reasoning.pattern, degraded=gr.degraded)

        llm_degraded = reasoning.mode != "llm"
        debug.update(
            rule=score.rule, pattern=reasoning.pattern.value, families=score.families,
            corroborated=score.corroborated, reasoning_mode=reasoning.mode,
            dropped_signal_ids=reasoning.dropped_ids,
            family_weights={k: round(v, 4) for k, v in family_weights(reasoning.signals, self.t).items()},
            signals=[{"id": s.id, "source": s.source, "family": s.family.value,
                      "severity": s.severity, "reliability": round(s.reliability, 3),
                      "applicable": s.applicable, "strength": s.strength.value,
                      "benign": s.benign, "weight": round(s.weight, 4), "note": s.note}
                     for s in reasoning.signals],
            timings=timings,
        )

        return self._store(key, Assessment(
            subject=parsed.original, level=score.level, score=score.score,
            signals=signal_texts, summary=summary,
            cacheable=True,
            cache_ttl=self.s.cache_ttl_degraded if (gr.degraded or llm_degraded) else self.s.cache_ttl,
            debug=debug))

    def _no_signal_result(self, parsed: ParsedSubject, gr: GatherResult, debug: dict,
                          timings: dict) -> Assessment:
        """No signal came back. Which of the three LOW outcomes is this?"""
        debug = {**debug, "timings": timings}
        if gr.all_failed:
            debug["rule"] = "A3_sources_unreachable"
            return _low(parsed.original, template_summary([], Pattern.NONE, degraded=True),
                        cacheable=False, debug=debug)

        # Nothing ran that could speak to this KIND of subject (e.g. a bare wallet address:
        # no source covers wallets). Saying "the sources that apply found nothing adverse"
        # would claim a check that never happened, so say what is actually true.
        uncovered = self.gatherer.uncovered_kinds(parsed)
        if uncovered and not any(s == "ok" for s in gr.source_status.values()):
            debug["rule"] = "A4_no_source_covers_subject"
            debug["uncovered_kinds"] = uncovered
            named = " or ".join(k.replace("_", " ") for k in uncovered)
            return _low(
                parsed.original,
                (f"This subject is {'an' if named[0] in 'aeiou' else 'a'} {named}, and no "
                 f"signal source available to this miner can check that kind of identifier, "
                 f"so there is insufficient information to assess it. This is an absence of "
                 f"evidence, not an indication that it is safe."),
                cacheable=True, ttl=self.s.cache_ttl, debug=debug)

        debug["rule"] = "A2_no_signal_found"
        # Distinguish "nothing matched" from "nothing was given": a detailed scenario we did
        # not recognise deserves an honest statement of the limit, not a claim that it was
        # devoid of detail.
        detailed = not parsed.entities and parsed.assessable
        if detailed:
            summary = ("None of the fraud indicators this miner checks - payment and account "
                       "behaviour, known scam patterns, IP and domain reputation, phishing "
                       "feeds and card-BIN metadata - matched this description, and it carries "
                       "no identifier that could be looked up. That is a limit of what was "
                       "checked rather than a finding that the subject is safe.")
        else:
            summary = ("The sources that apply to this subject returned no adverse finding, "
                       "and its description contains no behaviour matching a known fraud "
                       "pattern; there is nothing here to raise the grade on.")
        return _low(
            parsed.original,
            summary,
            cacheable=True,
            ttl=self.s.cache_ttl_degraded if gr.degraded else self.s.cache_ttl,
            debug=debug)

    def _store(self, key: str, res: Assessment) -> Assessment:
        if res.cacheable and res.cache_ttl:
            self.cache.set(key, res, res.cache_ttl)
        return res

    def _log(self, res: Assessment) -> None:
        d = res.debug
        log.info("fraud_risk", extra={"fields": {
            "event": "fraud_risk",
            "subject": res.subject[:500],
            "risk_level": res.level.value,
            "risk_score": res.score,
            "rule": d.get("rule"),
            "pattern": d.get("pattern"),
            "primary_kind": d.get("primary_kind"),
            "latency_ms": d.get("latency_ms"),
            "cache_hit": bool(d.get("cache_hit")),
            "sources": d.get("sources"),
            "degraded": d.get("degraded"),
            "corroborated": d.get("corroborated"),
            "families": d.get("families"),
            "family_weights": d.get("family_weights"),
            "n_signals_returned": len(res.signals),
            "signals": d.get("signals"),
            "reasoning_mode": d.get("reasoning_mode"),
            "dropped_signal_ids": d.get("dropped_signal_ids"),
            "timings": d.get("timings"),
            "fallback": d.get("fallback"),
        }})
