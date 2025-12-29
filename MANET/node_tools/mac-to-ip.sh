#!/bin/bash
# ==============================================================================
# MAC to IP Lookup
# ==============================================================================
# Queries the mesh registry to find the IPv4 address for a given MAC address
# Usage: mac-to-ip.sh <MAC_ADDRESS>
# ==============================================================================

REGISTRY_FILE="/var/run/mesh_node_registry"

# Check for input
if [ $# -eq 0 ]; then
    echo "Usage: $0 <MAC_ADDRESS>"
    echo "Example: $0 aa:bb:cc:dd:ee:ff"
    exit 1
fi

MAC_INPUT="$1"

# Check if registry exists
if [ ! -f "$REGISTRY_FILE" ]; then
    echo "Error: Registry file not found: $REGISTRY_FILE"
    exit 1
fi

# Sanitize MAC address (remove colons for registry lookup)
MAC_SANITIZED=$(echo "$MAC_INPUT" | tr -d ':' | tr '[:lower:]' '[:upper:]')

# Try exact match first (primary MAC)
IPV4_VAR="NODE_${MAC_SANITIZED}_IPV4_ADDRESS"
IPV4=$(grep "^${IPV4_VAR}=" "$REGISTRY_FILE" 2>/dev/null | cut -d'=' -f2 | tr -d "'")

if [ -n "$IPV4" ]; then
    echo "$IPV4"
    exit 0
fi

# If not found as primary MAC, search in MAC_ADDRESSES list
# This handles cases where you query with wlan0/wlan1/end0 MAC instead of br0
while IFS= read -r line; do
    if [[ $line =~ NODE_([0-9A-F]+)_MAC_ADDRESSES=\'([^\']+)\' ]]; then
        NODE_ID="${BASH_REMATCH[1]}"
        MAC_LIST="${BASH_REMATCH[2]}"

        # Check if our MAC is in this node's list
        if echo "$MAC_LIST" | grep -qi "$MAC_INPUT"; then
            # Found it! Get the IPv4 for this node
            IPV4_VAR="NODE_${NODE_ID}_IPV4_ADDRESS"
            IPV4=$(grep "^${IPV4_VAR}=" "$REGISTRY_FILE" 2>/dev/null | cut -d'=' -f2 | tr -d "'")

            if [ -n "$IPV4" ]; then
                echo "$IPV4"
                exit 0
            fi
        fi
    fi
done < "$REGISTRY_FILE"

# Not found
echo "Error: No IPv4 address found for MAC: $MAC_INPUT"
exit 1
