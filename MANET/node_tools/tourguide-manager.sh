#!/bin/bash
# ==============================================================================
# Tourguide Manager
# ==============================================================================
# Handles tourguide election, radio hopping, broadcasting, and partition detection
# Called by node-manager.sh during tourguide windows
# ==============================================================================

. "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/mesh-acs-common.sh" || exit 1

CONTROL_IFACE="br0"
ALFRED_HELPER_TYPE=69
ALFRED_AUTH_HELPER_TYPE=75
AGREEMENT_TOOL="${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/mesh-channel-agreement.py"
RENDEZVOUS_TOOL="${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet_rendezvous.py"
REGISTRY_STATE_FILE="/var/run/mesh_node_registry"
WPA_IFACE_2_4=""
WPA_IFACE_5_0=""
WPA_CONF_2_4=""
WPA_CONF_5_0=""
HELPER_STALE_SECONDS=300  # Ignore foreign helper beacons older than this
ENCODER_PATH="/usr/local/bin/encoder.py"
BATCTL_PATH="/usr/sbin/batctl"
PEER_COUNTER="${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/mesh-peer-count.py"
ELECTION_OUTPUT_FILE="/var/run/mesh_channel_election"

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - TOURGUIDE: $1" | systemd-cat -t tourguide-manager
}

elect_tourguide() {
    local peers excluded
    peers=$(python3 "$PEER_COUNTER" --batctl "$BATCTL_PATH" --list) || return 1
    excluded=$(python3 "$AGREEMENT_TOOL" tourguide-exclusions) || return 1
    python3 "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/mesh-tourguide-election.py" \
        --self "$1" --peers "$peers" --registry "$REGISTRY_STATE_FILE" --band "$TOURGUIDE_BAND" --exclude "$excluded"
}

select_tourguide_radio() {
    # Alternate on the GLOBAL tourguide window index, never on this node's own
    # history. Two split partitions can only discover each other if their
    # tourguides hop to the same band in the same window. The previous
    # implementation read this node's LAST_TOURGUIDE_RADIO from the registry,
    # which goes permanently out of phase the moment one side misses a window
    # (elected tourguide was excluded for hosting a service, a restart, a failed
    # hop) -- after that the two partitions hop to opposite bands every window
    # and never meet, silently disabling partition healing. That is the only
    # recovery path a 2-node mesh has: quorum-checker.sh cannot rescue an
    # isolated node below 3 remembered peers.
    #
    # 120 matches should_perform_tourguide's window in node-manager-acs.sh
    # (every even minute). This inherits the wall-clock alignment the rest of
    # the ACS pipeline already depends on; it adds no new time-sync requirement.
    local NOW=$(date +%s)
    local iface="$WPA_IFACE_5_0"
    [ $(( (NOW / 120) % 2 )) -ne 0 ] || iface="$WPA_IFACE_2_4"
    # Skip an unavailable band; substituting another band would break the
    # global rendezvous schedule for mixed single/dual-band partitions.
    radio_iface_enabled "$iface" || return 1
    echo "$iface"
}

hop_to_lobby_frequency() {
    local iface=$1
    local freq=$2
    local conf=$3

    sed -i "s/frequency=.*/frequency=${freq}/" "$conf"
    timeout 3 wpa_cli -i "$iface" reconfigure >/dev/null 2>&1 || return 1

    for i in {1..20}; do
        CURRENT_FREQ=$(timeout 2 iw dev "$iface" info 2>/dev/null | grep -oP 'channel.*\((\K[0-9]+)' || echo "0")
        if [ "$CURRENT_FREQ" == "$freq" ]; then
            break
        fi
        sleep 0.5
    done

    [ "$CURRENT_FREQ" = "$freq" ] || { log "Failed to reach lobby on $iface."; return 1; }

    # Set lobby bitrates
    if [ "$freq" -lt 2500 ]; then
        iw dev "$iface" set bitrates legacy-2.4 1 2 5.5 11
    else
        iw dev "$iface" set bitrates legacy-5 6 9 12 18
    fi
}

hop_to_data_frequency() {
    local iface=$1
    local freq=$2
    local conf=$3

    local landed i
    sed -i "s/frequency=.*/frequency=${freq}/" "$conf" || return 1
    timeout 2 iw dev "$iface" set bitrates
    timeout 3 wpa_cli -i "$iface" reconfigure >/dev/null 2>&1
    for i in {1..20}; do
        landed=$(timeout 2 iw dev "$iface" info 2>/dev/null | grep -oP 'channel.*\((\K[0-9]+)' || true)
        [ "$landed" = "$freq" ] && return 0
        sleep 0.5
    done
    timeout 15 systemctl restart "wpa_supplicant@${iface}.service" || return 1
    for i in {1..20}; do
        landed=$(timeout 2 iw dev "$iface" info 2>/dev/null | grep -oP 'channel.*\((\K[0-9]+)' || true)
        [ "$landed" = "$freq" ] && return 0
        sleep 0.5
    done
    log "Failed to restore $iface to $freq."
    return 1
}

