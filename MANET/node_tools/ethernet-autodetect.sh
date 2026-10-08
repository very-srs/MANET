#!/bin/bash
usage() {
    printf '%s\n' \
        'usage: ethernet-autodetect.sh [--hotplug] [--iface IFACE]' \
        '       [--mode {gateway|wired-eud}]' >&2
}
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

# Ethernet Auto-Detection Script
# Detects ethernet role and configures bridging appropriately
#
# Modes:
#   gateway: end0 has internet (DHCP from ISP) - stays routed, NAT enabled
#   wired-eud: end0 connected to EUD device - bridge to br0
#
# wlan1 handling:
#   - In wireless/auto mode with no cable: wlan1 is AP (br0, not bat0)
#   - In wired mode or auto with wired EUD: wlan1 returns to mesh (bat0)
#   - In gateway mode: wlan1 behavior depends on EUD config
#	-  - EUD wired: wlan1 into mesh
#   -  - Wireless:  wlan1 AP
#   -  - Auto:  wlan1 into mesh

# Full xtrace + tee-to-journal only when debugging: set -x sends every traced
# line to journald via the dispatcher, which is real load when events loop.
if [ -f /etc/eth-detect-debug ]; then
    exec > >(tee /var/log/ethernet-detect.log) 2>&1
    set -x
else
    exec > /var/log/ethernet-detect.log 2>&1
fi

# systemctl unmask always triggers a full daemon-reload, and enable on units
# with sysv shims (dnsmasq, hostapd) spawns update-rc.d which reloads again.
# Guard them so repeated runs of this script don't churn PID 1.
unmask_if_masked() {
    [ "$(systemctl is-enabled "$1" 2>/dev/null)" = "masked" ] && \
        systemctl unmask "$1" 2>/dev/null
    return 0
}

enable_if_disabled() {
    systemctl is-enabled --quiet "$1" 2>/dev/null || \
        systemctl enable "$1" 2>/dev/null
    return 0
}

# hostapd binds the interface named in its config at start time, and
# "systemctl start" is a no-op against a unit that is already active. If the
# radio names moved under it - radio-setup renames interfaces on first boot and
# reboots - the running daemon keeps whatever it grabbed then. On both bench
# CM4s that was the HaLow radio, which left the S1G mesh dead and the real AP
# radio idle. An interface hostapd has released, or had deinited under it,
# keeps "type AP" but loses its SSID, so test the interface the config names
# rather than looking for whichever interface is type AP. The SSID is the
# discriminator rather than the carrier: during a DFS channel-availability
# check the SSID is set while the carrier is still down, and restarting there
# would mean CAC never completes.
hostapd_serving() {
    iw dev "$1" info 2>/dev/null | awk '$1 == "type" { t = $2 } $1 == "ssid" { s = 1 }
        END { exit !(t == "AP" && s) }'
}

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

# Determine which upstream interface to use.
# Priority: end0 (native ethernet) > USB ethernet (usb*, enx*)
# Can be overridden by passing --iface <name> or via /var/run/upstream_iface
resolve_eth_iface() {
    # Explicit override from caller
    if [ -n "${FORCE_IFACE:-}" ]; then
        printf '%s\n' "$FORCE_IFACE"
        return
    fi
    # Saved upstream from previous detection
    if [ -f /var/run/upstream_iface ]; then
        local saved
        saved=$(cat /var/run/upstream_iface)
        if ip link show "$saved" &>/dev/null; then
            printf '%s\n' "$saved"
            return
        fi
    fi
    # Native ethernet first
    if ip link show end0 &>/dev/null; then
        echo "end0"
        return
    fi
    # USB ethernet: usb0, usb1, enxXXX (CDC ECM/RNDIS/NCM dongles/tethering)
    for iface in $(ls /sys/class/net/); do
        local bus
        bus=$(readlink /sys/class/net/$iface/device/subsystem 2>/dev/null | grep -o 'usb' || true)
        if [ "$bus" = "usb" ] && [[ "$iface" != wlan* ]] && [[ "$iface" != bat* ]] && [[ "$iface" != br* ]]; then
            printf '%s\n' "$iface"
            return
        fi
    done
    echo "end0"
}

