# Intent rules (read before adding or changing any intent)

These rules are enforced by `tests/rules/` and by `tests/gateway/`. If you add an intent and
`python -m pytest` is green, you have followed the mechanical ones; the rest are on you.
**Adding an intent = one registry entry + one package + tests + `python scripts/sync_intents.py`.**

## A. Identity is frozen

1. `id: 83159274`, `slug: telegraph-intent-miner`, `name: TelegraphIntent` and
   `base_url: https://fraudrisk.onrender.com` never change. Not for a new intent, not for a rename,
   not "temporarily". A changed id or slug is a new miner on the leaderboard (the old
   `sentinel-fraud-risk` miner vanished that way).
2. The seven original intents (`FRAUD_RISK_GENERAL`, `FACT_CHECK_GENERAL`,
   `CONTRACT_OBLIGATION_AUDIT_GENERAL`, `IP_GEOLOCATION_GENERAL`, `EMAIL_SECURITY_GENERAL`,
   `URL_SAFE_GENERAL`, `MALWARE_DETECTION_HASH`) keep their paths, packages and response fields.
   Adding intents must not change their behaviour.

## B. Build only to Telegraph's published spec

3. Intent names are canonical: exact spelling, case-sensitive, from
   `GET https://devnode.telegraphprotocol.com/engine/v1/intents`. Never invent or "improve" a name.
4. Build an intent only when `lifecycle.state` is `active` **and** `request` and `response` are
   non-null in `GET /engine/v1/intents/<ID>`. A `registered` intent has no response format, so every
   field name would be a guess, and a wrong field name fails the intent.
5. Commit the spec as `docs/intent_specs/<INTENT>.json` (`python scripts/snapshot_spec.py <INTENT>`).
   Request param names and `required` flags, and response field names, in the registry, in the YAML
   and in the pydantic model must equal the snapshot. Re-snapshot when Telegraph changes a spec.

## C. The response contract

6. `POST <path>` answers **HTTP 200 with exactly the response fields**, always: malformed body,
   missing field, timeout, internal error, every upstream down. Telegraph revokes miners that error
   or go silent. Use `intentkit.service.create_service_app`; do not hand-roll an app.
7. Response models use `ConfigDict(extra="forbid")`. Field types follow the spec: `bool` is a JSON
   boolean, `number` a JSON number (never a string), `enum` one of `allowed` exactly, `list` a list.
   Honour `range` and `decimals` where that does not turn a correct value into `null` (a tiny
   currency conversion below the range's floor is still the true answer).
8. Return the spec's response fields only, with the spec's spelling, not the `onchain` slot names
   (`price_usd`, not `price`).

## D. Honesty and fair play

9. Never fabricate. When a value cannot be established return the honest empty value for its type:
   `null` for a number, `""` for a string, `[]` for a list, `false` for a boolean. Never `0` for a
   number whose range excludes it, and never a plausible-looking guess. An enum that has an explicit
   "unknown" member (`unresolved`) uses it; an enum without one is `null`. A free-text answer field
   (`answer`, `reply`, `summary`) is never empty: when nothing could be established it says so in one honest
   sentence ("No sources were found for ..."), because an empty answer reads as a failed spot check.
10. Never hard-code answers for an intent's `question_keys` (the questions Telegraph asks to
    score it). Every answer comes from a real lookup, because the same code must answer any input.
11. Keyless by default; a language model is an optional extra, never a dependency. The only model allowed is
    Groq, only through `intentkit.groq` (no direct calls to it, no Anthropic, no OpenAI), and only when
    `GROQ_API_KEY` is set. An intent that uses it is marked `llm=True` in the registry and MUST give a valid, honest
    answer with no key and on every Groq failure: no key, 401, 429, a retired model, 5xx, timeout, malformed or
    ungrounded output. So: gather what public sources say first (verify each live, note its terms in the docs), build
    the keyless answer from that, and offer Groq only the time that is left. A source being down degrades the answer,
    it never breaks it. Model output is untrusted: `sources` are URLs of documents that were actually retrieved
    (never ones the model wrote), enums are mapped to the spec's values, and a claim must be grounded (a security
    finding counts only if it quotes code that occurs in the input).