get_partition_size() {
    # Own partition size = unique batman originators + self
    local peers
    peers=$(python3 "$PEER_COUNTER" --batctl "$BATCTL_PATH") || return 1
    echo $((peers + 1))
}

analyze_partition_data() {
    local payloads="$1" original24="$2" original5="$3" original_size="$4"
    local selected new_2_4 new_5_0
    # The helper checks authentication, freshness, support and partition rank.
    # Peers captured before hopping are excluded, not a stale ACTIVE registry.
    [[ "$original_size" =~ ^[1-9][0-9]*$ ]] || { log "Deferring partition comparison: invalid pre-hop size."; return 1; }
    selected=$(printf '%s' "$payloads" | python3 "$AGREEMENT_TOOL" helper-select --stdin \
        --size "$original_size" --exclude "${ORIGINAL_MEMBERS:-}" \
        --current "$(jq -cn --arg a "$original24" --arg b "$original5" \
            '{} + (if $a != "" then {"2.4": ($a|tonumber)} else {} end) + (if $b != "" then {"5": ($b|tonumber)} else {} end)')") || return 1
    [ -n "$selected" ] || return 0
    IFS='|' read -r new_2_4 new_5_0 <<< "$selected"
    cat > "$ELECTION_OUTPUT_FILE" <<-EOF
WINNER_2_4=$new_2_4
WINNER_5_0=$new_5_0
LIMP_MODE=false
PARTITION_MERGE=true
EOF
    log "Fresh authenticated partition wins. Scheduling migration to $selected."
}

# === MAIN EXECUTION ===
acs_clock_ready || { log "Waiting for initial time sync before scheduled tourguide duty."; exit 0; }
exec 9>"${MANET_ACS_LOCK_FILE:-/var/run/channel-election.lock}"
flock -n 9 || { log "Another ACS channel operation is in progress. Skipping tourguide."; exit 1; }
acs_agreement_busy && { log "Channel agreement in progress. Skipping tourguide."; exit 0; }
if python3 "$RENDEZVOUS_TOOL" halow-ready; then
    log "HaLow mesh is ready. Keeping Wi-Fi on data channels for recovery over HaLow."
    exit 0
fi
load_mesh_roles
if ! acs_configs_ready; then
    log "Mesh role files not ready; cannot run tourguide manager."
    exit 1
fi
# Initialize the explicit operating mode before any temporary config write.
# Searching nodes follow the rotation through the agreement daemon instead.
[ "$(acs_discovery_state)" = false ] || exit 0
MY_MAC=$(cat "/sys/class/net/${CONTROL_IFACE}/address")
NOW=$(date +%s)
TOURGUIDE_RADIO=$(select_tourguide_radio) || {
    log "Scheduled tourguide band is absent or disabled. Skipping this window."
    exit 0
}
TOURGUIDE_BAND=5
[ "$TOURGUIDE_RADIO" != "$WPA_IFACE_2_4" ] || TOURGUIDE_BAND=2.4
SLOT=$(python3 "$RENDEZVOUS_TOOL" slot "$NOW") || exit 1
IFS='|' read -r SLOT_BAND LOBBY_FREQ <<< "$SLOT"
[ "$SLOT_BAND" = "$TOURGUIDE_BAND" ] || exit 1
if ! python3 "$RENDEZVOUS_TOOL" permitted "$TOURGUIDE_RADIO" "$LOBBY_FREQ"; then
    log "Scheduled rendezvous frequency $LOBBY_FREQ unavailable. Skipping this slot."
    exit 0
fi

# Read last tourguide state
LAST_TOURGUIDE_TIME=0
LAST_TOURGUIDE_RADIO=""
if [ -f /var/run/tourguide_state ]; then
    source /var/run/tourguide_state
fi

ELECTED_TOURGUIDE=$(elect_tourguide "$MY_MAC") || {
    log "Tourguide election unavailable. Skipping this window."
    exit 1
}

if [ "$ELECTED_TOURGUIDE" != "$MY_MAC" ]; then
    log "Tourguide is $ELECTED_TOURGUIDE. Standing by."
    exit 0
fi

# Refuse a late start: every partition must overlap the same lobby dwell.
[ "$((NOW % 120))" -ge 30 ] && [ "$((NOW % 120))" -lt 50 ] || exit 0
RENDEZVOUS_END=$(( (NOW / 120) * 120 + 75 ))
log "=== I AM TOURGUIDE ==="

HOSTNAME=$(hostname)

