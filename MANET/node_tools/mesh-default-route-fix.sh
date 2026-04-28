#!/usr/bin/env bash
set -euo pipefail

BATCTL=/usr/sbin/batctl
REGISTRY=/var/run/mesh_node_registry

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] - DEFAULT-ROUTE-FIX: $*" >&2; }

# Only run on non-gateway (client) nodes — gateway nodes keep their ethernet default route
gw_mode=$("$BATCTL" gw_mode 2>/dev/null | awk '{print $1}' || true)
if [ "$gw_mode" = "server" ] || [ -f /var/run/mesh-gateway.state ]; then
    log "Gateway mode active ($gw_mode); skipping mesh route fix"
    exit 0
fi

# Remove stale service VIP (10.30.2.2 is mediamtx-election VIP, not routable as next-hop)
ip addr del 10.30.2.2/24 dev br0 2>/dev/null || true

# Get local br0 primary IP for use as route src
get_local_ip() {
    ip -4 -o addr show dev br0 2>/dev/null | awk 'NR==1{split($4,a,"/"); print a[1]}'
}

# Resolve gateway IP from batctl gwl + registry
get_gateway_ip() {
    local gw_mac gw_ip
    gw_mac=$("$BATCTL" gwl 2>/dev/null | awk '
        tolower($1) ~ /^([0-9a-f]{2}:){5}[0-9a-f]{2}$/ { print tolower($1); exit }
    ')
    [ -z "$gw_mac" ] && return 1
    [ -f "$REGISTRY" ] || return 1
    gw_ip=$(grep -i "$gw_mac" "$REGISTRY" 2>/dev/null \
        | grep -Eo '10\.[0-9]+\.[0-9]+\.[0-9]+' | head -n1 || true)
    [ -n "$gw_ip" ] && echo "$gw_ip"
}

for i in $(seq 1 30); do
    # Re-attempt VIP removal each iteration (mediamtx-election may re-add it briefly)
    ip addr del 10.30.2.2/24 dev br0 2>/dev/null || true

    if ! ip route show default | grep -q '^default '; then
        gw_ip=$(get_gateway_ip || true)
        local_ip=$(get_local_ip)
        if [ -n "$gw_ip" ] && [ -n "$local_ip" ]; then
            if ping -c 1 -W 1 "$gw_ip" >/dev/null 2>&1; then
                ip route replace default via "$gw_ip" dev br0 src "$local_ip"
                log "Default route set: via $gw_ip dev br0 src $local_ip"
            fi
        fi
    fi

    if ip route show default | grep -q '^default'; then
        log "Default route confirmed, exiting"
        exit 0
    fi

    sleep 2
done

log "Warning: could not establish default route within timeout"
exit 0
