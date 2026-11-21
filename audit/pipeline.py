"""The audit pipeline: resolve -> extract -> ground -> assess -> roll up -> summarise.

Two invariants hold on every path through this module:

* **Nothing is cited that is not in the source.** Citations come out of `ground_quotes`,
  which only ever returns slices of the document.
* **No status stronger than "unverifiable" survives without located clause wording.** A
  "met" or "breached" claim whose quote could not be found in the text is demoted rather
  than trusted, so the verdict can never rest on wording that does not exist. This is the
  rule that makes the audit grounded rather than merely plausible.

Every failure degrades instead of raising: no API key, a refusal, a timeout, a truncated
reply or unparseable JSON all fall through to the deterministic reader, and a request that
cannot be audited at all returns `Unclear` saying why.
"""
from __future__ import annotations

import hashlib
import logging
import time

import httpx

from .cache import TTLCache
from .config import Settings
from .extract import extract
from .fetch import resolve_input
from .grounding import GroundedSource, ground_quotes, normalize
from .llm import LLMClient, LLMUnavailable
from .models import Audit, ContractFlags, Obligation, SourceDoc, Status, Verdict
from .reader import find_unsupplied_documents, modal_obligations
from .summary import compose, with_notes
from .verdict import roll_up

log = logging.getLogger("audit.pipeline")

#: When no quote at all could be grounded, cite the opening of the contract so the answer
#: still shows the wording it was based on. Taken as a slice, so it is verbatim.
_FALLBACK_CITE_CHARS = 400


def cache_key(contract: str) -> str:
    """Normalised hash: whitespace and typography must not split the cache."""
    norm, _ = normalize(contract or "")
    return hashlib.sha256(norm.lower().encode("utf-8")).hexdigest()


def fallback_audit(reason: str) -> Audit:
    """The answer for a request that could not be audited - never empty, never an error."""
    return Audit(
        verdict=Verdict.UNCLEAR,
        obligations=[],
        clauses=[],
        summary=("This contract could not be audited within the service's time budget, so no "
                 "verdict is asserted. The result is Unclear because the audit did not "
                 "complete, not because the contract is deficient."),
        rule=reason,
        degraded=True,
        diagnostics={"reason": reason},
    )


class AuditPipeline:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client
        self.llm = LLMClient(settings)
        self.cache = TTLCache(max_items=settings.cache_max_items)

    @property
    def uses_llm(self) -> bool:
        mode = self.settings.extraction_mode
        if mode == "off":
            return False
        if mode == "llm":
            return True
        return self.llm.available

    async def audit(self, contract: str) -> Audit:
        key = cache_key(contract)
        hit = self.cache.get(key)
        if hit is not None:
            hit.diagnostics = {**hit.diagnostics, "cache": "hit"}
            return hit
        result: Audit = await self.cache.single_flight(key, lambda: self._audit(contract))
        ttl = self.settings.cache_ttl_degraded if result.degraded else self.settings.cache_ttl
        self.cache.set(key, result, ttl)
        return result

    async def _audit(self, contract: str) -> Audit:
        t0 = time.monotonic()
        doc = await resolve_input(contract, self.client, self.settings)

        if not doc.usable:
            verdict, rule = roll_up([], ContractFlags(), doc)
            summary = compose(verdict, rule, [], ContractFlags(), doc)
            return Audit(verdict=verdict, obligations=[], clauses=[], summary=summary,
                         rule=rule, degraded=True,
                         diagnostics={"origin": doc.origin, "fetch_error": doc.fetch_error,
                                      "elapsed_ms": int((time.monotonic() - t0) * 1000)})

        source = GroundedSource.build(doc.text)
        degraded_reason = ""
        overall: list[str] = []

        if self.uses_llm:
            try:
                obligations, flags, overall = await extract(doc, source, self.llm, self.settings)
            except LLMUnavailable as exc:
                degraded_reason = str(exc)
                log.warning("extraction unavailable; using the deterministic reader",
                            extra={"fields": {"event": "llm_unavailable", "error": str(exc)}})
                obligations, flags = self._read_deterministically(doc)
        else:
            degraded_reason = "no model configured" if not self.llm.available else "disabled"
            obligations, flags = self._read_deterministically(doc)

        # Regex-backed evidence for an un-supplied schedule outranks the model's silence:
        # this is the finding that earns an honest Unclear instead of a confident guess.
        missing_docs = find_unsupplied_documents(doc.text)
        flags.missing_documents = missing_docs
        if missing_docs:
            flags.missing_dependency = True

        clauses, dropped, demoted = self._ground(source, obligations, overall)
        verdict, rule = roll_up(obligations, flags, doc)
        summary = with_notes(
            compose(verdict, rule, obligations, flags, doc,
                    n_clauses=len(clauses), degraded=bool(degraded_reason)),
            doc, dropped=dropped, degraded=degraded_reason)

        return Audit(
            verdict=verdict,
            obligations=[o.render() for o in obligations],
            clauses=clauses,
            summary=summary,
            rule=rule,
            degraded=bool(degraded_reason),
            diagnostics={
                "origin": doc.origin, "url": doc.url, "truncated": doc.truncated,
                "chars": len(doc.text), "rule": rule, "n_obligations": len(obligations),
                "n_clauses": len(clauses), "quotes_dropped": dropped,
                "statuses_demoted": demoted, "missing_documents": missing_docs,
                "degraded": degraded_reason,
                "elapsed_ms": int((time.monotonic() - t0) * 1000),
            },
        )

    def _read_deterministically(self, doc: SourceDoc) -> tuple[list[Obligation], ContractFlags]:
        obligations = modal_obligations(doc.text, limit=self.settings.max_obligations)
        return obligations, ContractFlags(missing_key_terms=not obligations)

    def _ground(self, source: GroundedSource, obligations: list[Obligation],
                overall: list[str]) -> tuple[list[str], int, int]:
        """Locate every quote, demote unsupported statuses, and build the citation list."""
        demoted = 0
        for ob in obligations:
            located = source.locate(ob.quote) if ob.quote else None
            ob.cited = located or ""
            if located is None and ob.status is not Status.UNVERIFIABLE:
                # The claim was stronger than the evidence found for it. Keep the obligation,
                # drop the claim: an ungrounded "met" is exactly what this Intent penalises.
                ob.status = Status.UNVERIFIABLE
                ob.reason = (ob.reason + "; no clause wording for this could be located in the "
                             "supplied text").lstrip("; ")
                demoted += 1

        candidates = list(overall) + [ob.cited or ob.quote for ob in obligations]
        clauses, dropped = ground_quotes(
            source, [c for c in candidates if c],
            max_items=self.settings.max_clauses, max_chars=self.settings.max_clause_chars)

        if not clauses and obligations and source.original.strip():
            # An obligation was found but none of its wording could be located - cite the
            # opening of the contract so the answer still shows the text it rests on. Taken
            # as a slice, so it is verbatim. Deliberately NOT done when no obligation was
            # found at all: `clauses` means cited clause wording, and quoting the prose of a
            # title page or an email is noise dressed up as evidence. There the empty list
            # is the honest answer and the summary explains it.
            opening, _ = ground_quotes(
                source, [source.original.strip()[:_FALLBACK_CITE_CHARS]],
                max_items=1, max_chars=self.settings.max_clause_chars)
            clauses = opening
        return clauses, dropped, demoted
