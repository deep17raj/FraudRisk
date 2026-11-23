# More intents (`intents/`)

Twenty-six intents added on top of the original seven, chosen by observed demand (see the end) and by whether a
public source can answer them honestly. They follow [INTENT_RULES.md](INTENT_RULES.md); the request and response
fields are Telegraph's own, snapshotted in `docs/intent_specs/`. Twenty-one need no API key and no model. Five can
use Groq as an optional extra (see "Groq-assisted intents") and answer without it. Each returns HTTP 200 with
exactly its response fields for any input. When a value cannot be established it is `null`
(number), `""` (string), `[]` (list) or `false` (boolean), never a guess.

| Intent | Endpoint | Request | Response | Sources |
|---|---|---|---|---|
| `SANCTIONS_SCREENING_MATCH_GENERAL` | `POST /sanctions-screening` | `name` | `name`, `matched`, `matches`, `lists`, `match_score` | OFAC SDN + Consolidated lists |
| `CRYPTO_PRICE_USD` | `POST /crypto-price` | `symbol` | `price_usd` | Coinbase, Kraken, Binance, OKX, Bitstamp |
| `REGULATORY_FILING_MONITOR_GENERAL` | `POST /regulatory-filings` | `entity`, `period` | `entity`, `filing_count`, `filings`, `urls` | SEC EDGAR |
| `CORPORATE_REGISTRY_LOOKUP_GENERAL` | `POST /corporate-registry` | `company`, `jurisdiction` | `legal_name`, `registration_id`, `active`, `jurisdiction`, `registered_address`, `incorporated` | GLEIF |
| `URL_SCAN_VERDICT` | `POST /url-scan` | `url` | `safe` | same checks as `URL_SAFE_GENERAL` |
| `WEATHER_FORECAST_GENERAL` | `POST /weather-forecast` | `location`, `window_hours`? | `location`, `when`, `temp_max_c`, `temp_min_c`, `precipitation_probability_pct`, `condition` | Open-Meteo |
| `GAS_PRICE_GENERAL` | `POST /gas-price` | `chain` | `chain`, `gas_price_gwei`, `transfer_fee_usd`, `as_of` | public JSON-RPC nodes, Coinbase |
| `SSL_VERIFICATION_CERT` | `POST /ssl-verification` | `host` | `certificate_valid` | a real TLS handshake |
| `WEATHER_CHECK_TEMPERATURE` | `POST /weather-temperature` | `location` | `temperature_c` | Open-Meteo, met.no |
| `CVE_LOOKUP_CVSS` | `POST /cve-lookup` | `cve_id` | `cve_id`, `cvss_score`, `severity`, `description`, `published`, `references` | NVD, CIRCL |
| `CURRENCY_EXCHANGE_CONVERT` | `POST /currency-convert` | `from_currency`, `to_currency`, `amount`? | `converted_amount` | ECB (Frankfurter), open.er-api.com |
| `THREAT_INTELLIGENCE_INDICATOR` | `POST /threat-intelligence` | `indicator` | `malicious` | same sources as `MALWARE_DETECTION_HASH`, plus IPsum for IPs |
| `TVL_LOOKUP_GENERAL` | `POST /tvl-lookup` | `protocol` | `protocol_or_chain`, `tvl_usd`, `chain_tvl_usd`, `as_of` | DefiLlama |
| `FX_NOW_EUR` / `FX_NOW_CAD` / `FX_NOW_SEK` | `POST /fx-now-eur` / `-cad` / `-sek` | `currency` | `rate` | ECB (Frankfurter), open.er-api.com |
| `VULNERABILITY_TRIAGE_GENERAL` | `POST /vulnerability-triage` | `cve_id` | `cvss_base_score` | NVD, CIRCL |
| `DNS_RECORD_LOOKUP_GENERAL` | `POST /dns-record` | `name`, `record_type` | `has_record` | Google and Cloudflare DNS-over-HTTPS |
| `TOKEN_HOLDER_COUNT_GENERAL` | `POST /token-holders` | `token`, `chain` | `token`, `chain`, `holders_count`, `token_symbol` | Blockscout, Ethplorer, Routescan |
| `SLA_COMPLIANCE_GENERAL` | `POST /sla-compliance` | `service`, `period` | `service`, `uptime_pct`, `sla_target_pct`, `compliant`, `period` | Upptime's published uptime data |
| `RETURN_POLICY_VERIFY_GENERAL` | `POST /return-policy` | `retailer` | `retailer`, `return_window_days`, `conditions`, `verified`, `sources` | the retailer's own policy page (Wikidata finds the site) |
| `WEB_SEARCH_GENERAL` | `POST /web-search` | `query` | `answer`, `sources`, `brief` | Wikipedia, DuckDuckGo, Google News; Tavily/Brave/Serper if keyed; Groq optional |
| `CHAT_COMPLETION_GENERAL` | `POST /chat-completion` | `message` | `reply`, `verdict`, `brief` | exact arithmetic/date helpers, Wikipedia/DuckDuckGo; Groq optional |
| `RESEARCH_QUERY_GENERAL` | `POST /research-query` | `question` | `answer`, `source_count`, `sources`, `brief` | PubMed, OpenAlex, Wikipedia; Groq optional |
| `SECURITY_REVIEW_GENERAL` | `POST /security-review` | `target` | `verdict`, `vulnerability_count`, `issues`, `summary` | OSV, a real TLS/HTTP check, CWE/SWC code rules; Groq optional |
| `EVENT_OUTCOME_RESOLUTION_GENERAL` | `POST /event-outcome` | `event` | `event`, `outcome`, `resolved_yes`, `winner`, `resolved_at`, `sources` | settled Polymarket markets; Groq optional |

