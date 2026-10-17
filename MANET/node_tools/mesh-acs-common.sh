#!/bin/bash
# Shared role/config handling for ACS. Empty role files mean an absent band.

# Test the initial-sync marker in the configured time/ACS run directory.
acs_clock_ready() {
    [ -f "${MANET_TIME_RUN_DIR:-${MANET_ACS_RUN_DIR:-/run}}/initial_time_synced" ]
}

# Load WPA_IFACE_2_4/5_0 and their WPA_CONF paths from local role files.
load_mesh_roles() {
    local roles="${MANET_IFACE_STATE_DIR:-/var/lib}"
    local configs="${MANET_WPA_DIR:-/etc/wpa_supplicant}"
    WPA_IFACE_2_4=$(cat "$roles/mesh_24_if" 2>/dev/null || true)
    WPA_IFACE_5_0=$(cat "$roles/mesh_5_if" 2>/dev/null || true)
    is_builtin_wifi_uplink_iface "$WPA_IFACE_2_4" && WPA_IFACE_2_4=""
    is_builtin_wifi_uplink_iface "$WPA_IFACE_5_0" && WPA_IFACE_5_0=""
    WPA_CONF_2_4=""; WPA_CONF_5_0=""
    [ -z "$WPA_IFACE_2_4" ] || WPA_CONF_2_4="$configs/wpa_supplicant-${WPA_IFACE_2_4}.conf"
    [ -z "$WPA_IFACE_5_0" ] || WPA_CONF_5_0="$configs/wpa_supplicant-${WPA_IFACE_5_0}.conf"
    return 0
}

