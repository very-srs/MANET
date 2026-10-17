#!/bin/bash
# Detect a wired uplink or EUD and reconcile bridge, AP and gateway state.
. "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet-common.sh" || exit 1
# Print the accepted detection arguments to stderr.
usage() {
    printf '%s\n' \
        'usage: ethernet-autodetect.sh [--hotplug] [--iface IFACE]' \
        '       [--mode {gateway|wired-eud}]' >&2
}
# Reject malformed options before opening logs or changing network state.
validate_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --hotplug) shift ;;
            --iface)
                [ $# -ge 2 ] && [ -n "$2" ] || { usage; return 1; }
                [[ "$2" != -* ]] || { usage; return 1; }
                shift 2
                ;;
            --mode)
                [ $# -ge 2 ] || { usage; return 1; }
                case "$2" in gateway|wired-eud) ;; *) usage; return 1 ;; esac
                shift 2
                ;;
            *) usage; return 1 ;;
        esac
    done
}
validate_args "$@" || exit 1

# Detect Ethernet role and configure bridging.
# Gateway: keep end0 routed with NAT when it has internet.
# Wired EUD: bridge end0 to br0.
# wlan1 serves the AP in wireless/auto mode without a cable.
# Wired mode or auto with a wired EUD returns wlan1 to mesh.
# As a gateway, the configured AP runs in wireless or auto EUD mode.

# Enable xtrace and tee-to-journal only for debugging to limit dispatcher load.
if [ -f /etc/eth-detect-debug ]; then
    exec > >(tee /var/log/ethernet-detect.log) 2>&1
    set -x
else
    exec > /var/log/ethernet-detect.log 2>&1
fi

# Guard unmask and enable calls. Unmask reloads systemd, and enabling
# SysV-backed units such as dnsmasq or hostapd also reloads via update-rc.d.


# hostapd binds its configured interface at startup. Starting an active unit
# does not rebind it after radio-setup renames interfaces.
# Check the configured interface SSID. A deinitialized AP retains its type
# but loses the SSID. DFS CAC retains the SSID with carrier down, so testing
# carrier would cause restarts that prevent CAC from completing.
hostapd_serving() {
    iw dev "$1" info 2>/dev/null | awk '$1 == "type" { t = $2 } $1 == "ssid" { s = 1 }
        END { exit !(t == "AP" && s) }'
}

# Start hostapd, checking a running unit's AP twice with a 2 s pause.
start_hostapd_checked() {
    local want
    want=$(grep -E '^[[:space:]]*interface=' /etc/hostapd/hostapd.conf 2>/dev/null \
           | head -1 | cut -d'=' -f2)
    if [ -n "$want" ] && systemctl is-active --quiet hostapd.service; then
        if ! hostapd_serving "$want"; then
            sleep 2     # it may simply still be coming up
            if ! hostapd_serving "$want"; then
                log "hostapd is up but $want is not serving - restarting onto it"
                systemctl restart hostapd.service 2>/dev/null
                return $?
            fi
        fi
    fi
    systemctl start hostapd.service 2>/dev/null
}

# The physical Ethernet target is independent of the promoted internet uplink.
# All automatic callers pass end0 explicitly. Manual calls default to end0.
ETH_IFACE=end0
DETECTED_MODE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --iface) ETH_IFACE="$2"; shift 2 ;;
        --mode) DETECTED_MODE="$2"; shift 2 ;;
        --hotplug) shift ;;
    esac
done
# Reject wireless devices even when renamed to look like Ethernet. Tethers
# and USB uplinks belong to the common dispatcher, never passive EUD detection.
case "$ETH_IFACE" in
    wlan*|wl*|lo|br*|bat*|usb*|wwan*|*[!a-zA-Z0-9_.-]*|'')
        printf 'Not an Ethernet detection target: %s\n' "$ETH_IFACE" >&2
        exit 1 ;;
esac
if [ -d "/sys/class/net/$ETH_IFACE/wireless" ] ||
        [ -e "/sys/class/net/$ETH_IFACE/phy80211" ]; then
    printf 'Wireless interface cannot undergo Ethernet detection: %s\n' "$ETH_IFACE" >&2
    exit 1
