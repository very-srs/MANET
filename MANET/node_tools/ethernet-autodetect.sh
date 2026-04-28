#!/bin/bash
# ==============================================================================
# Ethernet Auto-Detection Script
# ==============================================================================
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
# ==============================================================================

exec > >(tee /var/log/ethernet-detect.log) 2>&1
set -x

# Determine which upstream interface to use.
# Priority: end0 (native ethernet) > USB ethernet (usb*, enx*)
# Can be overridden by passing --iface <name> or via /var/run/upstream_iface
resolve_eth_iface() {
    # Explicit override from caller
    if [ -n "${FORCE_IFACE:-}" ]; then
        echo "$FORCE_IFACE"
        return
    fi
    # Saved upstream from previous detection
    if [ -f /var/run/upstream_iface ]; then
        local saved
        saved=$(cat /var/run/upstream_iface)
        if ip link show "$saved" &>/dev/null; then
            echo "$saved"
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
            echo "$iface"
            return
        fi
    done
    echo "end0"
}

ETH_IFACE=$(resolve_eth_iface)
LOCK_FILE="/var/run/ethernet-autodetect.lock"

# Networkd config paths
NETWORKD_DIR="/etc/systemd/network"
GATEWAY_CONFIG="${NETWORKD_DIR}/20-end0-gateway.network.off"
ACTIVE_CONFIG="${NETWORKD_DIR}/20-${ETH_IFACE}.network"

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - ETH-DETECT: $1" | systemd-cat -t ethernet-autodetect
}

run_no_carrier_cleanup() {
    log "No carrier on $ETH_IFACE - running unplug cleanup"

    if [ -x /etc/networkd-dispatcher/off.d/50-gateway-disable ]; then
        IFACE="$ETH_IFACE" /etc/networkd-dispatcher/off.d/50-gateway-disable
    elif [ -x /root/networkd-dispatcher/off ]; then
        IFACE="$ETH_IFACE" /root/networkd-dispatcher/off
    else
        rm -f "$ACTIVE_CONFIG" /var/run/mesh-gateway.state /var/run/mesh-ntp.state /var/run/ethernet_detection_state
        ip addr flush dev "$ETH_IFACE" 2>/dev/null || true
        ip link set "$ETH_IFACE" nomaster 2>/dev/null || true
        batctl gw_mode client 2>/dev/null || true
        nft flush chain ip nat postrouting 2>/dev/null || true
        systemctl restart gateway-route-manager.service 2>/dev/null || true
        systemctl restart dnsmasq.service 2>/dev/null || true
    fi
}

wait_for_end0_ip() {
    local wait_count=0
    local max_wait="${1:-20}"
    local ip=""

    while [ "$wait_count" -lt "$max_wait" ]; do
        ip=$(ip -4 addr show dev "$ETH_IFACE" | grep -oP 'inet \K[\d.]+' | head -1)
        if [ -n "$ip" ]; then
            echo "$ip"
            return 0
        fi

        sleep 1
        ((wait_count++))
    done

    return 1
}

detect_hotplug_mode() {
    local carrier ip

    carrier=$(cat /sys/class/net/$ETH_IFACE/carrier 2>/dev/null || echo 0)
    if [ "$carrier" != "1" ]; then
        run_no_carrier_cleanup
        exit 0
    fi

    # Hotplug events can be emitted repeatedly after networkd restarts. If this
    # node is already a working gateway, do not flush end0 or restart networkd;
    # that creates a loop which interrupts dnsmasq and EUD DHCP.
    ip=$(ip -4 addr show dev "$ETH_IFACE" | grep -oP 'inet \K[\d.]+' | head -1)
    if [ -f /var/run/mesh-gateway.state ] && [ -n "$ip" ] && \
       ip route show dev "$ETH_IFACE" | grep -q '^default '; then
        log "Existing gateway state is healthy on $ETH_IFACE ($ip); skipping re-detection"
        DETECTED_MODE="gateway"
        return 0
    fi

    log "Carrier present on $ETH_IFACE - detecting role"

    # If a previous wired-EUD state left end0 bridged, detach it before DHCP.
    ip link set "$ETH_IFACE" nomaster 2>/dev/null || true

    cat > "$ACTIVE_CONFIG" << EOF
[Match]
Name=${ETH_IFACE}

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

    ip addr flush dev "$ETH_IFACE" 2>/dev/null || true
    networkctl reload 2>/dev/null || true
    networkctl reconfigure "$ETH_IFACE" 2>/dev/null || true

    ip=$(wait_for_end0_ip 20 || true)
    if [ -n "$ip" ]; then
        log "IP acquired on $ETH_IFACE: $ip"
        if ping -c 3 -W 2 -I "$ETH_IFACE" 8.8.8.8 >/dev/null 2>&1; then
            DETECTED_MODE="gateway"
            return 0
        fi

        log "DHCP succeeded but internet test failed; leaving as mesh client"
        run_no_carrier_cleanup
        exit 0
    fi

    DETECTED_MODE="wired-eud"
}

