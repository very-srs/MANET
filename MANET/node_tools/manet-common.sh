#!/bin/bash
# Shared shell helpers for MANET node tools. Source this file.
. "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet-runtime-client.sh"

# Keep data on stdout. systemd timestamps diagnostics on stderr.
manet_log() {
    local program="$1"
    shift
    printf '%s: %s\n' "$program" "$*" >&2
}

# Keep explicit journal routing when a caller captures helper diagnostics.
manet_journal() {
    local program="$1"
    shift
    printf '%s\n' "$*" | systemd-cat -t "$program"
}

node_primary_ipv4() {
    local rc=0 address
    address=$(manet_runtime_call ipv4) || rc=$?
    if [ "$rc" = 125 ]; then
        address=$(python3 /usr/local/bin/manet_node_ipv4.py br0) || return $?
    elif [ "$rc" != 0 ]; then
        return "$rc"
    fi
    # Both resident and standalone selectors can observe a withdrawn allocation.
    # An allocated manager must retry that observation, not publish it as valid.
    [[ "$address" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
    printf '%s\n' "$address"
}

node_syncthing_id() {
    local rc=0
    manet_runtime_call syncthing || rc=$?
    if [ "$rc" = 125 ]; then python3 "${MANET_TOOLS_DIR:-/usr/local/bin}/manet-syncthing-id.py"; else return "$rc"; fi
}

collect_radio_mcs() {
    WLAN0_TX_MCS=""; WLAN0_RX_MCS=""
    WLAN1_TX_MCS=""; WLAN1_RX_MCS=""
    WLAN2_TX_MCS=""; WLAN2_RX_MCS=""
    WLAN2_MCS_PEER=""
    [ -x "$HALOW_MCS_SUMMARY" ] || return 0
    for iface in wlan0 wlan1 wlan2; do
        [ -d "/sys/class/net/$iface" ] || continue
        local values rc=0
        values=$(manet_runtime_call mcs "$iface" 2>/dev/null) || rc=$?
        if [ "$rc" = 125 ]; then
            values=$("$HALOW_MCS_SUMMARY" --iface "$iface" --shell 2>/dev/null) || values=""
        elif [ "$rc" != 0 ]; then
            # Optional telemetry: omit unavailable rates, never eval an error.
            values=""
        fi
        eval "$values"
    done
}

collect_interfaces_json() {
    local rc=0 value
    value=$(manet_runtime_call interfaces 2>/dev/null) || rc=$?
    if [ "$rc" = 125 ]; then
        python3 /usr/local/bin/manet_interfaces.py 2>/dev/null || echo '[]'
    elif [ "$rc" != 0 ]; then
        # Optional telemetry has an explicit empty-list representation.
        echo '[]'
    else
        printf '%s\n' "$value"
    fi
}

# The SSID hostapd actually broadcasts (base name plus node suffix), not the
# base value in mesh.conf.
collect_ap_ssid() {
    local line
    [ -r /etc/hostapd/hostapd.conf ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        if [[ "$line" = ssid=* ]]; then printf '%s\n' "${line#ssid=}"; return 0; fi
    done < /etc/hostapd/hostapd.conf
}

# mesh-radio-state.py records operator choices in "desired". Only an explicit
# "down" disables a radio. Missing or unreadable state leaves it enabled.
radio_iface_enabled() {
    [ -n "$1" ] || return 1
    # Reuse one strict jq parse across watchdog interface checks until the
    # content changes, including deletion or malformed input. This avoids
    # mtime races and delayed radio-down decisions.
    local path="${MANET_RADIO_STATE_FILE:-/var/lib/mesh_radio_state.json}" contents=missing
    [ ! -r "$path" ] || contents="present:$(<"$path")"
    if [ "${RADIO_STATE_CONTENTS:-}" != "$contents" ]; then
        RADIO_STATE_CONTENTS=$contents
        if [ "$contents" = missing ]; then
            RADIO_DOWN_IFACES=""
        elif ! RADIO_DOWN_IFACES=$(jq -sr '
            if length == 1 and (.[0] | type) == "object" and (.[0].desired | type) == "object"
            then .[0].desired | to_entries[] | select(.value == "down") | .key
            else empty end' <<< "${contents#present:}" 2>/dev/null); then
            RADIO_DOWN_IFACES=""; RADIO_STATE_CONTENTS=""  # Retry a failed parser.
        fi
    fi
    # Only a single valid document with the interface value "down" disables it.
    # Missing, empty, malformed, wrong-shape or multiple documents allow it.
    local iface
    while IFS= read -r iface; do
        [ "$iface" != "$1" ] || return 1
    done <<< "$RADIO_DOWN_IFACES"
    return 0
}

# Return whole boot-clock seconds from /proc/uptime for local timers.
# Wall-clock steps do not affect this value. Do not compare across boots or
# nodes. Invalid decimal input fails without output, avoiding arithmetic zero.
uptime_now() {
    local up _
    read -r up _ < "${MESH_UPTIME_FILE:-/proc/uptime}" || return 1
    [[ "$up" =~ ^[0-9]+(\.[0-9]+)?$ ]] || return 1
    up=${up%.*}
    printf '%s\n' "$((10#$up))"
}

# An empty local Alfred cache has no command to authenticate or activate.
# Nonempty records always reach the receiver, even if byte-identical: ACKs,
# activation deadlines and replay checks must still run at the usual cadence.
sync_alfred_command() {
    local helper="$1" type="$2" records
    [ -x "$helper" ] || return 0
    records=$(timeout 5 alfred -r "$type" 2>/dev/null) || return 1
    [[ "$records" == *[![:space:]]* ]] || return 0
    "$helper" sync --stdin <<< "$records"
}

# Print the separator used by login status banners.
rule() {
    printf '  %s\n' \
        '────────────────────────────────────────────────────────────'
}

# Check markers owned by the time service.
is_ntp_time_source() {
    local run_dir="${MANET_TIME_RUN_DIR:-/run}"
    [ -f "$run_dir/mesh-ntp.state" ] || [ -f "$run_dir/mesh-ntp-gps.state" ]
}

# Print the low 32 bits as an IPv4 dotted quad.
int_to_ip() {
    printf '%d.%d.%d.%d\n' \
        "$(( ($1 >> 24) & 255 ))" "$(( ($1 >> 16) & 255 ))" \
        "$(( ($1 >> 8) & 255 ))" "$(( $1 & 255 ))"
}

# Read the driver from sysfs, falling back to ethtool.
iface_driver() {
    local iface="$1" driver
    driver="$(basename "$(readlink -f "/sys/class/net/$iface/device/driver" \
        2>/dev/null)" 2>/dev/null || true)"
    if [ -z "$driver" ] || [ "$driver" = "." ]; then
        driver="$(ethtool -i "$iface" 2>/dev/null |
            awk -F': ' '$1 == "driver" {print $2; exit}')"
    fi
    printf '%s\n' "$driver"
}

# Read the interface PHY name from iw.
iface_phy() {
    local iface="$1"
    iw dev "$iface" info 2>/dev/null | awk '/wiphy/ {print "phy"$2; exit}'
}

# Check the PHY for mesh point support.
iface_supports_mesh() {
    local iface="$1"
    local phyname

    phyname="$(iface_phy "$iface")"
    [[ -n "$phyname" ]] &&
        iw phy "$phyname" info 2>/dev/null | grep -q "mesh point"
}

# Reserve the onboard brcmfmac radio for the EUD AP.
is_nonmesh_wifi() {
    [[ "$(iface_driver "$1")" == brcmfmac ]]
}

# Accept existing USB Wi-Fi devices outside mesh/AP roles.
is_usb_wifi_uplink_iface() {
    local iface="$1" bus driver
    [ -n "$iface" ] || return 1
    [ -d "/sys/class/net/$iface" ] || return 1

    case "$iface" in
        wlan0|wlan1|wlan2|wlan3|lo|br*|bat*) return 1 ;;
        wlan*) ;;
        *) return 1 ;;
    esac

    bus="$(readlink "/sys/class/net/$iface/device/subsystem" 2>/dev/null |
        grep -o 'usb' || true)"
    [ "$bus" = "usb" ] || return 1

    driver="$(iface_driver "$iface")"
    case "$driver" in
        morse*|brcmfmac) return 1 ;;
    esac

    grep -qx "$iface" /var/lib/mesh_if /var/lib/halow_if \
        /var/lib/no_mesh_if 2>/dev/null && return 1
    return 0
}

