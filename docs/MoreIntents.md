# More intents: keyless, code-only (`intents/`)

Eighteen intents added on top of the original seven, chosen by observed demand (see the end) and by whether a
keyless public source can answer them honestly. They follow [INTENT_RULES.md](INTENT_RULES.md); the request and
response fields are Telegraph's own, snapshotted in `docs/intent_specs/`. None needs an API key or an LLM, and each
returns HTTP 200 with exactly its response fields for any input. When a value cannot be established it is `null`
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

`?` marks an optional request param (declared under `optional:` in `miner.yaml`).

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
3. **Update the YAML at the registered `yamlUrl`.** For this miner that is the gist
   (`gist.githubusercontent.com/deep17raj/.../raw/miner.yaml`), not Render, so pushing alone changes nothing
   on-chain. Hash the LF bytes: `git show HEAD:miner.yaml | sha256sum` (or the output of
   `python scripts/sync_intents.py` on a clean tree). Do not hash a checked-out copy on Windows: with
   `core.autocrlf` it can become CRLF and the hash will not match what is served.
4. **Call `updateMiner`, not `register.sh`.** `register.sh` calls `registerMiner` and stops at "id 83159274 is taken".
   `updateMiner(oldRegistrationId, yamlUrl, yamlHash, feeAddress, minPriceUsdc, supportedIntents)` takes all 25
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
(Python 3.11, one worker), 10-11 s cold start (the intents' downloads run concurrently). Confirm your Render
instance has at least 512 MB.

## How the intents were chosen (2026-11-22)

Telegraph's node reports `total_requests_served` per miner, not per intent, so demand per intent is an estimate: each
miner's requests split evenly across the intents it supports. By that measure the busiest intents among the ones
built here were sanctions screening (about 3,900), crypto price (1,100), regulatory filings (900), company registry
(650) and URL scan (300). Miner counts per intent (competition) ranged from 17 to 56. The totals are small (25,790
requests across 2,189 miners) and include Telegraph's own scoring probes, so treat them as a ranking, not as traffic.
The busiest intents not built here (`SECURITY_REVIEW`, `WASH_TRADING_DETECTION`, `SMART_CONTRACT_AUDIT`,
`CODE_REVIEW`, `NEWS_SEARCH`, `WEB_SEARCH`) need a model or a paid search API, or have no published response format.
