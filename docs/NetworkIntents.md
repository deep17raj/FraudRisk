# Network-check intents: `ipgeo`, `emailsec`, `urlsafe`, `malware`

Four intents that answer from live public data with no API key and no LLM. Their request and response
fields are Telegraph's own, read from `GET https://devnode.telegraphprotocol.com/engine/v1/intents/<INTENT>`
on 2026-11-22. Each endpoint returns exactly those response fields, as HTTP 200, for every input.

| Intent | Endpoint | Request | Response |
|---|---|---|---|
| `IP_GEOLOCATION_GENERAL` | `POST /ip-geolocation` | `ip` | `ip`, `country`, `city`, `latitude`, `longitude`, `isp` |
| `EMAIL_SECURITY_GENERAL` | `POST /email-security` | `domain`, `record_type` | `lookup_ok` |
| `URL_SAFE_GENERAL` | `POST /url-safe` | `url` | `url`, `verdict`, `detections`, `summary` |
| `MALWARE_DETECTION_HASH` | `POST /malware-detection` | `indicator` | `malicious` |

The shared helpers (TTL cache, JSON logging, circuit breaker, DNS-over-HTTPS client, feed loader, lenient
request reading) live in `intentkit/`. The three older packages do not import it.

## `ipgeo`: IP_GEOLOCATION_GENERAL

Asks five keyless providers at once (ipinfo.io, ipwho.is, ipapi.co, freeipapi.com, geojs.io), collects every
answer that arrives within the soft deadline (1.2 s, stopping early once all have answered), and answers with the
**consensus**: fixes within 100 km form a cluster, the largest cluster wins, and `latitude`/`longitude` are its
per-axis median. Free databases disagree by hundreds of km on anycast addresses (8.8.8.8 is placed in San Jose,
Mountain View and Kansas by different providers), so a lone outlier must not decide the scored field. An earlier
version stopped at the first two agreeing providers; that made the latitude depend on who answered first (37.67
and 37.38 in two live runs for the same IP), so it now waits for the rest.

- Typical latency 300-800 ms, repeated lookups of the same IP give the same answer. Results are cached 6 h.
- A private, loopback or reserved address, or a request with no IP in it, makes no network call and returns
  empty strings with `latitude`/`longitude` of `null`. If every provider is unreachable the same shape is
  returned. `null` rather than `0` because (0, 0) is a real place that would be scored as a wrong answer.
- `country` is a full name when any provider gives one (ipinfo gives only the ISO code).
- Optional: `IPINFO_TOKEN` lifts ipinfo.io's keyless rate limit; ipapi.co is the most likely to rate-limit.

## `emailsec`: EMAIL_SECURITY_GENERAL

`lookup_ok` is true exactly when a public DNS resolver answers the query with status 0 (NOERROR), which is
Telegraph's own definition. A name that exists but has no record of that type (NODATA) is NOERROR, so true.
NXDOMAIN, SERVFAIL, REFUSED and a malformed name or type are false. Google's resolver is preferred, Cloudflare's
is the fallback; both are asked at once and a disagreement is settled in Google's favour.

The intent's description mentions SPF, DKIM and DMARC analysis, but the published response format has only
`lookup_ok`, so nothing else is returned. If both resolvers are unreachable the answer is `false` (the field
is boolean and cannot say "unknown"); that case is logged and cached for only 10 s.

Whether the node reads a literal JSON boolean here is the one thing the docs do not state (`true_when` lists
only `"0"` and `"NOERROR"`). The registry's scoring data answers it: on this intent `zk-email-dns` returns a
boolean `lookup_ok` plus `Status` and scored 0.9906, `guardwire-email-security` returns posture fields and
scored 0.9994, and raw resolver wrappers that return fixed-domain record lists scored 0 or 0.5. So the node
reads answers flexibly and rewards answering the domain and record type actually asked, which this does. Those
miners also return extra fields, so "exactly the declared fields" is a conservative choice, not a requirement.

## `urlsafe`: URL_SAFE_GENERAL

Two keyless sources, each counted in `detections` when it flags the address:

- **OpenPhish** live feed. An exact URL match is `Malicious` on its own. A host matches only if the feed lists
  the site itself (an entry with no path), so one phishing page on docs.google.com does not flag every Google
  Doc.
- **Cloudflare's security DNS** (`security.cloudflare-dns.com`), which answers `0.0.0.0` for a name on its
  malware/phishing list.

Verdicts: `Malicious` (exact URL listed, or two sources) / `Suspicious` (one source, or enough structural red
flags: brand name outside its own domain, punycode, `user@host`, abused TLD plus lure wording...) / `Safe`
(at least one source consulted, none lists it) / `Unknown` (nothing could be consulted). Structural flags raise
a verdict but are never counted as detections. The summary names the sources that flagged it, or that were
checked. Only two sources are consulted, so `Safe` is not a guarantee and the summary says so.

URLhaus is not used: its keyless downloads are gone (HTTP 410) and its API now requires an `Auth-Key`.

## `malware`: MALWARE_DETECTION_HASH

`malicious` for a file hash (MD5, SHA-1, SHA-256), and also for a domain or IP, since the request parameter
`indicator` is defined as "domain, IP address or file hash".

