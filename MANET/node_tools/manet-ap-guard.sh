#!/bin/bash
# ==============================================================================
# AP interface guard for wpa_supplicant@<iface>.service
# ==============================================================================
# Answers one question, for one interface: may a mesh supplicant start on it
# right now?
#
# The AP radio needs a mesh supplicant config on disk. In wired EUD mode it is
# always a mesh interface; in auto mode it joins the mesh whenever an EUD is
# plugged into Ethernet, and goes back to being an AP when that EUD leaves.
# Only ethernet-autodetect makes that call, and it stops hostapd before it
# does. Every other caller starting a supplicant on that radio is a mistake:
# twelve places in the tree restart wpa_supplicant@<iface> from interface lists
# assembled in different ways, and any of them landing on the AP radio while
# hostapd holds it produces a silent failure - the mesh join returns -95, and
# the supplicant's teardown deinits the netdev out from under hostapd, which
# stays "active" over a dead BSS and logs nothing.
#
# Installed as an ExecCondition on the templated unit, so the check applies to
# every caller rather than to whichever call site was remembered.
#
# Exit 0  - allowed to start (not the AP radio, or hostapd is not holding it)
# Exit 1  - skip: this radio is currently an AP
# ==============================================================================

IFACE="$1"
[ -n "$IFACE" ] || exit 0

AP_IFACE="$(cat /var/lib/ap_interface 2>/dev/null || true)"
[ -n "$AP_IFACE" ] || exit 0
[ "$IFACE" = "$AP_IFACE" ] || exit 0

# Not merely "hostapd is running" - it may be starting, or holding another
# radio. The interface itself is the evidence: hostapd sets an SSID on the
# netdev it serves, and a released one keeps "type AP" without one.
systemctl is-active --quiet hostapd.service || exit 0
/usr/sbin/iw dev "$IFACE" info 2>/dev/null | awk '
    $1 == "type" { t = $2 }
    $1 == "ssid" { s = 1 }
    END { exit !(t == "AP" && s) }
' || exit 0

echo "manet-ap-guard: $IFACE is serving as an AP; not starting a mesh supplicant on it" >&2
exit 1