fi
LOCK_FILE="/var/run/ethernet-autodetect.lock"

# Written when DHCP works but the internet test fails, so repeated dispatcher
# events on the same iface+IP don't rerun the cleanup/reconfigure cycle.
NO_INET_STATE="/var/run/eth-no-internet.state"
UPLINK_SPEED="${MANET_UPLINK_SPEED:-/usr/local/bin/manet-uplink-speed.sh}"
# Retry a cached no-internet verdict after 600 boot-clock seconds.
NO_INET_RECHECK_SECS=600

# Physical link decision: "<iface> <mode> <carrier_changes>".
# The kernel counter changes on cable or peer power transitions. Unchanged
# counts suppress re-detection after local bridge/flush/reconfigure events,
# which otherwise detach the port for about 20 s of every 30 s.
CARRIER_GEN_STATE="/var/run/eth-carrier-generation"

# Networkd config paths
NETWORKD_DIR="/etc/systemd/network"
ACTIVE_CONFIG="${NETWORKD_DIR}/20-${ETH_IFACE}.network"

# Send a prefixed diagnostic to the Ethernet detection journal tag.
log() {
    printf '%s\n' "ETH-DETECT: $1" | systemd-cat -t ethernet-autodetect
}

# Print the kernel carrier-change counter for ETH_IFACE.
carrier_generation() {
    cat "/sys/class/net/$ETH_IFACE/carrier_changes" 2>/dev/null
}

# Record the carrier count before the roughly 20 s DHCP probe. A cable swap
# during the probe makes that count stale so the queued event re-detects.
DETECT_GENERATION=""

# Save mode $1 and DETECT_GENERATION, or the live count, in CARRIER_GEN_STATE.
record_carrier_generation() {
    local generation now
    now=$(carrier_generation)
    generation=${DETECT_GENERATION:-$now}
    if [[ "$generation" =~ ^[0-9]+$ ]]; then
        printf '%s\n' "$ETH_IFACE $1 $generation" > "$CARRIER_GEN_STATE"
        [ "$generation" = "$now" ] ||
            log "Link changed on $ETH_IFACE during detection; the pending event will re-detect"
    else
        rm -f "$CARRIER_GEN_STATE"
    fi
}

# Check whether mode $1 was decided on this physical link.
same_carrier_generation() {
    local iface mode generation now
    [ -f "$CARRIER_GEN_STATE" ] || return 1
    read -r iface mode generation < "$CARRIER_GEN_STATE" || return 1
    now=$(carrier_generation)
    [[ "$now" =~ ^[0-9]+$ ]] && [ "$iface" = "$ETH_IFACE" ] &&
        [ "$mode" = "$1" ] && [ "$generation" = "$now" ]
}

# Confirm failures before removing NAT and batman gateway mode. Mesh clients
# retain a default route here until gateway-route-manager next polls.
# Use extra confirmation attempts while this node is the gateway.
internet_probe_confirmed() {
    local iface="$1"
    local attempts=2 i probe_mode=any
    "$UPLINK_SPEED" is-ethernet "$iface" && probe_mode=http-only

    [ -f /var/run/manet-uplink/mesh-gateway.state ] && attempts=3

    # Use two probes, or three for an active gateway, with 3 s between failures.
    for i in $(seq 1 "$attempts"); do
        internet_probe "$iface" "$probe_mode" && return 0
        [ "$i" -lt "$attempts" ] && sleep 3
    done

    return 1
}

