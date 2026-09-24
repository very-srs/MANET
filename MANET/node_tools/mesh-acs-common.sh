#!/bin/bash
# Shared role/config handling for ACS. Empty role files mean an absent band.

acs_clock_ready() {
    [ -f "${MANET_TIME_RUN_DIR:-${MANET_ACS_RUN_DIR:-/run}}/initial_time_synced" ]
}

load_mesh_roles() {
    local roles="${MANET_IFACE_STATE_DIR:-/var/lib}"
    local configs="${MANET_WPA_DIR:-/etc/wpa_supplicant}"
    WPA_IFACE_2_4=$(cat "$roles/mesh_24_if" 2>/dev/null || true)
    WPA_IFACE_5_0=$(cat "$roles/mesh_5_if" 2>/dev/null || true)
    WPA_CONF_2_4=""; WPA_CONF_5_0=""
    [ -z "$WPA_IFACE_2_4" ] || WPA_CONF_2_4="$configs/wpa_supplicant-${WPA_IFACE_2_4}.conf"
    [ -z "$WPA_IFACE_5_0" ] || WPA_CONF_5_0="$configs/wpa_supplicant-${WPA_IFACE_5_0}.conf"
    return 0
}

get_current_freq() {
    [ -f "$1" ] || return 0
    grep -oP 'frequency=\K[0-9]+' "$1" | head -1
}

radio_iface_enabled() {
    [ -n "$1" ] || return 1
    python3 - "$1" "${MANET_RADIO_STATE_FILE:-/var/lib/mesh_radio_state.json}" <<'PY'
import json, sys
try:
    with open(sys.argv[2]) as f:
        state = json.load(f).get('desired', {}).get(sys.argv[1], 'up')
except (OSError, ValueError, AttributeError):
    state = 'up'
sys.exit(1 if state == 'down' else 0)
PY
}

acs_configs_ready() {
    local band iface conf seen=0
    # One physical radio cannot serve both bands at once.
    [ -z "$WPA_IFACE_2_4" ] || [ "$WPA_IFACE_2_4" != "$WPA_IFACE_5_0" ] || return 1
    for band in 2_4 5_0; do
        local iface_var="WPA_IFACE_$band" conf_var="WPA_CONF_$band"
        iface=${!iface_var}; conf=${!conf_var}
        radio_iface_enabled "$iface" || continue
        [ -f "$conf" ] && [[ "$(get_current_freq "$conf")" =~ ^[0-9]{4}$ ]] || return 1
        seen=1
    done
    [ "$seen" -eq 1 ]
}

acs_discovery_state() {
    python3 "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet_rendezvous.py" state \
        "$(radio_iface_enabled "$WPA_IFACE_2_4" && get_current_freq "$WPA_CONF_2_4")" \
        "$(radio_iface_enabled "$WPA_IFACE_5_0" && get_current_freq "$WPA_CONF_5_0")"
}

acs_channels_usable() {
    local freq24="$1" freq5="$2"
    # Missing remote bands are allowed; at least one usable band must overlap.
    [[ -z "$freq24" || "$freq24" =~ ^24[0-9]{2}$ ]] || return 1
    [[ -z "$freq5" || "$freq5" =~ ^5[0-9]{3}$ ]] || return 1
    { [ -n "$freq24" ] && radio_iface_enabled "$WPA_IFACE_2_4"; } ||
        { [ -n "$freq5" ] && radio_iface_enabled "$WPA_IFACE_5_0"; }
}

acs_write_channels() (
    exec 8>"${MANET_ACS_LOCK_FILE:-/var/run/channel-election.lock}"
    flock -n 8 || return 1
    acs_agreement_busy && return 1
    local freq24="$1" freq5="$2" mode="${3:-data}"
    [ "$mode" = data ] || [ "$mode" = search ] || return 1
    acs_configs_ready && acs_channels_usable "$freq24" "$freq5" || return 1
    if [ -n "$freq24" ] && radio_iface_enabled "$WPA_IFACE_2_4"; then
        sed -i "s/frequency=.*/frequency=${freq24}/" "$WPA_CONF_2_4" || return 1
    fi
    if [ -n "$freq5" ] && radio_iface_enabled "$WPA_IFACE_5_0"; then
        sed -i "s/frequency=.*/frequency=${freq5}/" "$WPA_CONF_5_0" || return 1
    fi
    python3 "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet_rendezvous.py" set-mode "$mode" || return 1
    echo "$(( $(date +%s) + 30 ))" > "${MANET_ACS_RUN_DIR:-/run}/manet-acs-busy"
    return 0
)

# A fixed expiry prevents a stopped agreement process from suppressing healing.
acs_agreement_busy() {
    local expiry now
    expiry=$(cat "${MANET_ACS_RUN_DIR:-/run}/manet-acs-busy" 2>/dev/null) || return 1
    [[ "$expiry" =~ ^[0-9]+$ ]] || return 1
    now=$(date +%s)
    [ "$now" -le "$expiry" ] && [ "$((expiry - now))" -le 125 ]
}
