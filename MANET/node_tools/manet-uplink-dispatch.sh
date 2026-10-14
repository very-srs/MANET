#!/bin/bash
# Reconcile local internet uplinks, gateway state and EUD radio roles.
. "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet-common.sh" || exit 1
set -euo pipefail
. "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet-runtime-client.sh"

STATE_FILE=/run/manet-uplink.env
LEGACY_GATEWAY_STATE=/var/run/mesh-gateway.state
LEGACY_NTP_STATE=/var/run/mesh-ntp.state
LEGACY_ETH_STATE=/var/run/ethernet_detection_state
UPSTREAM_IFACE_FILE=/var/run/upstream_iface
LOCK_FILE=/run/manet-uplink-dispatch.lock
ETH_DETECT_LOCK=${MANET_ETH_DETECT_LOCK:-/var/run/ethernet-autodetect.lock}
AP_MESH_HELPER=${MANET_AP_MESH_HELPER:-/usr/local/bin/manet_ap_mesh.py}
UPLINK_SPEED=${MANET_UPLINK_SPEED:-/usr/local/bin/manet-uplink-speed.sh}
NETWORKD_DIR=/etc/systemd/network

# Explicit arguments override networkd-dispatcher environment fields.
EVENT="${1:-${STATE:-reconcile}}"
IFACE="${2:-${IFACE:-${INTERFACE:-}}}"

# Print accepted events and argument syntax to stderr.
usage() {
    printf '%s\n' \
        'usage: manet-uplink-dispatch.sh [EVENT [IFACE]]' \
        'events: carrier routable configured online add reconcile --hotplug' \
        '        off no-carrier degraded remove offline provision' >&2
}
[ $# -le 2 ] || { usage; exit 1; }
case "$EVENT" in
    carrier|routable|configured|online|add|reconcile|--hotplug|provision) ;;
    off|no-carrier|degraded|remove|offline) ;;
    *) usage; exit 1 ;;
esac

# Setup needs DNS before apt, so allow an existing dispatch/detection to finish.
# Runtime events still skip overlap and retry on the next manager cycle.
lock_args=(-n)
[ "$EVENT" != provision ] || lock_args=(-w 30)
exec 200>"$LOCK_FILE"
flock "${lock_args[@]}" 200 || exit 0

# ethernet-autodetect owns end0 during DHCP probing and bridging. It does not
# call this script while locked. Runtime events skip a busy lock and retry
# on a later telemetry pass; setup waits within its overall timeout.
exec 201>"$ETH_DETECT_LOCK"
if ! flock "${lock_args[@]}" 201; then
    exit 0
fi

# Send the uplink message to stderr and the manet-uplink journal tag.
log() {
    local msg="MANET-UPLINK: $*"
    printf '%s\n' "$msg" >&2
    printf '%s\n' "$msg" | systemd-cat -t manet-uplink
}


# Guard unmask/enable with state checks on each node-manager cycle.
# Unmask reloads systemd. Enabling SysV-backed units such as dnsmasq and
# hostapd also reloads through update-rc.d.


# Start an inactive unit, treating a start failure as recoverable.
start_if_inactive() {
    systemctl is-active --quiet "$1" 2>/dev/null || \
        systemctl start "$1" 2>/dev/null || true
}

# Restart radvd when its config changes. Otherwise ensure it is running.
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

# Accept end0 and eligible USB devices outside mesh and AP roles.
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

# Check the named interface for a sysfs carrier value of one.
has_carrier() {
    local iface="$1"
    [ "$(cat "/sys/class/net/$iface/carrier" 2>/dev/null || echo 0)" = "1" ]
}

# A wired EUD is an Ethernet-class port with carrier bridged into br0 by
# ethernet-autodetect. Uplinks are never br0 ports, so membership decides
# the role independently of state files.
is_wired_eud_port() {
    local iface="$1" master
    is_upstream_iface "$iface" || return 1
    has_carrier "$iface" || return 1
    master=$(basename "$(readlink "/sys/class/net/$iface/master" 2>/dev/null)" 2>/dev/null || true)
    [ "$master" = "br0" ]
}

