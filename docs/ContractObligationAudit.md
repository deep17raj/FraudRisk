# ClauseWire — Telegraph Contract Obligation Auditor

A miner for Telegraph Protocol's **`CONTRACT_OBLIGATION_AUDIT_GENERAL`** intent
(Hackathon Season II, Miners/Providers track).

It reads a contract, extracts its obligations (who owes what, by when, under which clause),
judges whether each one stands on the face of the text, and **cites the exact clause wording
as evidence** — verified, character for character, against the contract it was given.

```bash
curl -X POST https://your-host/audit -H 'Content-Type: application/json' \
  -d '{"contract":"4.1 The Consultant shall deliver the final report by 1 October 2025. 4.2 The parties record that the final report was delivered on 15 November 2025."}'
```

```json
{
  "verdict": "Non-compliant",
  "obligations": [
    "The Consultant: deliver the final report by 1 October 2025 (4.1) - not met: the text records delivery on 15 November 2025, 45 days late"
  ],
  "clauses": [
    "The parties record that the final report was delivered on 15 November 2025"
  ],
  "summary": "Non-compliant: The Consultant's obligation to deliver the final report (4.1) is not met - the text records delivery on 15 November 2025, 45 days late."
}
```

---

## The response contract

`CONTRACT_OBLIGATION_AUDIT_GENERAL` is a **judgment** intent: there is no fixed correct
answer, and a reward model scores the *quality* of the four fields. The endpoint returns
exactly those four and nothing else:

| field | type | notes |
|---|---|---|
| `verdict` | enum | `Compliant` \| `Partially compliant` \| `Non-compliant` \| `Unclear` — the exact spelling, including the space. This is the only field Telegraph records **on-chain**. |
| `obligations` | list of strings | one line per obligation: party, duty, deadline, verified clause reference, outcome |
| `clauses` | list of strings | verbatim clause wording from the supplied contract |
| `summary` | string | one or two sentences naming the decisive obligation and why the verdict follows |

Telegraph encodes the request field `contract` in **256 bytes** on-chain, so a routed request
often carries a *link* rather than a document. Resolving one is part of the core path here,
not an extra.

---

## The quality edge

The intent has few competitors, and most of them are data-index wrappers that return
government datasets without auditing anything. Of the two that genuinely audit, one runs a
plain LLM pass and the other returns a literal clause index that always answers `Unclear`.
This service is built to beat both on the three things the reward model actually measures.

### 1. Citations are verbatim or they are not returned

Fabricated clause wording is the heaviest penalty this intent carries. Nothing reaches
`clauses` unless it was **located in the source document**, and what is returned is a slice
of that document (`source[a:b]`) rather than the model's rendering of it.

A naive `quote in source` check is not enough, and failing it quietly is just as bad as
inventing text — it leaves `clauses` empty and the audit ungrounded. Real quotes come back
with line wraps collapsed, curly quotes straightened, PDF hyphenation joined, inline quote
marks dropped, or a "Clause 4.1 states that" preamble glued on. So the source is normalised
once into a parallel view that remembers, for every normalised character, the offset of the
original character it came from. A quote is normalised the same way, found in that view, and
mapped back through the offset table into an exact slice of the untouched source.

What that buys, all as genuine verbatim slices:

| the model returns | the document contains | result |
|---|---|---|
| `deliver the Goods to the Buyer's warehouse by 1 March 2026` | the same clause wrapped across two lines | located, slice keeps the line break |
| `in accordance with the delivery timetable` | `the deliv-\nery timetable` | located |
| `within "thirty (30) days" of invoice` | `within "thirty (30) days" of invoice` (curly) | located |
| `The Buyer shall pay £10,000 within thirty (30) days of invoice` | the same, with inline quote marks | located |
| `Clause 4.1 states that The Supplier shall deliver the Goods` | `The Supplier shall deliver the Goods` | preamble trimmed |
| `The Supplier shall indemnify the Buyer against all losses` | nothing of the sort | **dropped** |

The last row is the one that matters most. Edge-trimming alone would salvage that fabrication
down to `The Supplier shall` — three words that occur in nearly every contract and are
evidence of nothing. A salvaged run must therefore clear a length floor **and** account for
at least half of what the model claimed, or it is discarded. An invented clause number is
treated the same way: `(Clause 7.3)` is printed only if that numbering really appears in the
text.

### 2. No status stronger than "unverifiable" survives without evidence

The grounding rule is wired into the verdict, not just the citation list. If an obligation is
claimed `met` or `breached` and the clause wording supporting it **cannot be located**, the
claim is demoted to `unverifiable` and the obligation says so. A finding of compliance and an
accusation of breach both need evidence, so a model that hallucinates its way to `Compliant`
cannot get there — the demotion changes the verdict.

### 3. Honest `Unclear`, backed by the text

