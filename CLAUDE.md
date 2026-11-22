# TelegraphIntent

One Telegraph miner (id 83159274, slug `telegraph-intent-miner`) serving many intents from one
service and one `miner.yaml`.

**Before adding, changing or removing any intent, read and follow [docs/INTENT_RULES.md](docs/INTENT_RULES.md).**
Those rules are binding, and `python -m pytest` enforces the mechanical ones (`tests/rules/`).

Short version: never change the miner's id, slug, name or base_url; build only intents that are
`active` with a published request/response spec; return exactly the spec's fields as HTTP 200 every
time; never fabricate or hard-code answers; keyless sources only; generate `miner.yaml` with
`python scripts/sync_intents.py`; do not commit, push, deploy or call `updateMiner` unless the user asks.
