Build a production-quality Telegraph Protocol "Miner" service for the FACT_CHECK_GENERAL
Intent, for the Telegraph Hackathon Season II (Miners/Providers track). Goal: rank #1 on
the leaderboard for this Intent, which means being judged the most accurate, best-evidenced,
most honest fact-checker among currently 19+ live competing miners.

## Ground truth (do not deviate from these — pulled live from Telegraph's own API)

Request schema (GET https://devnode.telegraphprotocol.com/engine/v1/intents/FACT_CHECK_GENERAL):
```json
{
  "request": { "params": [ { "name": "claim", "type": "string", "required": true, "description": "The claim to check" } ] },
  "response": { "fields": [
    { "name": "claim", "type": "string", "description": "The claim that was checked" },
    { "name": "verdict", "type": "enum", "allowed": ["True","Mostly True","Mixed","Misleading","Mostly False","False","Unverified"], "description": "Fact-check verdict" },
    { "name": "reasoning", "type": "string", "description": "The reasoning behind the verdict" },
    { "name": "sources", "type": "list", "description": "URLs or names of the sources the answer cites" }
  ]}
}
```
Intent class: `judgment` — there is no single correct answer. A trained reward model
(an "Evaluator adapter") scores the quality of `verdict` + `reasoning` + `sources` holistically.
There is no reference answer to beat — only "would a good human fact-checker approve of this
answer and its evidence." Confidently wrong answers score far worse than an honest
`"Unverified"` with no sources.

Known strong competitor already live on this Intent: a miner called "Qarinah ProofPack"
which advertises "evidence-backed, hash-verifiable web research... returns a verdict,
deterministic confidence components, supporting and refuting evidence, contradictions,
content hashes... and a governed abstention decision when evidence is insufficient."
Match or beat this bar: multi-source evidence, explicit contradiction handling, and a real
abstention path.

## What to build

A small, fast, well-tested backend service plus the Telegraph miner registration config.

### 1. Core API

- `POST /fact-check` — body `{"claim": "<string>"}` → returns exactly the 4 fields above,
  nothing more, nothing less, matching the enum exactly (case-sensitive, e.g. "Mostly True"
  not "mostly_true").
- Also expose `GET /healthz` for liveness checks (Telegraph does periodic spot-checks; any
  non-empty response on the real endpoint counts as alive — make sure the real endpoint
  itself never times out or returns empty under normal load).
- Framework: FastAPI (Python) or Express (Node) — pick FastAPI unless the repo already has a
  JS convention. Must be deployable as a single public HTTPS endpoint (Render, Fly.io,
  Railway, or similar free/cheap tier is fine for the hackathon).

### 2. The fact-checking pipeline (this is what determines whether we win)

Implement as clearly separated stages so each is independently testable:

1. **Claim parsing** — extract the actual checkable assertion, named entities, numbers,
   dates. Reject/short-circuit claims that are pure opinion or not checkable (return
   `Unverified` with reasoning explaining why) — don't guess on non-factual claims.
2. **Evidence retrieval** — query multiple independent sources in parallel with a hard
   timeout (e.g. 8–10s total budget):
   - General web search (use a search API — Tavily, Serper, Brave Search, or Bing — whichever
     the user has a free API key for; make the provider swappable via an interface/adapter
     pattern so it's not hard-coded to one vendor).
   - Wikipedia API (free, no key, good for stable factual claims).
   - A news search API if available (for recent/current-events claims).
   - Optionally Google Fact Check Tools API (free) for claims that may already have a
     published fact-check.
   Design this as a plugin list so new sources can be added without touching core logic.
3. **Source credibility scoring** — maintain a simple tiered trust list (gov/edu/major wire
   services/established fact-checkers = high; established mainstream media = medium;
   unknown domains = low; known misinformation domains = reject). Don't over-engineer with
   ML here — a maintained allow/deny list + a few heuristics (domain age signal not required)
   is enough for the hackathon timeline.
4. **Cross-source agreement analysis** — explicitly compute: do sources agree, partially
   agree, or contradict? Surface *which* sources support vs. refute the claim — this directly
   feeds the verdict mapping and should be visible in the reasoning text.
5. **Verdict decision logic** (deterministic, testable, NOT left to an LLM to improvise):
   - No credible evidence found, or evidence too thin/old/irrelevant → `Unverified`.
   - Strong, multi-source, uncontested support → `True`.
   - Mostly supported but with a minor caveat/imprecision → `Mostly True`.
   - Roughly balanced support and refutation → `Mixed`.
   - Technically factually accurate but framed/selectively presented to deceive →
     `Misleading` (write explicit logic/examples for this — most competitors get this wrong).
   - Mostly refuted with a small true element → `Mostly False`.
   - Clearly and strongly refuted → `False`.
   Write this as an explicit function with clear thresholds, not a black box, so it can be
   unit-tested.
6. **Reasoning + sources synthesis** — one LLM call (Claude, via the Anthropic API) that
   receives ONLY the retrieved evidence snippets + the computed verdict + the agreement
   analysis, and writes a tight 2–5 sentence `reasoning` string plus the final `sources` list
   (real URLs only, deduplicated, no fabrication — enforce this by only allowing URLs that
   actually appeared in the evidence retrieval step, never letting the LLM invent one).

### 3. Reliability & speed

- Hard global timeout per request (e.g. 12s) — if evidence retrieval is too slow, fail over
  to `Unverified` rather than hanging (Telegraph revokes miners that go silent or time out
  repeatedly).
- In-memory or Redis cache keyed on a normalized version of the claim (claims repeat often
  across callers) with a sane TTL (e.g. 1 hour for most, shorter for anything with "today",
  "this week", "breaking" in it).
- Structured logging of every request: claim, evidence retrieved, verdict, latency — needed
  both for debugging and as submission evidence for the hackathon.
- Retry/circuit-breaker around each external search provider so one flaky API doesn't take
  the whole service down.

### 4. Telegraph miner registration config (`miner.yaml`)

Follow the Telegraph Miner YAML Standard exactly (schema is strict — unknown top-level keys
are rejected outright). Produce a file shaped like this (fill in real values, pick an unused
numeric `id` and a free kebab-case `slug` by checking
`https://devnode.telegraphprotocol.com/api/miners` first):

```yaml
version: "1"
kind: miner
id: <pick an unused number, e.g. 7-8 random digits>
slug: <kebab-case-unique-slug>
protocol: generic
name: <Display Name> Fact Checker
description: Evidence-backed, multi-source fact-checking with honest abstention when evidence is insufficient.
base_url: https://<your-deployed-host>

auth:
  type: none   # or bearer/header if you put the endpoint behind your own API key

endpoints:
  - path: /fact-check
    external_path: /fact-check
    method: POST
    description: Checks a specific, checkable factual claim against multiple independent evidence sources and returns a verdict, reasoning and cited sources.
    intents: [FACT_CHECK_GENERAL]
    params:
      body:
        required:
          - name: claim
            type: string
            intents: ["*"]
            description: "The specific factual claim to verify. Must be a checkable assertion, not a general knowledge question or opinion."

semantics:
  signal_mapping:
    label_field: verdict
    reason_field: reasoning
  supported_intents:
    - FACT_CHECK_GENERAL
```

Do NOT add `input_schema`/`output_schema` nested under `endpoints[]` (they're top-level only,
if used at all). Do NOT add any key not explicitly in the Telegraph YAML field reference —
registration is rejected outright on any unknown key.

### 5. Registration script (`register.sh`)

A shell script wrapping the `cast` (Foundry) commands for Ethereum Sepolia testnet:
1. Compute `sha256sum miner.yaml` for the hash.
2. `approve` the Diamond contract to take the MACHINA bond.
3. Dry-run with `cast call registerMiner(...)` first, then the real `cast send`.
4. Print the resulting `registrationId` and a reminder to verify via
   `curl https://devnode.telegraphprotocol.com/api/miners/$REGISTRATION_ID`.
Use placeholder env vars for `DIAMOND`, `RPC`, `MACHINA`, `YAML_URL`, `FEE_ADDRESS`,
`MINER_PRIVATE_KEY` — never hardcode real secrets into the script.

### 6. Tests

- Unit tests for the verdict-decision function covering every one of the 7 enum values with
  synthetic evidence inputs (this is the highest-leverage test — it's pure logic, fast to get
  right, and is literally what's being judged).
- A regression fixture file of 15–20 real claims with known ground truth (mix of clearly true,
  clearly false, misleading, mixed, and a few that should correctly come back `Unverified`
  because they're obscure/unverifiable) — run the full pipeline against these and assert the
  verdicts land correctly (or acceptably close, e.g. True vs Mostly True is fine, True vs
  False is not).
- A latency test asserting p95 response time stays under whatever ceiling you choose (no hard
  published SLA for this Intent, unlike some others — but keep it well under 10s so routing
  never sees us as unreliable).

### 7. README

Explain: what this is, how to run it locally, how to test it, how to deploy it, how to
register it on Telegraph testnet, and known limitations. This doubles as hackathon
submission evidence ("how to run it" is a required submission field).

## Priorities if time is short

Build in this order, and each stage should work end-to-end before adding the next:
1. `/fact-check` endpoint that just does ONE search source + one LLM call → correct schema
   shape, deployed, working.
2. Add the explicit deterministic verdict-decision function (replace any LLM-guessed verdict
   with real logic) + abstention path.
3. Add multi-source cross-checking + contradiction detection.
4. Add caching, timeouts, logging, tests.
5. Write `miner.yaml` + `register.sh` + README last, once the API is stable.

Do not skip step 2 for step 3/4 — a well-reasoned single-source answer with honest abstention
beats a flaky multi-source pipeline that occasionally times out or hallucinates a source.
