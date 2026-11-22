Build a production-quality Telegraph Protocol "Miner" service for the FRAUD_RISK_GENERAL
Intent, for the Telegraph Hackathon Season II (Miners/Providers track). Goal: rank #1 on
the leaderboard for this Intent. Winning means being judged the most sensible,
best-justified, multi-signal fraud assessor among the 15 currently live competing miners —
and the key opportunity is that 14 of those 15 are thin single-source lookups with no real
reasoning. Beating them means fusing multiple signals and reasoning over them.

## Ground truth (do not deviate — pulled live from Telegraph's own API)

Request schema (GET https://devnode.telegraphprotocol.com/engine/v1/intents/FRAUD_RISK_GENERAL):
```json
{
  "request": { "params": [ { "name": "subject", "type": "string", "required": true, "description": "Entity, payment or account to assess" } ] },
  "response": { "fields": [
    { "name": "risk_level", "type": "enum", "allowed": ["LOW","MEDIUM","HIGH","CRITICAL"], "description": "Fraud risk grade" },
    { "name": "risk_score", "type": "number", "unit": "fraction (0-1)", "range": [0,1], "decimals": 6, "description": "Fraud risk score from 0 to 1" },
    { "name": "signals", "type": "list", "description": "Risk signals behind the assessment" },
    { "name": "summary", "type": "string", "description": "One- or two-sentence summary of the assessment" }
  ]}
}
```
Intent class: `judgment` — there is NO single correct score (the real fraud label only appears
later, if ever). A trained reward model scores the *quality* of risk_level + risk_score +
signals + summary: are they sensible, consistent, and well-justified? Fabricated signals and
inconsistent scores are heavily penalized. An honest "insufficient information → LOW" is far
better than inventing red flags.

The competitive field (checked live — this shapes the strategy):
- 14 of 15 live miners are single-source wrappers: IP-reputation feeds (OTX, IPinfo,
  IP2Location, IPWho), a phishing feed (OpenPhish), a card-BIN lookup (Binlist), a
  crypto-token-listing-only checker (Skein).
- The ONE real competitor is "RiskWire Fraud Risk": an LLM reasons over the described
  scenario and returns a graded risk level with one or two sentences naming the decisive
  signals and the fraud pattern they match. Match or beat this: do real multi-signal fusion
  PLUS reasoning PLUS name the fraud pattern.

## What to build

A small, fast, well-tested backend service plus the Telegraph miner registration config.

### 1. Core API

- `POST /fraud-risk` — body `{"subject": "<string>"}` → returns EXACTLY the 4 fields above,
  nothing more. Enum values must match exactly and uppercase: LOW / MEDIUM / HIGH / CRITICAL.
  `risk_score` must be a number in [0,1]. `risk_level` and `risk_score` must always be mutually
  consistent (see scoring below).
- `GET /healthz` for liveness. Telegraph spot-checks miners; any non-empty response on the real
  endpoint counts as alive — ensure the real endpoint never times out or returns empty under
  normal load (timeouts/empties → routing revocation).
- Framework: FastAPI (Python) unless the repo already uses JS. Deployable as a single public
  HTTPS endpoint (Render / Fly.io / Railway free tier is fine for the hackathon).

### 2. The fraud-assessment pipeline (this determines whether we win)

Separate, independently-testable stages:

1. **Subject classification** — detect what the `subject` string actually is so we route to the
   right signal sources. Handle at minimum: a bare IP address, a card BIN (6–8 digits), an
   email address, a domain/URL, a crypto wallet address, and a free-text described scenario
   (the most common and most valuable case). Extract structured hints from scenarios: dollar
   amounts, account age, "new payee", "first transfer", velocity cues, geography mismatches.
2. **Signal gathering** — query multiple independent sources IN PARALLEL with a hard timeout
   (8–10s budget), but only the ones relevant to the detected subject type:
   - IP reputation / geolocation: is it a known VPN, proxy, Tor exit, hosting range, or
     abuse-flagged IP? (use a free/freemium IP intel API — make the provider swappable via an
     adapter interface, don't hardcode one vendor).
   - Phishing / domain reputation: for URLs/domains (e.g. an OpenPhish-style feed, or a URL
     reputation API).
   - Card BIN metadata: for card BINs (issuer, country, prepaid flag — prepaid + country
     mismatch is a classic card-testing signal).
   - Behavioral/pattern heuristics: for described scenarios, a rule library matching known
     fraud patterns (account takeover, card testing, synthetic identity, phishing, mule
     account, first-party fraud) from the extracted hints.
   Implement sources as a plugin list so new ones drop in without touching core logic. Each
   source returns a normalized {signal_text, severity 0–1, source_reliability 0–1}.
3. **Reasoning layer** — one LLM call (Claude via the Anthropic API) that receives ONLY the
   subject + the gathered normalized signals, and decides: which signals genuinely apply here,
   how strong each is in context, and what overall fraud PATTERN they collectively match
   (name it). It must NOT invent signals not present in the gathered set — enforce this by
   only letting it select/annotate from the provided signal list, never add new evidence.
4. **Deterministic score mapping** (NOT the LLM — pure, testable logic): combine the applicable
   signals' severity × reliability into a single weighted risk magnitude, map that to
   `risk_score` in [0,1] (round to 6 decimals), and bucket it to `risk_level`:
   - score < 0.25 → LOW, 0.25–0.5 → MEDIUM, 0.5–0.8 → HIGH, ≥ 0.8 → CRITICAL (tune thresholds,
     but keep them fixed and documented). Identical inputs MUST give identical scores.
5. **Honest abstention** — if no concrete signal applies (subject too vague, e.g. "check this
   person"), return `LOW` with risk_score near 0 and a `summary` that honestly says there was
   insufficient information. Never fabricate a red flag to look confident.
6. **Summary synthesis** — the LLM writes a 1–2 sentence `summary` that names the decisive
   signals and the fraud pattern, reading like a real fraud analyst wrote it. The `signals`
   list returned must be the concrete applicable signals (specific, evidence-naming), not vague.

### 3. Reliability & speed

- Hard global per-request timeout (e.g. 12s) — if signal gathering is too slow, fail over to a
  best-effort assessment from whatever signals returned, or honest LOW/insufficient-info,
  rather than hanging.
- Cache keyed on a normalized subject (subjects repeat across callers), short TTL for anything
  time-sensitive.
- Retry/circuit-breaker around each external signal provider so one flaky API can't take the
  whole service down — a dead provider just drops its signal, it doesn't fail the request.
- Structured logging of every request: subject, signals gathered, final score/level, latency —
  for debugging AND as hackathon submission evidence.

### 4. Telegraph miner registration config (`miner.yaml`)

Follow the Telegraph Miner YAML Standard exactly — the schema is strict and rejects any unknown
key outright. Pick an unused numeric `id` and a free kebab-case `slug` by first checking
`https://devnode.telegraphprotocol.com/api/miners`. Shape:

```yaml
version: "1"
kind: miner
id: <pick an unused number, e.g. 7-8 random digits>
slug: <kebab-case-unique-slug>
protocol: generic
name: <Display Name> Fraud Risk
description: Multi-signal fraud risk assessment that fuses IP reputation, domain/phishing, card-BIN and behavioral pattern signals into a graded risk level, with honest abstention when evidence is insufficient.
base_url: https://<your-deployed-host>

auth:
  type: none   # or bearer/header if you gate the endpoint behind your own key

endpoints:
  - path: /fraud-risk
    external_path: /fraud-risk
    method: POST
    description: Assesses fraud risk for a subject (an IP, payment, card BIN, account, wallet or described scenario), fusing multiple independent signals into a graded risk level with named signals and a fraud-pattern summary.
    intents: [FRAUD_RISK_GENERAL]
    params:
      body:
        required:
          - name: subject
            type: string
            intents: ["*"]
            description: "The entity, payment, account or scenario to assess for fraud risk. Free text; may be an IP, card BIN, email, domain, wallet address, or a described transaction/behavior."

semantics:
  signal_mapping:
    label_field: risk_level
    confidence_field: risk_score
    reason_field: summary
  supported_intents:
    - FRAUD_RISK_GENERAL
```

Do NOT nest `input_schema`/`output_schema` under `endpoints[]` (top-level only, if used at all).
Do NOT add any key not in the Telegraph YAML field reference — registration is rejected on any
unknown key. `signal_mapping` accepts only `label_field`, `confidence_field`, `reason_field`.

### 5. Registration script (`register.sh`)

A shell script wrapping `cast` (Foundry) for Ethereum Sepolia testnet:
1. `sha256sum miner.yaml` for the hash.
2. `approve` the Diamond contract to take the MACHINA bond.
3. Dry-run `cast call registerMiner(...)` first, then real `cast send`.
4. Print the `registrationId` and a reminder to verify via
   `curl https://devnode.telegraphprotocol.com/api/miners/$REGISTRATION_ID`.
Use placeholder env vars (DIAMOND, RPC, MACHINA, YAML_URL, FEE_ADDRESS, MINER_PRIVATE_KEY) —
never hardcode real secrets.

### 6. Tests

- Unit tests for the deterministic score-mapping function covering every risk_level boundary
  (LOW/MEDIUM/HIGH/CRITICAL) with synthetic signal sets — highest-leverage test, pure logic,
  and directly asserts risk_score↔risk_level consistency.
- A regression fixture of 15–20 real subjects with expected risk bands: clear-high (new account
  + VPN + high first transfer to new payee), clear-low (normal returning-user scenario),
  card-testing (prepaid BIN + many small attempts), phishing domain, and a few deliberately
  vague ones that MUST come back LOW/insufficient-info rather than inventing signals.
- A latency test asserting p95 stays well under the timeout and never returns empty.

### 7. README

What it is, run locally, test, deploy, register on Telegraph testnet, known limitations.
Doubles as hackathon submission evidence (a required "how to run it" field).

## Priorities if time is short (build in this order, each working end-to-end before the next)

1. `/fraud-risk` endpoint returning the correct schema shape, deployed and working, with one
   signal source + the LLM reasoning + deterministic scoring.
2. Add the explicit deterministic score→level mapping and the honest-abstention path (replace
   any LLM-guessed score). Consistency here is what most competitors lack.
3. Add multi-signal fusion (2–3 sources) + subject-type routing — this is the main edge over
   the 14 single-source competitors.
4. Add caching, timeouts, circuit-breakers, logging, tests.
5. Write miner.yaml + register.sh + README last, once the API is stable.

Do not skip step 2 for step 3 — a consistent, well-reasoned 2-signal answer that abstains
honestly beats a flaky 4-signal pipeline that times out or returns inconsistent scores.
