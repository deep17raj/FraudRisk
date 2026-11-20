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
INTENTS='["FRAUD_RISK_GENERAL"]'

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

echo "-> checking the deployed endpoint answers before registering ..."
BASE_URL="$(awk '/^base_url:/ {print $2; exit}' "$YAML_FILE")"
PROBE="$(curl -fsS --max-time 20 -X POST "$BASE_URL/fraud-risk" \
  -H 'Content-Type: application/json' \
  -d '{"subject":"registration smoke test: a first transfer to a new payee"}' || true)"
case "$PROBE" in
  *'"risk_level"'*'"risk_score"'*) echo "   endpoint OK" ;;
  "") die "$BASE_URL/fraud-risk returned nothing - Telegraph revokes miners whose endpoint is silent" ;;
  *) die "$BASE_URL/fraud-risk did not return the intent fields. Got: ${PROBE:0:200}" ;;
esac

echo "-> checking id/slug are unused on $NODE ..."
if LIST="$(curl -fsS --max-time 120 "$NODE/api/miners")"; then
  grep -qF "\"id\":\"$MY_ID\"" <<<"$LIST" && die "id $MY_ID is taken - pick another in $YAML_FILE"
  grep -qF "\"slug\":\"$MY_SLUG\"" <<<"$LIST" && die "slug $MY_SLUG is taken - pick another"
  echo "   id and slug are free"
else
  echo "   WARNING: could not fetch $NODE/api/miners (that endpoint is slow; it serves ~4 MB)."
  echo "   The dry run below still reverts if the id or slug is taken, so this is not fatal."
fi

echo "-> checking FRAUD_RISK_GENERAL is a canonical intent ..."
[ "$(cast call "$DIAMOND" "isCanonicalIntent(string)(bool)" "FRAUD_RISK_GENERAL" --rpc-url "$RPC")" = "true" ] \
  || die "FRAUD_RISK_GENERAL is not canonical on this Diamond (wrong DIAMOND/RPC?)"

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
