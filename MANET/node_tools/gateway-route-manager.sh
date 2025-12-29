#!/bin/bash
# ==============================================================================
# Gateway Route Manager
# ==============================================================================
# Monitors batctl gateway selection and automatically updates the system
# default route to point to the currently selected gateway's mesh IP.
# Removes the default route when no gateway is available.
# ==============================================================================
### --- Configuration ---
REGISTRY_FILE="/var/run/mesh_node_registry"
POLL_INTERVAL=10 # Check for gateway changes every N seconds
BATCTL_PATH="/usr/sbin/batctl"
ROUTE_TABLE="main" # Routing table to update
### --- State Variables ---
CURRENT_GW_MAC=""
CURRENT_GW_IP=""
### --- Helper Functions ---
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - GW-ROUTE-MGR: $1"
}
# Get the currently selected gateway MAC from batctl
get_selected_gateway_mac() {
    # Look for the line starting with * in batctl gwl output
    local gw_line=$("$BATCTL_PATH" gwl 2>/dev/null | grep '^\*')
   
    if [ -z "$gw_line" ]; then
        echo ""
        return 1
    fi
   
    # Extract MAC address (second field)
    echo "$gw_line" | awk '{print $2}'
}
# Look up the IP address for a given MAC in the registry
lookup_gateway_ip() {
    local gw_mac=$1
    if [ ! -f "$REGISTRY_FILE" ]; then
        log "Warning: Registry file not found: $REGISTRY_FILE"
        return 1
    fi
    # Search for the MAC in the _MAC_ADDRESSES field in the registry
    local matching_line=$(grep -E "_MAC_ADDRESSES='(.*,)?${gw_mac}(,.*)?'" "$REGISTRY_FILE" 2>/dev/null)
    if [ -z "$matching_line" ]; then
        log "Warning: No registry entry found for MAC $gw_mac"
        return 1
    fi
    # Extract the node ID from the matching line
    local node_id=$(echo "$matching_line" | sed 's/NODE_\([^_]*\)_MAC_ADDRESSES.*/\1/')
    if [ -z "$node_id" ]; then
        log "Warning: Could not extract node ID from registry line"
        return 1
    fi
    # Get the IP for this node
    local var_name="NODE_${node_id}_IPV4_ADDRESS"
    local ip=$(source "$REGISTRY_FILE" 2>/dev/null && eval echo "\$$var_name")
    if [ -z "$ip" ]; then
        log "Warning: No IP found for gateway node $node_id"
        return 1
    fi
    echo "$ip"
}
# Get hostname for a MAC from registry (for logging)
get_hostname_for_mac() {
    local gw_mac=$1
    if [ ! -f "$REGISTRY_FILE" ]; then
        echo "unknown"
        return 1
    fi
    # Search for the MAC in the _MAC_ADDRESSES field in the registry
    local matching_line=$(grep -E "_MAC_ADDRESSES='(.*,)?${gw_mac}(,.*)?'" "$REGISTRY_FILE" 2>/dev/null)
    if [ -z "$matching_line" ]; then
        echo "unknown"
        return 1
    fi
    local node_id=$(echo "$matching_line" | sed 's/NODE_\([^_]*\)_MAC_ADDRESSES.*/\1/')
    if [ -z "$node_id" ]; then
        echo "unknown"
        return 1
    fi
    local var_name="NODE_${node_id}_HOSTNAME"
    local hostname=$(source "$REGISTRY_FILE" 2>/dev/null && eval echo "\$$var_name")
    if [ -n "$hostname" ]; then
        echo "$hostname"
    else
        echo "unknown"
    fi
}
# Update the default route to point to a specific gateway IP
set_default_route() {
    local gw_ip=$1
    # Validate IP address format
    if ! echo "$gw_ip" | grep -qE '^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$'; then
        log "Error: Invalid IP address format: $gw_ip"
        return 1
    fi
    # Check if route already exists and points to this gateway
    local current_route=$(ip route show default 2>/dev/null | head -n1)
    if echo "$current_route" | grep -q "via $gw_ip"; then
        # Route already correct, no action needed
        return 0
    fi
    # Replace or add the default route
    if ip route replace default via "$gw_ip" 2>/dev/null; then
        log "Default route updated: via $gw_ip"
        return 0
    else
        log "Error: Failed to set default route via $gw_ip"
        return 1
    fi
}
# Remove the default route
remove_default_route() {
    local current_route=$(ip route show default 2>/dev/null)
    if [ -z "$current_route" ]; then
        # No default route exists, nothing to do
        return 0
    fi
    if ip route del default 2>/dev/null; then
        log "Default route removed (no gateway available)"
        return 0
    else
        log "Warning: Failed to remove default route"
        return 1
    fi
}
### --- Main Loop ---
log "Starting Gateway Route Manager (polling every ${POLL_INTERVAL}s)"
# Initial state check
if ! command -v "$BATCTL_PATH" &> /dev/null; then
    log "Error: batctl not found at $BATCTL_PATH"
    exit 1
