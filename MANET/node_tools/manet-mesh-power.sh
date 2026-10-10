#!/bin/bash
# Request 30 dBm on Wi-Fi and HaLow mesh radios. Firmware and hardware apply
# their own limits. Driver readback is not an RF measurement, and a lower
# reported value is not an error. The EUD AP keeps its own low power.
# Hold the manet_ap_mesh.py channel lock while reading roles and types so
# the radio cannot become an AP before the request. Reject PHYs with another
# active interface, matching manet_ap_mesh's PHY power writer.
# Never reload drivers: live reload can wedge the USB HaLow card.

ROLES_DIR="${MANET_IFACE_STATE_DIR:-/var/lib}"
SYS_NET="${MANET_SYS_NET:-/sys/class/net}"
LOCK_FILE="${MANET_ACS_LOCK_FILE:-/run/channel-election.lock}"
LOCK_WAIT="${MANET_LOCK_WAIT:-10}"
# iw accepts hundredths of dBm, so 3000 requests 30 dBm.
REQUEST_MBM=3000
READBACK_ATTEMPTS=10

# Bound iw to 10 seconds by default, then force termination after 2 more.
IW_TIMEOUT="${MANET_IW_TIMEOUT:-10}"

failures=()
# Append one diagnostic to the failures array for the final error report.
fail() { failures+=("$1"); }

# Every iw call is bounded: a wedged one must not hold the channel lock.
iw_bounded() { timeout --kill-after=2 "$IW_TIMEOUT" iw "$@"; }

# Extract the first signed txpower value in dBm from supplied iw output.
reported_power() {
    sed -n 's/^[[:space:]]*txpower \(-\{0,1\}[0-9][0-9.]*\) dBm.*/\1/p' <<< "$1" | head -n 1
}

exec 9>>"$LOCK_FILE" || {
    printf '%s\n' "ERROR: mesh radio power: cannot open $LOCK_FILE" >&2
    exit 1
}
flock -w "$LOCK_WAIT" 9 || { echo "ERROR: mesh radio power: radio transition busy" >&2; exit 1; }

ifaces=()
for role in mesh_if halow_if; do
    [ -f "$ROLES_DIR/$role" ] || continue
    for iface in $(cat "$ROLES_DIR/$role"); do
        if ! [[ "$iface" =~ ^[A-Za-z0-9_.-]{1,15}$ ]]; then
            printf '%s\n' \
                "ERROR: mesh radio power: invalid interface name in $role" >&2
            exit 1
        fi
        [[ " ${ifaces[*]} " == *" $iface "* ]] || ifaces+=("$iface")
    done
done

for iface in "${ifaces[@]}"; do
    if [ ! -e "$SYS_NET/$iface" ]; then
        fail "$iface: interface not present"
        continue
    fi
    if ! info=$(iw_bounded dev "$iface" info 2>&1); then
        fail "$iface: cannot read interface"
        continue
    fi
    if grep -qE '^[[:space:]]*type AP[[:space:]]*$' <<< "$info"; then
        printf '%s\n' "$iface: serving as the EUD AP; leaving its power alone"
        continue
    fi
    phy=$(cat "$SYS_NET/$iface/phy80211/name" 2>/dev/null)
    if ! [[ "$phy" =~ ^phy[0-9]+$ ]]; then
        fail "$iface: cannot identify its PHY; not changing its power"
        continue
    fi
    shared=""
    for other in "$SYS_NET"/*; do
        name=${other##*/}
        [ "$name" != "$iface" ] || continue
        [ "$(cat "$other/phy80211/name" 2>/dev/null)" = "$phy" ] || continue
        # IFF_UP is bit 0. Unreadable flags count as up, blocking the write.
        flags=$(cat "$other/flags" 2>/dev/null || echo 0x1)
        (( flags & 1 )) && shared=$name && break
    done
    if [ -n "$shared" ]; then
        fail "$iface: $phy also serves $shared; not changing its power"
        continue
    fi
    if ! iw_bounded phy "$phy" set txpower fixed "$REQUEST_MBM" \
            2>/dev/null; then
        fail "$iface: power request refused"
        continue
    fi
    power=""
    # Allow ten readbacks with one-second pauses for a delayed driver report.
    for ((attempt = 1; attempt <= READBACK_ATTEMPTS; attempt++)); do
        power=$(reported_power "$(iw_bounded dev "$iface" info 2>/dev/null)")
        [ -n "$power" ] && break
        [ "$attempt" -lt "$READBACK_ATTEMPTS" ] && sleep 1
    done
    if [ -z "$power" ]; then
        fail "$iface: no transmit power reported after the request"
        continue
    fi
    printf '%s: requested %d dBm; driver reports %.2f dBm\n' "$iface" $((REQUEST_MBM / 100)) "$power"
done

if [ ${#failures[@]} -gt 0 ]; then
    (IFS=';'; printf '%s\n' "ERROR: mesh radio power: ${failures[*]}" >&2)
    exit 1
fi
