#!/bin/bash
# MAC to IP Lookup
# Queries the mesh registry to find the IPv4 address for a given MAC address
# Usage: mac-to-ip.sh <MAC_ADDRESS>

REGISTRY_FILE="/var/run/mesh_node_registry"

# Print the required MAC argument to stderr.
usage() {
    printf '%s\n' 'usage: mac-to-ip.sh MAC_ADDRESS' >&2
}

[ $# -eq 1 ] || { usage; exit 1; }
MAC_INPUT="$1"
# Require six colon-separated hex octets before using the MAC in patterns.
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

# Search MAC_ADDRESSES for interface MACs such as wlan0/wlan1/end0.
while IFS= read -r line; do
    # Capture the node ID and its quoted interface MAC list from registry data.
    if [[ $line =~ NODE_([0-9A-Fa-f]+)_MAC_ADDRESSES=\'([^\']+)\' ]]; then
        NODE_ID="${BASH_REMATCH[1]}"
        MAC_LIST="${BASH_REMATCH[2]}"

        if printf '%s\n' "$MAC_LIST" | grep -qi "$MAC_INPUT"; then
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
