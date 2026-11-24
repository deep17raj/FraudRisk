"""OFAC sanctions lists (keyless, public) and fuzzy name matching against them.

Lists loaded (US Treasury OFAC publication service):
  SDN.CSV + ALT.CSV            Specially Designated Nationals, with their aliases ("OFAC SDN")
  CONS_PRIM.CSV + CONS_ALT.CSV the non-SDN Consolidated list, with aliases ("OFAC Consolidated (non-SDN)")

Matching is by name only, and is deliberately honest about what that means. Names are normalised (accents,
case and punctuation removed) and compared as an unordered set of words, so "ABU TEIR, Mohammed" and
"Mohammed Abu Teir" are the same name. The score is 1.0 for an identical word set, and otherwise the best of
the similarity of the sorted words and 0.9 when every word of a multi-word query appears in a listed name.
"""
from __future__ import annotations

import asyncio
import csv
import difflib
import io
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

import httpx

from intentkit.names import normalize

log = logging.getLogger("intents.sanctions")

BASE = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/"
SDN_LIST, CONS_LIST = "OFAC SDN", "OFAC Consolidated (non-SDN)"
RETRY_MISSING_S = 30.0       # how soon to try again for a list that has never downloaded
_NULL = {"-0-", ""}


@dataclass
class Entry:
    uid: str
    name: str
    kind: str
    programs: str
    list_name: str
    aliases: list[str] = field(default_factory=list)

    def describe(self) -> str:
        bits = [b for b in (self.list_name, self.kind or "entity", self.programs, f"uid {self.uid}") if b]
        return f"{self.name} ({'; '.join(bits)})"


@dataclass(frozen=True)
class Hit:
    entry: Entry
    matched_name: str
    score: float


def _clean(v: str) -> str:
    v = (v or "").strip()
    return "" if v in _NULL else v


def parse_primary(body: str, list_name: str) -> dict[str, Entry]:
    out: dict[str, Entry] = {}
    for row in csv.reader(io.StringIO(body)):
        if len(row) < 4 or not row[0].strip().isdigit():
            continue
        out[row[0].strip()] = Entry(row[0].strip(), _clean(row[1]), _clean(row[2]), _clean(row[3]), list_name)
    return out


def parse_aliases(body: str, entries: dict[str, Entry]) -> None:
    for row in csv.reader(io.StringIO(body)):
        if len(row) >= 4 and row[0].strip() in entries and _clean(row[3]):
            entries[row[0].strip()].aliases.append(_clean(row[3]))


