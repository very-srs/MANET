#!/bin/bash
# ==============================================================================
# Limp Mode Manager
# ==============================================================================
# Manages limp mode entry/exit based on mesh consensus
# ==============================================================================

REGISTRY_STATE_FILE="/var/run/mesh_node_registry"
LIMP_STATE_FILE="/var/run/mesh_limp_mode.state"
LIMP_MODE_MIN_DURATION=300 #five minutes
LIMP_MODE_CONSENSUS=0.5
STALE_NODE_THRESHOLD=600

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - LIMP-MODE: $1" | systemd-cat -t limp-mode-manager
}

[ ! -f "$REGISTRY_STATE_FILE" ] && exit 0

NOW=$(date +%s)

# Count active nodes
ACTIVE_ALFRED_COUNT=$(awk -F"['=]" -v now="$NOW" -v stale="$STALE_NODE_THRESHOLD" \
    '/LAST_SEEN_TIMESTAMP/ { if (now - $3 < stale) count++ } END { print count }' \
    "$REGISTRY_STATE_FILE")

[ "$ACTIVE_ALFRED_COUNT" -eq 0 ] && exit 0

# Count nodes reporting limp mode
LIMP_NODE_COUNT=$(grep -c "IS_IN_LIMP_MODE='true'" "$REGISTRY_STATE_FILE" 2>/dev/null || echo "0")

LIMP_RATIO=$(echo "scale=2; $LIMP_NODE_COUNT / $ACTIVE_ALFRED_COUNT" | bc)

log "Limp mode consensus: $LIMP_NODE_COUNT/$ACTIVE_ALFRED_COUNT ($LIMP_RATIO)"

# Check current state
if [ -f "$LIMP_STATE_FILE" ]; then
    CURRENT_LIMP_STATE="true"
    LIMP_MODE_ENTRY_TIME=$(cat "$LIMP_STATE_FILE")
else
    CURRENT_LIMP_STATE="false"
    LIMP_MODE_ENTRY_TIME=0
fi

# Determine action
if (( $(echo "$LIMP_RATIO > $LIMP_MODE_CONSENSUS" | bc -l) )); then
    # Should be in limp mode
    if [ "$CURRENT_LIMP_STATE" == "false" ]; then
        log "ENTERING LIMP MODE (consensus: $LIMP_RATIO)"
        iw dev wlan0 set bitrates legacy-2.4 1 2 5.5 11
        iw dev wlan1 set bitrates legacy-5 6 9 12 18
        echo "$NOW" > "$LIMP_STATE_FILE"
    fi
else
    # Should exit limp mode
    if [ "$CURRENT_LIMP_STATE" == "true" ]; then
        TIME_IN_LIMP=$((NOW - LIMP_MODE_ENTRY_TIME))

        if [ $TIME_IN_LIMP -ge $LIMP_MODE_MIN_DURATION ]; then
            log "EXITING LIMP MODE (consensus: $LIMP_RATIO, duration: ${TIME_IN_LIMP}s)"
            iw dev wlan0 set bitrates
            iw dev wlan1 set bitrates
            rm -f "$LIMP_STATE_FILE"
        else
            log "Consensus lost but maintaining limp mode for minimum duration (${TIME_IN_LIMP}/${LIMP_MODE_MIN_DURATION}s)"
        fi
    fi
fi

exit 0
