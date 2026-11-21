"""Run the regression corpus and print a table: verdict, obligations, citations, latency.

Useful as a smoke test and as hackathon submission evidence.

    python scripts/audit_demo.py              # stubbed extraction, no API key needed, no cost
    python scripts/audit_demo.py --live       # the real model (needs ANTHROPIC_API_KEY)
    python scripts/audit_demo.py --live -v    # ... and print every obligation and citation
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audit.config import Settings                      # noqa: E402
from audit.pipeline import AuditPipeline               # noqa: E402
from tests.audit.conftest import make_settings               # noqa: E402
from tests.audit.fixtures import CASES                       # noqa: E402


def build(live: bool) -> AuditPipeline:
    if live:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            sys.exit("--live needs ANTHROPIC_API_KEY in the environment")
        return AuditPipeline(Settings.from_env(), client=None)
    from tests.audit.conftest import StubLLM

    pl = AuditPipeline(make_settings(cache_ttl=0), client=None)
    pl.llm = StubLLM()
    return pl


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="use the real Anthropic API")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print every obligation and citation")
    args = ap.parse_args()

    pl = build(args.live)
    print(f"{'case':<40} {'expected':<20} {'verdict':<20} {'obl':>4} {'cite':>5} "
          f"{'ms':>6}  grounded")
    print("-" * 110)

    failures = 0
    ungrounded = 0
    for case in CASES:
        if not args.live:
            pl.llm.payloads = [case["payload"]]
        # The marker keeps each case out of the previous one's cache entry. Grounding is
        # checked against the text actually sent, marker included.
        sent = case["contract"] + f"\n\n(demo {case['name']})"
        t0 = time.monotonic()
        res = await pl.audit(sent)
        ms = int((time.monotonic() - t0) * 1000)

        bad = [c for c in res.clauses if c not in sent]
        ungrounded += len(bad)
        ok = res.verdict.value == case["expect"]
        failures += 0 if ok else 1
        mark = "yes" if not bad else f"NO ({len(bad)})"
        flag = "" if ok else "  <-- mismatch"
        print(f"{case['name']:<40} {case['expect']:<20} {res.verdict.value:<20} "
              f"{len(res.obligations):>4} {len(res.clauses):>5} {ms:>6}  {mark}{flag}")

        if args.verbose:
            print(f"      summary: {res.summary}")
            for line in res.obligations:
                print(f"      obligation: {line}")
            for clause in res.clauses:
                print(f"      clause: {clause!r}")
            print()

    print("-" * 110)
    print(f"{len(CASES)} cases | verdict mismatches: {failures} | "
          f"ungrounded citations: {ungrounded}")
    if ungrounded:
        print("FAIL: a citation was returned that is not verbatim contract wording.")
    return 1 if (failures or ungrounded) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