`?` marks an optional request param (declared under `optional:` in `miner.yaml`).

## Groq-assisted intents

`WEB_SEARCH_GENERAL`, `CHAT_COMPLETION_GENERAL`, `RESEARCH_QUERY_GENERAL`, `SECURITY_REVIEW_GENERAL` and
`EVENT_OUTCOME_RESOLUTION_GENERAL` can use Groq (`GROQ_API_KEY`, an OpenAI-compatible API) but never depend on it.

- **Order of work.** Each request first gathers what public sources say, builds the complete keyless answer from that,
  and only then offers Groq the time that is left (a safety margin of `LLM_MARGIN_S` is always kept). If Groq answers
  well its answer replaces the keyless one; otherwise the keyless answer is returned. Nothing waits on Groq past the
  budget, so a slow model cannot turn into an empty answer.
- **Failure handling** (`intentkit/groq.py`, never raises): no key means Groq is skipped; a 401 switches it off for 10
  minutes (logged once, the key is never logged); a 429 cools down only that model for the `retry-after` time and tries
  the next model in `GROQ_MODELS` (Groq rate-limits per model); a retired model id is skipped for an hour; a 5xx, a
  timeout, a network error, empty or malformed output simply fall back. The state is process-wide because all five
  intents share one key and one rate limit.
- **Untrusted output.** Sources and URLs always come from documents we retrieved, never from the model; verdict and outcome
  values are mapped to the spec's literals or dropped; a security finding counts only if it names a real CWE/SWC/CVE id
  and quotes code that occurs verbatim in the input. A keyless answer is cached briefly (`CACHE_TTL_DEGRADED_S`) when a
  key is set but Groq failed, so a recovered Groq soon replaces it.
- **What the keyless answers are.** Honest and extractive, not fluent. `WEB_SEARCH` quotes the best sentences of the
  sources and lists recent headlines as headlines; `CHAT_COMPLETION` does exact arithmetic and dates, small talk, and
  reference lookups ("Here is what a reference source says: ...") and answers will-X-happen questions with `Uncertain`;
  `RESEARCH_QUERY` quotes abstracts with years; `EVENT_OUTCOME` answers `yes`/`no` only from a closed Polymarket market whose
  wording and numbers match, else `unresolved`; `SECURITY_REVIEW` is fully deterministic for dependencies, sites and
  pattern rules (a bare contract address, a private repository or an unreachable site gets a null verdict rather than a
  guess).
- **Privacy.** With a key set, the text of a request (and fetched source snippets) is sent to Groq. Telegraph job
  parameters are public, but do not set the key if that is not acceptable.
- **Model ids.** The defaults (`llama-3.3-70b-versatile`, `llama-3.1-8b-instant`) were not verified against a live key
  when written, because none was available. Copy the current ids from your Groq console into `GROQ_MODELS`.
- **Limits worth knowing.** `QUESTION_ANSWERING_GENERAL` is not built: Telegraph still lists it as `registered` with no
  response format. `EVENT_OUTCOME`'s keyless path depends on Polymarket's public API, which may be unreachable at times
  (the call is capped at 3.5 s and then ignored). OpenAlex's free budget is shared per IP, so `RESEARCH_QUERY` may lean
  on PubMed alone. `SECURITY_REVIEW`'s site checks count missing hardening headers as weaknesses (low severity), so an
  ordinary site will usually read "Needs changes".

## Behaviour worth knowing

