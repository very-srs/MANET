#!/bin/bash
# ==============================================================================
# Quorum Checker
# ==============================================================================
# Determines if node is isolated and should return to lobby
# Exit codes: 0 = healthy, 1 = return to lobby needed
# ==============================================================================

REGISTRY_STATE_FILE="/var/run/mesh_node_registry"
BATCTL_PATH="/usr/sbin/batctl"
STALE_NODE_THRESHOLD=600
QUORUM_THRESHOLD=0.5

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - QUORUM: $1" | systemd-cat -t quorum-checker
}

[ ! -f "$REGISTRY_STATE_FILE" ] && exit 0

NOW=$(date +%s)

# Count active nodes
ACTIVE_ALFRED_COUNT=$(awk -F"['=]" -v now="$NOW" -v stale="$STALE_NODE_THRESHOLD" \
    '/LAST_SEEN_TIMESTAMP/ { if (now - $3 < stale) count++ } END { print count }' \
    "$REGISTRY_STATE_FILE")

# Count shutting down nodes
SHUTTING_DOWN_COUNT=$(grep -c "NODE_STATE='SHUTTING_DOWN'" "$REGISTRY_STATE_FILE" 2>/dev/null || echo "0")

# Count reachable mesh nodes (originators)
UNIQUE_BATMAN_ORIGINATORS=$("$BATCTL_PATH" o 2>/dev/null | awk 'NR>1 {print $1}' | sort -u | wc -l)

log "Health: Originators=$UNIQUE_BATMAN_ORIGINATORS, Active=$ACTIVE_ALFRED_COUNT, Shutdown=$SHUTTING_DOWN_COUNT"

# Scenario 1: SOLO ISOLATION (critical)
if [ "$UNIQUE_BATMAN_ORIGINATORS" -eq 0 ] && [ "$ACTIVE_ALFRED_COUNT" -gt 2 ]; then
    log "!!! SOLO ISOLATION: Zero originators but $ACTIVE_ALFRED_COUNT active nodes"
    exit 1  # Return to lobby
fi

# Scenario 2: SMALL FUNCTIONAL ISLAND (stay operational)
if [ "$UNIQUE_BATMAN_ORIGINATORS" -ge 2 ] && [ "$UNIQUE_BATMAN_ORIGINATORS" -lt "$((ACTIVE_ALFRED_COUNT / 3))" ]; then
    log "Small island: $UNIQUE_BATMAN_ORIGINATORS originators vs $ACTIVE_ALFRED_COUNT total"
    log "Remaining operational. Partition healing via tourguide."
    exit 0  # Stay put
fi

# Scenario 3: BARELY CONNECTED (risky)
EXPECTED_ACTIVE=$((ACTIVE_ALFRED_COUNT - SHUTTING_DOWN_COUNT))

if [ "$EXPECTED_ACTIVE" -gt 3 ]; then
    QUORUM_MIN=$(echo "$EXPECTED_ACTIVE * $QUORUM_THRESHOLD" | bc | cut -d'.' -f1)

    if [ "$UNIQUE_BATMAN_ORIGINATORS" -lt "$QUORUM_MIN" ]; then
        if [ "$UNIQUE_BATMAN_ORIGINATORS" -ge 2 ]; then
            log "Quorum warning: Expected ~$QUORUM_MIN, have $UNIQUE_BATMAN_ORIGINATORS. Monitoring..."
            exit 0  # Stay put, still functional
        else
            log "Critical: Only $UNIQUE_BATMAN_ORIGINATORS originators. Returning to lobby."
            exit 1  # Return to lobby
        fi
    fi
fi

# Healthy
exit 0
