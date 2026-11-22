#!/usr/bin/env bash
# Register the miner on Telegraph (Ethereum Sepolia testnet) with Foundry's `cast`.
#
# Usage:
#   export DIAMOND=0x... RPC=https://... MACHINA=0x... FEE_ADDRESS=0x...
#   export YAML_URL=https://your-host/miner.yaml        # or ipfs://<cid>
#   export MINER_PRIVATE_KEY=0x...                      # never commit this
#   ./register.sh
#
# Current testnet addresses: https://docs.telegraphprotocol.com/docs/protocol/addresses-and-params
# (verify them there before sending anything).
#
# Optional env:
#   YAML_FILE=miner.yaml   MIN_PRICE=10000 (6-dec USDC floor, minimum 10000)
#   BOND_APPROVE=10000000000000000000 (MACHINA wei approved for the bond; ~10 MACHINA is plenty)
#   DRY_RUN_ONLY=1  stop after the free `cast call` simulation (no transactions are sent)
#   ASSUME_YES=1    skip the interactive confirmation before sending transactions
#   NODE=https://devnode.telegraphprotocol.com
set -euo pipefail

YAML_FILE="${YAML_FILE:-miner.yaml}"
MIN_PRICE="${MIN_PRICE:-10000}"
BOND_APPROVE="${BOND_APPROVE:-10000000000000000000}"
NODE="${NODE:-https://devnode.telegraphprotocol.com}"
# BEGIN GENERATED: intents (python scripts/sync_intents.py; do not edit)
INTENTS='["FRAUD_RISK_GENERAL","FACT_CHECK_GENERAL","CONTRACT_OBLIGATION_AUDIT_GENERAL","IP_GEOLOCATION_GENERAL","EMAIL_SECURITY_GENERAL","URL_SAFE_GENERAL","MALWARE_DETECTION_HASH","SANCTIONS_SCREENING_MATCH_GENERAL","CRYPTO_PRICE_USD","REGULATORY_FILING_MONITOR_GENERAL","CORPORATE_REGISTRY_LOOKUP_GENERAL","SSL_VERIFICATION_CERT","CVE_LOOKUP_CVSS","VULNERABILITY_TRIAGE_GENERAL","WEATHER_CHECK_TEMPERATURE","WEATHER_FORECAST_GENERAL","GAS_PRICE_GENERAL","URL_SCAN_VERDICT","THREAT_INTELLIGENCE_INDICATOR","CURRENCY_EXCHANGE_CONVERT","FX_NOW_CAD","FX_NOW_EUR","FX_NOW_SEK","DNS_RECORD_LOOKUP_GENERAL","TVL_LOOKUP_GENERAL"]'
# END GENERATED: intents

die() { echo "ERROR: $*" >&2; exit 1; }

# ---- 0. preflight ---------------------------------------------------------------------------
for bin in cast curl awk; do
  command -v "$bin" >/dev/null || die "'$bin' not found (Foundry: https://getfoundry.sh)"
done
for v in DIAMOND RPC MACHINA YAML_URL FEE_ADDRESS MINER_PRIVATE_KEY; do
  [ -n "${!v:-}" ] || die "environment variable $v is not set"
done
[ -f "$YAML_FILE" ] || die "$YAML_FILE not found"
if grep -q "REPLACE-WITH" "$YAML_FILE"; then
  die "$YAML_FILE still has the placeholder base_url - deploy the service first, then put its HTTPS URL in base_url"
fi
# Portable CR check (Git Bash's grep eats CR): the on-chain hash must match the served bytes.
if [ "$(wc -c < "$YAML_FILE")" -ne "$(tr -d '\r' < "$YAML_FILE" | wc -c)" ]; then
  die "$YAML_FILE has CRLF line endings. Run: sed -i 's/\\r$//' $YAML_FILE"
fi
sha256_of() {
  if command -v sha256sum >/dev/null; then sha256sum | awk '{print $1}';
  else shasum -a 256 | awk '{print $1}'; fi
}

MY_ID="$(awk '/^id:/ {print $2; exit}' "$YAML_FILE")"
MY_SLUG="$(awk '/^slug:/ {print $2; exit}' "$YAML_FILE")"
echo "Miner: id=$MY_ID slug=$MY_SLUG"