# Build helper payload (partition size lets two tourguides meeting in the
# lobby agree on which partition migrates)
MY_PARTITION_SIZE=$(get_partition_size) || {
    log "Cannot read BATMAN peers. Skipping tourguide hop."
    exit 1
}
ORIGINAL_MEMBERS=$(python3 "$AGREEMENT_TOOL" members) || exit 1
ORIGINAL_2_4=$(get_current_freq "$WPA_CONF_2_4")
ORIGINAL_5_0=$(get_current_freq "$WPA_CONF_5_0")
radio_iface_enabled "$WPA_IFACE_2_4" || ORIGINAL_2_4=""
radio_iface_enabled "$WPA_IFACE_5_0" || ORIGINAL_5_0=""
HELPER_ARGS=()
[ -z "$ORIGINAL_2_4" ] || HELPER_ARGS+=(--data-channel-2-4 "$ORIGINAL_2_4")
[ -z "$ORIGINAL_5_0" ] || HELPER_ARGS+=(--data-channel-5-0 "$ORIGINAL_5_0")
HELPER_PAYLOAD=$("$ENCODER_PATH" telemetry "${HELPER_ARGS[@]}" \
    "--partition-size" "$MY_PARTITION_SIZE" \
    "--timestamp" "$NOW" \
    2>/dev/null) || exit 1
[ -n "$HELPER_PAYLOAD" ] || exit 1

# Select the config; the shared absolute schedule already selected the frequency.
if [ "$TOURGUIDE_RADIO" == "$WPA_IFACE_2_4" ]; then
    TOURGUIDE_CONF=$WPA_CONF_2_4
else
    TOURGUIDE_CONF=$WPA_CONF_5_0
fi

DATA_FREQ=$(get_current_freq "$TOURGUIDE_CONF")

CHANNELS_JSON=$(jq -cn --arg a "$ORIGINAL_2_4" --arg b "$ORIGINAL_5_0" \
    '{} + (if $a != "" then {"2.4": ($a|tonumber)} else {} end) + (if $b != "" then {"5": ($b|tonumber)} else {} end)')
# Fail before hopping when the shared admin credential is unavailable.
AUTH_HELPER=$(python3 "$AGREEMENT_TOOL" helper-encode --channels "$CHANNELS_JSON" --size "$MY_PARTITION_SIZE") || exit 1
# Discovery/election/encoding may have been slow. Do not leave for a stale slot
# or start after the common entry window, even if the original call was timely.
# HaLow may have recovered while the election/helper was being prepared.
python3 "$RENDEZVOUS_TOOL" halow-ready && exit 0
LIVE_NOW=$(date +%s)
[ "$((LIVE_NOW / 120))" -eq "$((NOW / 120))" ] && [ "$((LIVE_NOW % 120))" -lt 50 ] || exit 0
restore_data() {
    [ "$DATA_FREQ" = "$LOBBY_FREQ" ] || hop_to_data_frequency "$TOURGUIDE_RADIO" "$DATA_FREQ" "$TOURGUIDE_CONF"
}
trap restore_data EXIT
trap 'exit 1' HUP INT TERM
log "Hopping $TOURGUIDE_RADIO to lobby ($LOBBY_FREQ)..."
TOURGUIDE_DEADLINE=$((SECONDS + 55))
if [ "$DATA_FREQ" != "$LOBBY_FREQ" ]; then
    hop_to_lobby_frequency "$TOURGUIDE_RADIO" "$LOBBY_FREQ" "$TOURGUIDE_CONF" || exit 1
fi
sleep 3

# Refresh the authenticated beacon throughout the common rendezvous interval.
# Alfred replication and manager wakeups can miss a single early broadcast.
while [ "$(date +%s)" -lt "$RENDEZVOUS_END" ] && [ "$SECONDS" -lt "$TOURGUIDE_DEADLINE" ]; do
    AUTH_HELPER=$(python3 "$AGREEMENT_TOOL" helper-encode --channels "$CHANNELS_JSON" --size "$MY_PARTITION_SIZE") || break
    printf '%s' "$AUTH_HELPER" | timeout 2 alfred -s "$ALFRED_AUTH_HELPER_TYPE"
    printf '%s' "$HELPER_PAYLOAD" | timeout 2 alfred -s "$ALFRED_HELPER_TYPE"
    sleep 5
done
OTHER_PARTITION_DATA=$(timeout 2 alfred -r "$ALFRED_AUTH_HELPER_TYPE" 2>/dev/null) || OTHER_PARTITION_DATA=""
if [ -n "$OTHER_PARTITION_DATA" ]; then
    analyze_partition_data "$OTHER_PARTITION_DATA" "$ORIGINAL_2_4" "$ORIGINAL_5_0" "$MY_PARTITION_SIZE"
fi
log "Returning to data channel ($DATA_FREQ)..."
restore_data || { trap - EXIT HUP INT TERM; exit 1; }
trap - EXIT HUP INT TERM

# Save state
cat > /var/run/tourguide_state <<EOF
LAST_TOURGUIDE_TIME=$NOW
LAST_TOURGUIDE_RADIO=$TOURGUIDE_RADIO
EOF

log "Tourguide duty complete."
exit 0