ETH_IFACE=$(resolve_eth_iface)
LOCK_FILE="/var/run/ethernet-autodetect.lock"

# Written when DHCP works but the internet test fails, so repeated dispatcher
# events on the same iface+IP don't rerun the cleanup/reconfigure cycle.
NO_INET_STATE="/var/run/eth-no-internet.state"
UPLINK_SPEED="${MANET_UPLINK_SPEED:-/usr/local/bin/manet-uplink-speed.sh}"
NO_INET_RECHECK_SECS=600

# Which physical link a decision was made on: "<iface> <mode> <carrier_changes>".
# Bridging, flushing or reconfiguring end0 makes networkd report "Gained
# carrier" again, and networkd-dispatcher then runs this script once more. The
# kernel's carrier_changes counter moves only on real link transitions (cable
# pulled, swapped, or the far end power-cycled), so an unchanged count means
# the same cable and the same peer: the earlier decision still holds. Without
# this, each wired-EUD run triggered the next and the port was detached for
# ~20 s of every ~30 s.
CARRIER_GEN_STATE="/var/run/eth-carrier-generation"

# Networkd config paths
NETWORKD_DIR="/etc/systemd/network"
GATEWAY_CONFIG="${NETWORKD_DIR}/20-end0-gateway.network.off"
ACTIVE_CONFIG="${NETWORKD_DIR}/20-${ETH_IFACE}.network"

log() {
    printf '%s\n' "ETH-DETECT: $1" | systemd-cat -t ethernet-autodetect
}

carrier_generation() {
    cat "/sys/class/net/$ETH_IFACE/carrier_changes" 2>/dev/null
}

# Detection takes ~20 s (DHCP probe). The decision describes the link as it
# was when detection STARTED, so record that generation. If the cable was
# swapped mid-probe, the recorded count is already stale and the event queued
# by that swap re-detects instead of being suppressed by this result.
DETECT_GENERATION=""

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

# Was the current decision of mode $1 made on this exact physical link?
same_carrier_generation() {
    local iface mode generation now
    [ -f "$CARRIER_GEN_STATE" ] || return 1
    read -r iface mode generation < "$CARRIER_GEN_STATE" || return 1
    now=$(carrier_generation)
    [[ "$now" =~ ^[0-9]+$ ]] && [ "$iface" = "$ETH_IFACE" ] &&
        [ "$mode" = "$1" ] && [ "$generation" = "$now" ]
}

# Is there real internet behind $1?
#
# ICMP echo alone is not a usable test. Plenty of LANs (the bench LAN included)
# drop or heavily rate-limit echo to public addresses while passing TCP and UDP
# normally: measured 95-100% echo loss to 8.8.8.8/1.1.1.1 there while DNS,
# HTTP and traceroute all worked. A ping-only probe made this node flap in and
# out of gateway mode, so try progressively less-filtered methods:
#   1. ICMP : cheapest, answers in milliseconds on a normal LAN
#   2. HTTP : the standard 204 captive-portal endpoints; a portal answers
#              200/302 rather than 204, which correctly counts as "no internet"
#   3. TCP  : bare connect to public DNS, for images without curl
internet_probe() {
    local iface="$1"
    local url code ip

    ping -c 1 -W 2 -I "$iface" 1.1.1.1 >/dev/null 2>&1 && return 0
    ping -c 1 -W 2 -I "$iface" 8.8.8.8 >/dev/null 2>&1 && return 0

    if command -v curl >/dev/null 2>&1; then
        for url in http://connectivitycheck.gstatic.com/generate_204 \
                   http://cp.cloudflare.com/; do
            code=$(curl --interface "$iface" -s -m 3 -o /dev/null \
                        -w '%{http_code}' "$url" 2>/dev/null || true)
            [ "$code" = "204" ] && return 0
        done
        return 1
    fi

    # No curl in this image: bare TCP connect instead. Every leg has a timeout
    # because this runs inside the dispatcher's flock.
    ip=$(ip -4 addr show dev "$iface" | grep -oP 'inet \K[\d.]+' | head -1)
    if [ -n "$ip" ] && command -v nc >/dev/null 2>&1; then
        nc -z -w 3 -s "$ip" 1.1.1.1 53 >/dev/null 2>&1 && return 0
        nc -z -w 3 -s "$ip" 8.8.8.8 53 >/dev/null 2>&1 && return 0
    fi

    return 1
}