# Read the first configured frequency, including an unterminated line.
get_current_freq() {
    local conf_file=$1
    local line
    [ -f "$conf_file" ] && [ -r "$conf_file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        # Match frequency=digits anywhere in the line and keep the first match.
        if [[ "$line" =~ frequency=([0-9]+) ]]; then
            printf '%s\n' "${BASH_REMATCH[1]}"
            return 0
        fi
    done < "$conf_file"
}

# A configured DFS radio must not select a lobby because peers disappear.
# Read the role/config afresh, including after a daemon restart or reset.
acs_dfs_operating() {
    local roles="${MANET_IFACE_STATE_DIR:-/var/lib}"
    local configs="${MANET_WPA_DIR:-/etc/wpa_supplicant}"
    local iface='' freq
    IFS= read -r iface 2>/dev/null < "$roles/mesh_5_if" ||
        [ -n "$iface" ] || return 1
    [[ "$iface" =~ ^[A-Za-z0-9_.-]{1,15}$ ]] || return 1
    freq=$(get_current_freq "$configs/wpa_supplicant-$iface.conf")
    [[ "$freq" =~ ^5[0-9]{3}$ ]] || return 1
    (( (freq >= 5260 && freq <= 5320 || freq >= 5500 && freq <= 5720) &&
       freq % 20 == 0 ))
}

. "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet-common.sh" || return 1
# radio_iface_enabled: manet-common.sh

# Require a four-digit MHz value for every enabled WPA role and one band.
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

# Print rendezvous discovery state using enabled WPA role frequencies.
acs_discovery_state() {
    python3 "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet_rendezvous.py" state \
        "$(radio_iface_enabled "$WPA_IFACE_2_4" && get_current_freq "$WPA_CONF_2_4")" \
        "$(radio_iface_enabled "$WPA_IFACE_5_0" && get_current_freq "$WPA_CONF_5_0")"
}

# Check 2.4/5 GHz MHz arguments for valid shape and an enabled local band.
acs_channels_usable() {
    local freq24="$1" freq5="$2"
    # Allow missing remote bands if at least one usable band overlaps.
    [[ -z "$freq24" || "$freq24" =~ ^24[0-9]{2}$ ]] || return 1
    [[ -z "$freq5" || "$freq5" =~ ^5[0-9]{3}$ ]] || return 1
    { [ -n "$freq24" ] && radio_iface_enabled "$WPA_IFACE_2_4"; } ||
        { [ -n "$freq5" ] && radio_iface_enabled "$WPA_IFACE_5_0"; }
}

# Write frequency arguments under the channel lock using fresh WPA roles.
# Mode is data/search. Activation none skips restarts, all restarts each
# selected radio, and changed restarts radios with different live MHz.
acs_write_channels() (
    exec 8>"${MANET_ACS_LOCK_FILE:-/var/run/channel-election.lock}"
    flock -n 8 || return 1
    load_mesh_roles
    acs_agreement_busy && return 1
    local freq24="$1" freq5="$2" mode="${3:-data}" activation="${4:-none}"
    local band iface target actual
    [ "$mode" = data ] || [ "$mode" = search ] || return 1
    acs_configs_ready && acs_channels_usable "$freq24" "$freq5" || return 1
    local -a changed=()
    # Keep activation under the same lock as the fresh role read and writes.
    # Otherwise an AP transition can occur between writing and restarting.
    if [ "$activation" != none ]; then
        for band in 2_4 5_0; do
            local iface_var="WPA_IFACE_$band"
            iface=${!iface_var}
            target="$freq24"; [ "$band" != 5_0 ] || target="$freq5"
            [ -n "$target" ] && radio_iface_enabled "$iface" || continue
            # Read parenthesized channel MHz from iw within two seconds.
            actual=$(timeout 2 iw dev "$iface" info 2>/dev/null | grep -oP 'channel.*\((\K[0-9]+)' || true)
            if [ "$activation" = all ] || [ "$actual" != "$target" ]; then
                changed+=("$iface")
            fi
        done
    fi
    if [ -n "$freq24" ] && radio_iface_enabled "$WPA_IFACE_2_4"; then
        sed -i "s/frequency=.*/frequency=${freq24}/" "$WPA_CONF_2_4" || return 1
    fi
    if [ -n "$freq5" ] && radio_iface_enabled "$WPA_IFACE_5_0"; then
        sed -i "s/frequency=.*/frequency=${freq5}/" "$WPA_CONF_5_0" || return 1
    fi
    python3 "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet_rendezvous.py" set-mode "$mode" || return 1
    acs_mark_busy || return 1
    for iface in "${changed[@]}"; do
        # Bound each supplicant restart to 30 seconds while holding the lock.
        timeout 30 systemctl restart "wpa_supplicant@${iface}.service" || return 1
    done
    return 0
)

# /proc/uptime and Python CLOCK_BOOTTIME include time spent suspended.
acs_mark_busy() {
    local boot now rest
    boot=$(cat "${MANET_BOOT_ID_FILE:-/proc/sys/kernel/random/boot_id}") || return 1
    boot=${boot//-/}
    read -r now rest < "${MESH_UPTIME_FILE:-/proc/uptime}" || return 1
    [[ "$boot" =~ ^[a-f0-9]{32}$ && "$now" =~ ^[0-9]+([.][0-9]+)?$ ]] || return 1
    # Publish this boot ID and a busy deadline 30 boot-clock seconds ahead.
    LC_ALL=C awk -v boot="$boot" -v now="$now" 'BEGIN { printf "%s %.2f\n", boot, now + 30 }' > "${MANET_ACS_RUN_DIR:-/run}/manet-acs-busy"
}

# A fixed expiry prevents a stopped agreement process from suppressing healing.
acs_agreement_busy() {
    local boot expiry extra own now rest
    read -r boot expiry extra 2>/dev/null < "${MANET_ACS_RUN_DIR:-/run}/manet-acs-busy" || return 1
    [[ "$boot" =~ ^[a-f0-9]{32}$ && "$expiry" =~ ^[0-9]+([.][0-9]+)?$ && -z "$extra" ]] || return 1
    own=$(cat "${MANET_BOOT_ID_FILE:-/proc/sys/kernel/random/boot_id}") || return 1
    [ "$boot" = "${own//-/}" ] || return 1
    read -r now rest < "${MESH_UPTIME_FILE:-/proc/uptime}" || return 1
    [[ "$now" =~ ^[0-9]+([.][0-9]+)?$ ]] || return 1
    # Accept only a deadline within 125 seconds on the current boot clock.
    LC_ALL=C awk -v expiry="$expiry" -v now="$now" 'BEGIN { exit !(0 <= expiry - now && expiry - now <= 125) }'
}
