#!/usr/bin/env bash
# Mesh Registry Builder
# Builds /var/run/mesh_node_registry from Alfred. This is the only place peer
# state comes from: nothing in this system queries another node directly.
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

# --- Configuration ---
ALFRED_IDENTITY_TYPE=67
ALFRED_DATA_TYPE=68
REGISTRY_STATE_FILE="${MESH_REGISTRY_FILE:-/var/run/mesh_node_registry}"
CLAIMED_CHUNKS_FILE="${MESH_CLAIMED_CHUNKS_FILE:-/tmp/claimed_chunks.txt}"
DECODER_PATH="${MESH_DECODER_PATH:-/usr/local/bin/decoder.py}"
STALE_AFTER_SECONDS="${MESH_REGISTRY_STALE_AFTER:-300}"
# Freshness is judged from when THIS node saw a peer's telemetry change, on
# this node's boot clock. A peer's own timestamp says nothing reliable: a node
# without an RTC can boot minutes or days behind, and it is still alive.
OBSERVED_FILE="${MESH_REGISTRY_OBSERVED_FILE:-/run/manet-registry/observed.tsv}"
UPTIME_FILE="${MESH_UPTIME_FILE:-/proc/uptime}"
# Observations of records Alfred stopped returning are kept this long, so an
# unchanged stale record that reappears cannot restart its freshness. Longer
# than Alfred's own 600 s expiry.
TOMBSTONE_SECONDS="${MESH_REGISTRY_TOMBSTONE_SECONDS:-900}"

# --- Helper Functions ---
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - REGISTRY: $1"
}

