# Proofline Fact Checker — Telegraph `FACT_CHECK_GENERAL` miner

Evidence-backed, multi-source fact-checking with **honest abstention**. A FastAPI service that takes a
claim and returns exactly the four fields of Telegraph's `FACT_CHECK_GENERAL` intent:

```json
{ "claim": "...", "verdict": "True | Mostly True | Mixed | Misleading | Mostly False | False | Unverified",
  "reasoning": "2-5 sentences naming which sources support / contradict", "sources": ["https://..."] }
```

The design principle: **a confidently wrong answer is worse than an honest `Unverified`.** The verdict is
computed by a deterministic, unit-tested function from weighted evidence — never improvised by an LLM — and
the service abstains whenever evidence is thin, off-topic, single-domain-conflicting, or from low-trust sources.

## How it works

```
POST /fact-check {"claim": "..."}
  1 parse        claim_parser.py   extract entities/numbers/dates; short-circuit opinions, predictions, open questions
  2 retrieve     retrieval.py      all sources in parallel under one budget, per-source circuit breaker + 1 retry
                 sources/          plugins: Wikipedia (no key), Tavily|Serper|Brave web+news, Google Fact Check
  3 credibility  credibility.py    tiers: HIGH (gov/edu/wires/fact-checkers/reference) MEDIUM LOW REJECTED(misinfo/satire)
  4 agreement    stance.py +       per-snippet stance -> weighted support / caveat / mixed / misleading / refute,
                 agreement.py      one domain can't dominate, freshness-weighted for time-sensitive claims
  5 verdict      verdict.py        PURE function, rules R1..R11 with explicit thresholds (see its docstring)
  6 synthesis    synthesis.py      one LLM call writes the reasoning; sources are chosen IN CODE from retrieved URLs
```

* **Misleading** (the verdict most competitors miss) has its own rule (R3): evidence labelled "literally accurate
  but missing context / deceptive framing" (from a published fact-check rating like *Misleading*/*Missing Context*,
  or from the stance labeller) outweighing outright refutation.
* **No fabricated sources, by construction:** the LLM never emits URLs. It may only pick *evidence ids*; the final
  `sources` list is built from URLs that were actually retrieved and is re-filtered against the retrieved set.
  Any URL in the model's reasoning text is stripped.
* **The 4-field contract holds on every path** — malformed JSON, missing claim, provider outage, global timeout,
  internal crash all return HTTP 200 with a valid `Unverified` body (Telegraph revokes miners that error or go silent).
* **Reliability:** hard 11.5 s global cap (retrieval 6 s, stance 2.5 s, synthesis 3 s budgets), TTL cache with
  single-flight de-duplication (1 h; 5 min for "today/breaking/this week…"; 30 s after partial provider failure;
  *never* cached when all providers failed), JSON-lines structured log per request.

### One deviation from the brief you should know about
The brief specifies **one** LLM call (reasoning). A deterministic verdict still needs per-snippet stance labels, and
a keyword heuristic cannot reliably tell "supports" from "merely mentions". So when `ANTHROPIC_API_KEY` is set the
service makes a **second, small, batched Claude call that only classifies snippets** (`STANCE_MODE=auto`); the verdict
is still computed by code. It has its own time slice and falls back to the heuristic labeller on any failure.
Set `STANCE_MODE=heuristic` for the strict one-call design (at lower accuracy). Without a key the service runs fully
keyless (heuristic labels + templated reasoning).

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env && $EDITOR .env                       # all keys optional; see below
set -a; source .env; set +a
uvicorn factcheck.app:app --port 8000

curl -s localhost:8000/healthz
curl -s -X POST localhost:8000/fact-check -H 'content-type: application/json' \
     -d '{"claim":"Humans only use 10% of their brains."}'
```

| Env var | Effect |
|---|---|
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` (default `claude-haiku-5-5`) | LLM stance labels + reasoning (recommended) |
| `TAVILY_API_KEY` / `SERPER_API_KEY` / `BRAVE_API_KEY` (+`SEARCH_PROVIDER`) | general web + news search (set one) |
| `GOOGLE_FACTCHECK_API_KEY` | pull existing published fact-checks (strongest signal for known viral claims) |
| none of the above | runs on Wikipedia alone, heuristic stance, templated reasoning |

Add a source: subclass `Source` in `factcheck/sources/`, register it in `build_sources()`. Add a search vendor:
subclass `SearchProvider`. Extend trust lists with `CREDIBILITY_OVERRIDES=/path/overrides.json`
(`{"high":[],"medium":[],"low":[],"reject":[]}`).

## Test

```bash
pytest                      # 200 offline tests, no network, no keys (~11 s)
pytest -m live -s           # opt-in: same claims vs live Wikipedia + any keys in env; includes live p95 latency
python scripts/record_fixtures.py --force   # re-snapshot Wikipedia evidence for the regression claims
```

