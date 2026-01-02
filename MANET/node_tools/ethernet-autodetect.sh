#!/bin/bash
# ==============================================================================
# Ethernet Auto-Detection Script
# ==============================================================================
exec > >(tee /var/log/ethernet-detect.log) 2>&1
set -x

ETH_IFACE="end0"
LOCK_FILE="/var/run/ethernet-autodetect.lock"

# Networkd config paths
NETWORKD_DIR="/etc/systemd/network"
GATEWAY_CONFIG="${NETWORKD_DIR}/20-end0-gateway.network.off"
EUD_CONFIG="${NETWORKD_DIR}/20-end0-eud.network.off"
ACTIVE_CONFIG="${NETWORKD_DIR}/20-end0.network"
BOOT_CONFIG="${NETWORKD_DIR}/10-end0.network"  # NEVER DELETE - allows retry on next boot

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - ETH-DETECT: $1" | systemd-cat -t ethernet-autodetect
}

# Ensure only one instance runs
exec 200>"$LOCK_FILE"
flock -n 200 || { log "Already running. Exiting."; exit 0; }

# Check if interface exists
if ! ip link show "$ETH_IFACE" &>/dev/null; then
    log "Interface $ETH_IFACE not found"
    exit 1
fi

# Check carrier (cable connected)
CARRIER=$(cat /sys/class/net/$ETH_IFACE/carrier 2>/dev/null || echo 0)
if [ "$CARRIER" != "1" ]; then
    log "No carrier on $ETH_IFACE - cable unplugged"
    
    # Clean up detection configs (but KEEP boot config for next attempt)
    rm -f "$ACTIVE_CONFIG"
    rm -f /var/run/mesh-gateway.state
    rm -f /var/run/mesh-ntp.state
    rm -f /var/run/ethernet_detection_state
    
    # Don't restart networkd here - this is called from off.d hook
    
    # In AUTO mode with no ethernet, enable AP
    EUD_MODE=$(grep "^eud=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)
    if [ "$EUD_MODE" == "auto" ] && [ -f /var/lib/ap_interface ]; then
        log "Auto mode: No ethernet, enabling AP for EUD connectivity"
        systemctl enable hostapd.service 2>/dev/null
        systemctl start hostapd.service 2>/dev/null
        systemctl start dnsmasq.service 2>/dev/null
        systemctl start ap-txpower.service 2>/dev/null
    fi
    
    exit 0
fi

log "Ethernet cable detected on $ETH_IFACE. Starting detection..."

# Check for EUD mode in config (only affects AP behavior, NOT ethernet detection)
EUD_MODE=$(grep "^eud=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)

case "$EUD_MODE" in
    "wireless")
        log "Wireless EUD mode (AP always on)"
        ;;
    "wired")
        log "Wired EUD mode (AP disabled)"
        ;;
    "auto")
        log "Auto mode (AP controlled by ethernet detection)"
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

# ===================================================================
# DETECTION: Check if we have IP and internet
# ===================================================================
DHCP_DETECTED=false

# Check if interface has an IP
EXISTING_IP=$(ip -4 addr show dev "$ETH_IFACE" | grep -oP 'inet \K[\d.]+' | head -1)

if [ -n "$EXISTING_IP" ]; then
    # Interface has IP (DHCP completed)
    log "Interface has IP: $EXISTING_IP"
    log "Testing internet connectivity..."
    
    # Test internet to determine gateway vs EUD
    if ping -c 3 -W 2 -I "$ETH_IFACE" 8.8.8.8 > /dev/null 2>&1; then
        log "Internet reachable - configuring as GATEWAY"
        DHCP_DETECTED=true
        ETH_IP="$EXISTING_IP"
    else
        log "Has IP but no internet - configuring as EUD"
        DHCP_DETECTED=false
    fi
else
    # NO IP: Unusual if called from routable.d
    log "WARNING: No IP found on $ETH_IFACE"
    log "This script expects to be called from routable.d AFTER IP acquisition"
    log "If called too early, DHCP may not have completed yet"
    log "Exiting without changes - routable.d will call again when IP arrives"
    exit 0
fi

# ===================================================================
# CONFIGURE BASED ON DETECTION
# ===================================================================