echo "-> checking every deployed endpoint answers before registering ..."
BASE_URL="$(awk '/^base_url:/ {print $2; exit}' "$YAML_FILE")"
BASE_URL="${BASE_URL%/}"
probe() {  # probe <path> <json body> <field regex> <field regex> ...
  local path="$1" body="$2"; shift 2
  local out
  out="$(curl -fsS --max-time 40 -X POST "$BASE_URL$path" -H 'Content-Type: application/json' -d "$body" || true)"
  [ -n "$out" ] || die "$BASE_URL$path returned nothing - Telegraph revokes miners whose endpoint is silent"
  for f in "$@"; do
    case "$out" in *"\"$f\""*) ;; *) die "$BASE_URL$path did not return the field '$f'. Got: ${out:0:200}" ;; esac
  done
  echo "   $path OK"
}
# BEGIN GENERATED: probes (python scripts/sync_intents.py; do not edit)
probe /fraud-risk '{"subject":"registration smoke test: a first transfer to a new payee"}' risk_level risk_score signals summary
probe /fact-check '{"claim":"The Eiffel Tower is in Paris."}' claim verdict reasoning sources
probe /audit '{"contract":"1.1 The Supplier shall deliver the Goods by 1 March 2026. 1.2 The Buyer shall pay GBP 10,000 within thirty (30) days of invoice."}' verdict obligations clauses summary
probe /ip-geolocation '{"ip":"8.8.8.8"}' ip country city latitude longitude isp
probe /email-security '{"domain":"example.com","record_type":"MX"}' lookup_ok
probe /url-safe '{"url":"https://www.example.com/"}' url verdict detections summary
probe /malware-detection '{"indicator":"275a021bbfb6489e54d471899f7db9d1663fc695ec2fe2a2c4538aabf651fd0f"}' malicious
probe /sanctions-screening '{"name":"Banco Nacional de Cuba"}' name matched matches lists match_score
probe /crypto-price '{"symbol":"BTC"}' price_usd
probe /regulatory-filings '{"entity":"Apple Inc","period":"last 12 months"}' entity filing_count filings urls
probe /corporate-registry '{"company":"Apple Inc","jurisdiction":"United States"}' legal_name registration_id active jurisdiction registered_address incorporated
probe /ssl-verification '{"host":"example.com"}' certificate_valid
probe /cve-lookup '{"cve_id":"CVE-2021-44228"}' cve_id cvss_score severity description published references
probe /vulnerability-triage '{"cve_id":"CVE-2021-44228"}' cvss_base_score
probe /weather-temperature '{"location":"London"}' temperature_c
probe /weather-forecast '{"location":"London","window_hours":24}' location when temp_max_c temp_min_c precipitation_probability_pct condition
probe /gas-price '{"chain":"ethereum"}' chain gas_price_gwei transfer_fee_usd as_of
probe /url-scan '{"url":"https://www.example.com/"}' safe
probe /threat-intelligence '{"indicator":"185.220.101.4"}' malicious
probe /currency-convert '{"from_currency":"USD","to_currency":"EUR","amount":100}' converted_amount
probe /fx-now-cad '{"currency":"CAD"}' rate
probe /fx-now-eur '{"currency":"EUR"}' rate
probe /fx-now-sek '{"currency":"SEK"}' rate
probe /dns-record '{"name":"example.com","record_type":"MX"}' has_record
probe /tvl-lookup '{"protocol":"aave"}' protocol_or_chain tvl_usd chain_tvl_usd as_of
# END GENERATED: probes

echo "-> checking id/slug are unused on $NODE ..."
if LIST="$(curl -fsS --max-time 120 "$NODE/api/miners")"; then
  grep -qF "\"id\":\"$MY_ID\"" <<<"$LIST" && die "id $MY_ID is taken - pick another in $YAML_FILE"
  grep -qF "\"slug\":\"$MY_SLUG\"" <<<"$LIST" && die "slug $MY_SLUG is taken - pick another"
  echo "   id and slug are free"
else
  echo "   WARNING: could not fetch $NODE/api/miners (that endpoint is slow; it serves ~4 MB)."
  echo "   The dry run below still reverts if the id or slug is taken, so this is not fatal."
fi

for INTENT in $(tr -d '[]"' <<<"$INTENTS" | tr ',' ' '); do
  echo "-> checking $INTENT is a canonical intent ..."
  [ "$(cast call "$DIAMOND" "isCanonicalIntent(string)(bool)" "$INTENT" --rpc-url "$RPC")" = "true" ]     || die "$INTENT is not canonical on this Diamond (wrong DIAMOND/RPC?)"
done

