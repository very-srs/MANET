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
