#!/bin/bash
# Run a routine tools update when enabled and a direct uplink has a route.
set -euo pipefail

CONF="${MANET_MESH_CONF:-/etc/mesh.conf}"
UPSTREAM="${MANET_UPSTREAM_FILE:-/var/run/upstream_iface}"
UPDATER="${MANET_UPDATER:-/usr/local/bin/node-update.sh}"

# Accept common true spellings with optional trailing whitespace.
grep -qiE '^auto_update=(y|yes|1|true)[[:space:]]*$' "$CONF" 2>/dev/null || exit 0
iface=$(cat "$UPSTREAM" 2>/dev/null) || exit 0
# Limit the saved interface to a Linux-sized name before route lookup.
[[ "$iface" =~ ^[a-zA-Z0-9_.-]{1,15}$ ]] || exit 0
case "$iface" in lo|br*|bat*) exit 0 ;; esac
# Gateway reconciliation has already checked connectivity. Let the updater's
# bounded HTTPS downloads establish reachability instead of requiring ICMP.
ip -4 route show default dev "$iface" | grep -q '^default ' || exit 0
exec "$UPDATER" --routine
