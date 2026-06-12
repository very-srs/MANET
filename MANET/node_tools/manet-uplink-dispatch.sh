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

    case "$iface" in
        wlan0|wlan1|wlan2|wlan3) return 1 ;;
        wlan*) ;;
        *) return 1 ;;
    esac

    bus=$(readlink "/sys/class/net/$iface/device/subsystem" 2>/dev/null | grep -o 'usb' || true)
    [ "$bus" = "usb" ] || return 1

    driver="$(iface_driver "$iface")"
    case "$driver" in
        morse*|brcmfmac) return 1 ;;
    esac

    grep -qx "$iface" /var/lib/mesh_if /var/lib/halow_if /var/lib/no_mesh_if 2>/dev/null && return 1
    return 0
}

# systemctl unmask/enable are NOT cheap no-ops: unmask always triggers a full
# daemon-reload, and enable on units with sysv shims (dnsmasq, hostapd) spawns
# update-rc.d which triggers more reloads. This script runs every node-manager
# cycle, so guard them behind state checks.
unmask_if_masked() {
    [ "$(systemctl is-enabled "$1" 2>/dev/null)" = "masked" ] && \
        systemctl unmask "$1" 2>/dev/null || true
}

enable_if_disabled() {
    systemctl is-enabled --quiet "$1" 2>/dev/null || \
        systemctl enable "$1" 2>/dev/null || true
}

start_if_inactive() {
    systemctl is-active --quiet "$1" 2>/dev/null || \
        systemctl start "$1" 2>/dev/null || true
}

# Restart radvd only when its config actually changes; otherwise just make
# sure it is running.
apply_radvd_conf() {
    local src="$1"
    [ -f "$src" ] || return 0
    if ! cmp -s "$src" /etc/radvd.conf 2>/dev/null; then
        cp "$src" /etc/radvd.conf 2>/dev/null || true
        systemctl restart radvd 2>/dev/null || true
    else
        start_if_inactive radvd.service
    fi
}

is_upstream_iface() {
    local iface="$1"

    [ -n "$iface" ] || return 1
    [ -d "/sys/class/net/$iface" ] || return 1

    case "$iface" in
        lo|br*|bat*) return 1 ;;
        wlan*) is_usb_wifi_uplink_iface "$iface" && return 0 || return 1 ;;
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

    # Only write the file if it does not already exist. Overwriting it with
    # identical content still fires an inotify event that causes networkd to
    # reconfigure the interface, briefly drops the DHCP lease, and re-triggers
    # this dispatch loop.
    if [ ! -f "$conf" ]; then
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
    fi

    # Only reconfigure (restart DHCP) when the interface has no IP yet.
    # networkctl reconfigure always causes a brief DHCP lease loss even without
    # a file change — avoid it when DHCP is already working.
    if [ -z "$(iface_ip "$iface")" ]; then
        networkctl reconfigure "$iface" 2>/dev/null || true
    fi
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
    local iface ip gw

    for iface in $(candidate_ifaces); do
        is_upstream_iface "$iface" || continue
        has_carrier "$iface" || continue

        ip link set "$iface" nomaster 2>/dev/null || true

        # Skip reconfiguring if the interface already has an IP and a default
        # route — rewriting the networkd config triggers inotify, briefly drops
        # the DHCP lease, and causes the internet probe to fail on the route
        # re-installation race, which creates a demote→carrier→loop cycle.
        ip=$(iface_ip "$iface")
        gw=$(iface_default_gw "$iface" || true)
        if [ -z "$ip" ] || [ -z "$gw" ]; then
            write_networkd_dhcp_config "$iface"
            ip=$(wait_for_ipv4 "$iface" 12 || true)
        fi

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
        unmask_if_masked dnsmasq.service
        enable_if_disabled hostapd.service
        start_if_inactive hostapd.service
        enable_if_disabled dnsmasq.service
        start_if_inactive dnsmasq.service
        start_if_inactive ap-txpower.service

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

    batctl gw_mode server 2>/dev/null || true

    # Steady state: reconcile runs every node-manager cycle; if this exact
    # uplink is already promoted, skip the reconfiguration below (firewall
    # rewrite, radvd/gateway-route-manager restarts, mesh-ip-manager rerun).
    local prev_mode="" prev_iface="" prev_ip="" prev_gw=""
    if [ -f "$STATE_FILE" ]; then
        prev_mode=$(awk -F= '$1 == "UPLINK_MODE" {print $2}' "$STATE_FILE" 2>/dev/null)
        prev_iface=$(awk -F= '$1 == "UPLINK_IFACE" {print $2}' "$STATE_FILE" 2>/dev/null)
        prev_ip=$(awk -F= '$1 == "UPLINK_IP" {print $2}' "$STATE_FILE" 2>/dev/null)
        prev_gw=$(awk -F= '$1 == "UPLINK_GW" {print $2}' "$STATE_FILE" 2>/dev/null)
    fi
    if [ "$prev_mode" = "gateway" ] && [ "$prev_iface" = "$iface" ] && \
       [ "$prev_ip" = "$ip" ] && [ "$prev_gw" = "${gw:-}" ] && \
       [ -f "$LEGACY_GATEWAY_STATE" ]; then
        ensure_eud_services
        return 0
    fi

    configure_firewall "$iface"

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

    apply_radvd_conf /etc/radvd-gateway.conf
    ensure_eud_services
    /usr/local/bin/mesh-ip-manager.sh 2>/dev/null || true
    systemctl restart gateway-route-manager.service 2>/dev/null || true

    log "Promoted $iface as MANET gateway (${ip}, gw=${gw:-none})"
}

demote_gateway() {
    local old_iface="${1:-}"
    local was_gateway=false

    if [ -f "$STATE_FILE" ] || [ -f "$LEGACY_GATEWAY_STATE" ] || [ -f "$LEGACY_ETH_STATE" ]; then
        was_gateway=true
    fi

    clear_firewall
    batctl gw_mode client 2>/dev/null || true
    rm -f "$LEGACY_GATEWAY_STATE" "$LEGACY_NTP_STATE" "$LEGACY_ETH_STATE" "$STATE_FILE" "$UPSTREAM_IFACE_FILE"

    if [ -n "$old_iface" ] && is_upstream_iface "$old_iface"; then
        ip addr flush dev "$old_iface" 2>/dev/null || true
        ip link set "$old_iface" nomaster 2>/dev/null || true
        # Do not delete 20-*.network or call networkctl reload: removing the
        # file fires an inotify event that triggers a networkd reconfigure,
        # briefly drops the DHCP lease, and re-triggers this loop.
        # 10-end0.network takes precedence and handles DHCP regardless.
        networkctl reconfigure "$old_iface" 2>/dev/null || true
    fi

    apply_radvd_conf /etc/radvd-mesh.conf
    ensure_eud_services

    # Steady state: with no uplink, reconcile lands here every node-manager
    # cycle. Only rerun mesh-ip-manager / restart gateway-route-manager when
    # an actual demotion happened.
    if [ "$was_gateway" = true ]; then
        /usr/local/bin/mesh-ip-manager.sh 2>/dev/null || true
        systemctl restart gateway-route-manager.service 2>/dev/null || true
        log "Demoted MANET gateway${old_iface:+ on $old_iface}"
    fi
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
