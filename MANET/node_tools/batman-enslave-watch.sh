#!/bin/bash
# Watchdog: re-enslaves HaLow (and standard mesh) interfaces into bat0 if they fall out.
# Runs continuously after batman-enslave.service. Safe to restart.
. "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet-common.sh" || exit 1

log() {
    manet_journal batman-enslave-watch "ENSLAVE-WATCH: $*"
}

# radio_iface_enabled: manet-common.sh

batman_mainif() {
    batctl bat0 o 2>/dev/null | sed -n '1s/.*MainIF\/MAC: \([^/]*\)\/.*/\1/p'
}

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
        CUR="$(iw dev "$IFACE" get mesh_param mesh_plink_timeout 2>/dev/null)"
        if [ -n "$CUR" ] && [ "${CUR%% *}" != "0" ]; then
            if iw dev "$IFACE" set mesh_param mesh_plink_timeout 0 2>/dev/null; then
                log "Set mesh_plink_timeout=0 on $IFACE (was: $CUR)"
            fi
        fi
    done

    for IFACE in $HALOW_IFS $MESH_IFS; do
        # Skip if radio-state says down
        radio_iface_enabled "$IFACE" || continue

        # Skip if interface doesn't exist
        [ -e "/sys/class/net/$IFACE" ] || continue

        # Check if already in bat0
        if [[ $'\n'"$BAT_MEMBERS" == *$'\n'"${IFACE}:"* ]]; then
            continue
        fi

        # Interface should be in bat0 but isn't: re-enslave
        log "WARNING: $IFACE not in bat0, re-enslaving..."
        ip link set "$IFACE" up 2>/dev/null || true
        ip link set "$IFACE" mtu 1532 2>/dev/null || true

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
