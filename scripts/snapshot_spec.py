"""Save Telegraph's published spec of an intent as docs/intent_specs/<INTENT>.json.

    python scripts/snapshot_spec.py FRAUD_RISK_GENERAL CRYPTO_PRICE_USD
    python scripts/snapshot_spec.py --all        every intent in telegraphintent/registry.py

The snapshot is what tests/rules compares the registry, miner.yaml and every response model against, so a
renamed or invented field fails offline. Re-run it when Telegraph changes an intent (docs/INTENT_RULES.md, B5).
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from telegraphintent.registry import INTENTS  # noqa: E402

NODE = "https://devnode.telegraphprotocol.com"
OUT = ROOT / "docs" / "intent_specs"


def fetch(intent: str) -> dict:
    req = urllib.request.Request(f"{NODE}/engine/v1/intents/{intent}", headers={"User-Agent": "TelegraphIntentMiner/0.1"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r)


def trim(spec: dict) -> dict:
    ev = spec.get("evaluation") or {}
    cmp_ = ev.get("comparator") or {}
    return {
        "intent_id": spec["intent_id"],
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lifecycle_state": (spec.get("lifecycle") or {}).get("state"),
        "class": spec.get("class"),
        "evaluation": {"kind": ev.get("kind"), "via": ev.get("via"),
                       "comparator": {k: cmp_.get(k) for k in ("code", "tolerance_bps", "scale", "question_template",
                                                               "question_keys")} if cmp_ else None},
        "request": spec.get("request"),
        "response": spec.get("response"),
    }


def main(argv: list[str]) -> int:
    ids = [s.intent for s in INTENTS] if "--all" in argv else [a for a in argv if not a.startswith("-")]
    if not ids:
        print(__doc__)
        return 2
    OUT.mkdir(parents=True, exist_ok=True)
    for intent in ids:
        spec = trim(fetch(intent))
        if spec["lifecycle_state"] != "active" or not spec["request"] or not spec["response"]:
            print(f"{intent}: NOT BUILDABLE (state={spec['lifecycle_state']}, request/response missing)")
            return 1
        (OUT / f"{intent}.json").write_bytes((json.dumps(spec, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        print(f"saved docs/intent_specs/{intent}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