# Run the unplug hook for ETH_IFACE with optional reason and force arguments.
# Reuse the held Ethernet lock and fall back to local cleanup if no hook exists.
run_no_carrier_cleanup() {
    log "${1:-No carrier on $ETH_IFACE} - running unplug cleanup"

    if [ -x /etc/networkd-dispatcher/off.d/50-gateway-disable ]; then
        MANET_ETH_LOCK_HELD=1 MANET_ETH_FORCE_CLEANUP="${2:-0}" IFACE="$ETH_IFACE" /etc/networkd-dispatcher/off.d/50-gateway-disable
    elif [ -x /root/networkd-dispatcher/off ]; then
        MANET_ETH_LOCK_HELD=1 MANET_ETH_FORCE_CLEANUP="${2:-0}" IFACE="$ETH_IFACE" /root/networkd-dispatcher/off
    else
        rm -f "$ACTIVE_CONFIG" /var/run/manet-uplink/mesh-gateway.state /var/run/mesh-ntp.state /var/run/ethernet_detection_state
        ip addr flush dev "$ETH_IFACE" 2>/dev/null || true
        ip link set "$ETH_IFACE" nomaster 2>/dev/null || true
        batctl gw_mode client 2>/dev/null || true
        "$UPLINK_SPEED" forget 2>/dev/null || true
        nft flush chain ip nat postrouting 2>/dev/null || true
        systemctl restart gateway-route-manager.service 2>/dev/null || true
        systemctl restart dnsmasq.service 2>/dev/null || true
    fi
}

# Classify incoming frames while waiting for DHCP. Network evidence keeps
# the port routed. Inconclusive captures use the wired-EUD fallback.
# Outputs:
#   network <reason>   router, switch or multiple devices detected
#   eud                exactly one device requesting an address
#   unknown            inconclusive
LINK_CAPTURE=""
LINK_CAPTURE_PID=""
EUD_DECISION_SECS=5

# Set LINK_CAPTURE and LINK_CAPTURE_PID for ETH_IFACE and install cleanup traps.
start_link_capture() {
    command -v tcpdump >/dev/null 2>&1 || return 0
    LINK_CAPTURE=$(mktemp /var/run/eth-detect-capture.XXXXXX) || return 0
    trap stop_link_capture EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    # Capture inbound frames only, excluding local DHCP and solicitations.
    # Bound capture to 2 MiB (1 MiB in POSIX mode). The classifier treats 1 MiB
    # as network evidence to reject truncated captures and limit /run usage.
    # The capture must not inherit the Ethernet policy lock.
    ( ulimit -f 2048 || exit 1
      exec timeout --kill-after=2 30 tcpdump -i "$ETH_IFACE" -Q in -nn -e -l -s 256
    ) 200>&- > "$LINK_CAPTURE" 2>/dev/null &
    LINK_CAPTURE_PID=$!
}

# Stop LINK_CAPTURE_PID, remove LINK_CAPTURE and clear both globals.
stop_link_capture() {
    [ -n "$LINK_CAPTURE_PID" ] && kill "$LINK_CAPTURE_PID" 2>/dev/null
    [ -n "$LINK_CAPTURE_PID" ] && wait "$LINK_CAPTURE_PID" 2>/dev/null
    [ -n "$LINK_CAPTURE" ] && rm -f "$LINK_CAPTURE"
    LINK_CAPTURE_PID=""
    LINK_CAPTURE=""
}

# Print network/reason, eud or unknown from LINK_CAPTURE for ETH_IFACE.
classify_link() {
    local own size
    [ -n "$LINK_CAPTURE" ] && [ -f "$LINK_CAPTURE" ] || { echo unknown; return; }
    size=$(stat -c %s "$LINK_CAPTURE" 2>/dev/null || echo 0)
    if [ "$size" -ge 1048576 ]; then
        echo "network capture-limit"
        return
    fi
    own=$(cat "/sys/class/net/$ETH_IFACE/address" 2>/dev/null)
    # tcpdump -nn -e places source MAC in field 2 and destination in field 4.
    # Router, switch-protocol or DHCP-server evidence outweighs client requests.
    # Switch evidence includes link-local/Cisco multicast or STP/LLDP/CDP/LACP.
    awk -v own="${own,,}" '
        { src = tolower($2); dst = tolower($4); sub(/,$/, "", dst) }
        /router advertisement/ { network = network ? network : "router-advertisement" }
        dst ~ /^01:80:c2:00:00:0[02e]$/ || dst == "01:00:0c:cc:cc:cc" ||
            / STP | LLDP|CDPv|LACPv/ { network = network ? network : "switch-protocol" }
        /BOOTP\/DHCP, Reply/ { network = network ? network : "dhcp-server" }
        src ~ /^([0-9a-f][0-9a-f]:){5}[0-9a-f][0-9a-f]$/ && src != own {
            macs[src] = 1
            if (/BOOTP\/DHCP, Request/) asking[src] = 1
        }
        END {
            n = 0; for (m in macs) n++
            if (network) print "network " network
            else if (n > 1) print "network multiple-devices"
            else if (n == 1) { for (m in asking) { print "eud"; exit } print "unknown" }
            else print "unknown"
        }' "$LINK_CAPTURE"
}

