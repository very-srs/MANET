#!/bin/bash
# Shared shell helpers for MANET node tools. Source this file.
. "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet-runtime-client.sh"

# Resolve the flash-time SSH account without sourcing mesh.conf as shell code.
# Old configurations retain the original account name. Account database paths
# take precedence over /home/<name> once the user exists.
manet_load_ssh_account() {
    local conf="${MANET_MESH_CONF:-/etc/mesh.conf}" entry uid gid unused
    MANET_SSH_USER=radio
    if [ -f "$conf" ]; then
        MANET_SSH_USER=$(awk -F= '$1 == "ssh_user" {print substr($0, index($0, "=")+1); found=1; exit} END {if (!found) print "radio"}' "$conf")
    fi
    if [[ ! "$MANET_SSH_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ || "$MANET_SSH_USER" = root || "$MANET_SSH_USER" = nobody ]]; then
        echo 'Invalid SSH username in mesh.conf' >&2
        return 1
    fi
    MANET_SSH_HOME="/home/$MANET_SSH_USER"
    MANET_SSH_GROUP="$MANET_SSH_USER"
    if entry=$(getent passwd "$MANET_SSH_USER"); then
        IFS=: read -r unused unused uid gid unused MANET_SSH_HOME unused <<< "$entry"
        if [[ "$uid" -lt 1000 || "$uid" -eq 65534 || "$MANET_SSH_HOME" != /* || "$MANET_SSH_HOME" = / ]]; then
            echo "Refusing system account or invalid home for SSH user '$MANET_SSH_USER'" >&2
            return 1
        fi
        MANET_SSH_GROUP="$gid"
    fi
}

# Generate the diagnostic sudo rule for this account. Do not ship a static
# copy in archives: a later install payload must not overwrite the local name.
manet_write_ssh_sudoers() {
    install -d -m 750 /etc/sudoers.d || return 1
    printf '%s ALL=(ALL) NOPASSWD: /usr/sbin/batctl, /sbin/ip link set wlan*, /usr/bin/iw\n' "$MANET_SSH_USER" > /etc/sudoers.d/perf || return 1
    chmod 440 /etc/sudoers.d/perf
}

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

# Print the allocated primary IPv4, failing if it is unavailable.
node_primary_ipv4() {
    local rc=0 address
    address=$(manet_runtime_call ipv4) || rc=$?
    # 125 means no runtime request was sent, so a standalone call is safe.
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

# Print the local certificate-derived Syncthing ID, or an empty result.
# Retry standalone only when the resident helper accepted no request.
node_syncthing_id() {
    local rc=0
    manet_runtime_call syncthing || rc=$?
    if [ "$rc" = 125 ]; then python3 "${MANET_TOOLS_DIR:-/usr/local/bin}/manet-syncthing-id.py"; else return "$rc"; fi
}

# Set WLAN0/1/2 TX/RX MCS globals and WLAN2_MCS_PEER for telemetry.
# Read HALOW_MCS_SUMMARY for the standalone collector path.
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
        # The local collector emits shell assignments for the MCS globals.
        eval "$values"
    done
}

# Print interface telemetry JSON, using [] when collection fails.
collect_interfaces_json() {
    local rc=0 value
    value=$(manet_runtime_call interfaces 2>/dev/null) || rc=$?
    # 125 means no runtime request was sent, so a standalone call is safe.
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
# "down" disables a radio. A reserved uplink is excluded from mesh operations.
# Missing or unreadable desired state leaves other radios enabled.
radio_iface_enabled() {
    [ -n "$1" ] || return 1
    is_builtin_wifi_uplink_iface "$1" && return 1
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

# Local config recovery must run even when Alfred has lost its ciphertext.
# Unchanged nonempty records still reach both receivers at normal cadence.
sync_alfred_command() {
    local helper="$1" type="$2" records resume=false
    [ -x "$helper" ] || return 0
    local journal_dir="${MANET_CONFIG_STATE_DIR:-/var/lib/manet-config}"
    local run_dir="${MANET_RUN_DIR:-/var/run}" rollback_dir
    rollback_dir="${MANET_ROLLBACK_DIR:-/var/lib/manet-config-rollback}"
    if [ "$type" = 70 ]; then
        if [ -f "$journal_dir/journal.json" ] ||
           [ -f "$run_dir/mesh_pending_config.json" ] ||
           [ -f "$rollback_dir/state" ]; then
            resume=true
        fi
    fi
    # A five-second read failure cannot prevent durable local recovery.
    if ! records=$(timeout 5 alfred -r "$type" 2>/dev/null); then
        [ "$resume" = true ] || return 1
        records=""
    fi
    [[ "$records" == *[![:space:]]* ]] || [ "$resume" = true ] || return 0
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
    # ethtool reports the driver as a "driver: name" record.
    if [ -z "$driver" ] || [ "$driver" = "." ]; then
        driver="$(ethtool -i "$iface" 2>/dev/null |
            awk -F': ' '$1 == "driver" {print $2; exit}')"
    fi
    printf '%s\n' "$driver"
}

# Read the interface PHY name from iw.
iface_phy() {
    local iface="$1"
    # iw prints a numeric wiphy index, which phy queries need as phyN.
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

# A persistent ownership marker blocks mesh recovery even before association.
is_builtin_wifi_uplink_iface() {
    local roles="${MANET_IFACE_STATE_DIR:-/var/lib}" owner=""
    [ ! -r "$roles/wifi_uplink_if" ] || read -r owner < "$roles/wifi_uplink_if"
    case "$owner" in wlan0|wlan1) [ "$1" = "$owner" ] ;; *) return 1 ;; esac
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

# Succeed when auto EUD mode should serve the AP without a wired EUD.
# Without ACS the AP serves whenever no wired EUD is attached. With ACS only
# a gateway serves it; other nodes keep the radio in the mesh.
auto_eud_ap_wanted() {
    grep -Eq '^acs=["'\'']?[yY]["'\'']?[[:space:]]*$' "${MANET_MESH_CONF:-/etc/mesh.conf}" 2>/dev/null || return 0
    [ -f "${MANET_GATEWAY_STATE:-/var/run/manet-uplink/mesh-gateway.state}" ]
}

# Read the first local IPv4 address on an interface.
iface_ip() {
    ip -4 -o addr show dev "$1" 2>/dev/null |
        awk '{print $4}' | cut -d/ -f1 | head -n1
}

# Probe internet reachability. http-only requires a clock-independent 204.
internet_probe() {
    local iface="$1" mode="${2:-any}"
    local url code ip host answer
    local dns="${MANET_UPLINK_DNS:-/usr/local/bin/manet-uplink-dns.py}"

    # Each ICMP target has a 2 s reply wait.
    if [ "$mode" != http-only ]; then
        ping -c 1 -W 2 -I "$iface" 1.1.1.1 >/dev/null 2>&1 && return 0
        ping -c 1 -W 2 -I "$iface" 8.8.8.8 >/dev/null 2>&1 && return 0
    fi

    if command -v curl >/dev/null 2>&1; then
        # Allow 3 s per HTTP request and accept only a 204 response.
        for url in http://connectivitycheck.gstatic.com/generate_204 \
                   http://cp.cloudflare.com/; do
            if code=$(curl -4 --noproxy '*' --interface "$iface" \
                           -s -m 3 -o /dev/null -w '%{http_code}' \
                           "$url" 2>/dev/null) && [ "$code" = 204 ]; then
                return 0
            fi
        done
        # A lone candidate cannot use global DNS before qualification. Query
        # its DHCP resolver with SO_BINDTODEVICE, then bypass curl's resolver.
        # One address per endpoint bounds this fallback to 14 extra seconds.
        if [ -x "$dns" ]; then
            for url in http://connectivitycheck.gstatic.com/generate_204 \
                       http://cp.cloudflare.com/; do
                host=${url#http://}
                host=${host%%/*}
                answer=$("$dns" resolve "$iface" "$host" 2>/dev/null |
                    head -n1)
                [ -n "$answer" ] || continue
                if code=$(curl -4 --noproxy '*' --interface "$iface" \
                        --resolve "${host}:80:${answer}" \
                        -s -m 3 -o /dev/null -w '%{http_code}' \
                        "$url" 2>/dev/null) && [ "$code" = 204 ]; then
                    return 0
                fi
            done
        fi
        return 1
    fi

    # Ethernet qualification must never fall back to a portal's open port.
    [ "$mode" = http-only ] && return 1
    # No curl in this image: bare TCP connect instead.
    ip=$(iface_ip "$iface")
    if [ -n "$ip" ] && command -v nc >/dev/null 2>&1; then
        # Bind the probe to this interface address, with a 3 s timeout.
        nc -z -w 3 -s "$ip" 1.1.1.1 53 >/dev/null 2>&1 && return 0
        nc -z -w 3 -s "$ip" 8.8.8.8 53 >/dev/null 2>&1 && return 0
    fi

    return 1
}
