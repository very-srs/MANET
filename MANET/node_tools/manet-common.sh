# Shared shell helpers for MANET node tools. Source it; do not run it.

# Whether a radio is allowed up. mesh-radio-state.py records operator choices
# in "desired"; anything but an explicit "down", including a missing or
# unreadable state file, means enabled.
radio_iface_enabled() {
    [ -n "$1" ] || return 1
    # One strict parse of the whole file, then a boolean inside jq: only a
    # single valid document whose value is exactly "down" disables. Missing,
    # empty, malformed, wrong-shape or multi-document input leaves it enabled.
    ! jq -se --arg iface "$1" \
        'if length == 1 then .[0].desired[$iface] == "down" else false end' \
        "${MANET_RADIO_STATE_FILE:-/var/lib/mesh_radio_state.json}" >/dev/null 2>&1
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
