"""Record LIVE Wikipedia evidence for every claim in tests/factcheck/fixtures/claims.json.

    python scripts/factcheck_record_fixtures.py            # records missing files only
    python scripts/factcheck_record_fixtures.py --force    # re-record everything

The regression suite then replays these snapshots offline (no network, no keys), so verdict
regressions are caught deterministically. Re-record occasionally to pick up article changes.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from factcheck.claim_parser import parse_claim  # noqa: E402
from factcheck.config import Settings  # noqa: E402
from factcheck.sources.wikipedia import WikipediaSource  # noqa: E402

FIX = ROOT / "tests" / "factcheck" / "fixtures"


async def main(force: bool) -> None:
    claims = json.loads((FIX / "claims.json").read_text(encoding="utf-8"))["claims"]
    out_dir = FIX / "evidence"
    out_dir.mkdir(exist_ok=True)
    s = Settings.from_env()
    src = WikipediaSource(s.user_agent)
    async with httpx.AsyncClient(http2=True, follow_redirects=True) as client:
        for c in claims:
            path = out_dir / f"{c['id']}.json"
            parsed = parse_claim(c["claim"])
            if c.get("synthetic") or not parsed.checkable:
                continue
            if path.exists() and not force:
                continue
            evidence = await src.search(parsed, client, 10.0)
            path.write_text(json.dumps({
                "claim": c["claim"],
                "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "source": "wikipedia (live API snapshot)",
                "evidence": [asdict(e) for e in evidence],
            }, indent=1, ensure_ascii=False), encoding="utf-8")
            print(f"recorded {c['id']}: {len(evidence)} items")
            await asyncio.sleep(0.5)   # be polite to Wikimedia


if __name__ == "__main__":
    asyncio.run(main("--force" in sys.argv))
