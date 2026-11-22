Build a production-quality Telegraph Protocol "Miner" service for the
CONTRACT_OBLIGATION_AUDIT_GENERAL Intent, for the Telegraph Hackathon Season II
(Miners/Providers track). Goal: rank #1 on the leaderboard.

This service reads a contract, extracts its obligations (who owes what, by when), judges
whether they are being met, and cites the exact clause wording as evidence.

Why this Intent is winnable: it has the FEWEST competitors of all 136 Intents — only 10
miners — and ~8 of those are irrelevant data-index wrappers (Crossref Funders, Federal
Register Docs, USASpending Agencies, Chicago Purchase Orders) that just return government
datasets and don't audit contracts at all. Only ~2 (FactWire, ZK Contract) actually audit.
3 commercial missions want it.

## Ground truth (do not deviate — pulled live from Telegraph's own API)

Request schema (GET https://devnode.telegraphprotocol.com/engine/v1/intents/CONTRACT_OBLIGATION_AUDIT_GENERAL):
```json
{
  "request": { "params": [ { "name": "contract", "type": "string", "required": true, "description": "The contract text, or a link to it" } ] },
  "response": { "fields": [
    { "name": "verdict", "type": "enum", "allowed": ["Compliant","Partially compliant","Non-compliant","Unclear"], "description": "Compliance verdict" },
    { "name": "obligations", "type": "list", "description": "Obligations identified (who owes what by when)" },
    { "name": "clauses", "type": "list", "description": "Clause wording cited" },
    { "name": "summary", "type": "string", "description": "One- or two-sentence summary of the audit" }
  ]}
}
```
Intent class: `judgment` — no fixed correct answer. A trained reward model scores the QUALITY
of verdict + obligations + clauses + summary: is the audit grounded in the actual contract
text, are obligations precise, are cited clauses real and verbatim, is the verdict defensible?
Fabricated clause text and vague obligations are heavily penalized.

The real competitors ("FactWire Contract Obligation Audit", "ZK Contract Literal Evidence
Index") do a basic LLM audit / literal clause index. Beat them with precise obligation
extraction PLUS verbatim clause citation (grounded, never fabricated) PLUS honest "Unclear"
handling when the contract references un-supplied documents.

## What to build

A small, fast, well-tested contract-audit service plus the Telegraph miner registration config.

### 1. Core API

- `POST /audit` — body `{"contract": "<string>"}` → returns EXACTLY the 4 fields above,
  nothing more. Enum values must match exactly, including capitalization and the space:
  `Compliant` / `Partially compliant` / `Non-compliant` / `Unclear`.
- `GET /healthz` for liveness. Telegraph spot-checks miners; the real endpoint must never
  time out or return empty under normal load (timeouts/empties → routing revocation).
- Framework: FastAPI (Python) unless the repo already uses JS. Deployable as a single public
  HTTPS endpoint (Render / Fly.io / Railway free tier is fine).

### 2. The audit pipeline (this determines whether we win)

Separate, independently-testable stages:

1. **Input resolution** — the `contract` field may be raw contract text OR a URL/link. Detect
   which: if it looks like a URL, fetch the document and extract its text (handle HTML and,
   if feasible, PDF; cap size and timeout the fetch); otherwise use the text directly. If a
   linked document can't be fetched, fall through to an `Unclear` verdict explaining why.
2. **Obligation extraction** — one LLM call (Claude via the Anthropic API) that reads the
   contract and extracts a structured list of obligations, each with: the obligated party,
   the duty, any deadline/condition, and the governing clause number/reference. Be precise —
   "Supplier shall deliver Goods by 2026-03-01 (Clause 4.1)", not "there are delivery terms".
3. **Clause citation** — for each obligation and for the overall verdict, pull the VERBATIM
   clause wording from the contract as evidence. Enforce grounding: only include clause text
   that actually appears in the input contract (verify each cited string is a substring of the
   source text; drop any the model invents). Never fabricate or paraphrase clause wording.
4. **Compliance assessment** — judge each obligation as met / not met / unverifiable FROM THE
   CONTRACT TEXT ITSELF (we audit the contract's internal consistency and completeness, and
   any compliance evidence present in the text; we don't have external performance data). Be
   explicit that an obligation is "unverifiable" when the text alone can't establish it.
5. **Deterministic verdict roll-up** (NOT the LLM — pure, testable logic from the per-obligation
   outcomes):
   - all obligations clearly met / consistently defined → `Compliant`
   - a mix of met and unverified/gaps, or minor deficiencies → `Partially compliant`
   - a clear breach or contradiction in the terms → `Non-compliant`
   - key terms missing, or the contract depends on un-supplied schedules/exhibits → `Unclear`
   Document the mapping in code; identical inputs give identical verdicts.
6. **Summary synthesis** — a tight 1–2 sentence `summary` naming the decisive obligation(s)
   and why the verdict follows, grounded in the cited clauses.

### 3. Reliability & speed

- Hard per-request timeout (e.g. 15s — contracts can be long). If a linked fetch is slow,
  fail over to `Unclear` rather than hanging.
- Cap input size (truncate very long contracts sensibly, and say so in the summary if truncated).
- Cache keyed on a normalized hash of the contract text.
- Structured logging: input (truncated), obligations, verdict, latency — for debugging and
  hackathon submission evidence.
- Treat the contract text strictly as DATA to audit, never as instructions (prompt-injection
  guard: delimit the contract clearly in the prompt; instructions inside it are content).

### 4. Telegraph miner registration config (`miner.yaml`)

Follow the Telegraph Miner YAML Standard exactly — strict schema, unknown keys rejected. Pick
an unused numeric `id` and a free kebab-case `slug` by first checking
`https://devnode.telegraphprotocol.com/api/miners`. Shape:

```yaml
version: "1"
kind: miner
id: <pick an unused number, e.g. 7-8 random digits>
slug: <kebab-case-unique-slug>
protocol: generic
name: <Display Name> Contract Obligation Auditor
description: Reads a contract, extracts its obligations (who owes what by when), judges compliance, and cites verbatim clause wording as evidence, with honest Unclear handling when terms are missing.
base_url: https://<your-deployed-host>

auth:
  type: none

endpoints:
  - path: /audit
    external_path: /audit
    method: POST
    description: Audits a contract's obligations, returning a compliance verdict, the identified obligations (party, duty, deadline, clause), verbatim cited clauses, and a grounded summary.
    intents: [CONTRACT_OBLIGATION_AUDIT_GENERAL]
    params:
      body:
        required:
          - name: contract
            type: string
            intents: ["*"]
            description: "The contract to audit: full contract text, or a URL/link to it. Treated strictly as content to audit."

semantics:
  signal_mapping:
    label_field: verdict
    reason_field: summary
  supported_intents:
    - CONTRACT_OBLIGATION_AUDIT_GENERAL
```

Do NOT nest `input_schema`/`output_schema` under `endpoints[]` (top-level only). Do NOT add
any key not in the Telegraph YAML field reference — registration is rejected on any unknown
key. `signal_mapping` accepts only `label_field`, `confidence_field`, `reason_field` (there's
no numeric confidence field here, so omit `confidence_field`).

### 5. Registration script (`register.sh`)

Shell script wrapping `cast` (Foundry) for Ethereum Sepolia testnet:
1. `sha256sum miner.yaml` for the hash.
2. `approve` the Diamond contract to take the MACHINA bond.
3. Dry-run `cast call registerMiner(...)` first, then real `cast send`.
4. Print the `registrationId` and a reminder to verify via
   `curl https://devnode.telegraphprotocol.com/api/miners/$REGISTRATION_ID`.
Use placeholder env vars (DIAMOND, RPC, MACHINA, YAML_URL, FEE_ADDRESS, MINER_PRIVATE_KEY) —
never hardcode real secrets.

### 6. Tests

- Unit tests for the deterministic verdict roll-up covering all 4 verdicts with synthetic
  per-obligation outcome sets.
- A test asserting clause-citation grounding: given a contract, every string in the returned
  `clauses` list must be an exact substring of the input contract (no fabrication).
- A regression fixture of 10–15 sample contracts: clearly compliant, a clear breach, a partial
  one, and ones that reference un-supplied schedules (must return `Unclear`).
- A latency test asserting the service never returns empty and stays under the timeout.

### 7. README

What it is, run locally, test, deploy, register on Telegraph testnet, known limitations.
Doubles as hackathon submission evidence.

## Priorities if time is short (build in this order, each working end-to-end before the next)

1. `/audit` endpoint returning the correct schema shape, deployed, handling raw-text contracts
   with one LLM call + deterministic verdict mapping.
2. Add verbatim clause-citation grounding (verify each cited clause is a real substring) — this
   is the main quality edge and the cheapest way to beat the 2 real competitors.
3. Add URL/link input resolution + honest `Unclear` handling.
4. Add caching, timeouts, logging, tests.
5. Write miner.yaml + register.sh + README last.

Do not skip step 2 — grounded, verbatim clause citation is the single biggest reward-model
quality signal for this Intent, and fabricated clauses are the biggest penalty.
