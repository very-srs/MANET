#!/bin/bash
# Limp Mode Manager
# Manages limp mode entry/exit based on mesh consensus

REGISTRY_STATE_FILE="${MESH_REGISTRY_FILE:-/var/run/mesh_node_registry}"
LIMP_STATE_FILE="${MANET_LIMP_STATE_FILE:-/var/run/mesh_limp_mode.state}"
LIMP_MODE_MIN_DURATION=300 #five minutes
LIMP_MODE_CONSENSUS=0.5
STALE_NODE_THRESHOLD=600

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - LIMP-MODE: $1" | systemd-cat -t limp-mode-manager
}

exec 8>"${MANET_ACS_LOCK_FILE:-/run/channel-election.lock}"
flock -n 8 || exit 0
mesh_iface_24="$(cat /var/lib/mesh_24_if 2>/dev/null || true)"
mesh_iface_5="$(cat /var/lib/mesh_5_if 2>/dev/null || true)"

[ ! -f "$REGISTRY_STATE_FILE" ] && exit 0

# Count active nodes
# Freshness is this node's own observation, not the peer's clock.
read -r UPTIME_NOW _ < "${MESH_UPTIME_FILE:-/proc/uptime}"
UPTIME_NOW=${UPTIME_NOW%.*}
# Age from the boot-clock observation time, so a registry that could not be
# rebuilt keeps aging instead of freezing its peers as fresh.
ACTIVE_ALFRED_COUNT=$(awk -F"['=]" -v now="$UPTIME_NOW" -v stale="$STALE_NODE_THRESHOLD" \
    '/_OBSERVED_AT_UPTIME=/ { if ($3 ~ /^[0-9]+$/ && now - $3 < stale) count++ } END { print count+0 }' \
    "$REGISTRY_STATE_FILE")

[ "$ACTIVE_ALFRED_COUNT" -eq 0 ] && exit 0

# Count nodes reporting limp mode
LIMP_NODE_COUNT=$(grep -c "IS_IN_LIMP_MODE='true'" "$REGISTRY_STATE_FILE" 2>/dev/null)

LIMP_RATIO=$(echo "scale=2; $LIMP_NODE_COUNT / $ACTIVE_ALFRED_COUNT" | bc)

log "Limp mode consensus: $LIMP_NODE_COUNT/$ACTIVE_ALFRED_COUNT ($LIMP_RATIO)"

# Check current state
if [ -f "$LIMP_STATE_FILE" ]; then
    CURRENT_LIMP_STATE="true"
    # Boot-clock seconds, so a time sync cannot cut short or stretch the
    # minimum residence. A value ahead of the boot clock (a wall time from an
    # older version) restarts the residence: the safe direction.
    LIMP_MODE_ENTRY_TIME=$(cat "$LIMP_STATE_FILE")
    if ! [[ "$LIMP_MODE_ENTRY_TIME" =~ ^[0-9]+$ ]] || [ "$LIMP_MODE_ENTRY_TIME" -gt "$UPTIME_NOW" ]; then
        LIMP_MODE_ENTRY_TIME=$UPTIME_NOW
        echo "$UPTIME_NOW" > "$LIMP_STATE_FILE"
    fi
else
    CURRENT_LIMP_STATE="false"
    LIMP_MODE_ENTRY_TIME=0
fi

# Determine action
if (( $(echo "$LIMP_RATIO > $LIMP_MODE_CONSENSUS" | bc -l) )); then
    # Should be in limp mode
    if [ "$CURRENT_LIMP_STATE" == "false" ]; then
        log "ENTERING LIMP MODE (consensus: $LIMP_RATIO)"
        [ -n "$mesh_iface_24" ] && iw dev "$mesh_iface_24" set bitrates legacy-2.4 1 2 5.5 11
        [ -n "$mesh_iface_5" ] && iw dev "$mesh_iface_5" set bitrates legacy-5 6 9 12 18
        echo "$UPTIME_NOW" > "$LIMP_STATE_FILE"
    fi
else
    # Should exit limp mode
    if [ "$CURRENT_LIMP_STATE" == "true" ]; then
        TIME_IN_LIMP=$((UPTIME_NOW - LIMP_MODE_ENTRY_TIME))

        if [ $TIME_IN_LIMP -ge $LIMP_MODE_MIN_DURATION ]; then
            log "EXITING LIMP MODE (consensus: $LIMP_RATIO, duration: ${TIME_IN_LIMP}s)"
            [ -n "$mesh_iface_24" ] && iw dev "$mesh_iface_24" set bitrates
            [ -n "$mesh_iface_5" ] && iw dev "$mesh_iface_5" set bitrates
            rm -f "$LIMP_STATE_FILE"
        else
            log "Consensus lost but maintaining limp mode for minimum duration (${TIME_IN_LIMP}/${LIMP_MODE_MIN_DURATION}s)"
        fi
    fi
fi

exit 0
