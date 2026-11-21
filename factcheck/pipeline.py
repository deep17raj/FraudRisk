"""Orchestrates the stages. Every stage is a separate module; this file only wires and budgets them."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

from . import agreement as agreement_mod
from .cache import TTLCache
from .claim_parser import cache_key_text, parse_claim
from .config import Settings
from .credibility import CredibilityScorer
from .llm import LLMClient
from .models import CheckResult, Evidence, ParsedClaim, ScoredEvidence, Tier, Verdict
from .retrieval import Retriever
from .sources import build_sources
from .sources.base import Source
from .stance import StanceService
from .synthesis import Synthesizer
from .verdict import decide

log = logging.getLogger("factcheck.pipeline")

RESERVE_S = 0.35   # time kept back to build and return the response


def _unverified(claim: str, reasoning: str, *, cacheable: bool, ttl: Optional[int] = None, debug: Optional[dict] = None,
                sources: Optional[list[str]] = None) -> CheckResult:
    return CheckResult(claim=claim, verdict=Verdict.UNVERIFIED, reasoning=reasoning, sources=sources or [],
                       cacheable=cacheable, cache_ttl=ttl, debug=debug or {})


def fallback_result(claim: str, why: str) -> CheckResult:
    """Used by the HTTP layer when the pipeline timed out or crashed: always a valid, honest answer."""
    return _unverified(
        claim,
        "The fact-check could not be completed in time, so no verdict is given. "
        "The claim is left Unverified; this says nothing about whether it is true or false.",
        cacheable=False, debug={"fallback": why},
    )


class FactCheckPipeline:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        sources: Optional[list[Source]] = None,
        llm: Optional[LLMClient] = None,
        cache: Optional[TTLCache] = None,
        scorer: Optional[CredibilityScorer] = None,
    ) -> None:
        self.s = settings
        self.scorer = scorer or CredibilityScorer(settings.credibility_overrides_path)
        self.llm = llm or LLMClient(settings)
        self.sources = sources if sources is not None else build_sources(settings)
        self.retriever = Retriever(self.sources, client, settings.provider_timeout)
        self.stance = StanceService(self.scorer, self.llm, settings.stance_mode)
        self.synth = Synthesizer(self.llm)
        self.cache = cache or TTLCache(settings.cache_max_items)

    # ------------------------------------------------------------------ public
    async def check(self, claim: str) -> CheckResult:
        t0 = time.monotonic()
        claim = (claim or "").strip()
        try:
            parsed = parse_claim(claim)
            if not parsed.checkable:
                res = _unverified(claim, parsed.reject_reason, cacheable=False,
                                  debug={"stage": "parse", "rejected": True})
            else:
                key = cache_key_text(parsed.text)
                cached = self.cache.get(key)
                if cached is not None:
                    res = CheckResult(claim=claim, verdict=cached.verdict, reasoning=cached.reasoning,
                                      sources=list(cached.sources), debug={**cached.debug, "cache_hit": True})
                else:
                    shared: CheckResult = await self.cache.single_flight(key, lambda: self._compute(parsed, key))
                    res = CheckResult(claim=claim, verdict=shared.verdict, reasoning=shared.reasoning,
                                      sources=list(shared.sources), debug=dict(shared.debug))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # last line of defence: never surface a 500 to Telegraph
            log.exception("pipeline crashed")
            res = fallback_result(claim, f"exception:{type(exc).__name__}")
        res.debug["latency_ms"] = int((time.monotonic() - t0) * 1000)
        self._log(res)
        return res

    # ------------------------------------------------------------------ internals
    async def _compute(self, parsed: ParsedClaim, key: str) -> CheckResult:
        t0 = time.monotonic()
        deadline = t0 + self.s.global_timeout - RESERVE_S
        timings: dict[str, int] = {}

        def remaining() -> float:
            return deadline - time.monotonic()

        # 2. retrieval
        rr = await self.retriever.retrieve(parsed, min(self.s.retrieval_timeout, max(1.0, remaining() - 1.0)))
        timings["retrieval_ms"] = rr.elapsed_ms
        debug = {"providers": rr.provider_status, "n_retrieved": len(rr.evidence),
                 "time_sensitive": parsed.time_sensitive, "entities": parsed.entities, "numbers": parsed.numbers}

        if not rr.evidence:
            if rr.all_failed:
                res = _unverified(
                    parsed.original,
                    "The evidence sources could not be reached in time, so this claim could not be checked. "
                    "It is left Unverified; this says nothing about whether it is true or false.",
                    cacheable=False, debug=debug)
            else:
                res = _unverified(
                    parsed.original,
                    "No credible source found in the searches directly confirms or refutes this claim. "
                    "It is left Unverified rather than guessed at; lack of evidence does not mean the claim is false.",
                    cacheable=True, ttl=self._ttl(parsed, rr.degraded), debug=debug)
            res.debug.update(rule="R1_no_evidence")
            res.debug["timings"] = timings
            return self._store(key, res)

        # 3. credibility
        scored: list[ScoredEvidence] = []
        rejected = 0
        for ev in rr.evidence:
            tier = self.scorer.tier_for(ev.url)
            if tier == Tier.REJECTED:
                rejected += 1
                continue
            scored.append(ScoredEvidence(evidence=ev, domain=self.scorer.domain_of(ev.url), tier=tier))
        debug["n_rejected_domains"] = rejected

        # 4a. stance labelling (heuristic always; LLM when available and in budget)
        ts = time.monotonic()
        stance_t = min(self.s.stance_timeout, max(0.0, remaining() - self.s.synthesis_timeout - 0.5))
        stance_mode = await self.stance.label(parsed, scored, stance_t)
        timings["stance_ms"] = int((time.monotonic() - ts) * 1000)

        # 4b. agreement  5. verdict
        agr = agreement_mod.analyze(parsed, scored)
        decision = decide(agr)

        # 6. synthesis
        ts = time.monotonic()
        synth_t = min(self.s.synthesis_timeout, max(0.0, remaining()))
        reasoning, urls, synth_mode = await self.synth.synthesize(parsed, decision, agr, scored, synth_t)
        timings["synthesis_ms"] = int((time.monotonic() - ts) * 1000)

        # Hard guarantee: every cited URL is one we actually retrieved. Dedupe, preserve order.
        retrieved = {e.url for e in rr.evidence}
        sources: list[str] = []
        for u in urls:
            if u in retrieved and u not in sources:
                sources.append(u)

        llm_failed = "llm_failed" in stance_mode or "llm_failed" in synth_mode
        debug.update(
            rule=decision.rule, support_share=decision.support_share, agreement_level=agr.level,
            heuristic_only=agr.heuristic_only,
            stance_mode=stance_mode, synthesis_mode=synth_mode, timings=timings,
            weights={"support": round(agr.support_w, 3), "caveat": round(agr.caveat_w, 3),
                     "mixed": round(agr.mixed_w, 3), "misleading": round(agr.misleading_w, 3),
                     "refute": round(agr.refute_w, 3)},
            evidence=[{"id": i.id, "url": i.evidence.url, "domain": i.domain, "tier": i.tier.name,
                       "stance": i.stance.value, "labeller": i.labeller, "relevance": i.relevance, "conf": round(i.stance_confidence, 2),
                       "weight": round(i.weight, 3)} for i in scored],
        )
        res = CheckResult(claim=parsed.original, verdict=decision.verdict, reasoning=reasoning, sources=sources,
                          cacheable=True, cache_ttl=self._ttl(parsed, rr.degraded or llm_failed), debug=debug)
        return self._store(key, res)

    def _ttl(self, parsed: ParsedClaim, degraded: bool) -> int:
        if degraded:
            return self.s.cache_ttl_degraded
        return self.s.cache_ttl_volatile if parsed.time_sensitive else self.s.cache_ttl

    def _store(self, key: str, res: CheckResult) -> CheckResult:
        if res.cacheable and res.cache_ttl:
            self.cache.set(key, res, res.cache_ttl)
        return res

    def _log(self, res: CheckResult) -> None:
        d = res.debug
        log.info(
            "fact_check",
            extra={"fields": {
                "event": "fact_check",
                "claim": res.claim[:500],
                "verdict": res.verdict.value,
                "rule": d.get("rule"),
                "latency_ms": d.get("latency_ms"),
                "cache_hit": bool(d.get("cache_hit")),
                "providers": d.get("providers"),
                "n_sources_cited": len(res.sources),
                "sources": res.sources,
                "stance_mode": d.get("stance_mode"),
                "synthesis_mode": d.get("synthesis_mode"),
                "agreement": d.get("agreement_level"),
                "weights": d.get("weights"),
                "evidence": d.get("evidence"),
                "timings": d.get("timings"),
                "fallback": d.get("fallback"),
                "rejected_unparseable": d.get("rejected"),
            }},
        )
