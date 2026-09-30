#!/bin/bash
set -euo pipefail

CONF="${MANET_MESH_CONF:-/etc/mesh.conf}"
UPSTREAM="${MANET_UPSTREAM_FILE:-/var/run/upstream_iface}"
UPDATER="${MANET_UPDATER:-/usr/local/bin/node-update.sh}"

grep -qiE '^auto_update=(y|yes|1|true)[[:space:]]*$' "$CONF" 2>/dev/null || exit 0
iface=$(cat "$UPSTREAM" 2>/dev/null) || exit 0
[[ "$iface" =~ ^[a-zA-Z0-9_.-]{1,15}$ ]] || exit 0
case "$iface" in lo|br*|bat*) exit 0 ;; esac
# Gateway reconciliation has already checked connectivity. Let the updater's
# bounded HTTPS downloads establish reachability instead of requiring ICMP.
ip -4 route show default dev "$iface" | grep -q '^default ' || exit 0
exec "$UPDATER" --routine
