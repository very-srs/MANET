#!/bin/bash
# mediamtx-election.sh

# --- Configuration ---
MEDIAMTX_VIP="10.0.0.123" # Your chosen static IPv4 VIP
# Get the CIDR suffix from the main network config
source /etc/mesh_ipv4.conf # Assumes IPV4_MASK="/XX" is defined here
VIP_WITH_MASK="${MEDIAMTX_VIP}${IPV4_MASK}"
CONTROL_IFACE="br0"
REGISTRY_STATE_FILE="/var/run/mesh_node_registry"
MY_MAC=$(cat "/sys/class/net/${CONTROL_IFACE}/address")

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - MEDIAMTX-ELECTION: $1" | systemd-cat -t mediamtx-election
}

# --- Read State & Run Election ---
log "Running MediaMTX election..."
# ... (Your existing logic to source registry and find BEST_CANDIDATE_MAC) ...

# --- Decide and Act ---
if [ -z "$BEST_CANDIDATE_MAC" ]; then
    log "No suitable candidates found."
    # Ensure service is stopped if no winner
    if systemctl is-active --quiet mediamtx.service; then
        log "Stopping local service as no winner was found."
        ip addr del "$VIP_WITH_MASK" dev "$CONTROL_IFACE" 2>/dev/null # Remove VIP if we had it
        systemctl stop mediamtx.service
    fi
elif [ "$MY_MAC" == "$BEST_CANDIDATE_MAC" ]; then
    # --- I AM THE LEADER ---
    # Check if VIP is already assigned (idempotency)
    if ! ip addr show dev "$CONTROL_IFACE" | grep -q "inet $MEDIAMTX_VIP/"; then
         log "Assigning MediaMTX VIP: $MEDIAMTX_VIP"
         ip addr add "$VIP_WITH_MASK" dev "$CONTROL_IFACE"
         # Send Gratuitous ARP immediately after assigning IP
         log "Sending Gratuitous ARP for $MEDIAMTX_VIP"
         arping -c 1 -A -I "$CONTROL_IFACE" "$MEDIAMTX_VIP"
    fi
    # Ensure service is started
    if ! systemctl is-active --quiet mediamtx.service; then
        log "Won election. Starting local MediaMTX service."
        systemctl start mediamtx.service
    else
        log "Won election. MediaMTX service already running."
    fi
else
    # --- I AM NOT THE LEADER ---
    # Ensure service is stopped and VIP is removed
    if systemctl is-active --quiet mediamtx.service; then
        log "Lost election to ${BEST_CANDIDATE_MAC}. Stopping local service."
        systemctl stop mediamtx.service
    fi
    # Check if VIP is assigned before trying to delete
    if ip addr show dev "$CONTROL_IFACE" | grep -q "inet $MEDIAMTX_VIP/"; then
         log "Removing MediaMTX VIP."
         ip addr del "$VIP_WITH_MASK" dev "$CONTROL_IFACE"
    fi
fi
