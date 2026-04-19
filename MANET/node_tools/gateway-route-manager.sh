#!/usr/bin/env bash
set -euo pipefail

POLL_INTERVAL=2
LOCK_FILE=/var/run/gateway-route-manager.lock

exec 200>"$LOCK_FILE"
flock -n 200 || exit 0

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] - GW-ROUTE-MGR: $*"
    echo "$msg" >&2
    echo "$msg" | systemd-cat -t gateway-route-manager
}

get_gateway_mac() {
    batctl gwl 2>/dev/null | awk '
        /^\*/ && tolower($2) ~ /^([0-9a-f]{2}:){5}[0-9a-f]{2}$/ { print tolower($2); exit }
    '
}

resolve_gateway_ip() {
    local mac="${1,,}"
    local ip=""
    [ -f /var/run/mesh_node_registry ] || return 0
    ip="$(grep -i "$mac" /var/run/mesh_node_registry 2>/dev/null | grep -Eo "10\.30\.2\.[0-9]+" | head -n1 || true)"
    [ -n "$ip" ] && printf "%s\n" "$ip"
}

log "Starting Gateway Route Manager (polling every ${POLL_INTERVAL}s)"

lookup_gateway_ip_by_mac() {
    local gw_mac="$1"
    local reg="/var/run/mesh_node_registry"

    [ -f "$reg" ] || return 1

    awk -F"'" -v mac="${gw_mac,,}" '
        BEGIN {
            found=0
            ip=""
            prefix=""
        }
        /_MAC_ADDRESSES=/ {
            this_mac_list=tolower($2)
            if (index(this_mac_list, mac) > 0) {
                found=1
                sub(/_MAC_ADDRESSES=.*/, "", $1)
                prefix=$1
            }
        }
        found && $1 == prefix "_IPV4_ADDRESS=" {
            ip=$2
            print ip
            exit
        }
    ' "$reg"
}

while true; do
    if [ -f /var/run/mesh-gateway.state ]; then
        cur="$(ip route show default | head -n1 || true)"
        log "Local gateway mode active; clearing only mesh-managed default route state"
        if echo "$cur" | grep -q " dev br0 "; then
            ip route del default dev br0 2>/dev/null || true
            log "Removed mesh-managed default route"
        else
            log "Leaving default route unchanged; it is not managed by gateway-route-manager: ${cur:-none}"
        fi
        sleep "$POLL_INTERVAL"
        continue
    fi

    gw_mac="$(get_gateway_mac || true)"
    if [ -z "${gw_mac:-}" ]; then
        sleep "$POLL_INTERVAL"
        continue
    fi

    gw_ip="$(lookup_gateway_ip_by_mac "$gw_mac" || true)"
    if [ -z "${gw_ip:-}" ]; then
        log "Warning: No registry entry found for MAC $gw_mac"
        sleep "$POLL_INTERVAL"
        continue
    fi

    local_ip="$(ip -4 -o addr show dev br0 | awk '{print $4}' | cut -d/ -f1 | head -n1)"
    cur="$(ip route show default | head -n1 || true)"

    if ping -c 1 -W 1 "$gw_ip" >/dev/null 2>&1; then
        if ! echo "$cur" | grep -q "via $gw_ip dev br0"; then
            ip route replace default via "$gw_ip" dev br0 src "$local_ip"
            log "Gateway detected: $gw_mac at $gw_ip"
            log "Default route updated: via $gw_ip src $local_ip"
        fi
    else
        log "Gateway IP $gw_ip not reachable; skipping route install"
    fi

    sleep "$POLL_INTERVAL"
done
