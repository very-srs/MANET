#!/bin/bash
# ExecCondition for mesh supplicants: an AP candidate needs an active mesh role.
# This also covers the interval before hostapd finishes starting.
IFACE="${1:-}"
ROLES="${MANET_IFACE_STATE_DIR:-/var/lib}"
[ -n "$IFACE" ] || exit 0
AP_IFACE="$(cat "$ROLES/ap_interface" 2>/dev/null || true)"
[ "$IFACE" = "$AP_IFACE" ] || exit 0
if ! grep -Fxq "$IFACE" "$ROLES/mesh_if" 2>/dev/null; then
    echo "manet-ap-guard: $IFACE is reserved for AP; skipping mesh supplicant" >&2
    exit 1
fi
# Refuse an inconsistent role list while a running hostapd still owns the radio.
if systemctl is-active --quiet hostapd.service; then
    echo "manet-ap-guard: hostapd still owns $IFACE; skipping mesh supplicant" >&2
    exit 1
fi
exit 0
