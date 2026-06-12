#!/usr/bin/env bash
set -euo pipefail

CONFIG_FILE=/etc/manet/usb-wifi-uplink.conf
WPA_DIR=/etc/wpa_supplicant
NETWORKD_DIR=/etc/systemd/network
CLIENT_SERVICE=/etc/systemd/system/usb-wifi-client@.service

ACTION="${1:-reconcile}"
IFACE="${2:-}"

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] - USB-WIFI-UPLINK: $*"
    echo "$msg" >&2
    echo "$msg" | systemd-cat -t usb-wifi-uplink
}

ensure_default_config() {
    mkdir -p "$(dirname "$CONFIG_FILE")"
    if [ ! -f "$CONFIG_FILE" ]; then
        cat > "$CONFIG_FILE" <<EOF
enabled=1
ssid=hotspot
password=raspberry
EOF
        chmod 600 "$CONFIG_FILE"
    fi
}

cfg_get() {
    local key="$1" default="${2:-}"
    awk -F= -v k="$key" '$1 == k {print substr($0, index($0, "=") + 1); found=1; exit} END {if (!found) print ""}' "$CONFIG_FILE" 2>/dev/null | {
        read -r value || true
        if [ -n "${value:-}" ]; then
            printf '%s\n' "$value"
        else
            printf '%s\n' "$default"
        fi
    }
}

write_config() {
    local ssid="$1" password="$2" enabled="${3:-1}"
    mkdir -p "$(dirname "$CONFIG_FILE")"
    (
        umask 077
        cat > "$CONFIG_FILE" <<EOF
enabled=${enabled}
ssid=${ssid}
password=${password}
EOF
    )
    chmod 600 "$CONFIG_FILE"
}

iface_driver() {
    local iface="$1" driver
    driver="$(basename "$(readlink -f "/sys/class/net/$iface/device/driver" 2>/dev/null)" 2>/dev/null || true)"
    if [ -z "$driver" ] || [ "$driver" = "." ]; then
        driver="$(ethtool -i "$iface" 2>/dev/null | awk -F': ' '$1 == "driver" {print $2; exit}')"
    fi
    echo "$driver"
}

is_usb_wifi_uplink_iface() {
    local iface="$1" bus driver
    [ -n "$iface" ] || return 1
    [ -d "/sys/class/net/$iface" ] || return 1

    case "$iface" in
        wlan0|wlan1|wlan2|wlan3|lo|br*|bat*) return 1 ;;
        wlan*) ;;
        *) return 1 ;;
    esac

    bus="$(readlink "/sys/class/net/$iface/device/subsystem" 2>/dev/null | grep -o 'usb' || true)"
    [ "$bus" = "usb" ] || return 1

    driver="$(iface_driver "$iface")"
    case "$driver" in
        morse*|brcmfmac) return 1 ;;
    esac

    grep -qx "$iface" /var/lib/mesh_if /var/lib/halow_if /var/lib/no_mesh_if 2>/dev/null && return 1
    return 0
}

candidate_iface() {
    if [ -n "$IFACE" ] && is_usb_wifi_uplink_iface "$IFACE"; then
        echo "$IFACE"
        return 0
    fi
    for path in /sys/class/net/wlan*; do
        [ -e "$path" ] || continue
        local iface
        iface="$(basename "$path")"
        if is_usb_wifi_uplink_iface "$iface"; then
            echo "$iface"
            return 0
        fi
    done
    return 1
}

ensure_client_service() {
    cat > "$CLIENT_SERVICE" <<'EOF'
[Unit]
Description=USB Wi-Fi internet uplink client on %i
Requires=sys-subsystem-net-devices-%i.device
After=sys-subsystem-net-devices-%i.device wifi-rfkill-unblock.service
Wants=wifi-rfkill-unblock.service

[Service]
Type=simple
ExecStartPre=/usr/local/bin/unblock-wifi-rfkill.sh
ExecStart=/usr/sbin/wpa_supplicant -c /etc/wpa_supplicant/wpa_supplicant-%I-uplink.conf -i %I -D nl80211
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
    chmod 644 "$CLIENT_SERVICE"
    systemctl daemon-reload 2>/dev/null || true
}

write_wpa_config() {
    local iface="$1" ssid="$2" password="$3" conf="${WPA_DIR}/wpa_supplicant-${iface}-uplink.conf"
    mkdir -p "$WPA_DIR"

    if command -v wpa_passphrase >/dev/null 2>&1; then
        wpa_passphrase "$ssid" "$password" | sed '/^[[:space:]]*#psk=/d' > "$conf"
    else
        cat > "$conf" <<EOF
network={
    ssid="${ssid}"
    psk="${password}"
}
EOF
    fi
    chmod 600 "$conf"
}

