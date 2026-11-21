# TelegraphIntent

One Telegraph miner, one service, one `miner.yaml`, three intents.

| Intent | Endpoint | Request field | Package | Details |
|---|---|---|---|---|
| `FRAUD_RISK_GENERAL` | `POST /fraud-risk` | `subject` | `fraudrisk/` | [docs/FraudRisk.md](docs/FraudRisk.md) |
| `FACT_CHECK_GENERAL` | `POST /fact-check` | `claim` | `factcheck/` | [docs/FactCheck.md](docs/FactCheck.md) |
| `CONTRACT_OBLIGATION_AUDIT_GENERAL` | `POST /audit` | `contract` | `audit/` | [docs/ContractObligationAudit.md](docs/ContractObligationAudit.md) |

Also served: `GET /healthz` (all three intents), `GET /miner.yaml` (byte-exact), `GET /`.

`telegraphintent/app.py` is a thin ASGI router. Each intent package is the unchanged standalone
service (always HTTP 200, exactly its intent's response fields); the router only dispatches by
path and starts/stops the three together. To add a fourth intent: drop in its package, add a
line to `INTENTS` in `telegraphintent/app.py`, add an endpoint to `miner.yaml`, and add the
intent to `INTENTS` in `register.sh`.

## Configuration

See [.env.example](.env.example). Unprefixed variables apply to all three intents; prefix with
`FRAUDRISK_`, `FACTCHECK_` or `AUDIT_` to override one intent only
(e.g. `FACTCHECK_ANTHROPIC_MODEL=claude-haiku-5-5`).

## Run / test

```bash
pip install -r requirements-dev.txt
uvicorn telegraphintent.app:app --port 8000
pytest                    # hermetic, ~1000 tests; `pytest -m live` hits real services
```

Per-intent demo/evidence scripts live in `scripts/` (`fraudrisk_demo.py`, `audit_demo.py`,
`factcheck_record_fixtures.py`).

## Register on Telegraph

1. Deploy (see `render.yaml`) and make sure `base_url` in `miner.yaml` is the deployed HTTPS host.
2. `GET <host>/miner.yaml` must return the same bytes as the local file (the SHA-256 is on-chain).
3. `DRY_RUN_ONLY=1 ./register.sh`, then `./register.sh` (probes all three endpoints, checks all
   three intents are canonical, hashes the YAML, simulates, then registers once for all intents).
