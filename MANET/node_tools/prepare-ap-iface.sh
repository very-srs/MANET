#!/bin/bash
# Run before each hostapd start, including a restart after wired/mesh use.
# Also used by the optional preparation unit; never tear down a running AP.
set -euo pipefail

exec 9>/run/manet-ap-prepare.lock
flock -w 10 9

if systemctl is-active --quiet hostapd.service; then
    exit 0
fi

IFS= read -r iface < /var/lib/ap_interface
if [[ ! "$iface" =~ ^[[:alnum:]_.:-]{1,15}$ ]]; then
    echo "Invalid or missing AP interface" >&2
    exit 1
fi

timeout 5 /usr/local/bin/unblock-wifi-rfkill.sh
# A supplicant may still own this radio after a wired-EUD -> AP transition.
# Stop it before changing the mode, and propagate preparation failures so
# hostapd cannot claim a successful start on an unprepared interface.
timeout 10 systemctl stop "wpa_supplicant@${iface}.service"
timeout 5 ip link set "$iface" down
timeout 5 ip link set "$iface" nomaster
timeout 5 iw dev "$iface" set type managed
timeout 5 ip link set "$iface" up