# Set DETECTED_MODE after checking ETH_IFACE, or exit on cleanup/cached state.
# Use carrier-generation and no-internet state to skip repeated probing.
detect_hotplug_mode() {
    local carrier ip verdict waited

    carrier=$(cat /sys/class/net/$ETH_IFACE/carrier 2>/dev/null || echo 0)
    if [ "$carrier" != "1" ]; then
        # Cable gone: forget the no-internet verdict so a re-plug re-detects.
        rm -f "$NO_INET_STATE" "$CARRIER_GEN_STATE"
        run_no_carrier_cleanup
        exit 0
    fi

    # A wired EUD configured on this same physical link needs nothing more.
    # Re-detecting would detach it from br0 for the whole DHCP probe.
    if same_carrier_generation wired-eud &&
       [ "$(basename "$(readlink "/sys/class/net/$ETH_IFACE/master" 2>/dev/null)" 2>/dev/null)" = br0 ] &&
       grep -qx 'ETH_MODE=WIRED_EUD' /var/run/ethernet_detection_state 2>/dev/null; then
        log "Existing wired-EUD state is current on $ETH_IFACE (no carrier change); skipping re-detection"
        exit 0
    fi

    # Repeated hotplug events must not flush a working gateway or restart
    # networkd and EUD DHCP. Require a decision for this physical link because
    # a fast cable swap can leave the old lease and route intact. Without a
    # decision record, use the address/route check for dispatcher promotion.
    ip=$(ip -4 addr show dev "$ETH_IFACE" | grep -oP 'inet \K[\d.]+' | head -1)
    if [ -f /var/run/manet-uplink/mesh-gateway.state ] && [ -n "$ip" ] && \
       { [ ! -f "$CARRIER_GEN_STATE" ] || same_carrier_generation gateway; } && \
       ip route show dev "$ETH_IFACE" | grep -q '^default '; then
        log "Existing gateway state is healthy on $ETH_IFACE ($ip); skipping re-detection"
        # The lease may have arrived while Wi-Fi was primary. Reconcile the
        # local priority without flushing/reconfiguring either connection.
        MANET_ETH_LOCK_HELD=1 /usr/local/bin/manet-uplink-dispatch.sh reconcile "$ETH_IFACE"
        exit $?
    fi

    # Cache the DHCP-but-no-internet verdict for this iface+IP. Cleanup calls
    # networkctl reconfigure, which otherwise repeats detection every 1-2 min.
    # The boot-clock timestamp permits periodic rechecks without wall-clock
    # steps changing the interval. Future timestamps count as expired.
    if [ -f "$NO_INET_STATE" ] && [ -n "$ip" ]; then
        read -r prev_iface prev_ip prev_ts < "$NO_INET_STATE" || true
        read -r up_now _ < "${MESH_UPTIME_FILE:-/proc/uptime}"
        up_now=${up_now%.*}
        [[ "$prev_ts" =~ ^[0-9]+$ ]] && [ "$prev_ts" -le "$up_now" ] || prev_ts=-1000000
        if [ "$prev_iface" = "$ETH_IFACE" ] && [ "$prev_ip" = "$ip" ] && \
           [ $(( up_now - prev_ts )) -lt "$NO_INET_RECHECK_SECS" ]; then
            log "No-internet state is current on $ETH_IFACE ($ip); skipping re-detection"
            exit 0
        fi
    fi

    # A DHCP-less network is already recorded for this physical connection.
    # Skip probes triggered by local flush/reconfigure events.
    if same_carrier_generation network; then
        log "Network without DHCP already detected on $ETH_IFACE (no carrier change); not bridging"
        exit 0
    fi

    DETECT_GENERATION=$(carrier_generation)
    log "Carrier present on $ETH_IFACE - detecting role"

    # Detach from any bridge before DHCP (wired-EUD may have enslaved it).
    ip link set "$ETH_IFACE" nomaster 2>/dev/null || true
    ip addr flush dev "$ETH_IFACE" 2>/dev/null || true

    # Trigger DHCP without writing a networkd config file. Writing
    # 20-end0.network fires an inotify event that causes networkd to
    # reconfigure, briefly drops any existing lease, and restarts this loop.
    # networkd uses 10-end0.network (or equivalent) for DHCP automatically.
    # Listen before asking: networkd's router solicitation and DHCP request
    # draw replies within a second or two on a real network.
    start_link_capture
    networkctl reconfigure "$ETH_IFACE" 2>/dev/null || true

    # A lease identifies an uplink. Decide a lone DHCP-requesting EUD after
    # EUD_DECISION_SECS without waiting the full 20 s.
    ip=""
    verdict=unknown
    for ((waited = 0; waited < 20; waited++)); do
        ip=$(ip -4 addr show dev "$ETH_IFACE" | grep -oP 'inet \K[\d.]+' | head -1)
        [ -n "$ip" ] && break
        verdict=$(classify_link)
        [ "$verdict" = eud ] && [ "$waited" -ge "$EUD_DECISION_SECS" ] && break
        sleep 1
    done
    [ -n "$ip" ] || verdict=$(classify_link)
    stop_link_capture

    if [ -n "$ip" ]; then
        log "IP acquired on $ETH_IFACE: $ip"
        if internet_probe_confirmed "$ETH_IFACE"; then
            # Connectivity qualifies the gateway even without a speed sample.
            DETECTED_MODE="gateway"
            return 0
        fi

        log "DHCP succeeded but internet test failed; leaving as mesh client"
        read -r up_now _ < "${MESH_UPTIME_FILE:-/proc/uptime}"
        printf '%s\n' "$ETH_IFACE $ip ${up_now%.*}" > "$NO_INET_STATE"
        run_no_carrier_cleanup "Internet test failed on $ETH_IFACE" 1
        exit 0
    fi

    if [ "${verdict%% *}" = network ]; then
        # Keep end0 routed to avoid serving DHCP onto an attached network.
        # networkd keeps requesting a lease for later gateway promotion.
        # Override with `ethernet-autodetect.sh --mode wired-eud` when needed.
        log "Not bridging $ETH_IFACE: a network is attached (${verdict#network }) but gave no DHCP lease"
        record_carrier_generation network
        exit 0
    fi

    log "Wired EUD detected on $ETH_IFACE after ${waited} s ($verdict)"
    DETECTED_MODE="wired-eud"
}