class SanctionsIndex:
    def __init__(self, ttl: float, timeout: float) -> None:
        self.ttl, self.timeout = ttl, timeout
        self.entries: list[Entry] = []
        self.names: list[tuple[int, str, tuple[str, ...]]] = []        # (entry index, name, words)
        self.by_word: dict[str, list[int]] = {}                         # word -> indexes into self.names
        self.lists: tuple[str, ...] = ()
        self.missing: tuple[str, ...] = ()             # lists that have never downloaded; retried soon, not at the TTL
        self.loaded_at = 0.0
        self.attempted_at = 0.0
        self._groups: dict[str, dict[str, Entry]] = {}
        self._task: Optional[asyncio.Task] = None

    @property
    def ready(self) -> bool:
        return bool(self.names)

    @property
    def stale(self) -> bool:
        now = time.monotonic()
        return now - self.loaded_at > self.ttl or (bool(self.missing) and now - self.attempted_at > RETRY_MISSING_S)

    def size(self) -> int:
        return len(self.entries)

    # ---- loading ------------------------------------------------------------------------------------
    async def _get(self, client: httpx.AsyncClient, name: str) -> Optional[str]:
        for attempt in (1, 2):
            try:
                r = await client.get(BASE + name, timeout=self.timeout, follow_redirects=True)
                r.raise_for_status()
                return r.content.decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001
                log.warning("sanctions list download failed", extra={"fields": {
                    "file": name, "attempt": attempt, "error": f"{type(exc).__name__}: {exc}"}})
        return None

    async def load(self, client: httpx.AsyncClient) -> None:
        sdn, sdn_alt, cons, cons_alt = await asyncio.gather(
            *(self._get(client, f) for f in ("SDN.CSV", "ALT.CSV", "CONS_PRIM.CSV", "CONS_ALT.CSV")))
        self.attempted_at = time.monotonic()
        fresh: dict[str, dict[str, Entry]] = {}
        for name, primary, alt in ((SDN_LIST, sdn, sdn_alt), (CONS_LIST, cons, cons_alt)):
            if not primary:
                continue
            if not alt and name in self._groups:      # the aliases did not download: the earlier complete copy is better
                continue
            e = parse_primary(primary, name)
            if alt:
                parse_aliases(alt, e)
            fresh[name] = e
        merged = {**self._groups, **fresh}            # a list that failed to download keeps what it had
        if not merged:
            return
        self._groups = merged
        groups = [(name, merged[name]) for name in (SDN_LIST, CONS_LIST) if name in merged]
        self.missing = tuple(name for name in (SDN_LIST, CONS_LIST) if name not in merged)
        entries = [e for _, g in groups for e in g.values()]
        names: list[tuple[int, str, tuple[str, ...]]] = []
        by_word: dict[str, list[int]] = {}
        for i, e in enumerate(entries):
            for nm in (e.name, *e.aliases):
                words = normalize(nm)
                if not words:
                    continue
                idx = len(names)
                names.append((i, nm, words))
                for w in set(words):
                    by_word.setdefault(w, []).append(idx)
        self.entries, self.names, self.by_word = entries, names, by_word
        self.lists = tuple(g for g, _ in groups)
        self.loaded_at = time.monotonic()
        log.info("sanctions lists loaded", extra={"fields": {"entries": len(entries), "names": len(names), "lists": list(self.lists)}})

    def ensure_fresh(self, client: httpx.AsyncClient) -> None:
        if (self._task and not self._task.done()) or (self.ready and not self.stale):
            return
        self._task = asyncio.ensure_future(self.load(client))
        self._task.add_done_callback(self._loaded)

    @staticmethod
    def _loaded(task: "asyncio.Task") -> None:
        if not task.cancelled() and task.exception() is not None:
            log.error("sanctions list load failed", exc_info=task.exception())

    async def settle(self, wait: float) -> None:
        """Wait up to `wait` seconds for a load that is already running, without cancelling it."""
        if self._task is not None and not self._task.done():
            await asyncio.wait({self._task}, timeout=wait)

    async def start(self, client: httpx.AsyncClient, wait: float) -> None:
        """Begin loading and wait up to `wait` seconds, without cancelling the download if it takes longer."""
        self.ensure_fresh(client)
        if self._task is not None and not self._task.done():
            await asyncio.wait({self._task}, timeout=wait)

    # ---- matching -----------------------------------------------------------------------------------
    @staticmethod
    def score(q: tuple[str, ...], c: tuple[str, ...]) -> float:
        if q == c:
            return 1.0
        ratio = difflib.SequenceMatcher(None, " ".join(q), " ".join(c)).ratio()
        sq, sc = set(q), set(c)
        if len(sq) >= 2 and all(len(w) >= 3 for w in sq) and sq < sc:
            ratio = max(ratio, 0.9)
        return ratio

    def _candidates(self, words: tuple[str, ...], limit: int = 300) -> list[int]:
        votes: Counter[int] = Counter()
        for w in words:
            hits = self.by_word.get(w)
            if hits is None and len(w) >= 4:         # a misspelt word: borrow its closest spellings
                for close in difflib.get_close_matches(w, self.by_word.keys(), n=3, cutoff=0.85):
                    for i in self.by_word[close]:
                        votes[i] += 1
                continue
            if hits is not None and len(hits) <= 4000:        # a word on thousands of names says little
                for i in hits:
                    votes[i] += 1
        return [i for i, _ in votes.most_common(limit)]

    def search(self, name: str, top: int = 5) -> list[Hit]:
        words = normalize(name)
        if not words:
            return []
        best: dict[int, Hit] = {}
        for idx in self._candidates(words):
            ent_i, nm, cw = self.names[idx]
            s = self.score(words, cw)
            if s >= 0.6 and (ent_i not in best or s > best[ent_i].score):
                best[ent_i] = Hit(self.entries[ent_i], nm, s)
        return sorted(best.values(), key=lambda h: -h.score)[:top]
