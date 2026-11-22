# TelegraphIntent

One Telegraph miner, one service, one `miner.yaml`, seven intents.

| Intent | Endpoint | Request field(s) | Package | Details |
|---|---|---|---|---|
| `FRAUD_RISK_GENERAL` | `POST /fraud-risk` | `subject` | `fraudrisk/` | [docs/FraudRisk.md](docs/FraudRisk.md) |
| `FACT_CHECK_GENERAL` | `POST /fact-check` | `claim` | `factcheck/` | [docs/FactCheck.md](docs/FactCheck.md) |
| `CONTRACT_OBLIGATION_AUDIT_GENERAL` | `POST /audit` | `contract` | `audit/` | [docs/ContractObligationAudit.md](docs/ContractObligationAudit.md) |
| `IP_GEOLOCATION_GENERAL` | `POST /ip-geolocation` | `ip` | `ipgeo/` | [docs/NetworkIntents.md](docs/NetworkIntents.md) |
| `EMAIL_SECURITY_GENERAL` | `POST /email-security` | `domain`, `record_type` | `emailsec/` | [docs/NetworkIntents.md](docs/NetworkIntents.md) |
| `URL_SAFE_GENERAL` | `POST /url-safe` | `url` | `urlsafe/` | [docs/NetworkIntents.md](docs/NetworkIntents.md) |
| `MALWARE_DETECTION_HASH` | `POST /malware-detection` | `indicator` | `malware/` | [docs/NetworkIntents.md](docs/NetworkIntents.md) |

Also served: `GET /healthz` (all seven intents), `GET /miner.yaml` (byte-exact), `GET /`.

`telegraphintent/app.py` is a thin ASGI router. Each intent package is a standalone service (always
HTTP 200, exactly its intent's response fields); the router only dispatches by path and
starts/stops them together. The last four share small helpers from `intentkit/`; the first three do
not use it. To add another intent: drop in its package, add a line to `INTENTS` in
`telegraphintent/app.py`, add an endpoint to `miner.yaml`, add the intent and a `probe` line to
`register.sh`, and add its package to the `Dockerfile`. Request and response field names must be
Telegraph's own (`GET https://devnode.telegraphprotocol.com/engine/v1/intents/<INTENT>`); an intent
whose `response` is `null` there has no published format yet and cannot be built to spec.

## Configuration

See [.env.example](.env.example). Unprefixed variables apply to all seven intents; prefix with
`FRAUDRISK_`, `FACTCHECK_`, `AUDIT_`, `IPGEO_`, `EMAILSEC_`, `URLSAFE_` or `MALWARE_` to override one
intent only (e.g. `FACTCHECK_ANTHROPIC_MODEL=claude-haiku-5-5`). The last four need no API key.

## Run / test

```bash
pip install -r requirements-dev.txt
uvicorn telegraphintent.app:app --port 8000
pytest                    # hermetic, ~1400 tests; `pytest -m live` hits real services
```

Per-intent demo/evidence scripts live in `scripts/` (`fraudrisk_demo.py`, `audit_demo.py`,
`factcheck_record_fixtures.py`).

## Register on Telegraph

1. Deploy (see `render.yaml`) and make sure `base_url` in `miner.yaml` is the deployed HTTPS host.
2. `GET <host>/miner.yaml` must return the same bytes as the local file (the SHA-256 is on-chain).
3. `DRY_RUN_ONLY=1 ./register.sh`, then `./register.sh` (probes all seven endpoints, checks all
   seven intents are canonical, hashes the YAML, simulates, then registers once for all intents).

To add intents to a miner that is already registered, see
[docs/NetworkIntents.md](docs/NetworkIntents.md#adding-these-to-the-already-registered-miner):
`updateMiner` creates a new registration id, and the testnet Diamond and bond currency changed on
2026-11-19, so re-check both before sending a transaction.
