#!/usr/bin/env bash
# ==============================================================================
# Mesh Hosts Updater
# ==============================================================================
# Reads /var/run/mesh_node_registry and populates /etc/hosts with
# hostname -> IP mappings for all mesh nodes.
# ==============================================================================

REGISTRY_FILE="/var/run/mesh_node_registry"
HOSTS_FILE="/etc/hosts"
BEGIN_MARKER="# === BEGIN MESH HOSTS ==="
END_MARKER="# === END MESH HOSTS ==="

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - MESH-HOSTS: $1"
}

if [ ! -s "$REGISTRY_FILE" ]; then
    log "Registry not found or empty, skipping"
    exit 0
fi

source "$REGISTRY_FILE"

# Build the new mesh hosts block
MESH_BLOCK="${BEGIN_MARKER}"$'\n'

ENTRY_COUNT=0
while IFS= read -r line; do
    if [[ $line =~ ^NODE_([0-9A-Fa-f]+)_HOSTNAME= ]]; then
        NODE_ID="${BASH_REMATCH[1]}"
        
        HOSTNAME_VAR="NODE_${NODE_ID}_HOSTNAME"
        IPV4_VAR="NODE_${NODE_ID}_IPV4_ADDRESS"
        
        NODE_HOSTNAME="${!HOSTNAME_VAR}"
        NODE_IPV4="${!IPV4_VAR}"
        
        if [[ -n "$NODE_HOSTNAME" && -n "$NODE_IPV4" ]]; then
            MESH_BLOCK+="${NODE_IPV4}    ${NODE_HOSTNAME} ${NODE_HOSTNAME}.local"$'\n'
            ((ENTRY_COUNT++))
        fi
    fi
done < "$REGISTRY_FILE"

MESH_BLOCK+="${END_MARKER}"

# Remove old block and insert new one
if grep -q "$BEGIN_MARKER" "$HOSTS_FILE" 2>/dev/null; then
    # Replace existing block
    TMPFILE=$(mktemp)
    awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" -v block="$MESH_BLOCK" '
        $0 == begin { skip=1; printed=1; print block; next }
        $0 == end   { skip=0; next }
        !skip       { print }
    ' "$HOSTS_FILE" > "$TMPFILE"
    mv "$TMPFILE" "$HOSTS_FILE"
else
    # Append new block
    echo "" >> "$HOSTS_FILE"
    echo "$MESH_BLOCK" >> "$HOSTS_FILE"
fi

chmod 644 /etc/hosts

log "Updated ${ENTRY_COUNT} mesh host entries"