# ---- 1. hash --------------------------------------------------------------------------------
YAML_HASH="0x$(sha256_of < "$YAML_FILE")"          # SHA-256 (NOT keccak) of the raw bytes
echo "-> YAML sha256: $YAML_HASH"
case "$YAML_URL" in
  http://*|https://*)
    REMOTE="0x$(curl -fsSL --max-time 30 "$YAML_URL" | sha256_of)" || die "could not fetch $YAML_URL"
    [ "$REMOTE" = "$YAML_HASH" ] || die "the file served at YAML_URL hashes to $REMOTE but local $YAML_FILE hashes to $YAML_HASH. Upload the current file first."
    echo "   hosted YAML matches the local file" ;;
  *) echo "   YAML_URL is not http(s); make sure those bytes equal $YAML_FILE exactly" ;;
esac

SENDER="$(cast wallet address --private-key "$MINER_PRIVATE_KEY")"
echo "-> registering from $SENDER, payouts to $FEE_ADDRESS"

# ---- 2. dry run (free) ----------------------------------------------------------------------
echo "-> dry run: cast call registerMiner(...)"
EXPECTED_ID="$(cast call "$DIAMOND" \
  "registerMiner(string,bytes32,address,uint256,string[])(uint256)" \
  "$YAML_URL" "$YAML_HASH" "$FEE_ADDRESS" "$MIN_PRICE" "$INTENTS" \
  --from "$SENDER" --rpc-url "$RPC")" \
  || die "dry run reverted (see the revert reason above: registration window, unsupported intent, price below the floor, or a missing MACHINA bond/approval)"
echo "   dry run OK - registrationId would be: $EXPECTED_ID"
[ "${DRY_RUN_ONLY:-0}" = "1" ] && { echo "DRY_RUN_ONLY=1: stopping before any transaction."; exit 0; }

if [ "${ASSUME_YES:-0}" != "1" ]; then
  read -r -p "Send the approve + register transactions on $RPC? [y/N] " ans
  [ "$ans" = "y" ] || [ "$ans" = "Y" ] || die "aborted"
fi

# ---- 3. approve the MACHINA bond ------------------------------------------------------------
echo "-> approving the Diamond to take the MACHINA bond"
cast send "$MACHINA" "approve(address,uint256)" "$DIAMOND" "$BOND_APPROVE" \
  --rpc-url "$RPC" --private-key "$MINER_PRIVATE_KEY" >/dev/null

# The first dry run ran *before* the approval existed; re-simulate now that it does.
EXPECTED_ID="$(cast call "$DIAMOND" \
  "registerMiner(string,bytes32,address,uint256,string[])(uint256)" \
  "$YAML_URL" "$YAML_HASH" "$FEE_ADDRESS" "$MIN_PRICE" "$INTENTS" \
  --from "$SENDER" --rpc-url "$RPC")" || die "post-approval simulation reverted"

# ---- 4. register ----------------------------------------------------------------------------
echo "-> sending registerMiner"
RECEIPT="$(cast send "$DIAMOND" \
  "registerMiner(string,bytes32,address,uint256,string[])" \
  "$YAML_URL" "$YAML_HASH" "$FEE_ADDRESS" "$MIN_PRICE" "$INTENTS" \
  --rpc-url "$RPC" --private-key "$MINER_PRIVATE_KEY" --json)"

REGISTRATION_ID="$EXPECTED_ID"
if command -v jq >/dev/null; then
  echo "   tx: $(jq -r '.transactionHash' <<<"$RECEIPT")  status: $(jq -r '.status' <<<"$RECEIPT")"
  # MinerRegistered(registrationId indexed, miner indexed, ...): first indexed topic
  TOPIC="$(jq -r --arg d "$(tr 'A-F' 'a-f' <<<"$DIAMOND")" \
    '[.logs[] | select((.address|ascii_downcase)==$d and (.topics|length)>=2)][0].topics[1] // empty' <<<"$RECEIPT")"
  if [ -n "$TOPIC" ]; then
    FROM_LOG="$(cast to-dec "$TOPIC")"
    [ "$FROM_LOG" = "${EXPECTED_ID%% *}" ] \
      || echo "   NOTE: id from the event log ($FROM_LOG) differs from the simulated id ($EXPECTED_ID); using the event log"
    REGISTRATION_ID="$FROM_LOG"
  fi
else
  echo "   (install jq to parse the receipt; using the simulated id)"
fi
REGISTRATION_ID="${REGISTRATION_ID%% *}"

echo
echo "registrationId = $REGISTRATION_ID"
echo "Nodes activate you ~10-15 min after the tx (they read Sepolia's 'safe' block). Verify with:"
echo "  curl -s $NODE/api/miners/$REGISTRATION_ID | jq '.miner | {activation_status, rejection_reason, retrying}'"
echo "Expect activation_status = active. If 'rejected', fix miner.yaml, re-host it, recompute the"
echo "hash and call updateMiner (see the README)."
