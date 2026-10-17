#!/bin/bash
# Configure and report a USB Wi-Fi internet client from saved credentials.
. "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet-common.sh" || exit 1
set -euo pipefail

CONFIG_FILE=${MANET_USB_WIFI_UPLINK_CONFIG:-/etc/manet/usb-wifi-uplink.conf}
WPA_DIR=/etc/wpa_supplicant
NETWORKD_DIR=/etc/systemd/network
CLIENT_SERVICE=/etc/systemd/system/usb-wifi-client@.service
SCAN_FILE=/run/manet-usb-wifi-scan.json
export PYTHONPATH="${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}:${PYTHONPATH:-}"

# Print accepted actions and arguments to stderr.
usage() {
    printf '%s\n' \
        'usage: usb-wifi-uplink.sh [ACTION [IFACE]]' \
        '       usb-wifi-uplink.sh set [SSID [PASSWORD [ENABLED]]]' \
        'actions: add online reconcile connect remove offline disconnect' \
        '         status-json status scan-json' >&2
}
case "${1:-reconcile}" in
    set-stdin) [ $# -eq 1 ] || { usage; exit 1; } ;;
    set) [ $# -le 4 ] || { usage; exit 1; } ;;
    add|online|reconcile|connect|remove|offline|disconnect|status-json|status|scan-json)
        [ $# -le 2 ] || { usage; exit 1; }
        ;;
    *) usage; exit 1 ;;
esac

ACTION="${1:-reconcile}"
IFACE="${2:-}"

# Write a prefixed message to stderr and the USB Wi-Fi journal tag.
log() {
    local msg="USB-WIFI-UPLINK: $*"
    printf '%s\n' "$msg" >&2
    printf '%s\n' "$msg" | systemd-cat -t usb-wifi-uplink
}

# Reuse the literal EUD naming rule for typed and saved credentials.
check_uplink_ssid() {
    python3 -c '
import sys
from manet_wifi_uplink import reject_mesh_hotspot
try:
    reject_mesh_hotspot(sys.argv[1])
except ValueError as error:
    sys.exit(str(error))
' "$1"
}

# Create CONFIG_FILE with default credentials only when it is missing.
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

# Print the first CONFIG_FILE value for a key, using $2 when absent or empty.
cfg_get() {
    local key="$1" default="${2:-}"
    # Keep equals signs after the first separator as part of the credential.
    awk -F= -v k="$key" '$1 == k {print substr($0, index($0, "=") + 1); found=1; exit} END {if (!found) print ""}' "$CONFIG_FILE" 2>/dev/null | {
        IFS= read -r value || true
        if [ -n "${value:-}" ]; then
            printf '%s\n' "$value"
        else
            printf '%s\n' "$default"
        fi
    }
}

# Save SSID, password and optional enabled flag to private CONFIG_FILE.
write_config() {
    local ssid="$1" password="$2" enabled="${3:-1}"
    mkdir -p "$(dirname "$CONFIG_FILE")"
    (
        umask 077
        cat > "$CONFIG_FILE" <<EOF
enabled=${enabled}
ssid=${ssid}
password=${password}
security=wpa2
EOF
    )
    chmod 600 "$CONFIG_FILE"
}


# Print an eligible IFACE or the first eligible wlan interface in sysfs.
candidate_iface() {
    if [ -n "$IFACE" ] && is_usb_wifi_uplink_iface "$IFACE"; then
        printf '%s\n' "$IFACE"
        return 0
    fi
    for path in /sys/class/net/wlan*; do
        [ -e "$path" ] || continue
        local iface
        iface="$(basename "$path")"
        if is_usb_wifi_uplink_iface "$iface"; then
            printf '%s\n' "$iface"
            return 0
        fi
    done
    return 1
}

# Write CLIENT_SERVICE and reload systemd before starting client instances.
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

# Write a private WPA_DIR config for the interface, SSID and password.
write_wpa_config() {
    local iface="$1" ssid="$2" password="$3" security="$4" bssid="$5" conf="${WPA_DIR}/wpa_supplicant-${iface}-uplink.conf"
    mkdir -p "$WPA_DIR"

    # Derive WPA2 PSK locally. stdin keeps passwords out of process arguments,
    # and hex SSID rendering preserves quotes/backslashes literally.
    ( umask 077
      printf '%s\n%s\n%s\n%s\n' "$ssid" "$password" "$security" "$bssid" | python3 -c '
import hashlib, re, sys
ssid = sys.stdin.readline().removesuffix("\n")
password = sys.stdin.readline().removesuffix("\n")
security = sys.stdin.readline().strip()
bssid = sys.stdin.readline().strip()
if (not 1 <= len(ssid.encode()) <= 32 or security not in ("open", "wpa2")
    or (security != "open" and not 8 <= len(password.encode()) <= 63)):
    sys.exit("Invalid USB Wi-Fi credentials")
if bssid and not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", bssid):
    sys.exit("Invalid USB Wi-Fi access point")
auth = "key_mgmt=NONE" if security == "open" else "psk=" + hashlib.pbkdf2_hmac(
    "sha1", password.encode(), ssid.encode(), 4096, 32).hex()
pin = "\n    bssid=" + bssid if bssid else ""
print("network={\n    ssid=" + ssid.encode().hex() + "\n    " + auth + pin + "\n}")
' > "$conf"
    )
    chmod 600 "$conf"
}