if [ "$DHCP_DETECTED" = true ]; then
    # ===================================
    # GATEWAY MODE - Has internet
    # ===================================
    log "Configuring as gateway/uplink..."

    if [ ! -f "$GATEWAY_CONFIG" ]; then
        log "ERROR: Gateway template not found at $GATEWAY_CONFIG"
        exit 1
    fi

    # Create gateway config (priority 20, overrides boot config priority 10)
    # Boot config stays in place for next boot attempt
    cp "$GATEWAY_CONFIG" "$ACTIVE_CONFIG"

    # Mark as gateway
    touch /var/run/mesh-gateway.state

    # Configure NAT
    log "Configuring NAT..."
    nft add table ip nat 2>/dev/null || true
    nft add chain ip nat postrouting { type nat hook postrouting priority 100 \; } 2>/dev/null || true
    nft flush chain ip nat postrouting 2>/dev/null || true
    nft add rule ip nat postrouting oifname "$ETH_IFACE" masquerade

    # Enable IP forwarding
    sysctl -q net.ipv4.ip_forward=1

    # Get default gateway
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

    if timeout 30 chronyc -a 'burst 4/4' >/dev/null 2>&1 && sleep 5 && chronyc sources 2>/dev/null | grep -q '^\*'; then
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
    if [ "$EUD_MODE" == "auto" ] && [ -n "$AP_INTERFACE" ]; then
        log "Auto mode + Gateway: Keeping AP enabled (dual role)"
        systemctl enable hostapd.service 2>/dev/null
        systemctl start hostapd.service 2>/dev/null
        systemctl start dnsmasq.service 2>/dev/null
        systemctl start ap-txpower.service 2>/dev/null
    elif [ "$EUD_MODE" == "wireless" ] && [ -n "$AP_INTERFACE" ]; then
        log "Wireless mode: Ensuring AP is enabled"
        systemctl enable hostapd.service 2>/dev/null
        systemctl start hostapd.service 2>/dev/null
        systemctl start dnsmasq.service 2>/dev/null
        systemctl start ap-txpower.service 2>/dev/null
    elif [ "$EUD_MODE" == "wired" ] && [ -n "$AP_INTERFACE" ]; then
        log "Wired mode: Disabling AP"
        systemctl stop hostapd.service 2>/dev/null
        systemctl stop dnsmasq.service 2>/dev/null
        systemctl stop ap-txpower.service 2>/dev/null
        systemctl disable hostapd.service 2>/dev/null
    fi

    # Save state
    cat > /var/run/ethernet_detection_state <<EOF
ETH_MODE=GATEWAY
ETH_IP=$ETH_IP
DEFAULT_GW=${DEFAULT_GW:-none}
DETECTED_AT=$(date +%s)
DETECTION_METHOD=IP_AND_INTERNET_TEST
EOF

    log "Gateway configuration complete"

else
    # ===================================
    # EUD MODE - Routed interface for wired clients
    # ===================================
    log "Configuring as wired EUD (routed mode)..."

    # Load chunk assignment
    MY_CHUNK=0
    if [ -f /var/run/my_ipv4_chunk ]; then
        MY_CHUNK=$(cat /var/run/my_ipv4_chunk)
    fi

    if [ "$MY_CHUNK" -eq 0 ]; then
        log "WARNING: No chunk assigned yet, cannot configure wired EUD"
        exit 0
    fi

    # Source mesh config for network
    IPV4_NETWORK=$(grep "^ipv4_network=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)
    MAX_EUDS=$(grep "^max_euds_per_node=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)

    # Calculate chunk IPs (inline function from mesh-ip-manager.sh)
    CALC_OUTPUT=$(ipcalc "$IPV4_NETWORK" 2>/dev/null)
    FIRST_IP=$(echo "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')
    MIN_INT=$(ip_to_int "$FIRST_IP")
    CHUNK_SIZE=$((MAX_EUDS + 2))
    CHUNK_START_INT=$((MIN_INT + 5 + (MY_CHUNK * CHUNK_SIZE)))

    BR0_IP=$(int_to_ip "$CHUNK_START_INT")
    AP_IP=$(int_to_ip $((CHUNK_START_INT + 1)))
    DHCP_START=$(int_to_ip $((CHUNK_START_INT + 2)))
    DHCP_END=$(int_to_ip $((CHUNK_START_INT + CHUNK_SIZE - 1)))

    log "Configuring end0 as EUD gateway: IP=$AP_IP, pool=$DHCP_START-$DHCP_END"

    # Configure end0 with its chunk IP
    ip addr flush dev "$ETH_IFACE" 2>/dev/null
    ip addr add "${AP_IP}/${IPV4_NETWORK#*/}" dev "$ETH_IFACE"
    ip link set "$ETH_IFACE" up

    # Configure dnsmasq for wired EUD
    cat > /etc/dnsmasq.d/mesh-wired-eud.conf <<EOF
# Listen only on wired EUD interface
interface=$ETH_IFACE
bind-interfaces

# DHCP configuration from this node's chunk
dhcp-range=$DHCP_START,$DHCP_END,12h

# Gateway is this interface
dhcp-option=3,$AP_IP

# DNS configuration
domain=mesh.local
local=/mesh.local/

# Disable DNS upstream (offline mesh)
no-resolv
no-poll

# Log for debugging
log-dhcp
EOF

    systemctl enable dnsmasq.service 2>/dev/null
    systemctl restart dnsmasq.service 2>/dev/null

    # Remove gateway state
    rm -f /var/run/mesh-gateway.state
    rm -f /var/run/mesh-ntp.state

    # Disable BATMAN gateway mode
    if command -v batctl &>/dev/null; then
        batctl gw_mode off 2>/dev/null
    fi

    # Revert radvd
    cp /etc/radvd-mesh.conf /etc/radvd.conf
    systemctl restart radvd 2>/dev/null

    # Remove NAT rules
    nft flush chain ip nat postrouting 2>/dev/null || true

    # === AP CONTROL ===
    if [ "$EUD_MODE" == "auto" ] && [ -n "$AP_INTERFACE" ]; then
        log "Auto mode + Wired EUD: Disabling AP (wired priority)"
        systemctl stop hostapd.service 2>/dev/null
        systemctl disable hostapd.service 2>/dev/null
    fi

    # Save state
    cat > /var/run/ethernet_detection_state <<EOF
ETH_MODE=WIRED_EUD
ETH_IP=$AP_IP
DETECTED_AT=$(date +%s)
DETECTION_METHOD=IP_BUT_NO_INTERNET
EOF

    log "Wired EUD configuration complete"
fi

exit 0
