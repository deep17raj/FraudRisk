"""Run the regression subjects through the pipeline (or a deployed URL) and print a table.

Doubles as hackathon submission evidence: it shows the grade, the score, the fused signal
families, the named fraud pattern and the latency for every fixture, plus the structured log
line behind each one if you ask for it.

    python scripts/fraudrisk_demo.py                      # in-process, live signal sources
    python scripts/fraudrisk_demo.py --url https://host   # against a deployed endpoint
    python scripts/fraudrisk_demo.py --json > evidence.jsonl
    python scripts/fraudrisk_demo.py --subject "new account, VPN login, $4k to a new payee"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fraudrisk.config import Settings
from fraudrisk.pipeline import FraudRiskPipeline

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fraudrisk" / "fixtures" / "subjects.json"


def load_subjects() -> list[str]:
    data = json.loads(FIXTURES.read_text("utf-8"))
    return [c["subject"] for c in data["cases"]]


async def via_http(url: str, subjects: list[str], as_json: bool) -> None:
    async with httpx.AsyncClient(timeout=30.0) as client:
        for subject in subjects:
            t0 = time.monotonic()
            try:
                r = await client.post(f"{url.rstrip('/')}/fraud-risk",
                                      json={"subject": subject})
                body = r.json()
            except Exception as exc:
                print(f"FAILED  {subject[:60]!r}: {type(exc).__name__}: {exc}")
                continue
            ms = int((time.monotonic() - t0) * 1000)
            if as_json:
                print(json.dumps({"subject": subject, "latency_ms": ms, **body},
                                 ensure_ascii=False))
            else:
                render(subject, body["risk_level"], body["risk_score"], body["signals"],
                       body["summary"], ms, pattern="-", families="-")


async def in_process(subjects: list[str], as_json: bool) -> None:
    settings = Settings.from_env()
    async with httpx.AsyncClient(headers={"User-Agent": settings.user_agent},
                                 follow_redirects=True) as client:
        pipeline = FraudRiskPipeline(settings, client)
        print(f"# sources: {', '.join(s.name for s in pipeline.sources)}", file=sys.stderr)
        print(f"# model: {settings.model} (reasoning "
              f"{'on' if pipeline.reasoner.uses_llm else 'OFF - no ANTHROPIC_API_KEY'})",
              file=sys.stderr)
        await pipeline.feeds.preload(client)
        print(f"# feeds: {pipeline.feeds.status()}", file=sys.stderr)

        for subject in subjects:
            res = await pipeline.assess(subject)
            if as_json:
                print(json.dumps({
                    "subject": subject, "risk_level": res.level.value,
                    "risk_score": res.score, "signals": res.signals,
                    "summary": res.summary, "debug": res.debug}, ensure_ascii=False,
                    default=str))
            else:
                render(subject, res.level.value, res.score, res.signals, res.summary,
                       res.debug.get("latency_ms", 0),
                       pattern=str(res.debug.get("pattern", "-")),
                       families=",".join(res.debug.get("families") or []) or "-")


def render(subject, level, score, signals, summary, ms, pattern, families) -> None:
    print()
    print("=" * 100)
    print(f"SUBJECT  {subject[:92]}")
    print(f"GRADE    {level:<9} score={score:<10} pattern={pattern}")
    print(f"FUSION   families=[{families}]  latency={ms}ms  signals={len(signals)}")
    for s in signals:
        print(f"   - {s}")
    print(f"SUMMARY  {summary}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="assess against a deployed endpoint instead of in-process")
    ap.add_argument("--json", action="store_true", help="JSON Lines output (evidence file)")
    ap.add_argument("--subject", action="append", help="assess this subject (repeatable)")
    args = ap.parse_args()

    subjects = args.subject or load_subjects()
    if args.url:
        asyncio.run(via_http(args.url, subjects, args.json))
    else:
        asyncio.run(in_process(subjects, args.json))


if __name__ == "__main__":
    main()
