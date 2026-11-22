# Brief: add low-competition intents to the TelegraphIntent miner

This file is a hand-off. It says what has already been built, which new intents to add and
why, and exactly which facts are still missing. **Read the "What I need from you" section
first.** Everything else is context.

## 1. Context

- The project is one Telegraph miner (`TelegraphIntent`) that serves several intents from a
  single service and a single `miner.yaml`. It is deployed on Render at
  `https://fraudrisk.onrender.com` and registered on the Telegraph devnode as miner id
  `83159274`, slug `telegraph-intent-miner`, status `active`.
- It currently serves three intents, each an independent Python package behind one router
  (`telegraphintent/app.py`):

  | Intent | Endpoint | Request field | Response fields |
  |---|---|---|---|
  | `FRAUD_RISK_GENERAL` | `POST /fraud-risk` | `subject` | `risk_level`, `risk_score`, `signals`, `summary` |
  | `FACT_CHECK_GENERAL` | `POST /fact-check` | `claim` | `claim`, `verdict`, `reasoning`, `sources` |
  | `CONTRACT_OBLIGATION_AUDIT_GENERAL` | `POST /audit` | `contract` | `verdict`, `obligations`, `clauses`, `summary` |

- Hard rule for every endpoint: always answer HTTP 200 with exactly the intent's response
  fields, including for a malformed body, a timeout or an internal error. Telegraph revokes
  routing from miners that error or go silent.
- The new intents must work **without an LLM**, using plain code and keyless public data
  sources only. (A Groq or other LLM provider may be added later, so keep the logic separable.)
- Hackathon facts (https://hackathon.telegraphprotocol.com/rules, Season II):
  - Main deadline: **December 1, 2026, 23:59 UTC**. Submit through the official H2 website.
  - Miners / Providers prizes: 4 "verified delivery" awards of $300 and an excellence prize
    ($500 first, $300 second). Delivery awards are decided first by "enables the most distinct
    commercial missions", so covering more distinct intents helps.
  - Every entry is scored out of 100 and needs at least 70, plus the track minimum
    (technical quality 24/35). Starring `github.com/telegraphprotocol/bounties` is required.
  - The rules page does not list any intent names, request fields or response fields.

## 2. Competition data

Source: `GET https://devnode.telegraphprotocol.com/api/miners` (about 3.8 MB), counting
`supported_intents` across 2,189 registered miners. Counts are miners registered for that
intent; fewer competitors means an easier top rank. Our three current intents have 15
(fraud), 20 (fact-check) and 13 (contract audit) competitors; our fraud intent ranks 3rd.

## 3. Intents to build

All can be answered by deterministic code plus keyless public data. Confidence is my
judgement of how well a code-only answer can score, not something I measured.

| # | Intent | Miners | Approach (no LLM) | Confidence |
|---|---|---|---|---|
| 1 | `IP_GEOLOCATION_GENERAL` | 14 | `ipwho.is` (already used in the fraud package), with a second keyless provider as fallback | High: scored against ground truth |
| 2 | `ENTITY_EXTRACTION_GENERAL` | 14 | regex and rule-based extraction (emails, URLs, dates, money, phone numbers, capitalised names) | Medium: depends on how quality is scored |
| 3 | `KEYWORD_EXTRACTION_GENERAL` | 15 | RAKE or TF-IDF with a stopword list | Medium |
| 4 | `SEMANTIC_SIMILARITY_GENERAL` | 17 | TF-IDF / character n-gram cosine similarity | Medium to low: LLM or embedding miners may win |
| 5 | `API_HEALTH_CHECK_GENERAL` | 18 | direct HTTP probe: status, latency, TLS, redirects, headers | High: scored against ground truth |
| 6 | `URL_SAFE_GENERAL` | 18 | existing OpenPhish feed, TLS check, redirect and header inspection, URL heuristics | Medium |
| 7 | `GRAMMAR_SPELL_CHECK_GENERAL` | 18 | dictionary-based spell check, simple grammar rules | Low to medium |
| 8 | `SERVER_UPTIME_MONITOR_GENERAL` | 19 | HTTP/TCP probe, availability and latency | High |
| 9 | `EMAIL_SECURITY_GENERAL` | 20 | SPF, DKIM and DMARC DNS lookups, MX records, domain age if available | High |
| 10 | `MALWARE_DETECTION_HASH` | 14 | hash lookup in a free service (MalwareBazaar, VirusTotal, or similar). Needs a free API key. | Medium; key required |

Suggested build order: 1, 5, 8, 9 (ground-truth intents first), then 3, 2, 6, then 4, 7, 10.

Excluded because they need models: speech to text, text to speech, image and video
intents, deepfake detection, chat and text generation, code generation, translation.

## 4. What I need from you (the missing facts)

For **each** intent in section 3, find its **canonical specification** on Telegraph's own
sources: the Telegraph/Anjoman docs, the dashboard (Earn / Build tabs), or the hackathon
site. Known entry points:

- `https://alexandria.telegraphprotocol.com` (Anjoman dashboard; `/dashboard/build`,
  `/dashboard/earn` and `/dashboard/hackathon` exist, `/docs` redirects to `anjoman.ai/docs`)
- `https://hackathon.telegraphprotocol.com` (`/intents`, `/tracks`, `/missions`, `/build`,
  `/how-it-works`; note `/intents` renders empty without JavaScript)
- `https://docs.telegraphprotocol.com` (redirects to `anjoman.ai/docs`)
- `https://devnode.telegraphprotocol.com/api/miners` (public list of competitors; its
  endpoint descriptions show what competing miners return, but they are **not** the canonical
  spec)

For each intent, report:

1. The exact canonical intent name.
2. **Request:** every parameter name, type, whether it is required, defaults and allowed
   values.
3. **Response:** every field name, type and enum value that Telegraph expects, in the exact
   spelling and casing. State whether extra fields are allowed or must not be present.
4. How the intent is **scored**: accuracy against ground truth, latency limits, format
   validity, and anything that triggers revocation.
5. One example request and one example response, if the source gives them.
6. Any rule about data sources, API keys or external services for this intent.

Format the answer as one section per intent, using a table for the request fields and a
table for the response fields. If a field is not documented anywhere you can find, write
`NOT FOUND` for it. **Do not guess or fill in plausible-looking field names**, because a
wrong field name makes the whole intent fail.

Also report, if you can find it: (a) whether `semantics.signal_mapping` in `miner.yaml` is
required for any of these intents and which fields it should map; (b) how to change a
registered miner's YAML and intents (`updateMiner` signature and whether the on-chain intent
list can grow); (c) whether there is a per-miner limit on the number of intents.

## 5. How the intents will be added (for reference)

Once the specs exist, each intent becomes:

- a new Python package in `TelegraphIntent/` (for example `ipgeo/`) with `app.py`
  exposing `create_app()`, a pipeline, models and tests, following the existing three;
- one entry in `INTENTS` in `telegraphintent/app.py`;
- one endpoint plus the intent name in `miner.yaml` and in `register.sh`;
- tests under `tests/<package>/`, plus a gateway test that the new route exists;
- then: redeploy on Render, update the YAML gist, recompute the SHA-256 and call
  `updateMiner`.

Related files in the repo: `docs/FraudRisk.md`, `docs/FactCheck.md`,
`docs/ContractObligationAudit.md`, `README.md`.