- **Sanctions.** Names are compared as unordered word sets (accents, case, punctuation and legal forms such as
  "Ltd" ignored), so "Mohammed Abu Teir" equals "ABU TEIR, Mohammed". Score 1.0 is an identical word set; 0.9 is a
  multi-word query contained in a longer listed name; otherwise string similarity. `matched` needs a score of at least
  `MATCH_THRESHOLD` (0.88). Aliases are searched too. Lists are downloaded at startup (a few seconds) and refreshed
  every 6 h. **If no list could be loaded the answer is `matched=false` with `lists=[]` and `match_score=null`: it
  says nothing was checked, and must never be read as "clear".** Only OFAC lists are covered, so a name sanctioned only
  by the EU, UN or UK is not found.
- **Crypto price.** Median of the exchanges that answer; a reading more than 3 % from the median is dropped. One
  exchange is enough; none gives `null`.
- **SEC filings.** If `entity` is an SEC registrant (name or ticker in SEC's company list) the answer is its own
  filings in the period; otherwise it is EDGAR full-text search for filings mentioning the phrase. `filing_count` is
  complete, the title and URL lists stop at 25. Set `SEC_USER_AGENT` to identify your service to the SEC.
- **Company registry.** GLEIF holds entities that have an LEI, not every company in every national register: an
  unknown name returns the empty answer. A 20-character LEI in `company` is looked up directly. "Siemens AG" is
  found as "Siemens Aktiengesellschaft"; "BMW" is not found as "Bayerische Motoren Werke Aktiengesellschaft".
- **SSL.** Verification uses certifi's CA bundle. Only public addresses are contacted (loopback, private and
  link-local hosts are refused and answered `false`), so the endpoint cannot be used to probe a private network.
  A host that does not resolve or presents no TLS is `false`.
- **CVE.** NVD's primary CVSS v3.x score, falling back to CIRCL's copy of the CNA score. A CVE with no score yet
  has `cvss_score` and `severity` `null` (the spec's enum has no "unknown"). NVD rate-limits keyless clients, so
  answers are cached for 6 h; `NVD_API_KEY` lifts the limit.
- **Threat intelligence.** An IP is reported when at least `IPSUM_MIN_LISTS` (2) of IPsum's aggregated public
  blocklists carry it. A CVE or actor name has nothing to look up and reads as `false`.
- **FX.** ECB reference rates are preferred, open.er-api.com fills other currencies. Both update about daily, so a
  "live" mid-market rate can differ by a fraction of a percent.
- **Gas.** `transfer_fee_usd` is `gas_price x 21,000 gas x the native token's Coinbase price`. Supported chains:
  ethereum, base, arbitrum, optimism, polygon, bsc, avalanche.
- **Token holders.** One primary source per chain answers and the others are fallbacks, never averaged, because they
  count differently (USDT on Ethereum: Blockscout 17.2M, Ethplorer 15.7M, Routescan 15.7M "approx"). Order:
  Blockscout, Ethplorer (Ethereum only), Routescan (Ethereum and Avalanche, last because it labels its count approximate).
  An address is always used as given; a symbol or name is resolved to the exact-symbol token that is reputable and has the
  largest market cap (so copycat tickers are not picked), which takes about 5 s on Ethereum. **Base, Polygon, Arbitrum and
  Gnosis rely on Blockscout instances that returned a Cloudflare bot challenge from the development machine** (never worked
  around), so they answer `null` wherever that happens. Check the Base example from the intent's own description
  (`0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` on base) after deploying, and treat Base as unsupported if it is blocked there too.
- **SLA compliance.** Returns Upptime's published availability figure verbatim for the windows Upptime publishes (24 hours, 7
  days, 30 days, 365 days, all time) for services a configured Upptime repository monitors (`UPPTIME_REPOS`, default
  `upptime/upptime`: Google, Wikipedia, Hacker News). Any other window or service is `null`: recomputing other windows from
  the daily downtime minutes did not reproduce Upptime's own day and month figures, and an incident-derived estimate from a
  status page would be an invented number. `sla_target_pct` is only a percentage written in the request ("99.9%");
  `compliant` is true only when both numbers are known and the uptime meets the target, so `false` also means "cannot confirm".
- **Return policy.** Finds the retailer's site (a domain or URL as given, else Wikidata's official-website property), then
  reads its policy page: a small map of known policy URLs (a locator, not an answer), links on the homepage mentioning
  returns, and a few common paths. Only statements about returning or refunding within a period count; shipping, processing
  and bank times are ignored. `verified` is true only when a window was read from the retailer's own domain. Pages that
  block bots (Framework, behind Cloudflare), render without JavaScript text, or state no period give a null window.
  Checked on 2026-11-23: Adafruit, System76 and Arduino's store parse (30 days each); SparkFun, Purism and OLIMEX do not.
  Pages are cached for six hours, so a shop is asked for a handful of pages a few times a day.
