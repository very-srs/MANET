#!/usr/bin/env bash
# ==============================================================================
# Mesh Registry Builder
# ==============================================================================
# Builds /var/run/mesh_node_registry from Alfred. This is the only place peer
# state comes from — nothing in this system queries another node directly.
#
# Peer data arrives as two Alfred types, joined on the record key:
#
#   type 67  identity   hostname, MACs, syncthing ID, chunk. Republished
#                       slowly, so it is cached across cycles: a node whose
#                       identity record has not been refreshed yet keeps the
#                       values from the previous registry rather than blanking.
#   type 68  telemetry  everything volatile, refreshed every cycle.
#
# Alfred stamps each record with the publishing node's MAC (it runs `-i br0`).
# That key is the join column AND the node's primary MAC, which is why the
# identity payload does not carry it.
# ==============================================================================

# --- Configuration ---
ALFRED_IDENTITY_TYPE=67
ALFRED_DATA_TYPE=68
REGISTRY_STATE_FILE="${MESH_REGISTRY_FILE:-/var/run/mesh_node_registry}"
CLAIMED_CHUNKS_FILE="${MESH_CLAIMED_CHUNKS_FILE:-/tmp/claimed_chunks.txt}"
DECODER_PATH="${MESH_DECODER_PATH:-/usr/local/bin/decoder.py}"
STALE_AFTER_SECONDS="${MESH_REGISTRY_STALE_AFTER:-300}"

# --- Helper Functions ---
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - REGISTRY: $1"
}

# Alfred prints one record per line as:  { "aa:bb:cc:dd:ee:ff", "<base64>" },
# Emit "<mac> <base64>" so the key travels with the payload.
alfred_records() {
    alfred -r "$1" 2>/dev/null |
        sed -n 's/^[[:space:]]*{[[:space:]]*"\([0-9a-fA-F:]\{17\}\)"[[:space:]]*,[[:space:]]*"\([^"]*\)".*/\1 \2/p'
}

# Single-quote a value for the registry file. Network-sourced strings land in
# here, so an embedded quote must not be able to end the assignment.
shell_escape() {
    printf '%s' "${1//\'/\'\\\'\'}"
}

# Pull one already-known field out of the registry we wrote last time.
prev_value() {
    [ -f "$REGISTRY_STATE_FILE" ] || return 0
    sed -n "s/^$1_$2='\(.*\)'$/\1/p" "$REGISTRY_STATE_FILE" | head -1
}

# --- Main Logic ---
NOW=$(date +%s)

declare -A IDENTITY_B64=()
declare -A TELEMETRY_B64=()

while read -r _mac _payload; do
    [ -n "$_mac" ] && [ -n "$_payload" ] && IDENTITY_B64[${_mac,,}]="$_payload"
done < <(alfred_records "$ALFRED_IDENTITY_TYPE")

while read -r _mac _payload; do
    [ -n "$_mac" ] && [ -n "$_payload" ] && TELEMETRY_B64[${_mac,,}]="$_payload"
done < <(alfred_records "$ALFRED_DATA_TYPE")

log "Found ${#TELEMETRY_B64[@]} telemetry and ${#IDENTITY_B64[@]} identity payloads from Alfred"

REGISTRY_TMP=$(mktemp)
CLAIMED_CHUNKS_TMP=$(mktemp)

echo "# Mesh Node Registry - Generated $(date)" > "$REGISTRY_TMP"
echo "# Sourced by other scripts to get network state." >> "$REGISTRY_TMP"
echo "" >> "$REGISTRY_TMP"

NODE_COUNT=0