# Tearing down a working gateway is expensive: it drops NAT and batman gw_mode
# here, and every mesh client is left holding a default route to this node that
# now black-holes (gateway-route-manager withdraws it, but only on its next
# poll). So confirm a failure before acting on it, and try harder when we are
# currently the gateway.
internet_probe_confirmed() {
    local iface="$1"
    local attempts=2 i

    [ -f /var/run/mesh-gateway.state ] && attempts=3

    for i in $(seq 1 "$attempts"); do
        internet_probe "$iface" && return 0
        [ "$i" -lt "$attempts" ] && sleep 3
    done

    return 1
}

run_no_carrier_cleanup() {
    log "${1:-No carrier on $ETH_IFACE} - running unplug cleanup"

    if [ -x /etc/networkd-dispatcher/off.d/50-gateway-disable ]; then
        MANET_ETH_LOCK_HELD=1 MANET_ETH_FORCE_CLEANUP="${2:-0}" IFACE="$ETH_IFACE" /etc/networkd-dispatcher/off.d/50-gateway-disable
    elif [ -x /root/networkd-dispatcher/off ]; then
        MANET_ETH_LOCK_HELD=1 MANET_ETH_FORCE_CLEANUP="${2:-0}" IFACE="$ETH_IFACE" /root/networkd-dispatcher/off
    else
        rm -f "$ACTIVE_CONFIG" /var/run/mesh-gateway.state /var/run/mesh-ntp.state /var/run/ethernet_detection_state
        ip addr flush dev "$ETH_IFACE" 2>/dev/null || true
        ip link set "$ETH_IFACE" nomaster 2>/dev/null || true
        batctl gw_mode client 2>/dev/null || true
        "$UPLINK_SPEED" forget 2>/dev/null || true
        nft flush chain ip nat postrouting 2>/dev/null || true
        systemctl restart gateway-route-manager.service 2>/dev/null || true
        systemctl restart dnsmasq.service 2>/dev/null || true
    fi
}

# What is on the other end of the cable, judged from frames it sends while we
# wait for DHCP. Absence of a DHCP reply alone does not mean a single user
# device: a static-addressed LAN, a slow DHCP server, or a switch port still
# coming up stays silent too, and bridging such a network would make this node
# a rogue DHCP server on it. Prints one of:
#   network <reason>   a router, switch or several devices are attached
#   eud                exactly one device, and it is asking for an address
#   unknown            nothing conclusive (old rule: treat as a user device)
LINK_CAPTURE=""
LINK_CAPTURE_PID=""
EUD_DECISION_SECS=5

start_link_capture() {
    command -v tcpdump >/dev/null 2>&1 || return 0
    LINK_CAPTURE=$(mktemp /var/run/eth-detect-capture.XXXXXX) || return 0
    trap stop_link_capture EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    # Inbound only: our own DHCP requests and solicitations are not evidence.
    # Limit the capture to at most 2 MiB (1 MiB in POSIX mode). The classifier
    # treats 1 MiB as network evidence before scanning it; a busy link must
    # not fill /run or turn a truncated capture into a confident EUD decision.
    # The capture must not inherit this detector's Ethernet policy lock.
    ( ulimit -f 2048 || exit 1
      exec timeout --kill-after=2 30 tcpdump -i "$ETH_IFACE" -Q in -nn -e -l -s 256
    ) 200>&- > "$LINK_CAPTURE" 2>/dev/null &
    LINK_CAPTURE_PID=$!
}

stop_link_capture() {
    [ -n "$LINK_CAPTURE_PID" ] && kill "$LINK_CAPTURE_PID" 2>/dev/null
    [ -n "$LINK_CAPTURE_PID" ] && wait "$LINK_CAPTURE_PID" 2>/dev/null
    [ -n "$LINK_CAPTURE" ] && rm -f "$LINK_CAPTURE"
    LINK_CAPTURE_PID=""
    LINK_CAPTURE=""
}

