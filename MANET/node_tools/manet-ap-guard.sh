#!/bin/bash
# AP interface guard for wpa_supplicant@<iface>.service
# Prevent a mesh supplicant from taking a radio currently used by hostapd.
#
# The AP radio needs a mesh supplicant config on disk. In wired EUD mode it is
# always a mesh interface; in auto mode it joins the mesh whenever an EUD is
# plugged into Ethernet, and goes back to being an AP when that EUD leaves.
# ethernet-autodetect stops hostapd before making that change. Starting a mesh
# supplicant while hostapd holds the radio fails the join with -95; the failed
# supplicant then tears down the netdev, leaving hostapd active but unable to
# serve clients.
#
# Installed as an ExecCondition on the templated unit, so the check applies to
# every caller that starts or restarts a mesh supplicant.
#
# Exit 0  - allowed to start (not the AP radio, or hostapd is not holding it)
# Exit 1  - skip: this radio is currently an AP

IFACE="$1"
[ -n "$IFACE" ] || exit 0

AP_IFACE="$(cat /var/lib/ap_interface 2>/dev/null || true)"
[ -n "$AP_IFACE" ] || exit 0
[ "$IFACE" = "$AP_IFACE" ] || exit 0

# Check which radio hostapd holds. It sets an SSID on its netdev; a released
# interface can retain "type AP" with no SSID.
systemctl is-active --quiet hostapd.service || exit 0
/usr/sbin/iw dev "$IFACE" info 2>/dev/null | awk '
    $1 == "type" { t = $2 }
    $1 == "ssid" { s = 1 }
    END { exit !(t == "AP" && s) }
' || exit 0

echo "manet-ap-guard: $IFACE is serving as an AP; not starting a mesh supplicant on it" >&2
exit 1