# Ensure only one instance runs
exec 200>"$LOCK_FILE"
# Wait at most 90 seconds for another local Ethernet policy operation.
flock -w 90 200 || { log "Ethernet policy busy; carrier detection deferred"; exit 1; }

if [ -z "$DETECTED_MODE" ]; then
    detect_hotplug_mode
    log "Hotplug detected mode: $DETECTED_MODE"
fi

# A definite gateway/wired-eud mode is selected here.
# The no-internet paths exit before this point.
rm -f "$NO_INET_STATE"

# Check if interface exists
if ! ip link show "$ETH_IFACE" &>/dev/null; then
    log "Interface $ETH_IFACE not found"
    exit 1
fi

# Recheck carrier in case the link changes after the dispatcher event.
CARRIER=$(cat /sys/class/net/$ETH_IFACE/carrier 2>/dev/null || echo 0)
if [ "$CARRIER" != "1" ]; then
    log "No carrier on $ETH_IFACE - cable unplugged"

    run_no_carrier_cleanup

    exit 0
fi

log "Ethernet cable detected on $ETH_IFACE"

# Check for EUD mode in config
EUD_MODE=$(grep "^eud=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)

case "$EUD_MODE" in
    "wireless")
        log "EUD mode: wireless (AP always on)"
        ;;
    "wired")
        log "EUD mode: wired (AP disabled)"
        ;;
    "auto")
        log "EUD mode: auto (AP controlled by ethernet detection)"
        ;;
    *)
        log "Unknown EUD mode, defaulting to auto"
        EUD_MODE="auto"
        ;;