- **Hashes:** all queried at once. CIRCL hashlookup (any hash type; returns an explicit `KnownMalicious` list
  name, and a plain record for known files such as NSRL entries); Team Cymru's Malware Hash Registry over DNS
  (MD5 and SHA-1 only: a SHA-256 label is 64 characters, over the DNS 63-byte label limit); AlienVault OTX's
  indicator API (any hash). Malicious if CIRCL names a malicious list, or Cymru reports at least 50% of antivirus
  engines detecting it, or (weakly) OTX cites it in at least one threat report and nothing marks it clean.
- **Domains:** the OpenPhish feed, Cloudflare security DNS, and OTX (at least 5 reports).
- **IPs:** OTX (at least 5 reports).

Things found by running against the real services, now covered by tests:

1. Cymru lists the MD5 of an **empty file** at 91% detection. The digests of a zero-byte file are therefore
   never malicious, whatever a source says. This is the only per-value rule, it holds for every empty file, and
   it covers one of the two hashes Telegraph's evaluation currently asks about. The live lookups give the same
   answer for it (CIRCL knows it as an NSRL file, OTX marks it `empty_file`).
2. OTX returns `whitelist` ("Whitelisted hash: MISP") for the **EICAR test file**, so `whitelist` means "a known
   file", not "a clean file". For hashes only OTX `empty_file` and `false_positive`, or a CIRCL known file with
   no malicious list, count as clean evidence, and only against OTX's weak reports; strong evidence (a CIRCL
   malicious list, a Cymru detection, a feed listing, a DNS block) is never overridden.
3. OTX alone was a single point of failure for a SHA-256 (2-5 s, occasionally no answer). CIRCL answers in about
   1 s and flags the EICAR SHA-256 by itself; a live test proves each source works with the other disabled.

Known limits: if every source is unreachable the answer is `false` (the field is boolean), logged as degraded
and cached for 20 s. MalwareBazaar and VirusTotal would add coverage for very new samples but need an API key.

## Configuration

All keyless. Per-intent overrides use the prefixes `IPGEO_`, `EMAILSEC_`, `URLSAFE_`, `MALWARE_` (see
`.env.example`). Common knobs: `GLOBAL_TIMEOUT_S`, `PROVIDER_TIMEOUT_S`, `CACHE_TTL_S`. Intent-specific:
`GEO_PROVIDERS`, `GEO_CLUSTER_KM`, `SUSPICIOUS_WEIGHT`, `ENABLE_PHISH_FEED`, `ENABLE_DNS_BLOCKLIST`,
`CYMRU_MIN_DETECTION`, `OTX_MIN_PULSES_HASH`, `OTX_MIN_PULSES_HOST`, `ENABLE_CIRCL`, `ENABLE_OTX`, `ENABLE_CYMRU`.

## Adding these to the already registered miner

This repo supports **one path**: a single `miner.yaml` listing all seven intents, applied to the existing
registration with `updateMiner`. (A second, separate miner for just the four new intents is possible but is not
scripted here: it would need its own four-endpoint YAML with a new `id` and `slug`, and `register.sh` hard-codes
the seven-intent list and `miner.yaml`.)

1. Deploy the new code first. The new endpoints are additive and the old three are unchanged. `render.yaml`
   has `autoDeploy: true`, so a push to `main` deploys, and `GET /miner.yaml` on the service then serves the
   seven-intent file. That is harmless while the registered `yamlUrl` is the gist (as in the hand-off brief);
   if it is the Render URL, the on-chain hash stops matching, so deploy together with `updateMiner`.
2. Host the new `miner.yaml` at the registered URL and recompute its SHA-256 over the raw bytes (LF line endings,
   no BOM; `tests/gateway/test_miner_yaml.py` checks both).
3. Call `updateMiner(oldRegistrationId, yamlUrl, yamlHash, feeAddress, minPriceUsdc, supportedIntents)` with all
   seven intents. **It ends the old registration and creates a new `registrationId`.** Telegraph's docs do not
   say whether rank, score or the 7-day grace period carry over, and `FRAUD_RISK_GENERAL` is currently ranked #2,
   so decide whether that risk is acceptable before sending it.
4. Re-check `DIAMOND` and the bond currency against
   https://docs.telegraphprotocol.com/docs/protocol/addresses-and-params: the testnet Diamond was replaced on
   2026-11-19 and bonds are now paid in USDC, while `register.sh` still approves a MACHINA bond.

After the first scoring epoch, read the new intents' scores from `GET /api/miners` (`scores` per miner) or
`GET /leaderboard/miners`.

`urlsafe` can report at most 2 detections (two sources), so if Telegraph's reference is a multi-engine count,
clean URLs will match and listed ones probably will not.

## Not built

`ENTITY_EXTRACTION`, `KEYWORD_EXTRACTION`, `SEMANTIC_SIMILARITY`, `API_HEALTH_CHECK`, `GRAMMAR_SPELL_CHECK` and
`SERVER_UPTIME_MONITOR` (all `_GENERAL`) have no response format on Telegraph yet (lifecycle state `registered`,
`response: null`), so any field names would be guesses. Re-check `GET /engine/v1/intents/<ID>`; build one when its
`response` is non-null.
