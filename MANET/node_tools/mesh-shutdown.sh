#!/bin/bash
#
# Graceful mesh node shutdown
# Announces shutdown to mesh before powering off
#

ENCODER_PATH="/usr/local/bin/encoder.py"
ALFRED_DATA_TYPE=68
CONTROL_IFACE="br0"

log() {
    printf '%s\n' "MESH-SHUTDOWN: $1" | systemd-cat -t mesh-shutdown
}

log "=== GRACEFUL MESH SHUTDOWN INITIATED ==="

# Gather current node info
HOSTNAME=$(hostname)
MY_MAC=$(cat "/sys/class/net/${CONTROL_IFACE}/address" 2>/dev/null || echo "")
if [ -z "$MY_MAC" ]; then
    log "ERROR: Cannot read MAC address from $CONTROL_IFACE"
    exit 1
fi

# The shutdown tombstone needs only telemetry. Peers retain the identity
# record, and Alfred supplies the MAC used to match this node in the registry.
TOMBSTONE_PAYLOAD=$("$ENCODER_PATH" telemetry \
    "--timestamp" "$(date +%s)" \
    "--node-state" "SHUTTING_DOWN" \
    2>/dev/null)

if [ -n "$TOMBSTONE_PAYLOAD" ]; then
    log "Broadcasting tombstone announcement..."
    printf '%s' "$TOMBSTONE_PAYLOAD" | alfred -s $ALFRED_DATA_TYPE

    # Repeat twice at 2-second intervals for three announcements total.
    sleep 2
    printf '%s' "$TOMBSTONE_PAYLOAD" | alfred -s $ALFRED_DATA_TYPE
    sleep 2
    printf '%s' "$TOMBSTONE_PAYLOAD" | alfred -s $ALFRED_DATA_TYPE

    log "Tombstone announced. Mesh will ignore our absence."
else
    log "ERROR: Failed to create tombstone payload."
fi

# Allow time for propagation
sleep 3

log "Proceeding with system shutdown..."
# Let systemd continue with shutdown