classify_link() {
    local own size
    [ -n "$LINK_CAPTURE" ] && [ -f "$LINK_CAPTURE" ] || { echo unknown; return; }
    size=$(stat -c %s "$LINK_CAPTURE" 2>/dev/null || echo 0)
    if [ "$size" -ge 1048576 ]; then
        echo "network capture-limit"
        return
    fi
    own=$(cat "/sys/class/net/$ETH_IFACE/address" 2>/dev/null)
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

    # Hotplug events can be emitted repeatedly after networkd restarts. If this
    # node is already a working gateway, do not flush end0 or restart networkd;
    # that creates a loop which interrupts dnsmasq and EUD DHCP. A recorded
    # decision from an earlier link (a fast cable swap whose unplug event was
    # coalesced) must not pass for healthy: the old lease and route outlive it.
    # Without a record (a gateway promoted by the uplink dispatcher) the
    # address/route check alone applies, as before.
    ip=$(ip -4 addr show dev "$ETH_IFACE" | grep -oP 'inet \K[\d.]+' | head -1)
    if [ -f /var/run/mesh-gateway.state ] && [ -n "$ip" ] && \
       { [ ! -f "$CARRIER_GEN_STATE" ] || same_carrier_generation gateway; } && \
       ip route show dev "$ETH_IFACE" | grep -q '^default '; then
        log "Existing gateway state is healthy on $ETH_IFACE ($ip); skipping re-detection"
        # Exit the whole script: returning here would still run the gateway
        # mode section, which rewrites 20-end0.network, triggers a networkd
        # inotify reconfigure, and restarts this loop.
        exit 0
    fi

    # Same idea for the DHCP-but-no-internet outcome: the cleanup below ends
    # with a networkctl reconfigure, which fires another dispatcher event and
    # would otherwise repeat the flush/DHCP/ping/cleanup cycle every 1-2 min
    # forever on LANs without internet. If we already concluded "no internet"
    # for this iface+IP recently, leave everything alone. The timestamp lets
    # a periodic re-check notice if internet comes back on the same lease.
    # The timestamp is boot-clock seconds, so a time sync stepping the wall
    # clock cannot end or extend it. One ahead of the boot clock (a wall time
    # written by an older version) counts as expired.
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

    # A network without DHCP was already found on this physical connection.
    # Our own flush/reconfigure re-triggers this script; do not probe again.
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

    # A lease means an uplink. A lone device asking for an address is a user
    # device, decided after EUD_DECISION_SECS rather than the full 20 s.
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
        # The speed test is also the internet check that matters: a captive
        # portal or a filtered network cannot complete it, and such a node
        # must not announce itself as a gateway. Its result is what the
        # gateway announces. Not an Ethernet port (exit 2): not tested.
        speed_rc=1
        if internet_probe_confirmed "$ETH_IFACE"; then
            speed_rc=0
            "$UPLINK_SPEED" measure "$ETH_IFACE" >/dev/null || speed_rc=$?
        fi
        if [ "$speed_rc" -eq 0 ] || [ "$speed_rc" -eq 2 ]; then
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
        # Never serve DHCP onto someone else's network. Leave end0 routed;
        # networkd keeps asking for a lease, and if one arrives later the
        # uplink dispatcher promotes it as usual. Force a wired EUD with
        # `ethernet-autodetect.sh --mode wired-eud` if this is wrong.
        log "Not bridging $ETH_IFACE: a network is attached (${verdict#network }) but gave no DHCP lease"
        record_carrier_generation network
        exit 0
    fi

    log "Wired EUD detected on $ETH_IFACE after ${waited} s ($verdict)"
    DETECTED_MODE="wired-eud"
}

# Ensure only one instance runs
exec 200>"$LOCK_FILE"
flock -w 90 200 || { log "Ethernet policy busy; carrier detection deferred"; exit 1; }

