#!/bin/bash
# MAC to IP Lookup
# Queries the mesh registry to find the IPv4 address for a given MAC address
# Usage: mac-to-ip.sh <MAC_ADDRESS>

REGISTRY_FILE="/var/run/mesh_node_registry"

usage() {
    printf '%s\n' 'usage: mac-to-ip.sh MAC_ADDRESS' >&2
}

[ $# -eq 1 ] || { usage; exit 1; }
MAC_INPUT="$1"
if ! [[ "$MAC_INPUT" =~ ^([[:xdigit:]]{2}:){5}[[:xdigit:]]{2}$ ]]; then
    usage
    exit 1
fi

# Check if registry exists
if [ ! -f "$REGISTRY_FILE" ]; then
    printf '%s\n' "mac-to-ip.sh: registry not found: $REGISTRY_FILE" >&2
    exit 1
fi

# Sanitize MAC address (remove colons for registry lookup)
MAC_SANITIZED=$(printf '%s\n' "$MAC_INPUT" |
    tr -d ':' | tr '[:lower:]' '[:upper:]')

# Try exact match first (primary MAC)
IPV4_VAR="NODE_${MAC_SANITIZED}_IPV4_ADDRESS"
IPV4=$(grep "^${IPV4_VAR}=" "$REGISTRY_FILE" 2>/dev/null | cut -d'=' -f2 | tr -d "'")

if [ -n "$IPV4" ]; then
    printf '%s\n' "$IPV4"
    exit 0
fi

# If not found as primary MAC, search in MAC_ADDRESSES list
# This handles cases where you query with wlan0/wlan1/end0 MAC instead of br0
while IFS= read -r line; do
    if [[ $line =~ NODE_([0-9A-Fa-f]+)_MAC_ADDRESSES=\'([^\']+)\' ]]; then
        NODE_ID="${BASH_REMATCH[1]}"
        MAC_LIST="${BASH_REMATCH[2]}"

        # Check if our MAC is in this node's list
        if printf '%s\n' "$MAC_LIST" | grep -qi "$MAC_INPUT"; then
            # Found it! Get the IPv4 for this node
            IPV4_VAR="NODE_${NODE_ID}_IPV4_ADDRESS"
            IPV4=$(grep "^${IPV4_VAR}=" "$REGISTRY_FILE" 2>/dev/null | cut -d'=' -f2 | tr -d "'")

            if [ -n "$IPV4" ]; then
                printf '%s\n' "$IPV4"
                exit 0
            fi
        fi
    fi
done < "$REGISTRY_FILE"

# Not found
printf '%s\n' "mac-to-ip.sh: no IPv4 address for $MAC_INPUT" >&2
exit 1
