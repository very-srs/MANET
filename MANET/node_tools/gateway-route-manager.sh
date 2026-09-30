#!/usr/bin/env bash
set -euo pipefail

POLL_INTERVAL=10
STARTUP_POLL_INTERVAL=1
# Bound the extra polling when no gateway exists. /proc/uptime is unaffected
# by the large clock corrections common on nodes without an RTC.
read -r started _ < /proc/uptime
FAST_POLL_UNTIL=$(( ${started%.*} + 90 ))
LOCK_FILE=/var/run/gateway-route-manager.lock

exec 200>"$LOCK_FILE"
flock -n 200 || exit 0

# The unit runs this with StandardError=journal and
# SyslogIdentifier=gateway-route-manager, so stderr already lands in the
# journal under the right tag. Piping to systemd-cat as well logged every line
# twice and forked an extra process per line. Run by hand, stderr goes to the
# terminal, which is what you want there.
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] - GW-ROUTE-MGR: $*" >&2
}

poll_wait() {
    local uptime unused
    read -r uptime unused < /proc/uptime
    if [ "${1:-pending}" = pending ] && [ "${uptime%.*}" -lt "$FAST_POLL_UNTIL" ]; then
        sleep "$STARTUP_POLL_INTERVAL"
    else
        sleep "$POLL_INTERVAL"
    fi
}

get_gateway_mac() {
    batctl gwl 2>/dev/null | awk '
        $1 == "*" && tolower($2) ~ /^([0-9a-f]{2}:){5}[0-9a-f]{2}$/ { print tolower($2); exit }
    '
}

resolve_gateway_ip() {
    local mac="${1,,}"
    local ip=""
    [ -f /var/run/mesh_node_registry ] || return 0
    ip="$(grep -i "$mac" /var/run/mesh_node_registry 2>/dev/null | grep -Eo "10\.30\.2\.[0-9]+" | head -n1 || true)"
    [ -n "$ip" ] && printf "%s\n" "$ip"
}

log "Starting Gateway Route Manager (pending startup ${STARTUP_POLL_INTERVAL}s, steady ${POLL_INTERVAL}s)"

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
    cur="$(ip route show default | head -n1 || true)"
    if [ -f /var/run/mesh-gateway.state ]; then
        # Only log when actually removing a route: this branch runs every
        # poll cycle while in gateway mode, and log() forks date.
        if [[ " $cur " == *" dev br0 "* ]]; then
            ip route del default dev br0 2>/dev/null || true
            log "Local gateway mode active; removed mesh-managed default route"
        fi
        poll_wait ready
        continue
    fi

    # A dispatcher may not have written its gateway marker yet. Preserve an
    # existing local uplink route during that transition too.
    if [ -n "$cur" ] && [[ " $cur " != *" dev br0 "* ]]; then
        poll_wait ready
        continue
    fi

    gw_mac="$(get_gateway_mac || true)"
    if [ -z "${gw_mac:-}" ]; then
        # Nobody is announcing a gateway any more. A default route installed on
        # an earlier pass now points at a node that has stopped NATing, so
        # traffic black-holes instead of visibly failing: withdraw it. Only
        # br0 routes are ours; a local uplink route lives on the ethernet iface
        # and must be left alone.
        if [[ " $cur " == *" dev br0 "* ]]; then
            ip route del default dev br0 2>/dev/null || true
            log "No mesh gateway announced; removed stale default route ($cur)"
        fi
        poll_wait
        continue
    fi

    gw_ip="$(lookup_gateway_ip_by_mac "$gw_mac" || true)"
    if [ -z "${gw_ip:-}" ]; then
        log "Warning: No registry entry found for MAC $gw_mac"
        poll_wait
        continue
    fi

    # The first address on br0 may be a service VIP or the EUD gateway alias.
    local_ip="$(python3 /usr/local/bin/manet_node_ipv4.py br0 2>/dev/null || true)"
    if [ -z "$local_ip" ]; then
        log "br0 has no IPv4 address yet; skipping route install"
        poll_wait
        continue
    fi

    route_ready=false
    if ping -c 1 -W 1 "$gw_ip" >/dev/null 2>&1; then
        if [[ " $cur " == *" via $gw_ip dev br0 "* && " $cur " == *" src $local_ip "* ]]; then
            route_ready=true
        elif ip route replace default via "$gw_ip" dev br0 src "$local_ip"; then
            route_ready=true
            log "Gateway detected: $gw_mac at $gw_ip"
            log "Default route updated: via $gw_ip src $local_ip"
        else
            log "Route installation failed; retrying"
        fi
    else
        log "Gateway IP $gw_ip not reachable; skipping route install"
    fi

    if [ "$route_ready" = true ]; then
        poll_wait ready
    else
        poll_wait
    fi
done