for NODE_MAC in "${!TELEMETRY_B64[@]}"; do
    declare -A F=()

    # --- Telemetry (required) ---
    DECODED=$("$DECODER_PATH" telemetry "${TELEMETRY_B64[$NODE_MAC]}" 2>&1)
    if [ $? -ne 0 ] || [ -z "$DECODED" ]; then
        log "Warning: telemetry decode failed for $NODE_MAC"
        continue
    fi

    # Parse assignments without eval — this is network data.
    while IFS= read -r _line; do
        _varname="${_line%%=*}"
        _val="${_line#*=}"
        _val="${_val#\'}"
        _val="${_val%\'}"
        F["$_varname"]="$_val"
    done < <(grep -E "^[A-Z0-9_]+=" <<< "$DECODED")

    PREFIX="NODE_$(tr -d ':' <<< "$NODE_MAC")"

    # --- Identity (cached when this cycle's copy is missing) ---
    if [ -n "${IDENTITY_B64[$NODE_MAC]}" ]; then
        DECODED_ID=$("$DECODER_PATH" identity "${IDENTITY_B64[$NODE_MAC]}" \
                     --node-mac "$NODE_MAC" 2>&1)
        if [ $? -eq 0 ] && [ -n "$DECODED_ID" ]; then
            while IFS= read -r _line; do
                _varname="${_line%%=*}"
                _val="${_line#*=}"
                _val="${_val#\'}"
                _val="${_val%\'}"
                F["$_varname"]="$_val"
            done < <(grep -E "^[A-Z0-9_]+=" <<< "$DECODED_ID")
        else
            log "Warning: identity decode failed for $NODE_MAC"
        fi
    fi
    if [ -z "${F[HOSTNAME]}" ]; then
        for _k in HOSTNAME MAC_ADDRESSES IPV4_ADDRESS IPV4_CHUNK SYNCTHING_ID; do
            F["$_k"]=$(prev_value "$PREFIX" "$_k")
        done
        [ -n "${F[HOSTNAME]}" ] && log "Using cached identity for $NODE_MAC"
    fi
    F[MAC_ADDRESS]="$NODE_MAC"
    [ -z "${F[MAC_ADDRESSES]}" ] && F[MAC_ADDRESSES]="$NODE_MAC"

    # --- Freshness ---
    EFFECTIVE_NODE_STATE="${F[NODE_STATE]:-ACTIVE}"
    if [[ "${F[LAST_SEEN_TIMESTAMP]:-0}" =~ ^[0-9]+$ ]] && [ "${F[LAST_SEEN_TIMESTAMP]:-0}" -gt 0 ]; then
        if [ $((NOW - ${F[LAST_SEEN_TIMESTAMP]})) -gt "$STALE_AFTER_SECONDS" ]; then
            EFFECTIVE_NODE_STATE="STALE"
        fi
    fi

    {
        for KEY in HOSTNAME MAC_ADDRESS MAC_ADDRESSES IPV4_ADDRESS IPV4_CHUNK \
                   SYNCTHING_ID MEAN_THROUGHPUT_MBPS GATEWAY_IFACE IS_NTP_SERVER \
                   IS_MUMBLE_SERVER IS_TAK_SERVER IS_MEDIAMTX_SERVER \
                   UPTIME_SECONDS BATTERY_PERCENTAGE CPU_LOAD_AVERAGE \
                   GPS_LATITUDE GPS_LONGITUDE GPS_ALTITUDE ATAK_USER \
                   DATA_CHANNEL_2_4 DATA_CHANNEL_5_0 CHANNEL_REPORT_JSON \
                   LAST_SEEN_TIMESTAMP IS_IN_LIMP_MODE \
                   LAST_TOURGUIDE_TIMESTAMP LAST_TOURGUIDE_RADIO \
                   CONFIG_ACK_VERSION HALOW_TX_MCS HALOW_RX_MCS HALOW_MCS_PEER \
                   WIFI_24_TX_MCS WIFI_24_RX_MCS WIFI_5_TX_MCS WIFI_5_RX_MCS \
                   INTERFACES_JSON EUD_MODE AP_SSID EUD_COUNT; do
            printf "%s_%s='%s'\n" "$PREFIX" "$KEY" "$(shell_escape "${F[$KEY]}")"
        done
        # IS_GATEWAY keeps its historic name; consumers grep for it.
        printf "%s_IS_GATEWAY='%s'\n" "$PREFIX" "$(shell_escape "${F[IS_INTERNET_GATEWAY]}")"
        printf "%s_NODE_STATE='%s'\n" "$PREFIX" "$EFFECTIVE_NODE_STATE"
        printf "%s_LAST_REGISTRY_UPDATE='%s'\n" "$PREFIX" "$NOW"
        echo ""
    } >> "$REGISTRY_TMP"

    if [[ "$EFFECTIVE_NODE_STATE" == "ACTIVE" && -n "${F[IPV4_CHUNK]}" && "${F[IPV4_CHUNK]}" != "0" ]]; then
        echo "${F[IPV4_CHUNK]},${NODE_MAC}" >> "$CLAIMED_CHUNKS_TMP"
    fi

    NODE_COUNT=$((NODE_COUNT + 1))
    unset F
done

sort -u "$CLAIMED_CHUNKS_TMP" > "$CLAIMED_CHUNKS_FILE"
rm "$CLAIMED_CHUNKS_TMP"

mv "$REGISTRY_TMP" "$REGISTRY_STATE_FILE"
chmod 644 "$REGISTRY_STATE_FILE"

log "Registry updated with $NODE_COUNT nodes"

exit 0
