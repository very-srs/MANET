#!/bin/bash
# Run a routine tools update through a direct uplink or mesh gateway.
set -euo pipefail

CONF="${MANET_MESH_CONF:-/etc/mesh.conf}"
UPSTREAM="${MANET_UPSTREAM_FILE:-/var/run/upstream_iface}"
UPDATER="${MANET_UPDATER:-/usr/local/bin/node-update.sh}"

# Prefer the existing direct-uplink rules; mesh clients may have no saved iface.
auto_update_route() {
    local iface
    iface=$(cat "$UPSTREAM" 2>/dev/null) || iface=""
    if [[ "$iface" =~ ^[a-zA-Z0-9_.-]{1,15}$ ]]; then
        case "$iface" in
            lo|br*|bat*) ;;
            *)
                if ip -4 route show default dev "$iface" | grep -q '^default '; then
                    echo direct
                    return 0
                fi
                ;;
        esac
    fi
    # gateway-route-manager is the sole writer of a br0 default route.
    # Require a next hop and exact device token, not a link-only bridge route.
    if ip -4 route show default | grep -qE '^default via ([0-9]{1,3}\.){3}[0-9]{1,3} dev br0([[:space:]]|$)'; then
        echo mesh
        return 0
    fi
    return 1
}

# Accept common true spellings with optional trailing whitespace.
grep -qiE '^auto_update=(y|yes|1|true)[[:space:]]*$' "$CONF" 2>/dev/null || exit 0
# Gateway reconciliation has already checked connectivity. Let the updater's
# bounded HTTPS downloads establish reachability instead of requiring ICMP.
route=$(auto_update_route) || exit 0
if [ ! -f "${MANET_TIME_RUN_DIR:-/run}/initial_time_synced" ]; then
    echo "MANET routine update check deferred until clock sync"
    exit 0
fi
if [ "$route" = mesh ]; then
    delay="${MANET_AUTO_UPDATE_DELAY:-$((RANDOM % 901))}"
    if [[ ! "$delay" =~ ^(0|[1-9][0-9]{0,2})$ ]] || [ "$delay" -gt 900 ]; then
        echo "MANET_AUTO_UPDATE_DELAY must be an integer from 0 to 900" >&2
        exit 1
    fi
    # The service's 45-minute timeout leaves 30 minutes after the longest sleep.
    echo "MANET routine update check through mesh gateway delayed ${delay}s"
    sleep "$delay"
    # Connectivity and opt-in can change during the delay.
    grep -qiE '^auto_update=(y|yes|1|true)[[:space:]]*$' "$CONF" 2>/dev/null || exit 0
    auto_update_route >/dev/null || exit 0
fi
exec "$UPDATER" --routine
