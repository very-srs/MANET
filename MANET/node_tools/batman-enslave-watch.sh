#!/bin/bash
# Watchdog: re-enslaves HaLow (and standard mesh) interfaces into bat0 if they fall out.
# Runs continuously after batman-enslave.service. Safe to restart.
. "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet-common.sh" || exit 1

# Send watchdog diagnostics to the journal.
log() {
    manet_journal batman-enslave-watch "ENSLAVE-WATCH: $*"
}

# radio_iface_enabled: manet-common.sh

# Print the MainIF name before the slash in the originator table header.
batman_mainif() {
    batctl bat0 o 2>/dev/null | sed -n '1s/.*MainIF\/MAC: \([^/]*\)\/.*/\1/p'
}

# Use the first HALOW_IFS entry as primary and rebuild membership if needed.
# Skip absent or disabled HaLow and throttle rebuilds to once per 60 s.
restore_halow_primary_if_needed() {
    local halow mainif now last cooldown_file

    read -r halow _ <<< "${HALOW_IFS//$'\n'/ }"
    [ -n "$halow" ] || return 0
    radio_iface_enabled "$halow" || return 0
    ip link show "$halow" >/dev/null 2>&1 || return 0
    batctl bat0 if 2>/dev/null | grep -q "^${halow}:" || return 0

    mainif="$(batman_mainif)"
    [ -n "$mainif" ] || return 0
    [ "$mainif" = "$halow" ] && return 0

    cooldown_file="/run/batman-enslave-watch-halow-primary-reset"
    now="$(uptime_now)" || return 0
    last=""
    [ ! -r "$cooldown_file" ] || read -r last < "$cooldown_file"
    # Boot-clock seconds. Missing, malformed or future values disable cooldown.
    [[ "$last" =~ ^[0-9]+$ ]] && [ "$last" -le "$now" ] || last=-1000000
    if [ $((now - last)) -lt 60 ]; then
        return 0
    fi
    printf '%s\n' "$now" > "$cooldown_file"

    log "WARNING: bat0 MainIF is $mainif, expected HaLow $halow; rebuilding bat0 membership"
    /usr/local/bin/batman-if-setup.sh stop 2>/dev/null || true
    # Leave a 2 s gap between tearing down bat0 and recreating it.
    sleep 2
    /usr/local/bin/batman-if-setup.sh start 2>/dev/null || true
}

# hostapd can be left "active" over a dead BSS: something downed or re-moded
# the netdev. PCIe resets can also leave it active without an SSID.
# Check the SSID because DFS CAC keeps the carrier down while the SSID is
# set. Restarting during CAC prevents it from completing.
ap_is_serving() {
    iw dev "$1" info 2>/dev/null | awk '$1 == "type" { t = $2 } $1 == "ssid" { s = 1 }
        END { exit !(t == "AP" && s) }'
}

# Restart an active AP after repeated missing-SSID checks under the AP lock.
# Limit restart attempts to one per 60 boot-clock seconds.
restart_dead_ap_if_needed() (
    # Keep the active check and restart together against mesh-return policy.
    exec 9>"${MANET_AP_ROLE_LOCK:-/run/manet-ap-role.lock}"
    flock -n 9 || return 0
    local ap now last cooldown_file strikes_file strikes

    ap=""
    [ ! -r /var/lib/ap_interface ] || read -r ap < /var/lib/ap_interface
    [ -n "$ap" ] || return 0
    systemctl is-active --quiet hostapd.service || return 0
    ip link show "$ap" >/dev/null 2>&1 || return 0

    strikes_file="/run/batman-enslave-watch-ap-strikes"
    if ap_is_serving "$ap"; then
        rm -f "$strikes_file"
        return 0
    fi

    # Two consecutive cycles, so a restart in progress is not counted as dead.
    strikes=$(( $(cat "$strikes_file" 2>/dev/null || echo 0) + 1 ))
    printf '%s\n' "$strikes" > "$strikes_file"
    [ "$strikes" -ge 2 ] || return 0

    cooldown_file="/run/batman-enslave-watch-ap-restart"
    now="$(uptime_now)" || return 0
    last="$(cat "$cooldown_file" 2>/dev/null)"
    # Boot-clock seconds. Missing, malformed or future values disable cooldown.
    [[ "$last" =~ ^[0-9]+$ ]] && [ "$last" -le "$now" ] || last=-1000000
    if [ $((now - last)) -lt 60 ]; then
        return 0
    fi
    printf '%s\n' "$now" > "$cooldown_file"

    log "WARNING: hostapd is active but $ap is not serving; restarting hostapd"
    systemctl restart hostapd.service 2>/dev/null || true
)

