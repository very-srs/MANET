#!/bin/bash
# Quorum Checker
# Determines if node is isolated and should return to lobby
# Exit codes: 0 = stay put, 1 = return to lobby needed, 2 = check unavailable

REGISTRY_STATE_FILE="${REGISTRY_STATE_FILE:-/var/run/mesh_node_registry}"
BATCTL_PATH="${BATCTL_PATH:-/usr/sbin/batctl}"
PEER_COUNTER="$(dirname "${BASH_SOURCE[0]}")/mesh-peer-count.sh"
STALE_NODE_THRESHOLD=600
QUORUM_THRESHOLD=0.5

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - QUORUM: $1" | systemd-cat -t quorum-checker
}

[ ! -f "$REGISTRY_STATE_FILE" ] && exit 0

NOW=$(date +%s)

# Count active nodes
# Freshness is this node's own observation on its boot clock,
# never the peer's timestamp: a peer's clock can be far off and still alive.
read -r UPTIME_NOW _ < "${MESH_UPTIME_FILE:-/proc/uptime}"
UPTIME_NOW=${UPTIME_NOW%.*}
# Age from the boot-clock observation time, so a registry that could not be
# rebuilt keeps aging instead of freezing its peers as fresh.
ACTIVE_ALFRED_COUNT=$(awk -F"['=]" -v now="$UPTIME_NOW" -v stale="$STALE_NODE_THRESHOLD" \
    '/_OBSERVED_AT_UPTIME=/ { if ($3 ~ /^[0-9]+$/ && now - $3 < stale) count++ } END { print count+0 }' \
    "$REGISTRY_STATE_FILE")

# Count shutting down nodes
SHUTTING_DOWN_COUNT=$(grep -c "NODE_STATE='SHUTTING_DOWN'" "$REGISTRY_STATE_FILE" 2>/dev/null)

# Count reachable mesh nodes (originators)
if ! UNIQUE_BATMAN_ORIGINATORS=$("$PEER_COUNTER" --batctl "$BATCTL_PATH"); then
    log "Cannot read BATMAN peers. Deferring quorum check."
    exit 2
fi

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