fi
# Check if we're in gateway client mode
GW_MODE=$("$BATCTL_PATH" gw_mode 2>/dev/null | awk '{print $1}')
if [ "$GW_MODE" != "client" ]; then
    log "Warning: Node is not in gateway client mode (current: $GW_MODE)"
    log "Run: batctl gw_mode client"
fi
while true; do
    # Get the currently selected gateway MAC (from batctl)
    NEW_GW_MAC=$(get_selected_gateway_mac)
    # Lookup gateway IP from registry by MAC
    if [ -n "$NEW_GW_MAC" ]; then
        NEW_GW_IP=$(lookup_gateway_ip "$NEW_GW_MAC")
    else
        NEW_GW_IP=""
    fi
    # Determine if state has changed
    STATE_CHANGED=false
    if [ -z "$NEW_GW_MAC" ] && [ -n "$CURRENT_GW_MAC" ]; then
        # Gateway disappeared
        STATE_CHANGED=true
        ACTION="lost"
    elif [ -n "$NEW_GW_MAC" ] && [ -z "$CURRENT_GW_MAC" ]; then
        # Gateway appeared
        STATE_CHANGED=true
        ACTION="detected"
    elif [ "$NEW_GW_MAC" != "$CURRENT_GW_MAC" ]; then
        # Gateway changed
        STATE_CHANGED=true
        ACTION="changed"
    elif [ "$NEW_GW_IP" != "$CURRENT_GW_IP" ] && [ -n "$NEW_GW_IP" ]; then
        # Gateway MAC same but IP changed (rare)
        STATE_CHANGED=true
        ACTION="ip_changed"
    fi
    # Handle state changes
    if [ "$STATE_CHANGED" = true ]; then
        if [ -z "$NEW_GW_MAC" ]; then
            # No gateway available
            log "Gateway lost: $CURRENT_GW_MAC ($CURRENT_GW_IP)"
            remove_default_route
            CURRENT_GW_MAC=""
            CURRENT_GW_IP=""
        elif [ -z "$NEW_GW_IP" ]; then
            # Gateway exists but IP not found in registry yet
            log "Gateway detected: $NEW_GW_MAC but IP not yet in registry. Will retry."
            # Don't update CURRENT_GW_MAC yet, so we keep trying
        else
            # Valid gateway with IP
            GW_HOSTNAME=$(get_hostname_for_mac "$NEW_GW_MAC")
            case "$ACTION" in
                "detected")
                    log "Gateway detected: $NEW_GW_MAC ($GW_HOSTNAME) at $NEW_GW_IP"
                    ;;
                "changed")
                    log "Gateway changed: $CURRENT_GW_MAC -> $NEW_GW_MAC ($GW_HOSTNAME) at $NEW_GW_IP"
                    ;;
                "ip_changed")
                    log "Gateway IP changed: $CURRENT_GW_IP -> $NEW_GW_IP ($GW_HOSTNAME)"
                    ;;
            esac
            if set_default_route "$NEW_GW_IP"; then
                CURRENT_GW_MAC="$NEW_GW_MAC"
                CURRENT_GW_IP="$NEW_GW_IP"
            fi
        fi
    fi
    sleep "$POLL_INTERVAL"
done