# Keep joined HaLow meshes from closing idle plinks during key teardown.
enforce_halow_plink_timeout() {
    local iface="$1" current pattern
    current=$(iw dev "$iface" get mesh_param mesh_plink_timeout 2>/dev/null) \
        || return 0
    # Match the readiness probe: bare integer or '<value> seconds'.
    pattern='^[[:space:]]*([0-9]+)([[:space:]]+seconds)?[[:space:]]*$'
    [[ "$current" =~ $pattern ]] || return 0
    [[ "${BASH_REMATCH[1]}" =~ ^0+$ ]] && return 0
    if iw dev "$iface" set mesh_param mesh_plink_timeout 0 2>/dev/null; then
        log "Set mesh_plink_timeout=0 on $iface (was: $current)"
    fi
}

# Wait 8 s between passes, in addition to time spent checking or repairing.
while true; do
    sleep 8

    # Read roles only after acquiring the transition/channel lock. Release it
    # before service restarts whose helpers take the same lock.
    exec 8>"${MANET_ACS_LOCK_FILE:-/run/channel-election.lock}"
    flock -n 8 || continue
    HALOW_IFS=""; MESH_IFS=""
    [ ! -r /var/lib/halow_if ] || HALOW_IFS=$(</var/lib/halow_if)
    [ ! -r /var/lib/mesh_if ] || MESH_IFS=$(</var/lib/mesh_if)
    # One membership snapshot per check, refreshed after a successful add.
    BAT_MEMBERS=$(batctl bat0 if 2>/dev/null) || BAT_MEMBERS=""

    # Keep mesh_plink_timeout=0 on HaLow. Inactivity-based plink closure can
    # wedge Morse firmware during key teardown (morse_cmd_disable_key ENODEV)
    # until a hard reboot. Re-forming the mesh resets the parameter.
    # Get/set works only after joining the mesh, so retry failures each pass.
    for IFACE in $HALOW_IFS; do
        radio_iface_enabled "$IFACE" || continue
        enforce_halow_plink_timeout "$IFACE"
    done

    for IFACE in $HALOW_IFS $MESH_IFS; do
        # Skip if radio-state says down
        radio_iface_enabled "$IFACE" || continue

        # Skip if interface doesn't exist
        [ -e "/sys/class/net/$IFACE" ] || continue

        # Check if already in bat0
        # Match the interface at a line start followed by batctl's colon.
        if [[ $'\n'"$BAT_MEMBERS" == *$'\n'"${IFACE}:"* ]]; then
            continue
        fi

        # Interface should be in bat0 but isn't: re-enslave
        log "WARNING: $IFACE not in bat0, re-enslaving..."
        ip link set "$IFACE" up 2>/dev/null || true
        # Match the 1532-byte mesh MTU used during initial attachment.
        ip link set "$IFACE" mtu 1532 2>/dev/null || true

        # Retry three times, checking 1 s after add and pausing 2 s on failure.
        for attempt in 1 2 3; do
            if batctl bat0 if add "$IFACE" 2>/dev/null; then
                sleep 1
                BAT_MEMBERS=$(batctl bat0 if 2>/dev/null) || BAT_MEMBERS=""
                if [[ $'\n'"$BAT_MEMBERS" == *$'\n'"${IFACE}:"* ]]; then
                    log "OK: $IFACE re-enslaved to bat0 (attempt $attempt)"
                    break
                fi
            fi
            sleep 2
        done
    done

    flock -u 8
    exec 8>&-
    restore_halow_primary_if_needed
    restart_dead_ap_if_needed
done