A contract that hands its real terms to a schedule nobody supplied cannot be audited, and
saying so is the correct answer rather than a hedge. That finding is not taken on the model's
word: a regex over dependency phrasing (`as set out in Schedule A`, `more particularly
described in the Statement of Work`, `Appendix C (not attached)`) is paired with a check for
whether that document actually appears as a heading in the text. If the contract leans on a
document that is not present, the verdict is `Unclear` and the summary **names the document**
and asks for it. This fires even when the model missed it.

---

## How the audit works

Five stages, each independently testable, in `audit/`:

| stage | module | what it does |
|---|---|---|
| 1. input resolution | `fetch.py` | Is this contract text or a link? Fetch HTML/PDF under a byte cap and a timeout; extract text; on failure, fall through to `Unclear` **explaining why**. |
| 2. obligation extraction | `extract.py` | One LLM call (Claude, `claude-opus-5-5`). Structured output constrains it to obligations with a status from a closed set — it has no field for a verdict or a summary. |
| 3. clause citation | `grounding.py` | Locate every quote in the source; return exact slices; drop fabrications; verify clause references. |
| 4. compliance assessment | `pipeline.py` | Demote any status the located evidence does not support. |
| 5. verdict + summary | `verdict.py`, `summary.py` | A pure roll-up table, then a templated summary that cannot contradict it. |

With no API key — or after a timeout, refusal, truncated reply or unparseable JSON — stage 2
degrades to `reader.py`, which reads obligation-bearing clauses (`shall`, `must`, `agrees
to`) straight out of the text. Those quotes are real contract wording, so citations stay
verbatim; everything is reported `unverifiable`, the verdict is honest, and the summary says
the deterministic reader was used.

### The verdict table

Computed in code from the per-obligation statuses, never asked for in prose, so identical
structure always gives an identical verdict. First match wins:

| rule | condition | verdict |
|---|---|---|
| `no_text` | nothing auditable was supplied or resolved | `Unclear` |
| `breach` | an obligation is breached on the face of the text | `Non-compliant` |
| `contradiction` | two terms cannot both hold | `Non-compliant` |
| `no_obligations` | text was read but creates no identifiable duty | `Unclear` |
| `missing_key_terms` | no identifiable parties, consideration or duties | `Unclear` |
| `missing_dependency` | the real terms sit in a document not supplied | `Unclear` |
| `nothing_established` | every obligation is unverifiable from the text | `Unclear` |
| `truncated` | all obligations met, but the text was cut short | `Partially compliant` |
| `all_met` | every obligation is met and consistent | `Compliant` |
| `mixed` | some met, some unverifiable | `Partially compliant` |

A breach outranks a missing schedule: a contradiction visible in the supplied text is
established, whereas a missing document only limits what else could have been checked.

**What `Compliant` claims.** We audit the contract text, not the world. It means every
obligation found is completely and consistently specified, or the text itself records
performance. It never asserts that the parties have actually performed — and the summary
says so in as many words.

---

## Run locally

```bash
pip install -r requirements-dev.txt
cp .env.example .env            # add ANTHROPIC_API_KEY (optional; it degrades without one)
set -a; source .env; set +a
uvicorn audit.app:app --reload --port 8000

curl -s localhost:8000/healthz
curl -s -X POST localhost:8000/audit -H 'Content-Type: application/json' \
  -d '{"contract":"1.1 The Supplier shall deliver 500 units by 1 March 2026."}' | jq
```

Routes: `POST /audit`, `GET /healthz`, `GET /miner.yaml` (byte-exact), `GET /`.

## Test

```bash
pytest                  # 399 tests, no network, no API key, no cost
pytest -m live -s       # opt-in: the real Anthropic API (needs a key; costs money)
python scripts/demo.py  # table: verdict, obligations, citations, latency per fixture
```

| file | what it pins |
|---|---|
| `test_verdict.py` | the roll-up: all four verdicts, precedence, totality over every outcome combination, determinism |
| `test_grounding.py` | wrapped/hyphenated/curly/preambled quotes are located; fabrications, paraphrases and generic fragments are dropped |
| `test_pipeline.py` | ungrounded `met`/`breached` are demoted; invented clause refs are not printed; injection cannot force a verdict; caching |
| `test_regression.py` | 15 sample contracts × 8 assertions — verdict, verbatim citations, decisive clause cited, summary agrees with verdict |
| `test_api.py` | exactly four fields and HTTP 200 on every path: malformed JSON, wrong types, crashes, timeouts, oversized input |
| `test_fetch.py` | link vs text detection, the SSRF guard, redirect re-checking, byte cap, HTML/PDF extraction |
| `test_latency.py` | p95 inside budget, never empty under a burst, single-flight de-duplication, bounded citation search |
| `test_miner_yaml.py` | Telegraph's closed schema, LF-only bytes, and that the served YAML hashes to the file on disk |

