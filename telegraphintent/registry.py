"""The single source of truth for which intents this miner serves.

`miner.yaml`, the intent block in `register.sh` and the `COPY` block in the `Dockerfile` are GENERATED
from this list by `python scripts/sync_intents.py`; `telegraphintent/app.py` routes from it. To add an
intent, follow docs/INTENT_RULES.md. Request/response names must equal docs/intent_specs/<INTENT>.json.
"""
from __future__ import annotations

from dataclasses import dataclass

#: Frozen (rule A1): changing any of these makes Telegraph see a different miner.
IDENTITY = {
    "id": 83159274,
    "slug": "telegraph-intent-miner",
    "name": "TelegraphIntent",
    "base_url": "https://fraudrisk.onrender.com",
}


@dataclass(frozen=True)
class Param:
    name: str
    description: str
    type: str = "string"        # string | integer | number, as in the spec (an enum is a string)
    required: bool = True


@dataclass(frozen=True)
class IntentSpec:
    package: str                # importable package with `app.create_app` and `config.Settings`
    intent: str                 # canonical Telegraph intent name
    path: str                   # POST path (also the YAML endpoint path)
    params: tuple[Param, ...]   # request params, in the spec's order
    fields: tuple[str, ...]     # response fields, exactly the spec's names
    endpoint_description: str   # Telegraph's node routes questions by this text
    summary: str                # one sentence in the miner's top-level description
    probe: str                  # JSON body register.sh smoke-tests the live endpoint with
    llm: bool = False           # may use Groq (intentkit.groq); then it MUST also answer without it (rule 11)

    @property
    def name(self) -> str:      # key in the gateway's app table and /healthz
        return self.package.rsplit(".", 1)[-1]

    @property
    def prefix(self) -> str:    # per-intent environment override prefix
        return self.name.upper()


def _p(name: str, description: str, **kw) -> Param:
    return Param(name, description, **kw)


