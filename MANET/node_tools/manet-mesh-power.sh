#!/bin/bash
# Request 30 dBm on every mesh radio, Wi-Fi and HaLow.
#
# MANET sets no transmit power ceiling of its own on mesh radios. Each one is
# asked for 30 dBm; the card's firmware and hardware apply their own limits.
# The value read back is what the driver/firmware reports, logged as such (a
# report, not a measurement of RF output); a lower report is not an error. The EUD access point is not a mesh radio and keeps
# its own low power.
#
# Holds the channel lock that manet_ap_mesh.py takes for AP <-> mesh moves,
# and reads roles and interface type under it, so a radio cannot become the
# AP between the check and the request. Like manet_ap_mesh's PHY power
# writer, it refuses a radio whose PHY also carries another active interface.
#
# Never reloads a driver: a live reload can wedge the USB HaLow card.

ROLES_DIR="${MANET_IFACE_STATE_DIR:-/var/lib}"
SYS_NET="${MANET_SYS_NET:-/sys/class/net}"
LOCK_FILE="${MANET_ACS_LOCK_FILE:-/run/channel-election.lock}"
LOCK_WAIT="${MANET_LOCK_WAIT:-10}"
REQUEST_MBM=3000
READBACK_ATTEMPTS=10

IW_TIMEOUT="${MANET_IW_TIMEOUT:-10}"

failures=()
fail() { failures+=("$1"); }

# Every iw call is bounded: a wedged one must not hold the channel lock.
iw_bounded() { timeout --kill-after=2 "$IW_TIMEOUT" iw "$@"; }

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