## Deploy

Any host that gives you one public HTTPS endpoint. `render.yaml` and a `Dockerfile` are
included:

```bash
docker build -t clausewire . && docker run -p 8000:8000 -e ANTHROPIC_API_KEY=sk-... clausewire
```

> **Do not use a free tier that sleeps.** Render's free web service sleeps after ~15 minutes
> idle with a 30–60 s cold start, which will fail Telegraph's spot-checks and can cost you
> routing. Use a paid instance, or keep a pinger on `/healthz`.

## Register on Telegraph (Sepolia testnet)

1. Deploy, then put the HTTPS URL in `base_url` in `miner.yaml` (replacing the placeholder —
   `register.sh` refuses to proceed while it is still there).
2. Host `miner.yaml` somewhere fetchable. The deployed service serves its own at
   `/miner.yaml`, byte for byte, so `YAML_URL` can point straight at it.
3. Run the script. It probes the live endpoint, checks the verdict is one of the four allowed
   values, checks the id/slug are free, verifies the hosted YAML hashes to the local file,
   and dry-runs the registration **before** anything is sent:

```bash
export DIAMOND=0x... RPC=https://... MACHINA=0x... FEE_ADDRESS=0x...
export YAML_URL=https://your-host/miner.yaml
export MINER_PRIVATE_KEY=0x...        # never commit this
DRY_RUN_ONLY=1 ./register.sh          # free simulation, no transactions
./register.sh                         # approve the bond + register
```

Verify ~10–15 minutes later (nodes read Sepolia's `safe` block):

```bash
curl -s https://devnode.telegraphprotocol.com/api/miners/$REGISTRATION_ID \
  | jq '.miner | {activation_status, rejection_reason, retrying}'
```

Expect `activation_status: active`. If it is `rejected`, fix `miner.yaml`, re-host it,
recompute the hash and call `updateMiner`. Registration is rejected on **any** unknown key,
so run `pytest tests/test_miner_yaml.py` first — it is far cheaper than a failed transaction.

Identity: `id: 71428539`, `slug: clausewire-contract-obligation-auditor` (both verified unused
against `/api/miners` at the time of writing — re-check before registering).

## Reliability

- **Hard 13.5 s global timeout**, with sub-budgets for fetching (6 s) and the model call
  (8 s). On expiry the service answers `Unclear` honestly rather than hanging.
- **`POST /audit` always returns HTTP 200 with the four fields** — malformed JSON, an empty
  body, wrong types, a crashed pipeline or a timeout all produce a valid `Unclear`.
- **Every model failure is non-fatal**: timeout, refusal (`stop_reason: "refusal"`, with
  server-side fallback enabled), truncation, malformed JSON and a missing key all degrade to
  the deterministic reader.
- **The contract is data, never instructions.** It is passed inside delimiters the input
  cannot close early (any occurrence in the input is neutralised), the system prompt states
  that embedded instructions are content to audit, and the model is given **no field** in
  which to return a verdict or summary — so even a compromised extraction cannot choose the
  answer.
- **SSRF-guarded fetching**: http/https only, every resolved address must be public, the
  check is repeated on **each redirect hop** (a public hostname can redirect to `127.0.0.1`
  or a cloud metadata endpoint), and the body is streamed under a hard byte cap.
- **Caching** on a normalised hash of the contract, with single-flight de-duplication so a
  burst of identical spot-checks costs one model call. Degraded results cache for 30 s only.
- **Structured JSON logging**: one `audit` record per request with the input (truncated),
  verdict, rule that fired, obligations, citation count, quotes dropped, statuses demoted and
  latency.

## Known limitations

- **No external performance data.** This audits the contract text and any compliance
  evidence recorded *in* it. It cannot know whether a delivery actually happened unless the
  document says so. `Compliant` is a statement about the text, and the summary makes that
  explicit.
- **Not legal advice**, and no jurisdiction-specific reasoning: no implied terms, statutory
  overrides or case law.
- **Scanned PDFs** have no text layer and there is no OCR; the service says the document
  contains no extractable text rather than guessing.
- **Very long contracts are truncated** at `MAX_CONTRACT_CHARS` (120k). A truncated audit can
  never return `Compliant` — it is downgraded to `Partially compliant` — and the summary says
  terms were not audited.
- **Obligation extraction is capped** at 10 obligations, most consequential first; a
  200-clause master agreement is summarised, not exhaustively enumerated.
- **The deterministic fallback is coarser.** It reports obligations as `unverifiable`, so with
  no API key the service rarely returns anything but `Unclear`. Citations stay verbatim.
- **In-memory cache, single worker.** Running multiple workers gives each its own cache;
  swap `TTLCache` for Redis to share one.