* `test_verdict.py` — every one of the 7 enum values, rule boundaries, monotonicity, grid invariants.
* `test_regression.py` — 19 claims with ground truth (true / false / mixed / misleading / unverifiable). Evidence is
  a **recorded live Wikipedia snapshot**; the *Misleading* and *Mixed* cases use **hand-written, clearly synthetic**
  fact-checker items (real fact-checker articles for those scenarios don't exist). Asserts no polarity inversion
  (True-side vs False-side) and prints a scorecard.
* `test_latency.py` — offline: 120 concurrent claims with 80–600 ms mocked providers (measures pipeline/HTTP
  overhead, p95 ≈ 0.6 s, ceiling 3 s). Live variant: ceiling 9 s.
* `test_api.py` — contract on every error path, timeouts, slow-provider isolation, cache/normalisation, no caching
  of outages, and `/miner.yaml` byte-for-byte (so the on-chain hash matches what's served).
* `test_factcheck_source.py` — a published fact-check rating is adopted **only for the same claim**: the negated
  form of a rated claim inverts the stance (a `False` rating of "X causes Y" supports "X does not cause Y"), a
  review of a differently worded claim is kept as plain evidence with no preset stance, and conflicting figures
  block adoption. These items bypass the heuristic cap, so a mis-import would be the worst failure mode here.
* `test_llm_wire.py` — runs the **real Anthropic SDK against a local mock Messages endpoint**: verifies the request
  we build, the `thinking: disabled` 400 fallback (retried once per process, inside the original budget, keeping
  `effort: low`), `stop_reason: "refusal"`, and a full `llm`-mode pipeline run. It proves the wire path is
  well-formed; it cannot prove a real model answers well.
* `test_miner_yaml.py` — rejects any key outside Telegraph's closed schema.

### Measured results (this repo, keyless mode, Wikipedia only)
Offline replay: **0 polarity inversions**, 6/19 exact, 10/15 informative-and-acceptable (the rest abstain with
`Unverified`). Live (real Wikipedia): all 17 non-synthetic claims acceptable, p95 ≈ 1.3 s.

Exact-match is *structurally* capped in keyless mode: with only heuristic stance labels the service refuses to
return `True`/`False` at all (see below), so a `True` ground truth can at best score `Mostly True`. The metrics
that matter here are the zero polarity inversions and the informative rate. **This is the keyless floor** —
LLM-mode and keyed-provider accuracy have *not* been measured (no API keys were available in this environment).

### Two safety rules worth knowing
* **Attributed claims are not evidence.** "Activist *says* vaccines cause autism" is a report of a claim, not
  support for it. Such snippets are labelled neutral, so a claim echoed by many outlets abstains instead of
  scoring `Mostly True`. The check is token-level (the passage's attribution words minus the claim's), so a claim
  that merely contains such a word can't switch the guard off.
* **A published rating is only adopted for the same claim.** Google Fact Check results must match the claim in
  both directions, with matching negation and non-conflicting figures. An opposite-polarity match is inverted; a
  merely related one carries no preset stance.
* **Heuristic-only evidence cannot reach `True`/`False`.** When no stance label came from a model or a published
  fact-check rating, `decide()` caps the verdict at `Mostly True`/`Mostly False` and says so in the reasoning —
  keyword matching cannot reliably tell "the source states X" from "the source reports that someone claims X".

## Deploy (public HTTPS endpoint)

`Dockerfile` + `render.yaml` are included; any container host works (Render, Fly.io, Railway).

```bash
# Render: New > Blueprint > point at this repo; set ANTHROPIC_API_KEY (+ a search key) in the dashboard.
# Fly.io:  fly launch --no-deploy && fly secrets set ANTHROPIC_API_KEY=... TAVILY_API_KEY=... && fly deploy
curl https://<your-host>/healthz
```

**Cold starts:** free tiers that sleep (Render free) will time out Telegraph's spot-checks and hurt routing. Use an
always-on instance (`render.yaml` uses `starter`) or ping `/healthz` every few minutes. Run **one worker** (the cache
is in-process; swap `TTLCache` for Redis if you scale out).

## Register on Telegraph testnet (Ethereum Sepolia)

Reference: <https://docs.telegraphprotocol.com/docs/miners/miner-registration> (registerMiner signature below is
taken from it: `registerMiner(string yamlUrl, bytes32 yamlHash, address feeAddress, uint256 minPriceUsdc, string[] supportedIntents)`).

1. Deploy, then edit `miner.yaml`: set `base_url` to your HTTPS host (it ships with an obvious placeholder, and
   `register.sh` refuses to run until it's replaced). `id: 48271905` / `slug: proofline-fact-checker` were free on the
   devnode when checked; the script re-checks right before sending.
2. Host `miner.yaml` at a stable URL (IPFS recommended; HTTPS ok) — **byte-identical, LF endings** (the repo forces LF via `.gitattributes`).
3. Get Sepolia ETH (gas) and MACHINA for the bond. Per the docs the bond is **$1 of MACHINA at the pool price**
   for the first intent (≈1 MACHINA; +$0.10 per further intent) and is **returned when you deregister**. Buy it by
   swapping test USDC on the MACHINA/USDC pool — see *Addresses & Parameters → Getting test tokens*.
4. Install [Foundry](https://getfoundry.sh) (`cast`) and `jq`, then:

```bash
# Addresses below are the Sepolia ones in the docs at the time of writing - re-check Addresses & Parameters.
export DIAMOND=0x3624EB012B5E998BB57B6bE6C9Cfc9c4bc5eb99b
export MACHINA=0xd68305cA83C7AF42A4F720144E7416fB53F8AaF4
export RPC=https://ethereum-sepolia-rpc.publicnode.com
export YAML_URL=https://<host>/miner.yaml  FEE_ADDRESS=0x...  MINER_PRIVATE_KEY=0x...
DRY_RUN_ONLY=1 ./register.sh      # preflight only; sends nothing (see note below)
./register.sh                     # approve bond -> free simulation -> registerMiner only if it passes
curl -s https://devnode.telegraphprotocol.com/api/miners/$REGISTRATION_ID | jq '.miner | {activation_status, rejection_reason}'
```

The script computes the **SHA-256** (not keccak) of `miner.yaml`, verifies the hosted file hashes identically, checks
the id/slug are unused and `FACT_CHECK_GENERAL` is canonical, reports balances/allowance, asks for confirmation,
approves the bond, **simulates `registerMiner` for free, and sends only if that simulation succeeds.**

*Why the approval comes before the simulation:* `registerMiner` pulls the MACHINA bond, so a `cast call`
simulation is **expected** to revert on that transfer until an allowance exists — a revert that would tell you
nothing about your YAML. (This is read from the docs' own ordering; it was not verified against a live chain here.)
With no allowance yet, `DRY_RUN_ONLY=1` therefore runs every off-chain preflight and stops without simulating;
approve the bond once (the script prints the exact command) and re-run it for a pure simulation.

The service also serves its own YAML at `GET /miner.yaml` (raw bytes, so the hash matches), so `YAML_URL` can simply
be `https://<host>/miner.yaml`. **Re-host and re-hash after any YAML edit** — including filling in `base_url`, which
means deploying, then committing the YAML change, then redeploying so the served bytes match what you hash locally.
IPFS is the more robust option (the bytes can't drift with a deploy).
Nodes activate you ~10–15 minutes after the transaction. If `activation_status` is `rejected`, fix the YAML, re-host,
and call `updateMiner(oldRegistrationId, yamlUrl, yamlHash, feeAddress, minPriceUsdc, intents)` (don't re-register).

## Known limitations (read before relying on this)

* **Never exercised against the real services:** the Claude calls were run only against a local mock endpoint
  (`test_llm_wire.py`) — no API key existed in this environment, so no real model has ever answered. Every keyed
  provider (Tavily, Serper, Brave, Google Fact Check) is untested live. Deployment and on-chain registration are
  untested: `cast` is not installed here, so `register.sh` was syntax-checked and exercised against a **stubbed**
  `cast` covering its five paths (no allowance, allowance present, approve→simulate→send, a reverting simulation,
  and confirmation prompts). All are written against the documented APIs.
* **Keyless accuracy is modest.** With Wikipedia alone and heuristic stance, the service mostly abstains or returns
  `Mostly True`/`Mostly False` — `True`/`False` need ≥ 2 independent domains. Real accuracy depends on setting
  `ANTHROPIC_API_KEY` plus a search key. Expect to tune `Thresholds` in `verdict.py` once you can see live traffic
  (every request logs per-source stance, weights, and the rule that fired).
* The Anthropic call uses `effort: low` (and tries `thinking: disabled`) for latency. Current Opus/Sonnet/Fable
  models reject `thinking: disabled` with a 400; the first such 400 is remembered for the process and the retry
  stays inside the original time budget. Any failure falls back to heuristic labels / templated reasoning.
* **Model default is `claude-haiku-5-5`**, chosen for the 2.5–3 s time slices, not because it is the most capable
  option. Override with `ANTHROPIC_MODEL` (e.g. `claude-opus-5-5` or `claude-sonnet-5-5`) and re-measure latency —
  accuracy will likely improve, p95 will rise.
* Stance labelling sees ≤ 10 snippets of ≤ 520 characters; snippets are search-result excerpts, not full articles.
* Source tiers are a hand-maintained list, not a live reputation service. Wikipedia counts as a HIGH-tier reference.
* In-memory cache and circuit breakers are per-process and reset on restart.
* English-only. Time-sensitive claims are only as fresh as the search provider's index.
* Wikimedia's edge rejected httpx's HTTP/1.1 requests from the dev machine (403) regardless of User-Agent; the client
  therefore uses HTTP/2. Set `HTTP_USER_AGENT` to a descriptive string with your own contact info, per Wikimedia policy.
