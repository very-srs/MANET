#!/usr/bin/env bash
set -euo pipefail

STATE_FILE=/run/manet-uplink.env
LEGACY_GATEWAY_STATE=/var/run/mesh-gateway.state
LEGACY_NTP_STATE=/var/run/mesh-ntp.state
LEGACY_ETH_STATE=/var/run/ethernet_detection_state
UPSTREAM_IFACE_FILE=/var/run/upstream_iface
LOCK_FILE=/run/manet-uplink-dispatch.lock
NETWORKD_DIR=/etc/systemd/network

EVENT="${1:-${STATE:-reconcile}}"
IFACE="${2:-${IFACE:-${INTERFACE:-}}}"

exec 200>"$LOCK_FILE"
flock -n 200 || exit 0

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] - MANET-UPLINK: $*"
    echo "$msg" >&2
    echo "$msg" | systemd-cat -t manet-uplink
}

is_upstream_iface() {
    local iface="$1"

    [ -n "$iface" ] || return 1
    [ -d "/sys/class/net/$iface" ] || return 1

    case "$iface" in
        lo|br*|bat*|wlan*) return 1 ;;
    esac

    if [ "$iface" = "end0" ]; then
        return 0
    fi

    # USB tethering and USB Ethernet dongles normally appear as usbX/enx*/en*
    # but wlan2 is also USB-backed on these nodes, so the name filter above
    # must run before the bus check.
    local bus
    bus=$(readlink "/sys/class/net/$iface/device/subsystem" 2>/dev/null | grep -o 'usb' || true)
    [ "$bus" = "usb" ]
}

has_carrier() {
    local iface="$1"
    [ "$(cat "/sys/class/net/$iface/carrier" 2>/dev/null || echo 0)" = "1" ]
}

iface_ip() {
    ip -4 -o addr show dev "$1" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1
}

iface_default_gw() {
    ip route show default dev "$1" 2>/dev/null | awk '/^default / {print $3; exit}'
}

write_networkd_dhcp_config() {
    local iface="$1"
    local conf="${NETWORKD_DIR}/20-${iface}.network"

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
Timeout=10

[DHCPv4]
UseRoutes=yes
UseGateway=yes
EOF

    networkctl reload 2>/dev/null || true
    networkctl reconfigure "$iface" 2>/dev/null || true
}

wait_for_ipv4() {
    local iface="$1"
    local max_wait="${2:-12}"
    local ip=""

    for _ in $(seq 1 "$max_wait"); do
        ip=$(iface_ip "$iface")
        if [ -n "$ip" ]; then
            echo "$ip"
            return 0
        fi
        sleep 1
    done

    return 1
}

internet_probe() {
    local iface="$1"

    ping -c 1 -W 2 -I "$iface" 1.1.1.1 >/dev/null 2>&1 && return 0
    ping -c 1 -W 2 -I "$iface" 8.8.8.8 >/dev/null 2>&1 && return 0
    return 1
}

