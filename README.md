# Sentinel Fraud Risk — a Telegraph `FRAUD_RISK_GENERAL` miner

A fraud-risk assessor for the [Telegraph Protocol](https://devnode.telegraphprotocol.com)
`FRAUD_RISK_GENERAL` intent. It takes one free-text `subject` — an IP, a card BIN, an email,
a domain or URL, a wallet address, or a described transaction — gathers signals from several
independent sources in parallel, has a language model decide which of those signals actually
apply and what fraud pattern they match, and then computes the score with fixed, deterministic
arithmetic.

```
POST /fraud-risk   {"subject": "..."}  ->  {risk_level, risk_score, signals, summary}
GET  /healthz                          ->  liveness + source/feed status
GET  /miner.yaml                       ->  the registration config, byte-exact
```

Note on input length: Telegraph's on-chain request encoding caps `subject` at **256 bytes**, so
a long scenario arriving through the chain is truncated before this service sees it. The
service itself accepts up to 2000 characters (`MAX_SUBJECT_CHARS`) for direct callers.

## Why this is built the way it is

`FRAUD_RISK_GENERAL` is a **judgment** intent: there is no single correct score, and the real
fraud label — if it ever arrives — arrives later. A reward model grades the *quality* of the
answer: are the level, the score, the signals and the summary sensible, mutually consistent
and genuinely justified? Two things are punished hardest: **fabricated signals** and
**a score that disagrees with its own level**. Three design decisions follow from that.

**1. The model annotates; it never scores.** The LLM receives only the subject and the signals
the sources actually returned. Its reply is constrained by a JSON schema that offers no field
in which to add evidence: it may return signal **ids drawn from the supplied list**, a strength
from `{weak, moderate, strong}`, a pattern from a closed enum, and the summary. Any id it
invents is dropped in code ([`reasoning.py`](fraudrisk/reasoning.py)). The score is then
computed from the annotated signals by a pure function ([`scoring.py`](fraudrisk/scoring.py)).
So identical inputs always produce an identical `risk_score`, and `risk_level` is derived from
that rounded score by one function — the two cannot drift apart at a boundary.

**2. Corroboration across signal families, not stacking within one.** Signals are grouped into
families (IP reputation, anonymity, phishing, domain, card, behaviour, identity). Within a
family, weights combine with a decaying noisy-OR, so three phrasings of one underlying fact
cannot climb the scale. Across families, a plain noisy-OR. **A single family alone is capped at
0.78** — it can reach `HIGH`, never `CRITICAL`. `CRITICAL` is reserved for subjects where
independent *kinds* of evidence agree.

One narrow exemption: a signal the *source* marked `verified` — a directly checked fact about
that exact entity — escapes the cap if it is also severe enough to justify the top band on its
own. In practice that is a live OpenPhish hit on the exact URL (0.92: the page is harvesting
credentials right now). Membership of the Tor exit list is equally verified but less severe
(0.72: concealment, with many legitimate privacy users), so it stays capped at `HIGH` — which
also keeps it consistent with the keyless path, where no model is there to rate it. The
exemption keys on provenance and severity, never on magnitude alone, so a pile of confident
*inferences* can never buy it.

**3. Described scenarios are treated as the main case, not the leftover one.** Most callers
describe a transaction rather than hand over an identifier, so a scenario must produce real
signal without any lookup succeeding. Alongside the generic cues (amount, payee novelty,
velocity, device change, credential reset) the rule library recognises eleven **named scam
archetypes** from their own wording — safe-account calls, invoice redirection and payroll
diversion, SIM swap, romance fraud, overpayment refunds, mule recruitment, gift-card demands,
coaching the payer against the bank, disputes that match the customer's own device, and stolen
or synthetic identities. Those archetypes are the reason a sentence like *"a caller claiming to
be from the bank fraud team persuaded the customer to move their balance to a safe account"*
grades `HIGH` and is named as an authorised push payment scam instead of returning nothing.

**4. Honest abstention, worded four different ways.** All four are `LOW` at score `0.0`, and
they are genuinely different statements about the evidence — telling a detailed scenario that
it "describes no behaviour" would be false, and telling a caller that an unchecked subject came
back clean would be worse:

| Outcome | When | Cached? |
|---|---|---|
| *Insufficient information* | the subject is short and unspecific — no identifier, no behaviour (`"check this person"`) | no |
| *Nothing this miner checks matched* | a detailed scenario, but none of the indicators applied — stated as a limit of the checks, not a clean bill of health | yes |
| *No source covers this subject* | the identifier is of a kind nothing handles (a bare crypto wallet) | yes |
| *Could not check* | every source failed, so absence of signal proves nothing | **no** |

A fifth outcome, *checked and clean*, is also `LOW` but is a positive statement: sources ran
and returned only benign observations, which the summary names.

Inventing a red flag to look confident is treated as a serious error, so the rule library fires
only on explicit cues: `new_payee is True` scores, `False` ("an existing payee") produces a
benign observation, and `None` ("not mentioned") produces nothing. Silence is never evidence.

## The pipeline

| Stage | Module | What it does |
|---|---|---|
| 1. Classify | [`classify.py`](fraudrisk/classify.py) | Detects a primary kind **and every embedded entity**, so one scenario can feed several sources. Extracts amounts, account age, new-payee/first-transfer, velocity, geography and social-engineering cues, with negation guards. |
| 2. Gather | [`gather.py`](fraudrisk/gather.py) | Runs every relevant source in parallel under a hard deadline, each behind its own circuit breaker and one bounded retry. |
| 3. Reason | [`reasoning.py`](fraudrisk/reasoning.py) | One Claude call: which signals genuinely apply, how strongly, which fraud pattern, and the summary. |
| 4. Score | [`scoring.py`](fraudrisk/scoring.py) | Pure, deterministic fusion → `risk_score` → `risk_level`. |
| 5. Respond | [`app.py`](fraudrisk/app.py) | Exactly four fields, always HTTP 200. |

### Signal sources

Sources are a plugin list ([`sources/__init__.py`](fraudrisk/sources/__init__.py)); each declares
the entity kinds it `handles`, and routing follows from that. Adding one is a single line.

| Source | Family | Key? | Signal |
|---|---|---|---|
| `behaviour_rules` | behaviour | — | 27 scored rules + 8 benign observations over a described scenario: generic cues (amount, payee novelty, velocity, device, credentials) plus 11 named scam archetypes (offline, so it always runs) |
| `domain_shape` | domain | — | brand-impersonation shape, abused TLD, punycode, hyphen runs (offline) |
| `email_hygiene` | identity | — | disposable mailbox providers, generated local parts (offline) |
| `ip:ipwho` *(swappable)* | ip / anonymity | no | geolocation, ASN ownership, VPN/hosting inference |
| `tor_exits` | anonymity | no | membership of the Tor Project's published exit list |
| `openphish` | phishing | no | membership of the live OpenPhish feed |
| `binlist` | card | no | issuer, country, **prepaid** flag; issuer-country mismatch |

The IP provider sits behind an adapter interface (`ipwho` keyless, `ipinfo`, `ip2location`);
`IP_PROVIDER=auto` prefers whichever is keyed. The two bulk feeds are downloaded once and
refreshed in the background — never inside a request.

> **On honesty about inference.** The keyless ipwho.is tier returns no proxy/VPN flag (its
> `security` object is `null` without a paid key). Where no vendor flag exists, anonymised
> access is inferred from ASN ownership and the signal text *says so* ("belongs to AS…, a
> commercial VPN operator") rather than claiming a vendor verdict. Tor membership comes from
> the Tor Project's own list, so that one is stated as fact. When the exit list already covers
> an address, the IP source stays quiet about its character rather than describing a known Tor
> exit as an ordinary ISP address.

### Score bands

Fixed and documented. `risk_score` is a fraction in `[0, 1]` rounded to 6 decimals.

| `risk_score` | `risk_level` |
|---|---|
| `< 0.25` | `LOW` |
| `0.25 – 0.50` | `MEDIUM` |
| `0.50 – 0.80` | `HIGH` |
| `>= 0.80` | `CRITICAL` |

### Fraud patterns

Named from a closed enum, so the model cannot invent one: account takeover, card testing,
synthetic identity, phishing, money mule activity, first-party fraud, authorised push payment
scam, anonymised access, or *no identifiable pattern*.

## Reliability

- **Hard 11.5 s global timeout**, with sub-budgets for gathering (4.5 s) and reasoning (5 s).
  On expiry the service answers honestly rather than hanging — Telegraph revokes routing from
  miners that time out or return empty.
- **`POST /fraud-risk` always returns HTTP 200** with the four fields — for malformed bodies,
  invalid JSON, an empty body, a crashed pipeline or a timeout.
- **A dead provider drops its signal, it does not fail the request.** Circuit breaker per
  source; a 4xx does not trip it (that is our bug, not an outage).
- **No key required.** With no `ANTHROPIC_API_KEY` the service runs the deterministic
  rule-vote path and a templated summary, and lands in the **same risk band**. A model that
  takes the signals at their source defaults reproduces the score exactly; a model that ranks
  them moves the score *within* its band, bounded to at most one band either way.
  `tests/test_regression.py` asserts both, for every fixture, including when the model rates
  everything strong, everything weak, or dismisses it all.
- **Every reasoning failure is non-fatal.** Timeout, refusal, truncation, malformed JSON, an
  SDK signature mismatch, anything unforeseen — all degrade to the deterministic answer
  instead of failing the assessment, because the score never depended on the model.
- **Cache** keyed on the normalized subject (15 min; 30 s when degraded), with single-flight
  de-duplication so concurrent identical requests share one computation.
- **Structured JSON-lines logging** of every request: subject, every signal with its weight,
  family weights, the rule that fired, pattern, latency, per-source status.

## Run it locally

```bash
pip install -r requirements-dev.txt
cp .env.example .env            # optional: add ANTHROPIC_API_KEY for the reasoning layer
set -a; source .env; set +a

uvicorn fraudrisk.app:app --reload --port 8000
```

```bash
curl -sS localhost:8000/fraud-risk -H 'Content-Type: application/json' \
  -d '{"subject":"New account created 2 days ago, login from a Dutch VPN, first transfer of $4,200 to a new payee"}'
```

```json
{
  "risk_level": "HIGH",
  "risk_score": 0.723543,
  "signals": [
    "This is a first transfer to a payee the account has never paid before - the single most common shape of both takeover cash-out and scam-induced payment.",
    "The account is only 2 days old, leaving no behavioural history to judge this activity against.",
    "The session is described as arriving over a VPN, Tor or proxy, so the true origin and any device/geo history are concealed.",
    "The amount, $4,200, is high-value for a consumer payment."
  ],
  "summary": "A first transfer to a never-before-paid payee from a two-day-old account, arriving over an anonymised connection, is consistent with account takeover."
}
```

The `risk_level`, `risk_score` and `signals` above are real output. The `summary` is
illustrative of the reasoning layer's wording — the model was not reachable from the
development environment, so no live Claude response is included here (see
[Known limitations](#known-limitations)).

With no `ANTHROPIC_API_KEY` this subject returns the **same level, score and signals**, and
the summary becomes the template built from those same signals and the voted pattern:

> "The decisive signals are a first transfer to a never-before-paid payee, a newly created
> account with no history and a session arriving over a VPN, Tor or proxy, alongside 1 further
> risk signal, which taken together are consistent with account takeover."

To be precise about what is guaranteed: the deterministic path and a model that accepts the
signals at their source defaults produce an identical score. A real model also *ranks* each
signal (`weak`/`moderate`/`strong`), which moves the score within its band. The regression
suite pins the limit — driving every signal to `strong`, to `weak`, or dismissing all of them
may shift a case by at most one band and may never invert a verdict or talk an abstention into
a red flag.

## Test it

```bash
python -m pytest            # 439 tests, hermetic (no network), ~10 s
python -m pytest -m live    # 9 more that hit the real providers (binlist's rate
                            # limit makes 2 of them skip rather than fail)
```

| Suite | Covers |
|---|---|
| `test_scoring.py` | every band boundary, score↔level consistency, determinism, order-independence, monotonicity, the corroboration cap and its exemption |
| `test_classify.py` | routing, entity extraction, BIN discipline (money/order numbers are **not** BINs), and every negation guard |
| `test_reasoning.py` | the model's contract: invented ids dropped, invented numbers rejected, summaries that grade the subject replaced, every failure path |
| `test_regression.py` | 25 real subjects with expected bands, run deterministically, with a model, and across model extremes (all-strong / all-weak / all-dismissed) |
| `test_no_fabrication.py` | 20 benign sentences and 14 legitimate hostnames that must yield **no** risk signal — the suite that caught every fabrication bug listed below |
| `test_api.py` | exactly four fields, hostile input, crashes, timeouts, boundary consistency |
| `test_latency.py` | p95 well inside budget, never empty, hanging/failing providers, single-flight, circuit breaker |
| `test_scams.py` | 15 classic scams in wording written *before* the patterns, plus ordinary-business sentences that share their vocabulary and must stay clean |
| `test_miner_yaml.py` | Telegraph's closed YAML schema, LF-only bytes, byte-exact `/miner.yaml`, `signal_mapping` names fields the API really returns |
| `test_live.py` | *(`-m live`, 9 tests)* the real providers still answer in the shape the adapters expect |

Evidence for submission:

```bash
python scripts/demo.py                       # table: grade, score, families, pattern, latency
python scripts/demo.py --json > evidence.jsonl
python scripts/demo.py --url https://your-host
```

## Deploy

Any single public HTTPS host. A `Dockerfile` and a Render blueprint are included.

```bash
docker build -t sentinel-fraud-risk . && docker run -p 8000:8000 --env-file .env sentinel-fraud-risk
```

> **Do not use a free tier that sleeps.** Render's free web service idles after ~15 minutes
> with a 30–60 s cold start, which will fail Telegraph's spot-checks and can cost you routing.
> `render.yaml` therefore specifies `plan: starter`; otherwise keep a pinger on `/healthz`.

Run one worker: the cache and bulk feeds are in-process.

## Register on the Telegraph testnet

`miner.yaml` is the registration config: id `58316447`, slug `sentinel-fraud-risk` (both
verified unused against `/api/miners`). The schema is closed — **any unknown key is rejected
outright** — and `tests/test_miner_yaml.py` checks it before you spend the bond.

1. Deploy the service and put its HTTPS URL in `base_url` (replacing the placeholder;
   `register.sh` refuses to proceed while the placeholder is there).
2. Host `miner.yaml` at a public URL. The service serves it itself at `GET /miner.yaml`
   (byte-exact, straight from disk), so `YAML_URL=https://your-host/miner.yaml` works with no
   extra hosting step. **The bytes must match exactly** — the SHA-256 is recorded on-chain, so
   keep it LF-only with no BOM (`.gitattributes` enforces this, a test asserts the route is
   byte-identical, and `register.sh` re-checks the hosted hash against the local file).
3. Register. **Redeploy after editing `base_url`** - `GET /miner.yaml` serves the copy baked
   into the running image, so an un-redeployed host serves the old bytes and the hash check
   in `register.sh` will (correctly) refuse to proceed.

```bash
export DIAMOND=0x... RPC=https://sepolia... MACHINA=0x... FEE_ADDRESS=0x...
export YAML_URL=https://your-host/miner.yaml
export MINER_PRIVATE_KEY=0x...        # never commit this

DRY_RUN_ONLY=1 ./register.sh          # free simulation, sends nothing
./register.sh                         # approve the bond, then register
```

The script probes your live endpoint first, checks the id/slug are free, confirms
`FRAUD_RISK_GENERAL` is canonical, hashes the YAML, dry-runs `registerMiner`, then asks before
sending anything. Addresses come from
[the protocol docs](https://docs.telegraphprotocol.com/docs/protocol/addresses-and-params) —
verify them there.

Nodes activate a miner ~10–15 minutes after the transaction (they read Sepolia's *safe* block):

```bash
curl -s https://devnode.telegraphprotocol.com/api/miners/$REGISTRATION_ID \
  | jq '.miner | {activation_status, rejection_reason, retrying}'
```

If it comes back `rejected`, fix `miner.yaml`, re-host it, recompute the hash, and call
`updateMiner`.

## Status: what has and has not been verified

Stated plainly, because the difference matters when reading the claims above.

**Verified:** all 439 hermetic tests and 9 live-provider tests pass; the service was run under
`uvicorn` and answered the full fixture set over HTTP with p95 609 ms and max 844 ms against
the 11.5 s budget, returning exactly the four intent fields for every input including
malformed bodies; the live Tor, OpenPhish, ipwho.is and binlist integrations were each
exercised against the real endpoints; `miner.yaml` validates against Telegraph's closed schema
and is served byte-exact; `register.sh` passes `bash -n`.

**Not verified:**

- **The service is not deployed and the miner is not registered.** `base_url` is still the
  placeholder, and `register.sh` refuses to run until it is replaced. No transaction has been
  sent and no MACHINA bond has been spent.
- **The Claude reasoning layer has never executed against the real API.** No credentials were
  available in the build environment (`ANTHROPIC_API_KEY` unset, no `ant` CLI profile), so
  every test of that path uses a fake client. The request shape follows the current SDK
  documentation — `output_config` with `effort` and a `json_schema` format, no `thinking`
  parameter, `max_tokens` well clear of truncation, server-side `fallbacks` for refusals —
  but it has not been confirmed against a live response. **Run `scripts/demo.py` with a key
  set before registering**, and check `reasoning_mode` reads `llm` in the logs rather than
  `llm_failed:*`.
- **The Docker image was not built.** The `Dockerfile` is unexercised; the service was run
  directly with `uvicorn`.
- **The id/slug uniqueness check was confirmed once, early.** `58316447` and
  `sentinel-fraud-risk` were free across all 2173 registered miners; a later re-check could
  not complete because `/api/miners` serves ~3.8 MB and timed out twice. `register.sh`
  re-checks before registering, and the on-chain dry run reverts if either is taken.

## Known limitations

- **The keyless IP tier cannot confirm VPN/proxy status.** Anonymised access is inferred from
  ASN ownership unless you supply `IPINFO_TOKEN` or `IP2LOCATION_KEY`, which add explicit
  vendor flags. The signal text always distinguishes the two.
- **binlist is rate-limited** to roughly 5 requests/minute unauthenticated. The cache and
  circuit breaker keep the service inside that, but a burst of distinct BINs will see the
  card signal drop out (it degrades, it does not fail).
- **OpenPhish's community feed is a snapshot** of a few hundred active URLs, refreshed hourly.
  A phishing URL that is absent from it is scored on domain shape alone — correctly lower.
- **No source covers crypto wallets.** A bare wallet address is answered honestly as
  insufficient information naming that gap, rather than as "checked and clean". Adding a
  chain-analytics source is one entry in `build_sources`.
- **The rule library reads English.** Hints are extracted with English regexes; a scenario in
  another language will fall through to whatever identifiers it contains.
- **Hint extraction is lexical, not semantic.** This is the largest remaining risk, because on
  the keyless path a regex misfire *is* a fabricated signal. Negation is handled within a
  clause, and `test_no_fabrication.py` pins 20 benign sentences and 14 legitimate hostnames
  that must stay clean — every case there is a bug that suite caught: a payroll batch read as
  card-testing velocity, `$45 payment` read as 45 attempts, "customer called 2 days ago" read
  as a two-day-old account, "an existing payee" cancelling "brand new account" *and* then
  asserting the account was established, "German customer in Germany" read as a cross-border
  mismatch, the pronoun "us" read as the United States, "immediately forwarded" read as
  social-engineering pressure, and `wellsfargo.com`, `amazon.de`, `usa.gov` and `google.co.uk`
  read as brand impersonation. Unusual phrasing will still slip through; the design errs
  toward *not* firing, and the reasoning layer can mark a mis-fired signal inapplicable.
- **Geography is a weak signal and is treated as one.** An elevated-risk jurisdiction
  contributes a small amount and can never carry a grade on its own.
- **Thresholds are tuned by hand, not learned.** They are fixed and documented so answers stay
  reproducible; with real labels they should be fitted instead.
- **In-memory cache, single process.** Scaling past one instance needs a shared cache
  (`cache.py` exposes the three methods a Redis implementation would need).
- **The reasoning layer is one call with no self-check.** It can still mis-rank a signal's
  strength within the bounds the schema allows; it cannot add or remove evidence.

## Layout

```
fraudrisk/
  app.py            FastAPI: four fields, always 200
  pipeline.py       stage orchestration and time budgets
  classify.py       subject -> primary kind + entities + hints
  gather.py         parallel, deadlined, breaker-wrapped collection
  reasoning.py      the single Claude call + summary validation
  scoring.py        pure deterministic fusion and band mapping
  patterns.py       the closed fraud-pattern enum
  scams.py          named scam archetypes (safe account, invoice redirection, SIM swap, ...)
  geo.py            country-name -> ISO normalisation (shared)
  models.py         dataclasses and enums
  llm.py            Anthropic client (structured output, no thinking param)
  cache.py          TTL cache with single-flight
  resilience.py     circuit breaker + bounded retry
  logging_setup.py  JSON-lines logs
  sources/          the plugin list (base, feeds, ip_rep, phish, card_bin, rules)
tests/              439 hermetic tests (+9 live) + fixtures/subjects.json
scripts/demo.py     evidence generator
miner.yaml          Telegraph registration config
register.sh         Foundry/cast registration on Sepolia
```