write_networkd_config() {
    local iface="$1" conf="${NETWORKD_DIR}/20-${iface}-usb-wifi-uplink.network"
    cat > "$conf" <<EOF
[Match]
Name=${iface}

[Link]
RequiredForOnline=yes

[Network]
DHCP=ipv4
IPv6AcceptRA=yes
Bridge=

[DHCP]
ClientIdentifier=mac
UseDNS=yes
UseNTP=yes
UseRoutes=yes
Timeout=15

[DHCPv4]
UseRoutes=yes
UseGateway=yes
EOF
    chmod 644 "$conf"
    networkctl reload 2>/dev/null || true
}

connect_iface() {
    ensure_default_config
    ensure_client_service

    local iface ssid password enabled
    iface="$(candidate_iface || true)"
    if [ -z "$iface" ]; then
        log "No USB Wi-Fi uplink adapter found"
        return 1
    fi

    enabled="$(cfg_get enabled 1)"
    if [ "$enabled" != "1" ]; then
        log "USB Wi-Fi uplink disabled in $CONFIG_FILE"
        return 1
    fi

    ssid="$(cfg_get ssid hotspot)"
    password="$(cfg_get password raspberry)"
    if [ -z "$ssid" ] || [ -z "$password" ]; then
        log "SSID/password missing in $CONFIG_FILE"
        return 1
    fi

    ip link set "$iface" nomaster 2>/dev/null || true
    ip link set "$iface" up 2>/dev/null || true
    write_wpa_config "$iface" "$ssid" "$password"
    write_networkd_config "$iface"

    systemctl stop "wpa_supplicant@${iface}.service" 2>/dev/null || true
    systemctl enable "usb-wifi-client@${iface}.service" 2>/dev/null || true
    systemctl restart "usb-wifi-client@${iface}.service"
    sleep 3
    networkctl reconfigure "$iface" 2>/dev/null || true

    log "Configured $iface as USB Wi-Fi uplink client for SSID '$ssid'"
    /usr/local/bin/manet-uplink-dispatch.sh add "$iface" || true
}

remove_iface() {
    local iface="$1"
    [ -n "$iface" ] || iface="$(candidate_iface || true)"
    [ -n "$iface" ] || return 0
    systemctl stop "usb-wifi-client@${iface}.service" 2>/dev/null || true
    systemctl disable "usb-wifi-client@${iface}.service" 2>/dev/null || true
    rm -f "${NETWORKD_DIR}/20-${iface}-usb-wifi-uplink.network" "${WPA_DIR}/wpa_supplicant-${iface}-uplink.conf"
    networkctl reload 2>/dev/null || true
    /usr/local/bin/manet-uplink-dispatch.sh remove "$iface" || true
}

status_json() {
    ensure_default_config
    local iface ssid enabled state ip connected service
    iface="$(candidate_iface || true)"
    ssid="$(cfg_get ssid hotspot)"
    enabled="$(cfg_get enabled 1)"
    state="missing"
    ip=""
    connected=false
    service=""
    if [ -n "$iface" ]; then
        ip="$(ip -4 -o addr show dev "$iface" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1)"
        service="$(systemctl is-active "usb-wifi-client@${iface}.service" 2>/dev/null || true)"
        if iw dev "$iface" link 2>/dev/null | grep -q '^Connected to '; then
            connected=true
            state="connected"
        elif [ "$service" = "active" ]; then
            state="connecting"
        else
            state="idle"
        fi
    fi
    python3 - "$enabled" "$iface" "$ssid" "$state" "$connected" "$ip" "$service" <<'PY'
import json
import sys

enabled, iface, ssid, state, connected, ip, service = sys.argv[1:]
print(json.dumps({
    "enabled": enabled == "1",
    "iface": iface,
    "ssid": ssid,
    "state": state,
    "connected": connected == "true",
    "ip": ip,
    "service": service,
}))
PY
}

case "$ACTION" in
    set)
        ensure_default_config
        write_config "${2:-hotspot}" "${3:-raspberry}" "${4:-1}"
        if ! connect_iface; then
            status_json
            exit 0
        fi
        ;;
    add|online|reconcile|connect)
        connect_iface
        ;;
    remove|offline|disconnect)
        remove_iface "$IFACE"
        ;;
    status-json|status)
        status_json
        ;;
    *)
        log "Unknown action '$ACTION'"
        exit 1
        ;;
esac
