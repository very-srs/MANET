#!/bin/bash

# ==============================================================================
#  Gateway Route Manager
# ==============================================================================
# Monitors batctl gateway selection and automatically updates the system
# default route to point to the currently selected gateway's mesh IP.
# Removes the default route when no gateway is available.
# ==============================================================================

### --- Configuration ---
REGISTRY_FILE="/var/run/mesh_node_registry"
POLL_INTERVAL=10  # Check for gateway changes every N seconds
BATCTL_PATH="/usr/sbin/batctl"
ROUTE_TABLE="main"  # Routing table to update

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

    # Extract MAC address
    echo "$gw_line" | awk '{print $2}'
}

# Look up the IP address for a given MAC in the registry
lookup_gateway_ip() {
    local mac=$1

    if [ ! -f "$REGISTRY_FILE" ]; then
        log "Warning: $REGISTRY_FILE not found"
        return 1
    fi

    # Convert MAC to registry variable format (remove colons)
    local mac_clean=$(echo "$mac" | tr -d ':')
    local var_name="NODE_${mac_clean}_IPV4_ADDRESS"

    # Source registry and get the IP
    local ip=$(source "$REGISTRY_FILE" 2>/dev/null && eval echo "\$$var_name")

    if [ -z "$ip" ]; then
        log "Warning: No IP found for gateway MAC $mac in registry"
        return 1
    fi

    echo "$ip"
}

# Update the default route to point to a specific gateway IP
set_default_route() {
    local gw_ip=$1

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

# Get hostname for a MAC from registry (for logging)
get_hostname_for_mac() {
    local mac=$1
    local mac_clean=$(echo "$mac" | tr -d ':')
    local var_name="NODE_${mac_clean}_HOSTNAME"

    if [ -f "$REGISTRY_FILE" ]; then
        local hostname=$(source "$REGISTRY_FILE" 2>/dev/null && eval echo "\$$var_name")
        if [ -n "$hostname" ]; then
            echo "$hostname"
            return 0
        fi
    fi

    echo "unknown"
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
    # Get the currently selected gateway
    NEW_GW_MAC=$(get_selected_gateway_mac)

    # Check if gateway has changed
    if [ "$NEW_GW_MAC" != "$CURRENT_GW_MAC" ]; then

        if [ -z "$NEW_GW_MAC" ]; then
            # No gateway available
            if [ -n "$CURRENT_GW_MAC" ]; then
                log "Gateway lost: $CURRENT_GW_MAC ($CURRENT_GW_IP)"
                remove_default_route
            fi
            CURRENT_GW_MAC=""
            CURRENT_GW_IP=""

        else
            # New gateway selected
            NEW_GW_IP=$(lookup_gateway_ip "$NEW_GW_MAC")

            if [ -n "$NEW_GW_IP" ]; then
                GW_HOSTNAME=$(get_hostname_for_mac "$NEW_GW_MAC")

                if [ -z "$CURRENT_GW_MAC" ]; then
                    log "Gateway detected: $NEW_GW_MAC ($GW_HOSTNAME) at $NEW_GW_IP"
                else
                    log "Gateway changed: $CURRENT_GW_MAC -> $NEW_GW_MAC ($GW_HOSTNAME) at $NEW_GW_IP"
                fi

                if set_default_route "$NEW_GW_IP"; then
                    CURRENT_GW_MAC="$NEW_GW_MAC"
                    CURRENT_GW_IP="$NEW_GW_IP"
                fi
            else
                log "Gateway MAC found ($NEW_GW_MAC) but IP lookup failed. Will retry."
            fi
        fi
    fi

    sleep "$POLL_INTERVAL"
done