# Try to enable a disabled unit without failing its caller.
enable_if_disabled() {
    systemctl is-enabled --quiet "$1" 2>/dev/null || \
        systemctl enable "$1" 2>/dev/null || true
}

# Try to unmask a unit without failing its caller.
unmask_if_masked() {
    [ "$(systemctl is-enabled "$1" 2>/dev/null)" = "masked" ] && \
        systemctl unmask "$1" 2>/dev/null || true
}

# Read the first local IPv4 address on an interface.
iface_ip() {
    ip -4 -o addr show dev "$1" 2>/dev/null |
        awk '{print $4}' | cut -d/ -f1 | head -n1
}

# Probe internet reachability with bounded ping, HTTP or TCP.
internet_probe() {
    local iface="$1"
    local url code ip

    ping -c 1 -W 2 -I "$iface" 1.1.1.1 >/dev/null 2>&1 && return 0
    ping -c 1 -W 2 -I "$iface" 8.8.8.8 >/dev/null 2>&1 && return 0

    if command -v curl >/dev/null 2>&1; then
        for url in http://connectivitycheck.gstatic.com/generate_204 \
                   http://cp.cloudflare.com/; do
            code=$(curl --interface "$iface" -s -m 3 -o /dev/null \
                        -w '%{http_code}' "$url" 2>/dev/null || true)
            [ "$code" = "204" ] && return 0
        done
        return 1
    fi

    # No curl in this image: bare TCP connect instead.
    ip=$(iface_ip "$iface")
    if [ -n "$ip" ] && command -v nc >/dev/null 2>&1; then
        nc -z -w 3 -s "$ip" 1.1.1.1 53 >/dev/null 2>&1 && return 0
        nc -z -w 3 -s "$ip" 8.8.8.8 53 >/dev/null 2>&1 && return 0
    fi

    return 1
}
