#!/bin/sh
set -u

IFACE="${1:-}"
[ -n "$IFACE" ] || exit 0
[ -e "/sys/class/net/$IFACE" ] || exit 0

/usr/local/bin/unblock-wifi-rfkill.sh 2>/dev/null || true

driver="$(basename "$(readlink -f "/sys/class/net/$IFACE/device/driver" 2>/dev/null)")"
if [ -z "$driver" ] || [ "$driver" = "." ]; then
    driver="$(ethtool -i "$IFACE" 2>/dev/null | awk -F': ' '$1 == "driver" {print $2; exit}')"
fi

case "$driver" in
    brcmfmac|morse*)
        exit 0
        ;;
esac

# AP candidates may rejoin mesh after a wired EUD connects. Active roles win.
AP_IF="$(cat /var/lib/ap_interface 2>/dev/null)"
if [ "$IFACE" = "$AP_IF" ] && ! grep -Fxq "$IFACE" /var/lib/mesh_if 2>/dev/null; then
    exit 0
fi

iw dev "$IFACE" info 2>/dev/null | grep -q 'type mesh point' && exit 0

ip link set "$IFACE" down 2>/dev/null || true
sleep 1
iw dev "$IFACE" set type mp 2>/dev/null || true
sleep 1

if ! iw dev "$IFACE" info 2>/dev/null | grep -q 'type mesh point'; then
    echo "Warning: $IFACE did not enter mesh point mode before wpa_supplicant" >&2
fi