# Ensure only one instance runs
exec 200>"$LOCK_FILE"
flock -n 200 || { log "Already running. Exiting."; exit 0; }

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

# Save which interface we're managing so other scripts know
echo "$ETH_IFACE" > /var/run/upstream_iface

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

    # In AUTO mode with no ethernet, ensure AP is enabled (if configured)
    EUD_MODE=$(grep "^eud=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)
    if [ "$EUD_MODE" == "auto" ] && [ -f /var/lib/ap_interface ]; then
        AP_INTERFACE=$(cat /var/lib/ap_interface)
        log "Auto mode: No ethernet, ensuring AP on $AP_INTERFACE"

        # Ensure wlan1 is NOT in bat0 (will be in br0 via hostapd/bridge config)
        if batctl if | grep -q "$AP_INTERFACE"; then
            log "Removing $AP_INTERFACE from bat0 (will be AP)"
            batctl if del "$AP_INTERFACE" 2>/dev/null || true
        fi

        systemctl unmask dnsmasq.service 2>/dev/null
        systemctl enable hostapd.service 2>/dev/null
        systemctl start hostapd.service 2>/dev/null
        systemctl enable dnsmasq.service 2>/dev/null
        systemctl start dnsmasq.service 2>/dev/null

		# If acting as an AP, lower the tx power
        systemctl start ap-txpower.service 2>/dev/null

        # Bridge AP interface to br0 for EUD connectivity
        if ! ip link show "$AP_INTERFACE" | grep -q "master br0"; then
            log "Bridging $AP_INTERFACE to br0"
            ip link set "$AP_INTERFACE" master br0
            ip link set "$AP_INTERFACE" up
        else
            log "$AP_INTERFACE already bridged to br0"
        fi

        # Reconfigure ebtables (wlan1 should allow DHCP)
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

# ===================================================================
# CONFIGURE BASED ON DETECTED MODE
# ===================================================================

if [ "$DETECTED_MODE" == "gateway" ]; then
    # ===================================
    # GATEWAY MODE - Has internet
    # ===================================
    log "Configuring as gateway/uplink..."

    if [ -z "$EXISTING_IP" ]; then
        log "ERROR: Gateway mode but no IP found on $ETH_IFACE"
        exit 1
    fi

    ETH_IP="$EXISTING_IP"

    cat > "$ACTIVE_CONFIG" << EOF
[Match]
Name=${ETH_IFACE}

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
        batctl gw_mode server 2>/dev/null || log "BATMAN not ready yet"
        log "Enabled BATMAN gateway mode"
    fi

    # Update router advertisements
    cp /etc/radvd-gateway.conf /etc/radvd.conf
    systemctl restart radvd 2>/dev/null

    # === NTP SERVER SETUP ===
    log "Attempting to sync time with external NTP..."
    cp /etc/chrony/chrony-test.conf /etc/chrony/chrony.conf
    systemctl restart chrony.service 2>/dev/null
    sleep 3

    if timeout 30 chronyc -a 'burst 4/4' >/dev/null 2>&1 && sleep 5 && chronyc sources 2>/dev/null | grep -q '\^\*'; then
        log "Time sync successful. Promoting to mesh NTP server."
        touch /var/run/mesh-ntp.state
        systemctl stop chrony.service
        cp /etc/chrony/chrony-server.conf /etc/chrony/chrony.conf
        systemctl start chrony.service
    else
        log "Failed to sync time. Will not become NTP server."
        rm -f /var/run/mesh-ntp.state
        systemctl stop chrony.service
        cp /etc/chrony/chrony-default.conf /etc/chrony/chrony.conf
    fi

    # === AP CONTROL ===
    # In gateway mode, AP behavior depends on EUD mode
    if [ "$EUD_MODE" == "auto" ] && [ -n "$AP_INTERFACE" ]; then
        log "Auto mode + Gateway: Keeping AP enabled"

        # Ensure wlan1 is NOT in bat0 (it's the AP)
        if batctl if | grep -q "$AP_INTERFACE"; then
            log "Removing $AP_INTERFACE from bat0 (dual role gateway+AP)"
            batctl if del "$AP_INTERFACE" 2>/dev/null || true
        fi

        systemctl unmask dnsmasq.service 2>/dev/null
        systemctl enable hostapd.service 2>/dev/null
        systemctl start hostapd.service 2>/dev/null
        systemctl enable dnsmasq.service 2>/dev/null
        systemctl start dnsmasq.service 2>/dev/null
        systemctl start ap-txpower.service 2>/dev/null

        # Bridge AP interface to br0 for EUD connectivity
        if ! ip link show "$AP_INTERFACE" | grep -q "master br0"; then
            log "Bridging $AP_INTERFACE to br0"
            ip link set "$AP_INTERFACE" master br0
            ip link set "$AP_INTERFACE" up
        else
            log "$AP_INTERFACE already bridged to br0"
        fi

    elif [ "$EUD_MODE" == "wireless" ] && [ -n "$AP_INTERFACE" ]; then
        log "Wireless mode: Ensuring AP is enabled"

        # Ensure wlan1 is NOT in bat0
        if batctl if | grep -q "$AP_INTERFACE"; then
            log "Removing $AP_INTERFACE from bat0 (wireless mode AP)"
            batctl if del "$AP_INTERFACE" 2>/dev/null || true
        fi

        systemctl unmask dnsmasq.service 2>/dev/null
        systemctl enable hostapd.service 2>/dev/null
        systemctl start hostapd.service 2>/dev/null
        systemctl enable dnsmasq.service 2>/dev/null
        systemctl start dnsmasq.service 2>/dev/null
        systemctl start ap-txpower.service 2>/dev/null

    elif [ "$EUD_MODE" == "wired" ] && [ -n "$AP_INTERFACE" ]; then
        log "Wired mode: Disabling AP, returning $AP_INTERFACE to mesh"

        systemctl stop hostapd.service 2>/dev/null
        systemctl stop dnsmasq.service 2>/dev/null
        systemctl stop ap-txpower.service 2>/dev/null
        systemctl disable hostapd.service 2>/dev/null

        # Bridge AP interface to br0 for EUD connectivity
        if ! ip link show "$AP_INTERFACE" | grep -q "master br0"; then
            log "Bridging $AP_INTERFACE to br0"
            ip link set "$AP_INTERFACE" master br0
            ip link set "$AP_INTERFACE" up
        else
            log "$AP_INTERFACE already bridged to br0"
        fi
        # Add wlan1 back to bat0
        if ! batctl if | grep -q "$AP_INTERFACE"; then
            log "Adding $AP_INTERFACE back to bat0 (wired mode)"

            # Stop hostapd first to release the interface
            systemctl stop hostapd.service 2>/dev/null
            ip link set "$AP_INTERFACE" down
            sleep 1

            # Set to mesh mode and bring up
            iw dev "$AP_INTERFACE" set type mesh
            ip link set "$AP_INTERFACE" up
            sleep 1
            # Restart wpa_supplicant for this interface to join mesh
            systemctl restart wpa_supplicant@$AP_INTERFACE.service 2>/dev/null
        fi
		sleep 3

        # Add to bat0
        if batctl if add "$AP_INTERFACE" 2>/dev/null; then
           log "$AP_INTERFACE added to bat0"
        else
           log "Failed to add $AP_INTERFACE to bat0"
        fi

    fi

    # Reconfigure ebtables and dnsmasq (handles wlan1 role changes)
    /usr/local/bin/mesh-ip-manager.sh

    # Save state
    cat > /var/run/ethernet_detection_state <<EOF
ETH_MODE=GATEWAY
ETH_IP=$ETH_IP
DEFAULT_GW=${DEFAULT_GW:-none}
DETECTED_AT=$(date +%s)
DETECTION_METHOD=CARRIER_WITH_INTERNET
EOF

    log "Gateway configuration complete"



    # ===================================
    # WIRED EUD MODE - Bridge to mesh
    # ===================================
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

            systemctl stop hostapd.service 2>/dev/null
            systemctl stop ap-txpower.service 2>/dev/null
            systemctl disable hostapd.service 2>/dev/null

            # Remove wlan1 from br0 if it's there
            if ip link show "$AP_INTERFACE" 2>/dev/null | grep -q "master br0"; then
                log "Removing $AP_INTERFACE from br0"
                ip link set "$AP_INTERFACE" nomaster 2>/dev/null || true
            fi

            # Add wlan1 back to bat0
            if ! batctl if | grep -q "$AP_INTERFACE"; then

                log "Adding $AP_INTERFACE back to bat0"

                # Stop hostapd first to release the interface
                systemctl stop hostapd.service 2>/dev/null
                ip link set "$AP_INTERFACE" down
                sleep 1

                # Set to mesh mode and bring up
                iw dev "$AP_INTERFACE" set type mesh
                ip link set "$AP_INTERFACE" up
                sleep 1
                # Restart wpa_supplicant for this interface to join mesh
                systemctl restart wpa_supplicant@$AP_INTERFACE.service 2>/dev/null
            fi
				sleep 2

                # Add to bat0
            if batctl if add "$AP_INTERFACE" 2>/dev/null; then
               log "$AP_INTERFACE added to bat0"
            else
               log "Failed to add $AP_INTERFACE to bat0"
            fi

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

    # Reconfigure ebtables and dnsmasq (handles wlan1 role + end0 addition)
    /usr/local/bin/mesh-ip-manager.sh

    # Save state
    cat > /var/run/ethernet_detection_state <<EOF
ETH_MODE=WIRED_EUD
ETH_BRIDGE=br0
DETECTED_AT=$(date +%s)
DETECTION_METHOD=CARRIER_NO_DHCP
EOF

    log "Wired EUD configuration complete"

else
    log "ERROR: Unknown mode: $DETECTED_MODE"
    exit 1
fi

exit 0