# Write and reload NETWORKD_DIR DHCP settings for the supplied interface.
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
UseDNS=no
UseNTP=yes
UseRoutes=yes
Timeout=15

[DHCPv4]
UseRoutes=yes
UseGateway=yes

# BEGIN MANET qualified DNS
[DHCPv4]
UseDNS=no
UseDomains=no
RoutesToDNS=no
RequestOptions=6 15 119

[DHCPv6]
UseDNS=no
UseDomains=no

[IPv6AcceptRA]
UseDNS=no
UseDomains=no
# END MANET qualified DNS
EOF
    chmod 644 "$conf"
    networkctl reload 2>/dev/null || true
}

# Configure the selected IFACE from CONFIG_FILE and dispatch gateway setup.
connect_iface() {
    ensure_default_config
    ensure_client_service

    local iface ssid password enabled security bssid
    iface="$(candidate_iface || true)"
    if [ -z "$iface" ]; then
        log "No USB Wi-Fi uplink adapter found"
        return 1
    fi

    enabled="$(cfg_get enabled 1)"
    if [ "$enabled" != "1" ]; then
        log "USB Wi-Fi uplink disabled in $CONFIG_FILE"
        remove_iface "$iface"
        return 1
    fi

    ssid="$(cfg_get ssid hotspot)"
    if ! check_uplink_ssid "$ssid"; then
        remove_iface "$iface"
        return 1
    fi
    security="$(cfg_get security wpa2)"
    bssid="$(cfg_get bssid '')"
    password="$(cfg_get password raspberry)"
    [ "$security" != open ] || password=""
    if [ -z "$ssid" ] || { [ "$security" != open ] && [ -z "$password" ]; }; then
        log "SSID/password missing in $CONFIG_FILE"
        return 1
    fi

    # Detach bridge membership before using this radio as an internet client.
    ip link set "$iface" nomaster 2>/dev/null || true
    ip link set "$iface" up 2>/dev/null || true
    write_wpa_config "$iface" "$ssid" "$password" "$security" "$bssid"
    write_networkd_config "$iface"

    # Stop the mesh instance before the dedicated client takes the interface.
    systemctl stop "wpa_supplicant@${iface}.service" 2>/dev/null || true
    systemctl enable "usb-wifi-client@${iface}.service" 2>/dev/null || true
    systemctl restart "usb-wifi-client@${iface}.service"
    # Wait three seconds after client startup before reconfiguring networkd.
    sleep 3
    networkctl reconfigure "$iface" 2>/dev/null || true

    log "Configured $iface as USB Wi-Fi uplink client"
    /usr/local/bin/manet-uplink-dispatch.sh add "$iface" || true
}

# Stop the supplied client interface and remove its configs and gateway role.
remove_iface() {
    local iface="$1"
    [ -n "$iface" ] || iface="$(candidate_iface || true)"
    [ -n "$iface" ] || return 0
    systemctl stop "usb-wifi-client@${iface}.service" 2>/dev/null || true
    systemctl disable "usb-wifi-client@${iface}.service" 2>/dev/null || true
    ip link set "$iface" down 2>/dev/null || true
    ip addr flush dev "$iface" 2>/dev/null || true
    rm -f "${NETWORKD_DIR}/20-${iface}-usb-wifi-uplink.network" "${WPA_DIR}/wpa_supplicant-${iface}-uplink.conf"
    networkctl reload 2>/dev/null || true
    /usr/local/bin/manet-uplink-dispatch.sh remove "$iface" || true
}