# Parse CLI argument
DETECTED_MODE=""
ARGS=("$@")
i=0
while [ $i -lt ${#ARGS[@]} ]; do
    case "${ARGS[$i]}" in
        --iface)
            i=$((i+1))
            FORCE_IFACE="${ARGS[$i]}"
            ETH_IFACE=$(resolve_eth_iface)
            ACTIVE_CONFIG="${NETWORKD_DIR}/20-${ETH_IFACE}.network"
            ;;
        --mode)
            i=$((i+1))
            DETECTED_MODE="${ARGS[$i]}"
            log "Called with mode: $DETECTED_MODE"
            ;;
        --hotplug|"")
            ;;
    esac
    i=$((i+1))
done

if [ -z "$DETECTED_MODE" ]; then
    detect_hotplug_mode
    log "Hotplug detected mode: $DETECTED_MODE"
fi

# Reaching here means a definite mode (gateway/wired-eud) was chosen;
# the no-internet paths above all exit before this point.
rm -f "$NO_INET_STATE"

# Save which interface we're managing so other scripts know
printf '%s\n' "$ETH_IFACE" > /var/run/upstream_iface

# Check if interface exists
if ! ip link show "$ETH_IFACE" &>/dev/null; then
    log "Interface $ETH_IFACE not found"
    exit 1
fi

