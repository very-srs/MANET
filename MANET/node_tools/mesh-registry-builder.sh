#!/bin/bash
# ==============================================================================
# Mesh Registry Builder
# ==============================================================================
# This script:
# 1. Queries Alfred for peer data
# 2. Decodes protobuf messages
# 3. Writes plain text /var/run/mesh_node_registry
# 4. Tracks claimed IPs for IP manager
# ==============================================================================

# --- Configuration ---
ALFRED_DATA_TYPE=68
REGISTRY_STATE_FILE="/var/run/mesh_node_registry"
CLAIMED_IPS_FILE="/tmp/claimed_ips.txt"
DECODER_PATH="/usr/local/bin/decoder.py"

# --- Helper Functions ---
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - REGISTRY: $1"
}

# --- Main Logic ---

# Query Alfred for all peer payloads
mapfile -t PEER_PAYLOADS < <(alfred -r $ALFRED_DATA_TYPE 2>/dev/null | grep -oP '"\K[^"]+(?="\s*\},?)' )

log "Found ${#PEER_PAYLOADS[@]} peer payloads from Alfred"

# Create temporary files
REGISTRY_TMP=$(mktemp)
CLAIMED_IPS_TMP=$(mktemp)

# Write registry header
echo "# Mesh Node Registry - Generated $(date)" > "$REGISTRY_TMP"
echo "# Sourced by other scripts to get network state." >> "$REGISTRY_TMP"
echo "" >> "$REGISTRY_TMP"

# Process each payload
for B64_PAYLOAD in "${PEER_PAYLOADS[@]}"; do
    if [ -z "$B64_PAYLOAD" ]; then
        continue
    fi

    # Decode the protobuf message
    DECODED_DATA=$("$DECODER_PATH" "${B64_PAYLOAD}" 2>&1)
    DECODER_EXIT=$?

    if [ $DECODER_EXIT -ne 0 ]; then
        log "Warning: decoder.py failed with exit code $DECODER_EXIT"
        continue
    fi

    if [ -z "$DECODED_DATA" ]; then
        log "Warning: decoder.py returned empty data"
        continue
    fi

    # Filter for valid variable assignments
    FILTERED_DATA=$(echo "$DECODED_DATA" | grep -E "^[A-Z0-9_]+=")

    if [ -z "$FILTERED_DATA" ]; then
        log "Warning: No valid variable assignments in decoded data"
        continue
    fi

    # Evaluate the decoded data to extract variables
    eval "$FILTERED_DATA"

    # Write to registry if we have a MAC address
    if [[ -n "$MAC_ADDRESS" ]]; then
        PREFIX="NODE_$(echo "$MAC_ADDRESS" | tr -d ':')"

        # Write all node data to registry
        {
            printf "%s_HOSTNAME='%s'\n" "$PREFIX" "${HOSTNAME:-}"
            printf "%s_MAC_ADDRESS='%s'\n" "$PREFIX" "${MAC_ADDRESS:-}"
            printf "%s_MAC_ADDRESSES='%s'\n" "$PREFIX" "${MAC_ADDRESSES:-}"
            printf "%s_IPV4_ADDRESS='%s'\n" "$PREFIX" "${IPV4_ADDRESS:-}"
            printf "%s_SYNCTHING_ID='%s'\n" "$PREFIX" "${SYNCTHING_ID:-}"
            printf "%s_TQ_AVERAGE='%s'\n" "$PREFIX" "${TQ_AVERAGE:-}"
            printf "%s_IS_GATEWAY='%s'\n" "$PREFIX" "${IS_INTERNET_GATEWAY:-}"
            printf "%s_IS_NTP_SERVER='%s'\n" "$PREFIX" "${IS_NTP_SERVER:-}"
            printf "%s_IS_MUMBLE_SERVER='%s'\n" "$PREFIX" "${IS_MUMBLE_SERVER:-}"
            printf "%s_IS_TAK_SERVER='%s'\n" "$PREFIX" "${IS_TAK_SERVER:-}"
            printf "%s_IS_MEDIAMTX_SERVER='%s'\n" "$PREFIX" "${IS_MEDIAMTX_SERVER:-}"
            printf "%s_UPTIME_SECONDS='%s'\n" "$PREFIX" "${UPTIME_SECONDS:-}"
            printf "%s_BATTERY_PERCENTAGE='%s'\n" "$PREFIX" "${BATTERY_PERCENTAGE:-}"
            printf "%s_CPU_LOAD_AVERAGE='%s'\n" "$PREFIX" "${CPU_LOAD_AVERAGE:-}"
            printf "%s_DATA_CHANNEL_2_4='%s'\n" "$PREFIX" "${DATA_CHANNEL_2_4:-}"
            printf "%s_DATA_CHANNEL_5_0='%s'\n" "$PREFIX" "${DATA_CHANNEL_5_0:-}"
            printf "%s_CHANNEL_REPORT_JSON='%s'\n" "$PREFIX" "${CHANNEL_REPORT_JSON:-}"
            printf "%s_LAST_SEEN_TIMESTAMP='%s'\n" "$PREFIX" "${LAST_SEEN_TIMESTAMP:-0}"
            printf "%s_IS_IN_LIMP_MODE='%s'\n" "$PREFIX" "${IS_IN_LIMP_MODE:-false}"
            printf "%s_LAST_TOURGUIDE_TIMESTAMP='%s'\n" "$PREFIX" "${LAST_TOURGUIDE_TIMESTAMP:-0}"
            printf "%s_LAST_TOURGUIDE_RADIO='%s'\n" "$PREFIX" "${LAST_TOURGUIDE_RADIO:-}"
            printf "%s_NODE_STATE='%s'\n" "$PREFIX" "${NODE_STATE:-ACTIVE}"
            echo ""
        } >> "$REGISTRY_TMP"

        # Track claimed IPs
        if [[ -n "$IPV4_ADDRESS" ]]; then
            echo "${IPV4_ADDRESS},${MAC_ADDRESS}" >> "$CLAIMED_IPS_TMP"
        fi
    fi

    # Clear variables for next iteration
    unset HOSTNAME MAC_ADDRESS MAC_ADDRESSES IPV4_ADDRESS SYNCTHING_ID TQ_AVERAGE \
        IS_INTERNET_GATEWAY IS_NTP_SERVER IS_MUMBLE_SERVER IS_TAK_SERVER IS_MEDIAMTX_SERVER \
        UPTIME_SECONDS BATTERY_PERCENTAGE CPU_LOAD_AVERAGE \
        DATA_CHANNEL_2_4 DATA_CHANNEL_5_0 CHANNEL_REPORT_JSON \
        LAST_SEEN_TIMESTAMP IS_IN_LIMP_MODE \
        LAST_TOURGUIDE_TIMESTAMP LAST_TOURGUIDE_RADIO NODE_STATE \
        GPS_LATITUDE GPS_LONGITUDE GPS_ALTITUDE ATAK_USER
done

# Sort and save claimed IPs
sort -u "$CLAIMED_IPS_TMP" > "$CLAIMED_IPS_FILE"
rm "$CLAIMED_IPS_TMP"

# Move registry into place
mv "$REGISTRY_TMP" "$REGISTRY_STATE_FILE"
chmod 644 "$REGISTRY_STATE_FILE"

log "Registry updated with ${#PEER_PAYLOADS[@]} nodes"

exit 0
