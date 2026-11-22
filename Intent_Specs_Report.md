# Canonical specs for the 10 candidate intents

Collected 2026-11-22 from Telegraph's live node (`https://devnode.telegraphprotocol.com`) and the
Anjoman docs. Leaderboard data is epoch 37. Everything marked **NOT FOUND** was searched for and
is not published anywhere I could reach. Nothing in this file is guessed.

Sources used:
- `GET /engine/v1/intents` and `GET /engine/v1/intents/{INTENT}` (request params, response fields, lifecycle, on-chain layout, evaluation spec)
- `GET /leaderboard/miners` (which intents were actually scored last epoch)
- `anjoman.ai/docs/using/intents`, `/docs/miners/*`, `/docs/protocol/addresses-and-params`, `/docs/using/intent-schema`

## 0. Read this first (four findings that change the plan)

1. **Only 4 of the 10 intents have a published request/response format.** IP_GEOLOCATION, URL_SAFE,
   EMAIL_SECURITY and MALWARE_DETECTION_HASH are `active` with a schema. The other six
   (ENTITY_EXTRACTION, KEYWORD_EXTRACTION, SEMANTIC_SIMILARITY, API_HEALTH_CHECK,
   GRAMMAR_SPELL_CHECK, SERVER_UPTIME_MONITOR) are lifecycle state `registered`, with
   `request: null`, `response: null`, and `onchain.ready: false` ("the intent has no response
   format"). They have been in that state since 2026-11-17. **There is no canonical field list for
   them, so none can be built to spec today.**
2. **EMAIL_SECURITY_GENERAL's real spec is much narrower than its description.** The description talks
   about SPF/DKIM/DMARC and spoofing, but the request is `domain` + `record_type` and the only
   response field is `lookup_ok` (a boolean: did the DNS lookup succeed). Build to the response
   format, not the description.
3. **Adding intents via `updateMiner` creates a new registration** (new `registrationId`). The docs do
   not say whether rank, score or grace-period status carry over. Our FRAUD_RISK rank (currently #2,
   score 1.0 in epoch 37) could be put at risk. See section 3.
4. **The testnet Diamond changed on 2026-11-19.** Current: `0x72a70e949362670Bf8bceAd24eFC5b8f950aa08b`.
   The registration page (dated Nov 1) still shows `0x3624EB01...` and a MACHINA bond. See section 3.

## 1. Summary table

| Intent | State | Class | Miners | Scored in epoch 37? | Spec published? | Buildable to spec now? |
|---|---|---|---|---|---|---|
| `IP_GEOLOCATION_GENERAL` | active (since 2026-11-22 02:22Z) | judgment (generic) | 14 | no (just activated) | yes | **yes** |
| `URL_SAFE_GENERAL` | active (since 2026-11-17) | judgment (generic) | 18 | no | yes | **yes** |
| `EMAIL_SECURITY_GENERAL` | active (since 2026-11-21) | judgment (generic) | 20 | no | yes (narrow) | **yes** |
| `MALWARE_DETECTION_HASH` | active (since 2026-11-21) | **hybrid** (comparator + generic) | 14 | **yes** (14 entries) | yes | **yes** (needs a data source) |
| `KEYWORD_EXTRACTION_GENERAL` | registered, no format | judgment (generic) | 15 | **yes** (15 entries) | **NOT FOUND** | no |
| `ENTITY_EXTRACTION_GENERAL` | registered, no format | judgment (generic) | 14 | no | **NOT FOUND** | no |
| `SEMANTIC_SIMILARITY_GENERAL` | registered, no format | judgment (generic) | 17 | no | **NOT FOUND** | no |
| `API_HEALTH_CHECK_GENERAL` | registered, no format | judgment (generic) | 18 | no | **NOT FOUND** | no |
| `GRAMMAR_SPELL_CHECK_GENERAL` | registered, no format | judgment (generic) | 18 | no | **NOT FOUND** | no |
| `SERVER_UPTIME_MONITOR_GENERAL` | registered, no format | judgment (generic) | 19 | no | **NOT FOUND** | no |

For reference, our current intents in epoch 37: `FRAUD_RISK_GENERAL` is scored and we are **#2** (score 1.0;
#1 is `txlens` at 1.0, #3 is `a1-preflight` at 0.9999). `FACT_CHECK_GENERAL` and
`CONTRACT_OBLIGATION_AUDIT_GENERAL` became active on 2026-11-21 12:45Z and are not on the epoch-37
leaderboard yet.

## 2. Per-intent specs

All intents here are served at the intent name shown (exact spelling, case-sensitive). "Extra fields
allowed?" is **NOT FOUND** for every intent: the docs never state whether undeclared response fields
are ignored or rejected. Return exactly the declared fields (our existing rule) until that is known.

### 2.1 `IP_GEOLOCATION_GENERAL`

Description (canonical): "Query supplies an IP address and asks where it is located geographically. For
abuse history, use THREAT_INTELLIGENCE_INDICATOR." Example query: "Where is the IP address 8.8.8.8
located?"

Request (`schema_version` 3, `served_by` IP_GEOLOCATION)

| Field | Type | Required | Description | On-chain slot |
|---|---|---|---|---|
| `ip` | string | yes | IP address | strings[0], max 256 bytes |

Response (`format_version` 3)

| Field | Type | Notes | Where it lives |
|---|---|---|---|
| `ip` | string | The IP address located | off-chain |
| `country` | string | Country the address resolves to. Aliases: `geolocation`, `ip location`, `ip located` | on-chain strings[0], max 256 bytes |
| `city` | string | City the address resolves to | off-chain |
| `latitude` | number | degrees, range -90..90, 6 decimals, **`scored: true`** | on-chain integers[0], 6 decimals |
| `longitude` | number | degrees, range -180..180, 6 decimals | on-chain integers[1], 6 decimals |
| `isp` | string | Network operator or ASN owning the address | off-chain |

Scoring: evaluation is `judgment` via `generic`, no wasm, no bootstrap evaluator. Only `latitude` carries the
`scored` flag. What `scored` means for a judgment-class intent, the tolerance, and the truth sources: **NOT
FOUND**. Latency limit: **NOT FOUND**. Revocation: protocol-wide only (consecutive failed spot checks = no
response or an empty answer; about one spot check per intent per hour).
Data-source rules: **NOT FOUND**.

### 2.2 `URL_SAFE_GENERAL`

Description (canonical): "Query supplies a URL or domain and asks whether it is listed as malicious across live threat
feeds, DNS blocklists, phishing databases or domain reputation services, with the sources that flagged it."
Routing notes in the description: a single "is this link safe" verdict is `URL_SCAN_VERDICT`; TLS certificate
detail is `SSL_VERIFICATION_CERT`. Example query: "Check https://login-secure-update.example.com against threat
feeds and DNS blocklists: is it listed for phishing, and by which engines?"

Request (`schema_version` 4, `served_by` URL_SAFE)

| Field | Type | Required | Description | On-chain slot |
|---|---|---|---|---|
| `url` | string | yes | The URL | strings[0], max 256 bytes |

Response (`format_version` 4)

| Field | Type | Notes | Where it lives |
|---|---|---|---|
| `url` | string | The URL checked | off-chain |
| `verdict` | enum | allowed values exactly: `Safe`, `Suspicious`, `Malicious`, `Unknown` (`processed: true`) | on-chain strings[0], max 256 bytes |
| `detections` | number | engines or databases flagging the URL; unit `detections`, range 0..1000, 0 decimals, **`scored: true`** | on-chain integers[0] |
| `summary` | string | One- or two-sentence summary of the check | off-chain |

Scoring: `judgment` via `generic`. Only `detections` carries `scored`. Meaning, tolerance, truth sources, latency:
**NOT FOUND**. Data-source rules: **NOT FOUND**.

### 2.3 `EMAIL_SECURITY_GENERAL`

Description (canonical): "Query supplies a domain or an email message and asks about mail authentication and spoofing: SPF,
DKIM and DMARC records, header anomalies, malicious attachments." Routing notes: a URL on a blocklist is
`URL_SAFE_GENERAL`; a file verdict is `MALWARE_DETECTION_HASH`. Example query: "Does example.com publish valid
SPF, DKIM and DMARC records, and do these message headers indicate spoofing?"

**The published request/response format does not match that description.** It is a DNS-lookup check only.

Request (`schema_version` 3, `served_by` EMAIL_SECURITY)

| Field | Type | Required | Description | On-chain slot |
|---|---|---|---|---|
| `domain` | string | yes | Email domain | strings[0], max 256 bytes |
| `record_type` | string | yes | DNS record type to look up, e.g. MX or TXT | strings[1], max 256 bytes |

Response (`format_version` 3)

| Field | Type | Notes | On-chain slot |
|---|---|---|---|
| `lookup_ok` | bool | "Whether the DNS {type} lookup for {name} succeeded: DNS Status 0 (NOERROR) means yes, any other status means no". **`scored: true`**. `true_when`: `0`, `NOERROR`. `false_when`: `1`,`2`,`3`,`4`,`5`,`FORMERR`,`SERVFAIL`,`NXDOMAIN`,`NOTIMP`,`REFUSED` | bools[0] |

That is the only response field. Scoring: `judgment` via `generic`; `lookup_ok` is the scored field. Tolerance and
truth source: **NOT FOUND** (the `true_when`/`false_when` lists suggest the reference is a DNS response status, but
that is an inference, not a published rule). Latency: **NOT FOUND**.

### 2.4 `MALWARE_DETECTION_HASH`

Description (canonical): "Query supplies a file hash (SHA-256, SHA-1 or MD5) and asks whether the file is malicious according to
antivirus engines or known-malware databases." Example query: "Is SHA-256 275a021bbfb6489e54d471899f7db9d1663fc695ec2fe2a2c4538aabf651fd0f flagged as
malware?" A URL on a phishing blocklist is `URL_SCAN_VERDICT`.

Request (`schema_version` 3, `served_by` MALWARE_DETECTION). Note the request parameter is wider than the
description: it says "Domain, IP address or file hash". The on-chain request layout is empty (`request: []`).

| Field | Type | Required | Description |
|---|---|---|---|
| `indicator` | string | yes | Domain, IP address or file hash |

Response (`format_version` 1, `served_by` MALWARE_DETECTION_HASH)

| Field | Type | Notes | On-chain slot |
|---|---|---|---|
| `malicious` | bool | "Whether the file with SHA-256 {key} is flagged as malicious". **`scored: true`**. `true_when`: `true`, `yes`, `malicious`, `flagged`, `listed`, `reported`. No `false_when` list is published. | bools[0] |

**Scoring (this is the only one of the 10 with a published evaluation spec):** class `hybrid`, via `generic`.
- Comparator: code `3` (PassFail, which the docs define as identical to Exact), `tolerance_bps` 0, `scale` 1.
- Question template: "Is the file with SHA-256 {key} flagged as malicious by antivirus engines or malware databases?"
- Question keys (the pool as published today): `275a021bbfb6489e54d471899f7db9d1663fc695ec2fe2a2c4538aabf651fd0f` (the EICAR test file) and `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` (the SHA-256 of an empty file).
- Fusion: `low` 3000, `mode` 0, `pass` 7000, `span` 5000, `floor` 1000 (how comparator and model scores are combined; the docs do not explain the units).

Caution: a miner must answer from a real lookup. Hard-coding answers for the two published keys would be gaming the
evaluation, which the fair-play rules prohibit, and it would also fail on any live request. Real hash lookups answer
those two correctly anyway. Whether free sources need an API key is not a Telegraph rule; **NOT FOUND** in Telegraph's
docs (check each provider's terms).

### 2.5 `KEYWORD_EXTRACTION_GENERAL`

Description (canonical): "Extracts representative topical keywords, keyphrases, and n-grams from body text."

Request: **NOT FOUND.** Response: **NOT FOUND.** Lifecycle reasons reported by the node: "output schema: it has no
response format, its own or its family's" and "input schema: no response format declares inputs". Not on-chain ready.

It is, however, **on the epoch-37 leaderboard with 15 entries** (#1 `vulnrank-triage` 0.995, #2 `kw-crossref`
0.9515, #3 `zk-keyphrase` 0.8642). So miners are being scored without a published schema, and the top scorer is a
vulnerability-triage miner. How questions are generated and answers read for this intent: **NOT FOUND**. Treat this
as unspecified; do not build against an assumed field list.

### 2.6 `ENTITY_EXTRACTION_GENERAL`

Description (canonical): "Extracts named entities (people, organizations, locations, dates) with character offsets from
unstructured text." Request: **NOT FOUND.** Response: **NOT FOUND.** No response format; not scored in epoch 37.
Note the description is NER (people, organizations, locations, dates, offsets), not regex entities (emails, URLs,
money, phone numbers) as the brief assumed.

### 2.7 `SEMANTIC_SIMILARITY_GENERAL`

Description (canonical): "Calculates mathematical cosine similarity and semantic relatedness distance between pairs of
sentences." Request: **NOT FOUND.** Response: **NOT FOUND.** No response format; not scored in epoch 37.

### 2.8 `API_HEALTH_CHECK_GENERAL`

Description (canonical): "Verifies REST/GraphQL API responsiveness, JSON schema payload structure, and HTTP status codes."
Request: **NOT FOUND.** Response: **NOT FOUND.** No response format; not scored in epoch 37.

### 2.9 `GRAMMAR_SPELL_CHECK_GENERAL`

Description (canonical): "Detects typographical, orthographic, punctuation, and grammatical syntax errors with correction
offsets." Request: **NOT FOUND.** Response: **NOT FOUND.** No response format; not scored in epoch 37.

### 2.10 `SERVER_UPTIME_MONITOR_GENERAL`

Description (canonical): "Monitors synthetic HTTP/HTTPS endpoint availability, handshake latency, and uptime status from
multiple edge regions." Request: **NOT FOUND.** Response: **NOT FOUND.** No response format; not scored in epoch 37.

Without a format, the docs say the intent "has no typed params, answers come back raw, and an on-chain job for it
fails" (`anjoman.ai/docs/using/intents`). A response format is added by the admin when an intent is approved, or
later. Watch for it with `GET /engine/v1/intents/<ID>`: when `lifecycle.state` becomes `active` and `response` is
non-null, the spec exists.

## 3. Cross-cutting answers

**(a) Is `semantics.signal_mapping` required?** No. In the YAML reference `semantics.supported_intents` is the
only required field in `semantics`; `signal_mapping` is optional and only accepts `confidence_field`, `label_field`,
`reason_field` (no other keys, including `type`). Our current `miner.yaml` has no `signal_mapping` and the miner is
active and ranked #2 on FRAUD_RISK, so it is not needed to be scored. No intent in this list documents a required
mapping. If you add one: `label_field` is the primary decision field (for example `verdict`, `lookup_ok`,
`malicious`), `confidence_field` a 0-1 number, `reason_field` the explanation text.

**(b) Changing a registered miner's YAML and intents.** `updateMiner(uint256 oldRegistrationId, string yamlUrl,
bytes32 yamlHash, address feeAddress, uint256 minPriceUsdc, string[] supportedIntents)` (from the registration
page, dated Nov 1, written against the previous Diamond; re-check with a free `cast call` dry run on the current
Diamond before sending). Yes, the on-chain intent list can grow: you pass the full new list. Rules: update the hosted
YAML first, recompute the SHA-256 of the raw bytes, every intent must be canonical (a bad entry reverts the whole
update and the old registration is untouched), only the registering wallet can call it. **The old registration ends
and a new `registrationId` (and a new `intentId`) is created.** Whether rank, score or the 7-day grace period carry
over to the new registration: **NOT FOUND.** Given FRAUD_RISK is ranked #2 at score 1.0, the safer alternative is to
register a **second miner** (new `id`, new `slug`) for the new intents, which leaves the current registration
untouched. The registration bond for a new miner is currently 0 (below).

**(c) Per-miner limit on number of intents.** **NOT FOUND** as a hard cap. The only related parameter is the bond
schedule (first intent, each further intent up to 9, then a lower rate for each after), which implies no cap.
Reading the live values from the current Diamond: `minerBondParams()` returns `0` for every rate ("no miner bond is
set yet"), and the 2-day withdrawal delay is `172800`.

**(d) The Diamond and bond changed.** Addresses & Parameters (updated 2026-11-19) lists the current testnet Diamond as
`0x72a70e949362670Bf8bceAd24eFC5b8f950aa08b` (deployed at block 12151179), and states the Diamond has **no MACHINA
token set; bonds are paid in USDC** (test USDC `0xcc194e1a843B658526D0D5C431f670028582Ae17`). The registration page
and our `register.sh` still approve a MACHINA bond (`register.sh` lines 114-117) and the page still cites
`0x3624EB01...`. Verify `DIAMOND` and `MACHINA` before running any transaction, and expect that step to be unnecessary
or to fail on the new Diamond. RPC `https://ethereum-sepolia-rpc.publicnode.com`, chain ID 11155111, minimum floor price
10000 (0.01 USDC).

**(e) How requests reach the miner.** The node turns each question into an HTTP call from the endpoint's YAML
`description`, `intents` and `params`; the parameter names must match the intent's request param names. So the new
endpoints need: IP_GEOLOCATION `ip`; URL_SAFE `url`; EMAIL_SECURITY `domain` and `record_type` (both required);
MALWARE_DETECTION_HASH `indicator`.

## 4. What this means for the build order

Build to spec now, in this order:
1. `IP_GEOLOCATION_GENERAL` (active today, 14 miners, simple response, `ipwho.is` already in the repo).
2. `EMAIL_SECURITY_GENERAL` (20 miners; response is one boolean from a DNS status, so it is small. Build to `lookup_ok`.)
3. `URL_SAFE_GENERAL` (18 miners; four-value verdict enum plus an integer detection count).
4. `MALWARE_DETECTION_HASH` (14 miners, already scored; needs a hash-lookup data source and the `indicator` param accepts domains and IPs too).

Do not build the other six yet. They have no response format, so any field names would be invented, and a wrong
name fails the intent. Re-check `GET /engine/v1/intents/<ID>` for each; build one when its `response` is non-null.
`KEYWORD_EXTRACTION_GENERAL` is the one to watch: it is already being scored without a schema.

What I could not determine: how `scored` fields are compared for judgment-class intents, latency limits for any of the
ten, whether extra response fields are rejected, whether updateMiner preserves rank, and the current-Diamond
`updateMiner` signature (documented only against the previous Diamond).