# Alfred prints one record per line as:  { "aa:bb:cc:dd:ee:ff", "<base64>" },
# Emit "<mac> <base64>" so the key travels with the payload.
alfred_records() {
    local records
    records=$(timeout 5 alfred -r "$1" 2>/dev/null) || return 1
    sed -n 's/^[[:space:]]*{[[:space:]]*"\([0-9a-fA-F:]\{17\}\)"[[:space:]]*,[[:space:]]*"\([^"]*\)".*/\1 \2/p' <<< "$records"
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
read -r UPTIME_NOW _ < "$UPTIME_FILE"
UPTIME_NOW=${UPTIME_NOW%.*}

# Several processes rebuild the registry. Serialize them so observations and
# the registry are replaced as one step.
mkdir -p "$(dirname "$OBSERVED_FILE")"
exec 9>"$OBSERVED_FILE.lock"
flock -w 10 9 || { log "Registry lock busy; keeping the previous registry"; exit 1; }

# mac -> "<payload sha256> <uptime that payload was first seen> <uptime last present>"
declare -A OBSERVED=()
if [ -f "$OBSERVED_FILE" ]; then
    while read -r _mac _hash _seen _present; do
        [[ "$_seen" =~ ^[0-9]+$ && "$_present" =~ ^[0-9]+$ ]] &&
            OBSERVED[$_mac]="$_hash $_seen $_present"
    done < "$OBSERVED_FILE"
fi

declare -A IDENTITY_B64=()
declare -A TELEMETRY_B64=()

# Read both types successfully before replacing either snapshot. A failed
# Alfred request is not an empty mesh. Process substitution hides that error.
IDENTITY_RECORDS=$(alfred_records "$ALFRED_IDENTITY_TYPE") &&
    TELEMETRY_RECORDS=$(alfred_records "$ALFRED_DATA_TYPE") || {
        log "Alfred read failed; keeping the previous registry and claims"
        exit 1
    }

while read -r _mac _payload; do
    [ -n "$_mac" ] && [ -n "$_payload" ] && IDENTITY_B64[${_mac,,}]="$_payload"
done <<< "$IDENTITY_RECORDS"

while read -r _mac _payload; do
    [ -n "$_mac" ] && [ -n "$_payload" ] && TELEMETRY_B64[${_mac,,}]="$_payload"
done <<< "$TELEMETRY_RECORDS"

log "Found ${#TELEMETRY_B64[@]} telemetry and ${#IDENTITY_B64[@]} identity payloads from Alfred"

REGISTRY_TMP=$(mktemp "$REGISTRY_STATE_FILE.XXXXXX")   # same filesystem: atomic rename
CLAIMED_CHUNKS_TMP=$(mktemp)
OBSERVED_TMP=$(mktemp "$OBSERVED_FILE.XXXXXX")

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

    # Parse assignments without eval: this is network data.
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
        for _k in HOSTNAME MAC_ADDRESSES IPV4_ADDRESS IPV4_CHUNK IPV4_CHUNK_SIZE SYNCTHING_ID; do
            F["$_k"]=$(prev_value "$PREFIX" "$_k")
        done
        [ -n "${F[HOSTNAME]}" ] && log "Using cached identity for $NODE_MAC"
    fi
    F[MAC_ADDRESS]="$NODE_MAC"
    [ -z "${F[MAC_ADDRESSES]}" ] && F[MAC_ADDRESSES]="$NODE_MAC"

    # --- Freshness ---
    # A live node republishes telemetry (it carries a timestamp) at least every
    # few minutes, so its payload keeps changing whatever its clock says. A
    # dead node's record stays byte-identical until Alfred expires it. Only a
    # change moves the observation time; reading the same payload never does.
    PAYLOAD_HASH=$(printf '%s' "${TELEMETRY_B64[$NODE_MAC]}" | sha256sum | cut -d' ' -f1)
    read -r _old_hash _seen _ <<< "${OBSERVED[$NODE_MAC]:-}"
    if [ "$_old_hash" != "$PAYLOAD_HASH" ] || ! [[ "$_seen" =~ ^[0-9]+$ ]]; then
        _seen=$UPTIME_NOW
    fi
    unset "OBSERVED[$NODE_MAC]"
    printf '%s %s %s %s\n' "$NODE_MAC" "$PAYLOAD_HASH" "$_seen" "$UPTIME_NOW" >> "$OBSERVED_TMP"
    OBSERVED_AGE=$((UPTIME_NOW - _seen))
    [ "$OBSERVED_AGE" -ge 0 ] || OBSERVED_AGE=0
    EFFECTIVE_NODE_STATE="${F[NODE_STATE]:-ACTIVE}"
    if [ "$OBSERVED_AGE" -gt "$STALE_AFTER_SECONDS" ]; then
        EFFECTIVE_NODE_STATE="STALE"
    fi

    {
        for KEY in HOSTNAME MAC_ADDRESS MAC_ADDRESSES IPV4_ADDRESS IPV4_CHUNK \
                   IPV4_CHUNK_SIZE SYNCTHING_ID MEAN_THROUGHPUT_MBPS GATEWAY_IFACE IS_NTP_SERVER \
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
        printf "%s_OBSERVED_AGE_SECONDS='%s'\n" "$PREFIX" "$OBSERVED_AGE"
        # Same observation as boot-clock seconds (/proc/uptime), so a reader
        # can age a registry file without trusting any wall clock.
        printf "%s_OBSERVED_AT_UPTIME='%s'\n" "$PREFIX" "$_seen"
        printf "%s_LAST_REGISTRY_UPDATE='%s'\n" "$PREFIX" "$NOW"
        echo ""
    } >> "$REGISTRY_TMP"

    # A claim is an absolute range: the advertised address plus the block
    # size. Chunk numbers are only meaningful to the node that chose them,
    # because block sizes are provisioned per node. Proto3 decodes an unset
    # chunk as zero, so a node awaiting allocation (no address) claims nothing.
    # Line format: chunk,mac,start_int,size (size 0 = not advertised).
    # Validate before shell arithmetic, which would evaluate network text. An
    # advertised address that is not a valid IPv4 still claims something, so
    # it is written without a start and the allocator treats it as incomplete.
    if [[ "$EFFECTIVE_NODE_STATE" == "ACTIVE" && "${F[IPV4_CHUNK]}" =~ ^[0-9]+$ && -n "${F[IPV4_ADDRESS]}" ]]; then
        _start=""
        if [[ "${F[IPV4_ADDRESS]}" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] &&
                (( 10#${BASH_REMATCH[1]} <= 255 && 10#${BASH_REMATCH[2]} <= 255 &&
                   10#${BASH_REMATCH[3]} <= 255 && 10#${BASH_REMATCH[4]} <= 255 )); then
            _start=$(( (10#${BASH_REMATCH[1]} << 24) + (10#${BASH_REMATCH[2]} << 16) +
                       (10#${BASH_REMATCH[3]} << 8) + 10#${BASH_REMATCH[4]} ))
        fi
        _size="${F[IPV4_CHUNK_SIZE]:-0}"
        [[ "$_size" =~ ^[0-9]{1,6}$ ]] || _size=0
        echo "${F[IPV4_CHUNK]},${NODE_MAC},${_start},${_size}" >> "$CLAIMED_CHUNKS_TMP"
    fi

    NODE_COUNT=$((NODE_COUNT + 1))
    unset F
done

# Allocators read claims without the builder lock: publish by rename so a
# reader sees the previous snapshot or the new one, never an empty file.
CLAIMS_SORTED=$(mktemp "$CLAIMED_CHUNKS_FILE.XXXXXX")
sort -u "$CLAIMED_CHUNKS_TMP" > "$CLAIMS_SORTED"
chmod 644 "$CLAIMS_SORTED"
mv "$CLAIMS_SORTED" "$CLAIMED_CHUNKS_FILE"
rm "$CLAIMED_CHUNKS_TMP"

chmod 644 "$REGISTRY_TMP"
mv "$REGISTRY_TMP" "$REGISTRY_STATE_FILE"
# Keep recently absent records as tombstones; forget them after that.
for _mac in "${!OBSERVED[@]}"; do
    read -r _hash _seen _present <<< "${OBSERVED[$_mac]}"
    if [ $((UPTIME_NOW - _present)) -le "$TOMBSTONE_SECONDS" ]; then
        printf '%s %s %s %s\n' "$_mac" "$_hash" "$_seen" "$_present" >> "$OBSERVED_TMP"
    fi
done
mv "$OBSERVED_TMP" "$OBSERVED_FILE"

log "Registry updated with $NODE_COUNT nodes"

exit 0