candidate_ifaces() {
    {
        if [ -n "$IFACE" ] && is_upstream_iface "$IFACE"; then
            echo "$IFACE"
        fi

        ip route get 1.1.1.1 2>/dev/null | awk '
            {
                for (i = 1; i <= NF; i++) {
                    if ($i == "dev") {
                        print $(i + 1)
                        exit
                    }
                }
            }
        '

        ip route show default 2>/dev/null | awk '
            /^default / {
                for (i = 1; i <= NF; i++) {
                    if ($i == "dev") {
                        print $(i + 1)
                    }
                }
            }
        '

        if is_upstream_iface end0; then
            echo end0
        fi

        for path in /sys/class/net/*; do
            local iface
            iface=$(basename "$path")
            is_upstream_iface "$iface" && echo "$iface"
        done
    } | awk '!seen[$0]++'
}

find_working_uplink() {
    local iface ip

    for iface in $(candidate_ifaces); do
        is_upstream_iface "$iface" || continue
        has_carrier "$iface" || continue

        ip link set "$iface" nomaster 2>/dev/null || true
        write_networkd_dhcp_config "$iface"
        ip=$(wait_for_ipv4 "$iface" 12 || true)
        [ -n "$ip" ] || continue
        iface_default_gw "$iface" >/dev/null || true

        if internet_probe "$iface"; then
            echo "$iface"
            return 0
        fi

        log "$iface has IPv4 ($ip) but no verified internet"
    done

    return 1
}

configure_firewall() {
    local iface="$1"
    local candidate

    nft add table inet filter 2>/dev/null || true
    nft add chain inet filter input '{ type filter hook input priority filter; policy drop; }' 2>/dev/null || true
    nft add chain inet filter forward '{ type filter hook forward priority filter; policy drop; }' 2>/dev/null || true
    nft add chain inet filter output '{ type filter hook output priority filter; policy accept; }' 2>/dev/null || true

    nft flush chain inet filter input 2>/dev/null || true
    nft flush chain inet filter forward 2>/dev/null || true

    nft add rule inet filter input ct state established,related accept
    nft add rule inet filter input ct state invalid drop
    nft add rule inet filter input iifname "lo" accept
    nft add rule inet filter input iifname "br0" accept
    nft add rule inet filter input iifname "bat0" accept
    for candidate in $(candidate_ifaces); do
        nft add rule inet filter input iifname "$candidate" accept
    done

    nft add rule inet filter forward iifname "br0" oifname "br0" accept
    nft add rule inet filter forward iifname "br0" oifname "$iface" accept
    nft add rule inet filter forward iifname "$iface" oifname "br0" ct state established,related accept

    nft add table ip nat 2>/dev/null || true
    nft add chain ip nat postrouting '{ type nat hook postrouting priority srcnat; policy accept; }' 2>/dev/null || true
    nft flush chain ip nat postrouting 2>/dev/null || true
    nft add rule ip nat postrouting oifname "$iface" masquerade

    nft add table ip mangle 2>/dev/null || true
    nft add chain ip mangle forward '{ type filter hook forward priority mangle; policy accept; }' 2>/dev/null || true
    nft flush chain ip mangle forward 2>/dev/null || true
    nft add rule ip mangle forward tcp flags syn tcp option maxseg size set rt mtu
}

clear_firewall() {
    nft flush chain ip nat postrouting 2>/dev/null || true
    nft flush chain ip mangle forward 2>/dev/null || true
}

eud_mode() {
    awk -F= '$1 == "eud" {print $2; exit}' /etc/mesh.conf 2>/dev/null || true
}

ensure_eud_services() {
    local mode ap_iface=""
    mode=$(eud_mode)
    [ -f /var/lib/ap_interface ] && ap_iface=$(cat /var/lib/ap_interface)

    if { [ "$mode" = "wireless" ] || [ "$mode" = "auto" ]; } && [ -n "$ap_iface" ]; then
        systemctl unmask dnsmasq.service 2>/dev/null || true
        systemctl enable hostapd.service 2>/dev/null || true
        systemctl start hostapd.service 2>/dev/null || true
        systemctl enable dnsmasq.service 2>/dev/null || true
        systemctl start dnsmasq.service 2>/dev/null || true
        systemctl start ap-txpower.service 2>/dev/null || true

        if ! ip link show "$ap_iface" 2>/dev/null | grep -q "master br0"; then
            ip link set "$ap_iface" master br0 2>/dev/null || true
            ip link set "$ap_iface" up 2>/dev/null || true
        fi
    fi
}

promote_gateway() {
    local iface="$1"
    local ip gw

    ip=$(iface_ip "$iface")
    gw=$(iface_default_gw "$iface")

    if [ -z "$ip" ]; then
        log "Refusing gateway promotion on $iface: no IPv4 address"
        return 1
    fi
    if [ -n "$gw" ]; then
        ip route replace default via "$gw" dev "$iface" src "$ip" metric 100 2>/dev/null || true
    fi

    configure_firewall "$iface"
    batctl gw_mode server 2>/dev/null || true

    touch "$LEGACY_GATEWAY_STATE"
    echo "$iface" > "$UPSTREAM_IFACE_FILE"
    cat > "$STATE_FILE" <<EOF
UPLINK_MODE=gateway
UPLINK_IFACE=$iface
UPLINK_IP=$ip
UPLINK_GW=${gw:-}
UPDATED_AT=$(date +%s)
EOF
    cat > "$LEGACY_ETH_STATE" <<EOF
ETH_MODE=GATEWAY
ETH_IP=$ip
DEFAULT_GW=${gw:-none}
DETECTED_AT=$(date +%s)
DETECTION_METHOD=MANET_UPLINK_DISPATCH
EOF

    cp /etc/radvd-gateway.conf /etc/radvd.conf 2>/dev/null || true
    systemctl restart radvd 2>/dev/null || true
    ensure_eud_services
    /usr/local/bin/mesh-ip-manager.sh 2>/dev/null || true
    systemctl restart gateway-route-manager.service 2>/dev/null || true

    log "Promoted $iface as MANET gateway (${ip}, gw=${gw:-none})"
}

demote_gateway() {
    local old_iface="${1:-}"

    clear_firewall
    batctl gw_mode client 2>/dev/null || true
    rm -f "$LEGACY_GATEWAY_STATE" "$LEGACY_NTP_STATE" "$LEGACY_ETH_STATE" "$STATE_FILE" "$UPSTREAM_IFACE_FILE"

    if [ -n "$old_iface" ] && is_upstream_iface "$old_iface"; then
        ip addr flush dev "$old_iface" 2>/dev/null || true
        ip link set "$old_iface" nomaster 2>/dev/null || true
        rm -f "${NETWORKD_DIR}/20-${old_iface}.network" "${NETWORKD_DIR}/20-${old_iface}-"*.network 2>/dev/null || true
        networkctl reload 2>/dev/null || true
        networkctl reconfigure "$old_iface" 2>/dev/null || true
    fi

    cp /etc/radvd-mesh.conf /etc/radvd.conf 2>/dev/null || true
    systemctl restart radvd 2>/dev/null || true
    ensure_eud_services
    /usr/local/bin/mesh-ip-manager.sh 2>/dev/null || true
    systemctl restart gateway-route-manager.service 2>/dev/null || true

    log "Demoted MANET gateway${old_iface:+ on $old_iface}"
}

current_uplink_iface() {
    if [ -f "$STATE_FILE" ]; then
        # shellcheck disable=SC1090
        . "$STATE_FILE" 2>/dev/null || true
        echo "${UPLINK_IFACE:-}"
        return
    fi

    cat "$UPSTREAM_IFACE_FILE" 2>/dev/null || true
}

reconcile() {
    local current working
    current=$(current_uplink_iface)

    working=$(find_working_uplink || true)
    if [ -n "$working" ]; then
        promote_gateway "$working"
        return 0
    fi

    demote_gateway "$current"
}

case "$EVENT" in
    carrier|routable|configured|online|add|reconcile|--hotplug)
        reconcile
        ;;
    off|no-carrier|degraded|remove|offline)
        current=$(current_uplink_iface)
        if [ -n "$IFACE" ] && [ "$IFACE" != "$current" ]; then
            log "$EVENT on $IFACE is not current uplink (${current:-none}); reconciling"
            reconcile
        else
            demote_gateway "${IFACE:-$current}"
            reconcile
        fi
        ;;
    *)
        log "Unknown event '$EVENT'; running reconcile"
        reconcile
        ;;
esac