# Check whether any eligible interface is a carrier-bearing br0 port.
wired_eud_active() {
    local path
    for path in /sys/class/net/*; do
        is_wired_eud_port "$(basename "$path")" && return 0
    done
    return 1
}


# Print the first default route next hop for the named interface.
iface_default_gw() {
    ip route show default dev "$1" 2>/dev/null | awk '/^default / {print $3; exit}'
}

# Add missing DHCP configuration under NETWORKD_DIR for the interface.
# Reload new configuration. Retry DHCP only if no IPv4 address is present.
write_networkd_dhcp_config() {
    local iface="$1"
    local conf="${NETWORKD_DIR}/20-${iface}.network"

    # Write only missing configs. Even identical writes trigger networkd
    # inotify reconfiguration, dropping DHCP and repeating dispatch.
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
UseDNS=no
UseNTP=yes
UseRoutes=yes
Timeout=10

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
        networkctl reload 2>/dev/null || true
    fi

    # Only reconfigure (restart DHCP) when the interface has no IP yet.
    # networkctl reconfigure always causes a brief DHCP lease loss even without
    # a file change: avoid it when DHCP is already working.
    if [ -z "$(iface_ip "$iface")" ]; then
        networkctl reconfigure "$iface" 2>/dev/null || true
    fi
}

# Print the first IPv4 on $1, polling once per second.
# Try $2 times, defaulting to 12, and fail if no address appears.
wait_for_ipv4() {
    local iface="$1"
    local max_wait="${2:-12}"
    local ip=""

    for _ in $(seq 1 "$max_wait"); do
        ip=$(iface_ip "$iface")
        if [ -n "$ip" ]; then
            printf '%s\n' "$ip"
            return 0
        fi
        sleep 1
    done

    return 1
}

# Print event, routed, default-route, end0 and discovered candidates in order.
# Read global IFACE and remove duplicates without changing their priority.
candidate_ifaces() {
    {
        if [ -n "$IFACE" ] && is_upstream_iface "$IFACE"; then
            printf '%s\n' "$IFACE"
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
            is_upstream_iface "$iface" && printf '%s\n' "$iface"
        done
    } | awk '!seen[$0]++'
}

# Print the first eligible uplink passing address and internet checks.
# Configure missing DHCP. Ethernet requires HTTP 204, never just ICMP.
find_working_uplink() {
    local iface ip gw probe_mode

    for iface in $(candidate_ifaces); do
        is_upstream_iface "$iface" || continue
        has_carrier "$iface" || continue
        # Never detach or DHCP-probe a port serving a wired EUD.
        is_wired_eud_port "$iface" && continue

        ip link set "$iface" nomaster 2>/dev/null || true

        # Keep an interface with an IP and default route configured. Rewriting
        # its networkd config drops the lease through inotify, so a probe can
        # race route restoration and repeatedly demote the uplink.
        ip=$(iface_ip "$iface")
        gw=$(iface_default_gw "$iface" || true)
        if [ -z "$ip" ] || [ -z "$gw" ]; then
            write_networkd_dhcp_config "$iface"
            ip=$(wait_for_ipv4 "$iface" 12 || true)
        fi

        [ -n "$ip" ] || continue
        iface_default_gw "$iface" >/dev/null || true

        probe_mode=any
        "$UPLINK_SPEED" is-ethernet "$iface" && probe_mode=http-only
        if internet_probe "$iface" "$probe_mode"; then
            # Measurement failure means unknown bandwidth, not no internet.
            if [ "$EVENT" != provision ]; then
                "$UPLINK_SPEED" measure "$iface" >/dev/null || true
            fi
            printf '%s\n' "$iface"
            return 0
        fi
    done

    return 1
}

# Rebuild filter, NAT and TCP MSS rules for the uplink interface in $1.
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
    # The earlier manet_ui table enforces access to ports 80 and 5201.
    # Keep these accepts broad. See manet-ui-firewall.sh.
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
    # Clamp TCP SYN maximum segment size to the route MTU.
    nft add rule ip mangle forward tcp flags syn tcp option maxseg size set rt mtu
}

# Flush the gateway NAT and MSS chains without changing filter rules.
clear_firewall() {
    nft flush chain ip nat postrouting 2>/dev/null || true
    nft flush chain ip mangle forward 2>/dev/null || true
}

# Print the first eud value from mesh.conf without executing its contents.
eud_mode() {
    awk -F= '$1 == "eud" {print $2; exit}' /etc/mesh.conf 2>/dev/null || true
}

# Start hostapd before its DHCP and power helpers, with $1 used in errors.
start_ap_services() {
    local ap_iface="$1"   # for the log only
    unmask_if_masked dnsmasq.service
    enable_if_disabled hostapd.service
    # Synchronous, so a start cannot stay queued past this pass and race the
    # next mesh return. hostapd's ExecStartPre (prepare-ap) bounds its wait for
    # the channel lock, and systemd bounds the start as a whole.
    if ! systemctl is-active --quiet hostapd.service 2>/dev/null &&
            ! systemctl start hostapd.service 2>/dev/null; then
        log "hostapd start failed on $ap_iface; retrying next reconcile"
        return 1
    fi
    enable_if_disabled dnsmasq.service
    start_if_inactive dnsmasq.service
    start_if_inactive ap-txpower.service
    # hostapd bridges the AP into br0 itself (bridge=br0). Enslaving the
    # radio here could grab it while it is still a mesh point.
}

# Return the AP candidate radio to the mesh. The helper is idempotent (a radio
# already meshing on the right channel is left alone) and owns roles, configs,
# channel choice and locking, so calling it every reconcile doubles as retry.
ap_mesh_request() {
    local rc=125
    # Use the caller's explicit override when provided.
    if [ -z "${MANET_AP_MESH_HELPER:-}" ]; then
        rc=0
        manet_runtime_call ap-mesh || rc=$?
        # Only 125 guarantees no request was sent and permits a fallback.
        [ "$rc" = 125 ] || return "$rc"
    fi
    python3 "$AP_MESH_HELPER" mesh
}

# Run the AP-to-mesh helper and log changes or a deferred transition.
return_ap_radio_to_mesh() {
    if [ ! -x "$AP_MESH_HELPER" ]; then
        log "Cannot return AP radio to mesh: $AP_MESH_HELPER missing"
        return 0
    fi
    local out err rc reason
    err=$(mktemp)
    if out=$(ap_mesh_request 2>"$err"); then
        # Log role changes and registry disagreement. Stay quiet when unchanged.
        if printf '%s' "$out" | grep -Eq '"changed": *true|"registry": *"conflict"'; then
            log "AP radio mesh transition: $out"
        fi
    else
        rc=$?
        reason=$(tail -c 400 "$err" | tr '\n' ' ')
        [[ "$reason" == *[![:space:]]* ]] ||
            reason="helper exited $rc without a diagnostic"
        log "AP radio mesh return deferred; retrying next reconcile: $reason"
    fi
    rm -f "$err"
}

# Apply mesh.conf EUD policy using the saved AP candidate and live port roles.
ensure_eud_services() {
    local mode ap_iface=""
    mode=$(eud_mode)
    [ -f /var/lib/ap_interface ] && ap_iface=$(cat /var/lib/ap_interface)
    [ -n "$ap_iface" ] || return 0

    case "$mode" in
        wireless)
            start_ap_services "$ap_iface" || true
            ;;
        auto)
            # A wired EUD takes over the EUD role: the radio belongs in the
            # mesh, and starting hostapd here would pull it back out. With
            # ACS, only a gateway serves the AP.
            if wired_eud_active || ! auto_eud_ap_wanted; then
                return_ap_radio_to_mesh
            else
                start_ap_services "$ap_iface" || true
            fi
            ;;
        wired)
            return_ap_radio_to_mesh
            ;;
    esac
}

# Promote $1 using its IPv4, firewall, announcements and gateway state files.
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

    # Announce measured/unknown Ethernet bandwidth or the metered default.
    if [ "$EVENT" != provision ]; then
        "$UPLINK_SPEED" announce "$iface" >/dev/null 2>&1 || true
    fi

    # Skip firewall rewrites, radvd/gateway-route-manager restarts and
    # mesh-ip-manager when this exact uplink is already promoted.
    # Reconciliation runs every node-manager cycle.
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
        if [ "$EVENT" = provision ]; then
            systemctl start manet-uplink-dns.service 2>/dev/null || true
        else
            ensure_eud_services
        fi
        return 0
    fi

    if [ -n "$prev_iface" ] && [ "$prev_iface" != "$iface" ]; then
        ip route del default dev "$prev_iface" metric 100 2>/dev/null || true
    fi

    configure_firewall "$iface"

    touch "$LEGACY_GATEWAY_STATE"
    printf '%s\n' "$iface" > "$UPSTREAM_IFACE_FILE"
    # A normal reconcile must finish radio/service work deferred by setup.
    local mode=gateway
    [ "$EVENT" != provision ] || mode=provisioning
    cat > "$STATE_FILE" <<EOF
UPLINK_MODE=$mode
UPLINK_IFACE=$iface
UPLINK_IP=$ip
UPLINK_GW=${gw:-}
UPDATED_AT=$(date +%s)
EOF
    # Make the qualified DHCP resolver available before dependent services.
    systemctl start manet-uplink-dns.service 2>/dev/null || true
    # Keep the wired-EUD record on end0 while a USB uplink serves as gateway.
    grep -qx 'ETH_MODE=WIRED_EUD' "$LEGACY_ETH_STATE" 2>/dev/null && wired_eud_active || \
    cat > "$LEGACY_ETH_STATE" <<EOF
ETH_MODE=GATEWAY
ETH_IP=$ip
DEFAULT_GW=${gw:-none}
DETECTED_AT=$(date +%s)
DETECTION_METHOD=MANET_UPLINK_DISPATCH
EOF

    if [ "$EVENT" = provision ]; then
        log "Qualified $iface for setup; deferring radio and manager actions"
        return 0
    fi

    apply_radvd_conf /etc/radvd-gateway.conf
    ensure_eud_services
    /usr/local/bin/mesh-ip-manager.sh 2>/dev/null || true
    systemctl restart gateway-route-manager.service 2>/dev/null || true

    log "Promoted $iface as MANET gateway (${ip}, gw=${gw:-none})"
}

# Withdraw gateway state and NAT, restoring mesh RA and EUD policy.
# Optionally flush the former uplink in $1 unless it serves a wired EUD.
demote_gateway() {
    local old_iface="${1:-}"
    local was_gateway=false

    # The Ethernet record counts only when it describes a gateway: in wired-EUD
    # mode it persists, and must not make every pass look like a demotion.
    if [ -f "$STATE_FILE" ] || [ -f "$LEGACY_GATEWAY_STATE" ] ||
            grep -qx 'ETH_MODE=GATEWAY' "$LEGACY_ETH_STATE" 2>/dev/null; then
        was_gateway=true
    fi

    clear_firewall
    # Withdraw the promoted route even if the address flush is delayed.
    if [ -n "$old_iface" ] && ! is_wired_eud_port "$old_iface"; then
        ip route del default dev "$old_iface" metric 100 2>/dev/null || true
    fi
    batctl gw_mode client 2>/dev/null || true
    "$UPLINK_SPEED" forget 2>/dev/null || true
    rm -f "$LEGACY_GATEWAY_STATE" "$LEGACY_NTP_STATE" "$STATE_FILE" "$UPSTREAM_IFACE_FILE"
    if [ "$was_gateway" = true ]; then
        systemctl start manet-uplink-dns.service 2>/dev/null || true
    fi
    # ethernet-autodetect's record of an active wired EUD is not ours to clear.
    if ! { grep -qx 'ETH_MODE=WIRED_EUD' "$LEGACY_ETH_STATE" 2>/dev/null && wired_eud_active; }; then
        rm -f "$LEGACY_ETH_STATE"
    fi

    if [ -n "$old_iface" ] && is_upstream_iface "$old_iface" && ! is_wired_eud_port "$old_iface"; then
        ip addr flush dev "$old_iface" 2>/dev/null || true
        ip link set "$old_iface" nomaster 2>/dev/null || true
        # Do not delete 20-*.network or call networkctl reload: removing the
        # file fires an inotify event that triggers a networkd reconfigure,
        # briefly drops the DHCP lease, and re-triggers this loop.
        # 10-end0.network takes precedence and handles DHCP regardless.
        networkctl reconfigure "$old_iface" 2>/dev/null || true
    fi

    if [ "$EVENT" != provision ]; then
        apply_radvd_conf /etc/radvd-mesh.conf
        ensure_eud_services
    fi

    # Steady state: with no uplink, reconcile lands here every node-manager
    # cycle. Only rerun mesh-ip-manager / restart gateway-route-manager when
    # an actual demotion happened.
    if [ "$was_gateway" = true ]; then
        if [ "$EVENT" != provision ]; then
            /usr/local/bin/mesh-ip-manager.sh 2>/dev/null || true
            systemctl restart gateway-route-manager.service 2>/dev/null || true
        fi
        log "Demoted MANET gateway${old_iface:+ on $old_iface}"
    fi
}

# Print the saved uplink from STATE_FILE or UPSTREAM_IFACE_FILE.
# Reject a saved interface that currently serves a wired EUD.
current_uplink_iface() {
    local iface=""
    if [ -f "$STATE_FILE" ]; then
        iface=$(awk -F= '$1 == "UPLINK_IFACE" {print $2; exit}' "$STATE_FILE" 2>/dev/null)
    else
        # ethernet-autodetect writes this in wired-EUD mode too.
        iface=$(cat "$UPSTREAM_IFACE_FILE" 2>/dev/null || true)
    fi
    # A wired-EUD port is never an uplink, whatever a state file says.
    if [ -n "$iface" ] && is_wired_eud_port "$iface"; then
        iface=""
    fi
    printf '%s\n' "$iface"
}

# Select a working uplink and promote it, or withdraw the current gateway.
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
    carrier|routable|configured|online|add|reconcile|--hotplug|provision)
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
        usage
        exit 1
        ;;
esac
