# Shared shell helpers for MANET node tools. Source it; do not run it.
. "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet-runtime-client.sh"

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
    WLAN0_MCS_PEER=""; WLAN1_MCS_PEER=""; WLAN2_MCS_PEER=""
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

# Whether a radio is allowed up. mesh-radio-state.py records operator choices
# in "desired"; anything but an explicit "down", including a missing or
# unreadable state file, means enabled.
radio_iface_enabled() {
    [ -n "$1" ] || return 1
    # A watchdog checks several interfaces against the same small document.
    # Reuse one strict jq parse until its CONTENT changes (including deletion
    # or malformed input); no mtime race or delayed radio-down decision.
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
    # Only a single valid document whose value is exactly "down" disables. Missing,
    # empty, malformed, wrong-shape or multi-document input leaves it enabled.
    local iface
    while IFS= read -r iface; do
        [ "$iface" != "$1" ] || return 1
    done <<< "$RADIO_DOWN_IFACES"
    return 0
}

# Whole seconds on the boot clock (/proc/uptime). Local timers and cooldowns
# use it, never the wall clock: a time sync can step the wall clock by any
# amount in either direction. Not comparable between nodes or across boots.
# Fails, printing nothing, unless the first field is a plain decimal number,
# so a bad read cannot become an arithmetic zero in a caller.
uptime_now() {
    local up _
    read -r up _ < "${MESH_UPTIME_FILE:-/proc/uptime}" || return 1
    [[ "$up" =~ ^[0-9]+(\.[0-9]+)?$ ]] || return 1
    up=${up%.*}
    echo "$((10#$up))"
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