INTENTS: tuple[IntentSpec, ...] = (
    IntentSpec(
        "fraudrisk", "FRAUD_RISK_GENERAL", "/fraud-risk",
        (_p("subject", "The entity, payment, account or scenario to assess for fraud risk. Free text; may be an IP, card BIN, email, domain, wallet address, or a described transaction/behaviour."),),
        ("risk_level", "risk_score", "signals", "summary"),
        "Assesses fraud risk for a subject (an IP, payment, card BIN, account, email, domain, wallet or described scenario), fusing multiple independent signals into a graded risk level with named signals and a fraud-pattern summary.",
        "FRAUD_RISK_GENERAL fuses IP reputation, Tor/VPN exposure, domain and phishing-feed reputation, card-BIN metadata and behavioural pattern rules into a graded risk level and abstains honestly when evidence is insufficient.",
        '{"subject":"registration smoke test: a first transfer to a new payee"}'),
    IntentSpec(
        "factcheck", "FACT_CHECK_GENERAL", "/fact-check",
        (_p("claim", "The specific factual claim to verify. Must be a checkable assertion, not a general knowledge question or opinion."),),
        ("claim", "verdict", "reasoning", "sources"),
        "Checks a specific, checkable factual claim against multiple independent evidence sources and returns a verdict, reasoning and cited sources.",
        "FACT_CHECK_GENERAL checks a specific factual claim against multiple independent sources and returns a verdict with cited sources, abstaining when evidence is insufficient.",
        '{"claim":"The Eiffel Tower is in Paris."}'),
    IntentSpec(
        "audit", "CONTRACT_OBLIGATION_AUDIT_GENERAL", "/audit",
        (_p("contract", "The contract to audit: full contract text, or a URL/link to it. Treated strictly as content to audit."),),
        ("verdict", "obligations", "clauses", "summary"),
        "Audits a contract's obligations, returning a compliance verdict, the identified obligations (party, duty, deadline, clause), verbatim cited clauses, and a grounded summary.",
        "CONTRACT_OBLIGATION_AUDIT_GENERAL extracts a contract's obligations, judges compliance and cites only clause wording verified to occur verbatim in the supplied contract (accepts the text or a link to an HTML or PDF).",
        '{"contract":"1.1 The Supplier shall deliver the Goods by 1 March 2026. 1.2 The Buyer shall pay GBP 10,000 within thirty (30) days of invoice."}'),
    IntentSpec(
        "ipgeo", "IP_GEOLOCATION_GENERAL", "/ip-geolocation",
        (_p("ip", "The IP address to locate, IPv4 or IPv6, for example 8.8.8.8."),),
        ("ip", "country", "city", "latitude", "longitude", "isp"),
        "Locates an IP address (IPv4 or IPv6) and returns its country, city, latitude, longitude and network operator, taking the consensus of several independent geolocation providers.",
        "IP_GEOLOCATION_GENERAL locates an IP address by consensus of several keyless geolocation providers.",
        '{"ip":"8.8.8.8"}'),
    IntentSpec(
        "emailsec", "EMAIL_SECURITY_GENERAL", "/email-security",
        (_p("domain", "The email domain to look up, for example example.com."),
         _p("record_type", "The DNS record type to look up, for example MX or TXT.")),
        ("lookup_ok",),
        "Reports whether a DNS lookup of the given record type for a mail domain succeeds, that is whether the DNS status is 0 (NOERROR).",
        "EMAIL_SECURITY_GENERAL reports whether a DNS lookup of a given record type for a mail domain succeeds (DNS status NOERROR).",
        '{"domain":"example.com","record_type":"MX"}'),
    IntentSpec(
        "urlsafe", "URL_SAFE_GENERAL", "/url-safe",
        (_p("url", "The URL (or bare domain) to check, for example https://login-secure-update.example.com/verify."),),
        ("url", "verdict", "detections", "summary"),
        "Checks a URL or domain against OpenPhish's live phishing feed and Cloudflare's malware and phishing DNS filter and returns a verdict, the number of sources that flag it, and a summary naming them.",
        "URL_SAFE_GENERAL checks a URL against OpenPhish's live phishing feed and Cloudflare's malware and phishing DNS filter, counting the sources that flag it.",
        '{"url":"https://www.example.com/"}'),
    IntentSpec(
        "malware", "MALWARE_DETECTION_HASH", "/malware-detection",
        (_p("indicator", "The file hash (MD5, SHA-1 or SHA-256), domain or IP address to check."),),
        ("malicious",),
        "Reports whether a file hash (MD5, SHA-1 or SHA-256), domain or IP address is flagged as malicious by CIRCL hashlookup, Team Cymru's malware hash registry, AlienVault OTX, OpenPhish and Cloudflare's security DNS.",
        "MALWARE_DETECTION_HASH reports whether a file hash (MD5, SHA-1 or SHA-256), domain or IP address is flagged as malicious by CIRCL hashlookup, Team Cymru's malware hash registry, AlienVault OTX, OpenPhish and Cloudflare's security DNS.",
        '{"indicator":"275a021bbfb6489e54d471899f7db9d1663fc695ec2fe2a2c4538aabf651fd0f"}'),
    # ---- keyless, code-only intents (intents/<package>), roughly by observed demand -----------------------------
    IntentSpec(
        "intents.sanctions", "SANCTIONS_SCREENING_MATCH_GENERAL", "/sanctions-screening",
        (_p("name", "Person or entity name to screen against sanctions lists."),),
        ("name", "matched", "matches", "lists", "match_score"),
        "Screens a person or organisation name against OFAC's SDN and consolidated sanctions lists with fuzzy name matching and returns whether it matched, the matching list entries, the lists checked and the best match score from 0 to 1.",
        "SANCTIONS_SCREENING_MATCH_GENERAL screens a name against OFAC's SDN and consolidated sanctions lists with fuzzy matching.",
        '{"name":"Banco Nacional de Cuba"}'),
    IntentSpec(
        "intents.crypto_price", "CRYPTO_PRICE_USD", "/crypto-price",
        (_p("symbol", "Asset symbol, e.g. BTC."),),
        ("price_usd",),
        "Returns the current price of a cryptocurrency in US dollars (BTC, ETH, SOL, BNB, XRP and others), the consensus of several public exchanges' last trade prices.",
        "CRYPTO_PRICE_USD returns a crypto asset's current dollar price as the consensus of several public exchanges.",
        '{"symbol":"BTC"}'),
    IntentSpec(
        "intents.sec_filings", "REGULATORY_FILING_MONITOR_GENERAL", "/regulatory-filings",
        (_p("entity", "Company, product or concern."),
         _p("period", "Period to search, e.g. last 12 months.")),
        ("entity", "filing_count", "filings", "urls"),
        "Counts and lists a company's SEC EDGAR filings in a period, newest first with their URLs; for a topic that is not a registrant it counts EDGAR filings that mention it.",
        "REGULATORY_FILING_MONITOR_GENERAL counts and lists SEC EDGAR filings in a period.",
        '{"entity":"Apple Inc","period":"last 12 months"}'),
    IntentSpec(
        "intents.corporate_registry", "CORPORATE_REGISTRY_LOOKUP_GENERAL", "/corporate-registry",
        (_p("company", "Company name or registry identifier."),
         _p("jurisdiction", "Country or jurisdiction of registration.")),
        ("legal_name", "registration_id", "active", "jurisdiction", "registered_address", "incorporated"),
        "Looks a company up in the GLEIF global legal-entity registry and returns its legal name, LEI, whether it is active, its jurisdiction, registered address and incorporation date.",
        "CORPORATE_REGISTRY_LOOKUP_GENERAL looks a company up in GLEIF's LEI registry.",
        '{"company":"Apple Inc","jurisdiction":"United States"}'),
    IntentSpec(
        "intents.ssl_cert", "SSL_VERIFICATION_CERT", "/ssl-verification",
        (_p("host", "Hostname."),),
        ("certificate_valid",),
        "Checks whether a host's TLS certificate is currently valid and trusted by making a real TLS handshake: false for expired, self-signed, wrong-host or untrusted certificates.",
        "SSL_VERIFICATION_CERT checks a host's TLS certificate with a real handshake.",
        '{"host":"example.com"}'),
    IntentSpec(
        "intents.cve", "CVE_LOOKUP_CVSS", "/cve-lookup",
        (_p("cve_id", "CVE identifier, e.g. CVE-2024-3094."),),
        ("cve_id", "cvss_score", "severity", "description", "published", "references"),
        "Looks up a CVE and returns its CVSS base score, severity, description, publication date and reference URLs from NVD (CIRCL as fallback).",
        "CVE_LOOKUP_CVSS returns a CVE's CVSS score and details from NVD.",
        '{"cve_id":"CVE-2021-44228"}'),
    IntentSpec(
        "intents.vulntriage", "VULNERABILITY_TRIAGE_GENERAL", "/vulnerability-triage",
        (_p("cve_id", "CVE identifier."),),
        ("cvss_base_score",),
        "Triages a vulnerability by returning the NVD primary CVSS base score (0.0 to 10.0) of a CVE.",
        "VULNERABILITY_TRIAGE_GENERAL returns a CVE's NVD CVSS base score.",
        '{"cve_id":"CVE-2021-44228"}'),
    IntentSpec(
        "intents.weather_temperature", "WEATHER_CHECK_TEMPERATURE", "/weather-temperature",
        (_p("location", "City or place name, or latitude,longitude."),),
        ("temperature_c",),
        "Returns the current air temperature in degrees Celsius for a city, place name or latitude,longitude, from Open-Meteo.",
        "WEATHER_CHECK_TEMPERATURE returns a place's current air temperature in Celsius.",
        '{"location":"London"}'),
    IntentSpec(
        "intents.weather_forecast", "WEATHER_FORECAST_GENERAL", "/weather-forecast",
        (_p("location", "City or place name."),
         _p("window_hours", "Forecast window in hours, 1 to 240. Defaults to 24.", type="integer", required=False)),
        ("location", "when", "temp_max_c", "temp_min_c", "precipitation_probability_pct", "condition"),
        "Returns the weather forecast for a place over a window of up to 240 hours: the high and low temperature in Celsius, the peak chance of precipitation and the expected condition.",
        "WEATHER_FORECAST_GENERAL returns a place's forecast high, low, rain chance and condition.",
        '{"location":"London","window_hours":24}'),
    IntentSpec(
        "intents.gas_price", "GAS_PRICE_GENERAL", "/gas-price",
        (_p("chain", "Blockchain network, e.g. ethereum."),),
        ("chain", "gas_price_gwei", "transfer_fee_usd", "as_of"),
        "Returns the current gas price in gwei of an EVM chain (Ethereum, Base, Arbitrum, Optimism, Polygon, BNB Chain, Avalanche) and the dollar cost of a standard 21,000-gas transfer.",
        "GAS_PRICE_GENERAL returns an EVM chain's gas price in gwei and the cost of a transfer.",
        '{"chain":"ethereum"}'),
    IntentSpec(
        "intents.urlscan", "URL_SCAN_VERDICT", "/url-scan",
        (_p("url", "The URL."),),
        ("safe",),
        "Returns whether a URL is safe to visit, checking it against OpenPhish's live phishing feed, Cloudflare's malware and phishing DNS filter and the URL's own structure.",
        "URL_SCAN_VERDICT says whether a URL is safe to visit.",
        '{"url":"https://www.example.com/"}'),
    IntentSpec(
        "intents.threatintel", "THREAT_INTELLIGENCE_INDICATOR", "/threat-intelligence",
        (_p("indicator", "IP address, domain, hash, CVE or actor."),),
        ("malicious",),
        "Reports whether an IP address, domain or file hash is reported as malicious in public threat-intelligence feeds (IPsum's aggregate of IP blocklists, CIRCL, Team Cymru, AlienVault OTX, OpenPhish, Cloudflare security DNS).",
        "THREAT_INTELLIGENCE_INDICATOR reports whether an IP, domain or hash is flagged in public threat feeds.",
        '{"indicator":"185.220.101.4"}'),
    IntentSpec(
        "intents.currency_convert", "CURRENCY_EXCHANGE_CONVERT", "/currency-convert",
        (_p("from_currency", "Currency code to convert from, e.g. USD."),
         _p("to_currency", "Currency code to convert into, e.g. EUR."),
         _p("amount", "Amount to convert, in the source currency. Defaults to 1.", type="number", required=False)),
        ("converted_amount",),
        "Converts an amount from one currency to another at the current exchange rate (ECB reference rates, with open.er-api.com for other currencies).",
        "CURRENCY_EXCHANGE_CONVERT converts an amount between two currencies.",
        '{"from_currency":"USD","to_currency":"EUR","amount":100}'),
    IntentSpec(
        "intents.fx_cad", "FX_NOW_CAD", "/fx-now-cad",
        (_p("currency", "Currency code priced against the US dollar, e.g. EUR."),),
        ("rate",),
        "Returns the current USD/CAD mid-market exchange rate, how many Canadian dollars one US dollar buys (any other currency code is answered the same way).",
        "FX_NOW_CAD returns the current USD/CAD exchange rate.",
        '{"currency":"CAD"}'),
    IntentSpec(
        "intents.fx_eur", "FX_NOW_EUR", "/fx-now-eur",
        (_p("currency", "Currency code priced against the US dollar, e.g. EUR."),),
        ("rate",),
        "Returns the current USD/EUR mid-market exchange rate, how many euros one US dollar buys (any other currency code is answered the same way).",
        "FX_NOW_EUR returns the current USD/EUR exchange rate.",
        '{"currency":"EUR"}'),
    IntentSpec(
        "intents.fx_sek", "FX_NOW_SEK", "/fx-now-sek",
        (_p("currency", "Currency code priced against the US dollar, e.g. EUR."),),
        ("rate",),
        "Returns the current USD/SEK mid-market exchange rate, how many Swedish kronor one US dollar buys (any other currency code is answered the same way).",
        "FX_NOW_SEK returns the current USD/SEK exchange rate.",
        '{"currency":"SEK"}'),
    IntentSpec(
        "intents.dns_lookup", "DNS_RECORD_LOOKUP_GENERAL", "/dns-record",
        (_p("name", "Domain name."),
         _p("record_type", "DNS record type: A, AAAA, CNAME, MX, NS, TXT, SOA, CAA, SRV or PTR.")),
        ("has_record",),
        "Reports whether a DNS lookup of a record type (A, AAAA, CNAME, MX, NS, TXT, SOA, CAA, SRV, PTR) for a domain name returns at least one record, using Google and Cloudflare DNS-over-HTTPS.",
        "DNS_RECORD_LOOKUP_GENERAL reports whether a name has a record of a given DNS type.",
        '{"name":"example.com","record_type":"MX"}'),
    IntentSpec(
        "intents.tvl", "TVL_LOOKUP_GENERAL", "/tvl-lookup",
        (_p("protocol", "DeFi protocol, pool or chain."),),
        ("protocol_or_chain", "tvl_usd", "chain_tvl_usd", "as_of"),
        "Returns the total value locked in US dollars of a DeFi protocol or blockchain from DefiLlama, and for a protocol on a named chain its value locked on that chain.",
        "TVL_LOOKUP_GENERAL returns the total value locked of a DeFi protocol or chain.",
        '{"protocol":"aave"}'),
    # ---- Groq-assisted intents (intents/<package>): each answers from public sources without a key ------------------
    IntentSpec(
        "intents.web_search", "WEB_SEARCH_GENERAL", "/web-search",
        (_p("query", "What to search for."),),
        ("answer", "sources", "brief"),
        "Answers a query that needs current or externally sourced information (latest news, facts, figures) with a written answer drawn from live web sources (Wikipedia, DuckDuckGo, Google News, and Tavily, Brave or Serper when configured), the URLs of the sources used, and a one-sentence brief.",
        "WEB_SEARCH_GENERAL answers current-information queries from live web sources, citing them.",
        '{"query":"What is the latest news on the Fed interest rate decision?"}', llm=True),
    IntentSpec(
        "intents.chat_completion", "CHAT_COMPLETION_GENERAL", "/chat-completion",
        (_p("message", "The message or question."),),
        ("reply", "verdict", "brief"),
        "Replies to a general-knowledge, reasoning, explanation or conversational message that needs no live data. For a will-X-happen question the reply leads with Likely, Unlikely or Uncertain. Also does arithmetic and answers date questions exactly.",
        "CHAT_COMPLETION_GENERAL replies to general questions and conversation.",
        '{"message":"Why is the sky blue?"}', llm=True),
    IntentSpec(
        "intents.research_query", "RESEARCH_QUERY_GENERAL", "/research-query",
        (_p("question", "The research question."),),
        ("answer", "source_count", "sources", "brief"),
        "Answers a research question in one paragraph supported by citations to scholarly sources (PubMed and OpenAlex abstracts, Wikipedia background), returning the answer, how many sources it drew on, their URLs, and a one-sentence brief.",
        "RESEARCH_QUERY_GENERAL answers research questions with cited scholarly sources.",
        '{"question":"What does the evidence say about the health effects of intermittent fasting?"}', llm=True),
    IntentSpec(
        "intents.security_review", "SECURITY_REVIEW_GENERAL", "/security-review",
        (_p("target", "Code, package, contract or site to review."),),
        ("verdict", "vulnerability_count", "issues", "summary"),
        "Reviews code, dependencies (requirements.txt, package.json, lock files, name@version), a Solidity contract, a GitHub repository or a website for security findings: known CVEs and GHSA advisories from OSV, hard-coded secrets, injection-prone patterns, TLS and security-header weaknesses. Returns a verdict, the number of vulnerabilities, their ids (CVE, GHSA, CWE, SWC) most serious first, and a summary.",
        "SECURITY_REVIEW_GENERAL reviews code, dependencies, contracts, repositories and websites for security findings.",
        '{"target":"lodash@4.17.15"}', llm=True),
    IntentSpec(
        "intents.event_outcome", "EVENT_OUTCOME_RESOLUTION_GENERAL", "/event-outcome",
        (_p("event", "The event or question to resolve."),),
        ("event", "outcome", "resolved_yes", "winner", "resolved_at", "sources"),
        "Resolves a specific, checkable real-world event for a prediction market: whether it happened (yes, no or unresolved), the winner if it has one, when it settled and the sources cited. Uses settled prediction markets and retrieved evidence, and answers unresolved for events that are in the future or not yet settled.",
        "EVENT_OUTCOME_RESOLUTION_GENERAL resolves named real-world events from settled markets and cited evidence.",
        '{"event":"Did the Fed cut rates in September 2025?"}', llm=True),
    # ---- more keyless intents, from the second demand list ------------------------------------------------------------
    IntentSpec(
        "intents.token_holders", "TOKEN_HOLDER_COUNT_GENERAL", "/token-holders",
        (_p("token", "Token contract address or name."),
         _p("chain", "Chain of the token.")),
        ("token", "chain", "holders_count", "token_symbol"),
        "Returns how many distinct addresses hold an ERC-20 token, given its contract address (or symbol) and chain (Ethereum, Optimism, Base, Polygon, Arbitrum, Gnosis, Avalanche), from public block-explorer indexes (Blockscout, Ethplorer, Routescan).",
        "TOKEN_HOLDER_COUNT_GENERAL returns the number of addresses holding a token.",
        '{"token":"0xdAC17F958D2ee523a2206206994597C13D831ec7","chain":"ethereum"}'),
    IntentSpec(
        "intents.sla_compliance", "SLA_COMPLIANCE_GENERAL", "/sla-compliance",
        (_p("service", "The service."),
         _p("period", "Period to measure, e.g. last 30 days.")),
        ("service", "uptime_pct", "sla_target_pct", "compliant", "period"),
        "Returns the published uptime percentage of a monitored service (Upptime public status data) for the last 24 hours, 7 days, 30 days, 365 days or all time, the SLA target if the request states one (for example 99.9%), and whether the uptime met it. Other periods and unmonitored services return no uptime.",
        "SLA_COMPLIANCE_GENERAL returns a service's published uptime for a period and whether it met a stated target.",
        '{"service":"Google","period":"last 30 days"}'),
    IntentSpec(
        "intents.return_policy", "RETURN_POLICY_VERIFY_GENERAL", "/return-policy",
        (_p("retailer", "Retailer."),),
        ("retailer", "return_window_days", "conditions", "verified", "sources"),
        "Reads a retailer's own return policy page live and returns the return window in days, the conditions attached to returns (unopened, original packaging, restocking fees, store credit), whether the policy was found and confirmed on the retailer's own site, and the page URLs cited. Pages that block bots or state no period return no window.",
        "RETURN_POLICY_VERIFY_GENERAL reads a retailer's return window and conditions from its own policy page.",
        '{"retailer":"Adafruit"}'),
)

BY_INTENT = {s.intent: s for s in INTENTS}