# Print selected interface, saved SSID, link and service state as JSON.
status_json() {
    ensure_default_config
    local iface ssid enabled state ip connected service link freq security gateway bssid error
    iface="$(candidate_iface || true)"
    ssid="$(cfg_get ssid hotspot)"
    enabled="$(cfg_get enabled 1)"
    state="missing"
    ip=""
    connected=false
    service=""
    freq=0
    security="$(cfg_get security wpa2)"
    gateway=false
    bssid=""
    if [ -n "$iface" ]; then
        # The first address comes from ip's CIDR column with the mask removed.
        ip="$(ip -4 -o addr show dev "$iface" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1)"
        service="$(systemctl is-active "usb-wifi-client@${iface}.service" 2>/dev/null || true)"
        # An iw association marks connected even if DHCP has no address yet.
        link="$(iw dev "$iface" link 2>/dev/null || true)"
        if grep -q '^Connected to ' <<< "$link"; then
            connected=true
            state="connected"
            freq="$(awk '/^[[:space:]]*freq: / {print $2; exit}' <<< "$link")"
            bssid="$(awk '/^Connected to / {print $3; exit}' <<< "$link")"
        elif [ "$service" = "active" ]; then
            state="connecting"
        else
            state="idle"
        fi
        if [ -f /var/run/gateway ] && [ "$(cat /run/upstream_iface 2>/dev/null)" = "$iface" ]; then
            gateway=true
        fi
    fi
    error=""
    if ! error="$(check_uplink_ssid "$ssid" 2>&1)"; then
        enabled=0
        connected=false
        gateway=false
        ip=""
        state=blocked
    fi
    jq -cn --arg error "$error" --arg iface "$iface" --arg ssid "$ssid" --arg state "$state" \
        --arg ip "$ip" --arg service "$service" \
        --arg security "$security" --argjson freq "${freq:-0}" --argjson gateway "$gateway" \
        --arg bssid "$bssid" \
        --argjson enabled "$([ "$enabled" = 1 ] && echo true || echo false)" \
        --argjson connected "$([ "$connected" = true ] && echo true || echo false)" \
        '{error: $error, enabled: $enabled, iface: $iface, ssid: $ssid, state: $state,
          connected: $connected, ip: $ip, service: $service,
          security: $security, freq: $freq, gateway: $gateway, bssid: $bssid}'
}

case "$ACTION" in
    scan-json)
        iface="$(candidate_iface || true)"
        [ -n "$iface" ] || { printf '%s\n' '{"error":"Plug in a USB Wi-Fi adapter and try again"}'; exit 1; }
        python3 -c '
import json, subprocess, sys
from pathlib import Path
from manet_config_io import atomic_write
from manet_wifi_uplink import usb_scan_result
iface, path = sys.argv[1:]
was_up = bool(int(Path("/sys/class/net", iface, "flags").read_text().strip(), 16) & 1)
def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=40).stdout
try:
    if not was_up:
        run(["ip", "link", "set", iface, "up"])
    output = run(["iw", "dev", iface, "scan", "passive"])
    result = usb_scan_result(output, iface)
    atomic_write(path, json.dumps(result) + "\n", mode=0o600)
    print(json.dumps(result))
except (OSError, subprocess.SubprocessError):
    print(json.dumps({"error": "Could not scan this USB adapter. Try again."}))
    sys.exit(1)
finally:
    if not was_up:
        run(["ip", "link", "set", iface, "down"])
' "$iface" "$SCAN_FILE"
        ;;
    set-stdin)
        mkdir -p "$(dirname "$CONFIG_FILE")"
        # Parse stdin as data, validate before writing, and never print secrets.
        python3 -c '
import json, os, sys, tempfile
from pathlib import Path
from manet_wifi_uplink import validate_usb_selection, reject_mesh_hotspot
p = json.load(sys.stdin)
ssid, password, enabled = p.get("ssid"), p.get("password"), p.get("enabled", True)
security = p.get("security", "wpa2")
if type(enabled) is not bool:
    sys.exit("Invalid enabled flag")
if not enabled:
    ssid, password, security = "", "", "wpa2"
elif (not isinstance(ssid, str) or not isinstance(password, str)
      or security not in ("open", "wpa2")
      or not 1 <= len(ssid.encode()) <= 32
      or (security != "open" and not 8 <= len(password.encode()) <= 63)
      or any(c in ssid + password for c in "\r\n\0")):
    sys.exit("Invalid USB Wi-Fi credentials")
if enabled:
    try:
        reject_mesh_hotspot(ssid)
    except ValueError as error:
        sys.exit(str(error))
bssid = ""
if enabled and "bssid" in p:
    try:
        validate_usb_selection(p, json.loads(Path(sys.argv[2]).read_text()), sys.argv[3])
    except (ValueError, OSError):
        sys.exit("Scan again and choose a network from the fresh list")
    bssid = p["bssid"]
if security == "open":
    password = ""
path = sys.argv[1]
fd, temporary = tempfile.mkstemp(dir=os.path.dirname(path))
with os.fdopen(fd, "w") as stream:
    stream.write("enabled=%d\nssid=%s\npassword=%s\nsecurity=%s\n" % (enabled, ssid, password, security))
    stream.write("bssid=%s\n" % bssid)
    stream.flush(); os.fsync(stream.fileno())
os.replace(temporary, path)
' "$CONFIG_FILE" "$SCAN_FILE" "$(candidate_iface || true)"
        if ! connect_iface; then status_json; exit 0; fi
        ;;
    set)
        if [ "${4:-1}" = 1 ]; then check_uplink_ssid "${2:-hotspot}"; fi
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