12. An answer that rests on a single source, or on nothing, must say so where the response has a
    free-text field (`summary`, `reasoning`), and be cached for a short time only.

## E. Structure

13. New intents live in `intents/<package>/` with `app.py` (`create_app(settings=None, pipeline=None)`),
    `config.py` (`Settings`, a subclass of `intentkit.settings.BaseSettings`) and their logic. Shared
    helpers (cache, DNS-over-HTTPS, feeds, circuit breaker, request reading, service factory) live
    in `intentkit/`. Do not copy them into a package.
14. A pipeline exposes `cache` (a `TTLCache`), `async answer(req) -> Model` and
    `fallback(req, reason) -> Model`, and optionally `async start()`. `fallback` must always return a
    valid model: a test builds it for every intent.
15. Read request fields with `intentkit.request.read_fields`: the documented name first, then
    aliases. A caller that sends `query`/`text`/... instead must still be answered.
16. Every external call has a timeout below the global timeout, goes through the circuit breaker,
    and is cached with `TTLCache.single_flight`.
17. Per-intent environment overrides use the prefix `<PACKAGE_NAME_UPPERCASE>_` and are applied by
    `scoped_env`. Document each variable in `.env.example`.

## F. Registration files are generated, not hand-edited

18. `telegraphintent/registry.py` is the single source of truth for the intent list. `miner.yaml`,
    the intent block in `register.sh` and the `COPY` block in the `Dockerfile` are generated by
    `python scripts/sync_intents.py` (check without writing: `--check`). Never edit those blocks.
19. `miner.yaml` follows Telegraph's closed schema (the keys in `tests/gateway/test_miner_yaml.py`):
    LF line endings, UTF-8 without a BOM, one endpoint per intent, `method: POST`,
    `intents: ["*"]` params, required params under `required:` and optional ones under `optional:`.
    The on-chain SHA-256 is over these exact bytes.

## G. Tests (all required)

20. Per intent, under `tests/<package>/`: an API contract test with a stub pipeline (exact fields,
    types, 200 for bad input), a pipeline test against `httpx.MockTransport` (each source, each
    failure mode), and a `@pytest.mark.live` test of the real sources. The registry-driven gateway
    and rules tests cover routing, YAML, spec parity and the fallback shape automatically.
21. `python -m pytest` must pass in full before you stop. Run the live tests of what you added
    (`python -m pytest -m live tests/<package>`) and call each new endpoint with its `question_keys`.

## H. Things only the user does

22. Never `git commit`, `git push`, deploy, or call `updateMiner`/`registerMiner` unless the user
    asks. `render.yaml` has `autoDeploy: true`, so a push deploys, and if the registered `yamlUrl`
    is the Render `/miner.yaml`, changed bytes break the on-chain hash.
23. Adding intents to a registered miner needs `updateMiner`, which ends the old registration and
    creates a new `registrationId`. Tell the user this; hand them the steps (host the new YAML,
    recompute its SHA-256, check the Diamond address and USDC bond) instead of running them.

## Adding an intent: the checklist

1. `GET /engine/v1/intents/<ID>`: confirm `active`, non-null `request` and `response`.
   `python scripts/snapshot_spec.py <ID>`.
2. Find a keyless source and call it live. If none works, stop: do not build the intent.
3. Create `intents/<package>/` (`__init__.py`, `config.py`, `app.py`, logic).
4. Add the `IntentSpec` to `telegraphintent/registry.py` (copy request/response names from the
   snapshot; write the endpoint description for Telegraph's node, which routes by it).
5. `python scripts/sync_intents.py`.
6. Tests (rule 20), then the full suite, then the live run.
7. Add the intent to the docs table in `docs/MoreIntents.md` and any new variables to `.env.example`.
8. Report to the user: what was added, what could not be verified, and the deploy/`updateMiner` steps.