# Check carrier (cable connected)
# Should not be needed, this script is called by networkd-dispatcher
# But this is a double check
CARRIER=$(cat /sys/class/net/$ETH_IFACE/carrier 2>/dev/null || echo 0)
if [ "$CARRIER" != "1" ]; then
    log "No carrier on $ETH_IFACE - cable unplugged"

    # Clean up detection configs
    rm -f "$ACTIVE_CONFIG"
    rm -f /var/run/mesh-gateway.state
    rm -f /var/run/mesh-ntp.state
    rm -f /var/run/ethernet_detection_state
    rm -f "$NO_INET_STATE"

    # In AUTO mode with no ethernet, ensure AP is enabled (if configured)
    EUD_MODE=$(grep "^eud=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)
    if [ "$EUD_MODE" == "auto" ] && [ -f /var/lib/ap_interface ]; then
        AP_INTERFACE=$(cat /var/lib/ap_interface)
        log "Auto mode: No ethernet, ensuring AP on $AP_INTERFACE"

        unmask_if_masked dnsmasq.service
        enable_if_disabled hostapd.service
        start_hostapd_checked || { log "AP start failed"; exit 1; }
        enable_if_disabled dnsmasq.service
        systemctl start dnsmasq.service 2>/dev/null

		# If acting as an AP, lower the tx power
        systemctl start ap-txpower.service 2>/dev/null


        # Refresh address/DHCP configuration and its isolation rules
        /usr/local/bin/mesh-ip-manager.sh



    fi
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
EXISTING_IP=$(ip -4 addr show dev "$ETH_IFACE" | grep -oP 'inet \K[\d.]+' | head -1)

# CONFIGURE BASED ON DETECTED MODE

if [ "$DETECTED_MODE" == "gateway" ]; then
    # GATEWAY MODE - Has internet
    log "Configuring as gateway/uplink..."

    if [ -z "$EXISTING_IP" ]; then
        log "ERROR: Gateway mode but no IP found on $ETH_IFACE"
        exit 1
    fi

    ETH_IP="$EXISTING_IP"

    # Do not rewrite $ACTIVE_CONFIG here: it was already written (if needed)
    # during detect_hotplug_mode, and rewriting it triggers an inotify event
    # that causes networkd to reconfigure end0, briefly drops the DHCP lease,
    # and restarts this loop. networkd uses 10-end0.network regardless.
    touch /var/run/mesh-gateway.state

    # Configure NAT
    log "Configuring NAT..."
    nft add table ip nat 2>/dev/null || true
    nft add chain ip nat postrouting { type nat hook postrouting priority 100 \; } 2>/dev/null || true
    nft flush chain ip nat postrouting 2>/dev/null || true
    nft add rule ip nat postrouting oifname "$ETH_IFACE" masquerade

    # Add MSS clamping for TCP packets going through the bridge
    nft add table ip mangle 2>/dev/null || true
    nft add chain ip mangle forward { type filter hook forward priority -150 \; } 2>/dev/null || true
    nft flush chain ip mangle forward 2>/dev/null || true
    nft add rule ip mangle forward tcp flags syn tcp option maxseg size set rt mtu

    sysctl -q net.ipv4.ip_forward=1

    DEFAULT_GW=$(ip route show dev "$ETH_IFACE" | grep default | awk '{print $3}')
    log "Default gateway: ${DEFAULT_GW:-none}"

    # Enable BATMAN gateway mode
    if command -v batctl &>/dev/null; then
        "$UPLINK_SPEED" announce "$ETH_IFACE" >/dev/null 2>&1 || log "BATMAN not ready yet"
        log "Enabled BATMAN gateway mode ($(batctl gw 2>/dev/null))"
    fi

    # Update router advertisements
    cp /etc/radvd-gateway.conf /etc/radvd.conf
    systemctl restart radvd 2>/dev/null

    # mesh-time-sync owns chrony. It observes mesh-gateway.state/upstream_iface
    # and advertises NTP through existing telemetry only after clock validation.
    systemctl --no-block start one-shot-time-sync.service 2>/dev/null || true

    # === AP CONTROL ===
    # In gateway mode, AP behavior depends on EUD mode
    if [ "$EUD_MODE" == "auto" ] && [ -n "$AP_INTERFACE" ]; then
        log "Auto mode + Gateway: Keeping AP enabled"

        unmask_if_masked dnsmasq.service
        enable_if_disabled hostapd.service
        start_hostapd_checked || { log "AP start failed"; exit 1; }
        enable_if_disabled dnsmasq.service
        systemctl start dnsmasq.service 2>/dev/null
        systemctl start ap-txpower.service 2>/dev/null


    elif [ "$EUD_MODE" == "wireless" ] && [ -n "$AP_INTERFACE" ]; then
        log "Wireless mode: Ensuring AP is enabled"

        unmask_if_masked dnsmasq.service
        enable_if_disabled hostapd.service
        start_hostapd_checked || { log "AP start failed"; exit 1; }
        enable_if_disabled dnsmasq.service
        systemctl start dnsmasq.service 2>/dev/null
        systemctl start ap-txpower.service 2>/dev/null

    elif [ "$EUD_MODE" == "wired" ] && [ -n "$AP_INTERFACE" ]; then
        log "Wired mode: Disabling AP, returning $AP_INTERFACE to mesh"

        python3 /usr/local/bin/manet_ap_mesh.py mesh ||
            log "AP-to-mesh transition deferred; reconcile will retry"

    fi

    # Refresh DHCP isolation and dnsmasq (handles wlan1 role changes)
    /usr/local/bin/mesh-ip-manager.sh

    # Save state
    cat > /var/run/ethernet_detection_state <<EOF
ETH_MODE=GATEWAY
ETH_IP=$ETH_IP
DEFAULT_GW=${DEFAULT_GW:-none}
DETECTED_AT=$(date +%s)
DETECTION_METHOD=CARRIER_WITH_INTERNET
EOF

    record_carrier_generation gateway
    log "Gateway configuration complete"



    # WIRED EUD MODE - Bridge to mesh
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

    # Remove gateway state
    rm -f /var/run/mesh-gateway.state
    rm -f /var/run/mesh-ntp.state

    # Disable BATMAN gateway mode
    if command -v batctl &>/dev/null; then
        batctl gw_mode client 2>/dev/null || log "BATMAN not ready yet"
        log "Set BATMAN to client mode"
    fi

    # Revert radvd
    cp /etc/radvd-mesh.conf /etc/radvd.conf
    systemctl restart radvd 2>/dev/null

    # Remove NAT rules
    nft flush chain ip nat postrouting 2>/dev/null || true

    # Refresh DHCP isolation and dnsmasq (handles wlan1 role + end0 addition)
    /usr/local/bin/mesh-ip-manager.sh

    # Save state
    cat > /var/run/ethernet_detection_state <<EOF
ETH_MODE=WIRED_EUD
ETH_BRIDGE=br0
DETECTED_AT=$(date +%s)
DETECTION_METHOD=CARRIER_NO_DHCP
EOF

    record_carrier_generation wired-eud
    log "Wired EUD configuration complete"

else
    log "ERROR: Unknown mode: $DETECTED_MODE"
    exit 1
fi

exit 0