esac

# Read AP interface if configured
AP_INTERFACE=""
if [ -f /var/lib/ap_interface ]; then
    AP_INTERFACE=$(cat /var/lib/ap_interface)
    log "AP interface: $AP_INTERFACE"
fi

# Get existing IP if any
# Extract the first dotted IPv4 after inet, leaving off the CIDR mask.
EXISTING_IP=$(ip -4 addr show dev "$ETH_IFACE" | grep -oP 'inet \K[\d.]+' | head -1)

# Configure the detected mode

if [ "$DETECTED_MODE" == "gateway" ]; then
    # Gateway mode: internet uplink
    log "Configuring as gateway/uplink..."

    if [ -z "$EXISTING_IP" ]; then
        log "ERROR: Gateway mode but no IP found on $ETH_IFACE"
        exit 1
    fi

    sysctl -q net.ipv4.ip_forward=1

    # One owner updates routes, NAT, DNS and bandwidth. Keep the Ethernet
    # lock across the handoff, including when this came from the udev service.
    MANET_ETH_LOCK_HELD=1 /usr/local/bin/manet-uplink-dispatch.sh reconcile "$ETH_IFACE" || exit 1

    # Preserve the detector's boot/rename repair for an active but unbound
    # AP. The dispatcher decides whether AP service is wanted at all.
    if [ -n "$AP_INTERFACE" ] && systemctl is-active --quiet hostapd.service; then
        start_hostapd_checked || { log "AP start failed"; exit 1; }
    fi
    systemctl --no-block start one-shot-time-sync.service 2>/dev/null || true

    record_carrier_generation gateway
    log "Gateway configuration complete"


    # Wired EUD mode: bridge to mesh
elif [ "$DETECTED_MODE" == "wired-eud" ]; then
    log "Configuring as wired EUD (bridged mode)..."

    # Remove any networkd configs for end0 (bridge will handle it)
    rm -f "$ACTIVE_CONFIG"

    # Flush IP from end0 (will get address via br0)
    ip addr flush dev "$ETH_IFACE" 2>/dev/null

    # Ensure end0 is enslaved to br0
    if ! ip link show "$ETH_IFACE" | grep -q "master br0"; then
        log "Enslaving $ETH_IFACE to br0"
        ip link set "$ETH_IFACE" master br0
        ip link set "$ETH_IFACE" up
    else
        log "$ETH_IFACE already in br0"
    fi

    # Disable AP if in auto or wired mode (wlan1 returns to mesh)
    if [ "$EUD_MODE" == "auto" ] || [ "$EUD_MODE" == "wired" ]; then
        if [ -n "$AP_INTERFACE" ]; then
            log "$EUD_MODE mode with wired EUD: Disabling AP, returning $AP_INTERFACE to mesh"

            python3 /usr/local/bin/manet_ap_mesh.py mesh ||
                log "AP-to-mesh transition deferred; reconcile will retry"

        fi
    elif [ "$EUD_MODE" == "wireless" ] && [ -n "$AP_INTERFACE" ]; then
        log "Wireless mode: AP stays enabled even with wired EUD"
        # AP stays running, no changes
    fi

    # Save state
    cat > /var/run/ethernet_detection_state <<EOF
ETH_MODE=WIRED_EUD
ETH_BRIDGE=br0
DETECTED_AT=$(date +%s)
DETECTION_METHOD=CARRIER_NO_DHCP
EOF

    MANET_ETH_LOCK_HELD=1 /usr/local/bin/manet-uplink-dispatch.sh reconcile "$ETH_IFACE" || exit 1

    # A surviving, unchanged USB/Wi-Fi gateway takes the dispatch shortcut.
    # Still refresh DHCP isolation for the newly bridged wired EUD immediately.
    /usr/local/bin/mesh-ip-manager.sh 2>/dev/null || true

    record_carrier_generation wired-eud
    log "Wired EUD configuration complete"

else
    log "ERROR: Unknown mode: $DETECTED_MODE"
    exit 1
fi

exit 0
