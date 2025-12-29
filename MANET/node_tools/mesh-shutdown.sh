#!/bin/bash
#
# Graceful mesh node shutdown
# Announces shutdown to mesh before powering off
#

ENCODER_PATH="/usr/local/bin/encoder.py"
ALFRED_DATA_TYPE=68
CONTROL_IFACE="br0"

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - MESH-SHUTDOWN: $1" | systemd-cat -t mesh-shutdown
}

log "=== GRACEFUL MESH SHUTDOWN INITIATED ==="

# Gather current node info
HOSTNAME=$(hostname)
MY_MAC=$(cat "/sys/class/net/${CONTROL_IFACE}/address" 2>/dev/null || echo "")
if [ -z "$MY_MAC" ]; then
    log "ERROR: Cannot read MAC address from $CONTROL_IFACE"
    exit 1
fi

ALL_MACS=("$MY_MAC")
for iface in wlan0 wlan1 end0; do
    if [ -d "/sys/class/net/$iface" ]; then
        MAC=$(cat "/sys/class/net/$iface/address" 2>/dev/null)
        [ -n "$MAC" ] && ALL_MACS+=("$MAC")
    fi
done

CURRENT_IPV4=$(ip addr show dev "$CONTROL_IFACE" | grep -oP 'inet \K[\d.]+' | head -1)
SYNCTHING_ID=$(runuser -u radio -- syncthing --device-id 2>/dev/null || echo "")

# Build tombstone payload
TOMBSTONE_PAYLOAD=$("$ENCODER_PATH" \
    "--hostname" "$HOSTNAME" \
    "--mac-addresses" "${ALL_MACS[@]}" \
    "--ipv4-address" "${CURRENT_IPV4:-}" \
    "--syncthing-id" "${SYNCTHING_ID:-}" \
    "--timestamp" "$(date +%s)" \
    "--node-state" "SHUTTING_DOWN" \
    2>/dev/null)

if [ -n "$TOMBSTONE_PAYLOAD" ]; then
    log "Broadcasting tombstone announcement..."
    echo -n "$TOMBSTONE_PAYLOAD" | alfred -s $ALFRED_DATA_TYPE

    # Broadcast 3 times over 5 seconds for reliability
    sleep 2
    echo -n "$TOMBSTONE_PAYLOAD" | alfred -s $ALFRED_DATA_TYPE
    sleep 2
    echo -n "$TOMBSTONE_PAYLOAD" | alfred -s $ALFRED_DATA_TYPE

    log "Tombstone announced. Mesh will ignore our absence."
else
    log "ERROR: Failed to create tombstone payload."
fi

# Allow time for propagation
sleep 3

log "Proceeding with system shutdown..."
# Let systemd continue with shutdown