- **Not built:** `STOCK_PRICE_US_EQUITY` (no keyless quote source was found that works: Yahoo's quote API
  answered 401 and Stooq's CSV URL answered 404 when tried on 2026-11-22; a source that works would unblock it) and
  every intent whose `response` is still `null` on Telegraph.

## Configuration

All keyless; see [.env.example](.env.example). Per-intent overrides use the package name in upper case
(`SANCTIONS_`, `CRYPTO_PRICE_`, `SEC_FILINGS_`, `CORPORATE_REGISTRY_`, `SSL_CERT_`, `CVE_`, `VULNTRIAGE_`,
`WEATHER_TEMPERATURE_`, `WEATHER_FORECAST_`, `GAS_PRICE_`, `URLSCAN_`, `THREATINTEL_`, `CURRENCY_CONVERT_`,
`FX_CAD_`, `FX_EUR_`, `FX_SEK_`, `DNS_LOOKUP_`, `TVL_`), for example `SANCTIONS_GLOBAL_TIMEOUT_S`. Leave the shared
`CACHE_TTL_S` / `GLOBAL_TIMEOUT_S` unset: each intent has its own tuned default.

## Deploying (you do this, not the code)

1. `python scripts/sync_intents.py --check` and `python -m pytest` must be green.
2. Push to `main` (Render deploys). Confirm `GET /healthz` lists every intent as `ok`. Set `SEC_USER_AGENT` on
   Render to something with a contact address ("YourName you@example.com"): SEC may reject an anonymous client
   from a cloud IP.
3. **Put the YAML in the gist and register a revision-pinned URL.** Edit the gist (`gist.github.com/deep17raj/<id>`),
   then note its new revision id (`https://api.github.com/gists/<id>` -> `history[0].version`) and register
   `https://gist.githubusercontent.com/deep17raj/<id>/raw/<revision>/miner.yaml`, NOT `.../raw/miner.yaml`. A revision URL
   always returns the same bytes (checked on 2026-11-22: the three revisions of this gist return three different files),
   so a later gist edit can never break the on-chain hash. This is exactly what went wrong before: the hash registered
   on-chain (`da448ddc...`) is the hash of gist revision `e4d0454f9b` (the original 7-intent file), the gist was then edited
   in place, and the mutable `/raw/miner.yaml` URL started returning different bytes. Hash the LF bytes before sending
   (`git show HEAD:miner.yaml | sha256sum`) and confirm `curl -sL <the pinned URL> | sha256sum` is identical. Do not hash a
   checked-out copy on Windows: with `core.autocrlf` it can become CRLF.
4. **Call `updateMiner`, not `register.sh`.** `register.sh` calls `registerMiner` and stops at "id 83159274 is taken".
   `updateMiner(oldRegistrationId, yamlUrl, yamlHash, feeAddress, minPriceUsdc, supportedIntents)` takes all 33
   intents, ends the old registration and creates a new `registrationId`; the `id` and `slug` in the YAML do not
   change. Re-check the Diamond address and bond currency first (changed 2026-11-19, bonds in USDC). See also
   [NetworkIntents.md](NetworkIntents.md#adding-these-to-the-already-registered-miner).
   *What happened last time (observation, not a guarantee):* the previous `updateMiner` (registered
   2026-11-22 13:08Z, adding four intents) kept the FRAUD_RISK rank (#2, score 0.9996 in epoch 38) but restarted
   `total_requests_served` from zero. Expect the request count to reset again.
5. **After it is active, check values, not just field names.** `register.sh` only confirms fields exist. Run the
   question keys from `docs/intent_specs/*.json` against `https://fraudrisk.onrender.com` and look for `null`s: Render's
   US egress can differ from a laptop (exchange region blocks, NVD 403s, SEC user-agent checks).

Measured on a laptop with every intent warmed and the 10 MB DefiLlama list parsed: 209 MB peak, 167 MB steady
(Python 3.11, one worker), 10-20 s cold start (the intents' downloads run concurrently). Confirm your Render
instance has at least 512 MB.

## How the intents were chosen (2026-11-22)

Telegraph's node reports `total_requests_served` per miner, not per intent, so demand per intent is an estimate: each
miner's requests split evenly across the intents it supports. By that measure the busiest intents among the ones
built here were sanctions screening (about 3,900), crypto price (1,100), regulatory filings (900), company registry
(650) and URL scan (300). Miner counts per intent (competition) ranged from 17 to 56. The totals are small (25,790
requests across 2,189 miners) and include Telegraph's own scoring probes, so treat them as a ranking, not as traffic.
The busiest intents not built here (`SECURITY_REVIEW`, `WASH_TRADING_DETECTION`, `SMART_CONTRACT_AUDIT`,
`CODE_REVIEW`, `NEWS_SEARCH`, `WEB_SEARCH`) need a model or a paid search API, or have no published response format.
