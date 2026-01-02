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
ACTIVE_CONFIG="${NETWORKD_DIR}/20-end0.network"

# --- Helper Functions ---
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - ETH-DETECT: $1" | systemd-cat -t ethernet-autodetect
}

# IP conversion functions
ip_to_int() {
    local ip=$1
    if [[ -z "$ip" || ! "$ip" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
        return 1
    fi
    local a b c d
    IFS=. read -r a b c d <<<"$ip"
    echo "$(( (a << 24) + (b << 16) + (c << 8) + d ))"
}

int_to_ip() {
    local ip_int=$1
    echo "$(( (ip_int >> 24) & 255 )).$(( (ip_int >> 16) & 255 )).$(( (ip_int >> 8) & 255 )).$(( ip_int & 255 ))"
}

# Ensure only one instance runs
exec 200>"$LOCK_FILE"
flock -n 200 || { log "Already running. Exiting."; exit 0; }

# --- Parse Mode Parameter ---
DETECTED_MODE=""
if [ "$1" == "--mode" ] && [ -n "$2" ]; then
    DETECTED_MODE="$2"
    log "Called with mode: $DETECTED_MODE"
else
    log "ERROR: Missing --mode parameter"
    log "Usage: $0 --mode {gateway|wired-eud}"
    exit 1
fi

# Check if interface exists
if ! ip link show "$ETH_IFACE" &>/dev/null; then
    log "Interface $ETH_IFACE not found"
    exit 1
fi

# Check carrier (cable connected)
CARRIER=$(cat /sys/class/net/$ETH_IFACE/carrier 2>/dev/null || echo 0)
if [ "$CARRIER" != "1" ]; then
    log "No carrier on $ETH_IFACE - cable unplugged"
    
    # Clean up detection configs
    rm -f "$ACTIVE_CONFIG"
    rm -f /var/run/mesh-gateway.state
    rm -f /var/run/mesh-ntp.state
    rm -f /var/run/ethernet_detection_state
    
    # In AUTO mode with no ethernet, enable AP
    EUD_MODE=$(grep "^eud=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)
    if [ "$EUD_MODE" == "auto" ] && [ -f /var/lib/ap_interface ]; then
        log "Auto mode: No ethernet, enabling AP for EUD connectivity"
        systemctl unmask dnsmasq.service 2>/dev/null
        systemctl enable hostapd.service 2>/dev/null
        systemctl start hostapd.service 2>/dev/null
        systemctl enable dnsmasq.service 2>/dev/null
        systemctl start dnsmasq.service 2>/dev/null
        systemctl start ap-txpower.service 2>/dev/null
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
    
    if [ ! -f "$GATEWAY_CONFIG" ]; then
        log "ERROR: Gateway template not found at $GATEWAY_CONFIG"
        exit 1
    fi
    
    cp "$GATEWAY_CONFIG" "$ACTIVE_CONFIG"
    touch /var/run/mesh-gateway.state
    
    # Configure NAT
    log "Configuring NAT..."
    nft add table ip nat 2>/dev/null || true
    nft add chain ip nat postrouting { type nat hook postrouting priority 100 \; } 2>/dev/null || true
    nft flush chain ip nat postrouting 2>/dev/null || true
    nft add rule ip nat postrouting oifname "$ETH_IFACE" masquerade
    
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
        
        # Get current chunk allocation
        MY_CHUNK=0
        if [ -f /var/run/my_ipv4_chunk ]; then
            MY_CHUNK=$(cat /var/run/my_ipv4_chunk)
        fi
        
        if [ "$MY_CHUNK" -gt 0 ]; then
            # Reconfigure dnsmasq and AP with correct chunk IPs
            IPV4_NETWORK=$(grep "^ipv4_network=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)
            MAX_EUDS=$(grep "^max_euds_per_node=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)
            
            CALC_OUTPUT=$(ipcalc "$IPV4_NETWORK" 2>/dev/null)
            FIRST_IP=$(echo "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')
            MIN_INT=$(ip_to_int "$FIRST_IP")
            CHUNK_SIZE=$((MAX_EUDS + 2))
            CHUNK_START_INT=$((MIN_INT + 5 + (MY_CHUNK * CHUNK_SIZE)))
            
            BR0_IP=$(int_to_ip "$CHUNK_START_INT")
            AP_IP=$(int_to_ip $((CHUNK_START_INT + 1)))
            DHCP_START=$(int_to_ip $((CHUNK_START_INT + 2)))
            DHCP_END=$(int_to_ip $((CHUNK_START_INT + CHUNK_SIZE - 1)))
            
            log "Updating AP configuration: gateway=$AP_IP, pool=$DHCP_START-$DHCP_END"
            
            # Remove AP from bridge if enslaved
            if ip link show "$AP_INTERFACE" | grep -q "master br0"; then
                log "Removing $AP_INTERFACE from br0 (must be routed, not bridged)"
                ip link set "$AP_INTERFACE" nomaster
            fi
            
            # Assign AP IP
            ip addr flush dev "$AP_INTERFACE" 2>/dev/null
            ip addr add "${AP_IP}/${IPV4_NETWORK#*/}" dev "$AP_INTERFACE"
            ip link set "$AP_INTERFACE" up
            
            # Enable routing between AP and mesh
            sysctl -q net.ipv4.conf.$AP_INTERFACE.proxy_arp=1
            sysctl -q net.ipv4.conf.br0.proxy_arp=1
            
            # Update dnsmasq config
            cat > /etc/dnsmasq.d/mesh-ap.conf <<EOF
# Listen only on AP interface
interface=$AP_INTERFACE
bind-interfaces

# Do not serve DHCP on br0
no-dhcp-interface=br0

# DHCP configuration from this node's chunk
dhcp-range=$DHCP_START,$DHCP_END,12h

# Gateway is this node's AP interface
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
        fi
        
        systemctl unmask dnsmasq.service 2>/dev/null
        systemctl enable hostapd.service 2>/dev/null
        systemctl start hostapd.service 2>/dev/null
        systemctl enable dnsmasq.service 2>/dev/null
        systemctl restart dnsmasq.service 2>/dev/null
        systemctl start ap-txpower.service 2>/dev/null
        
    elif [ "$EUD_MODE" == "wireless" ] && [ -n "$AP_INTERFACE" ]; then
        log "Wireless mode: Ensuring AP is enabled"
        
        # Get current chunk allocation
        MY_CHUNK=0
        if [ -f /var/run/my_ipv4_chunk ]; then
            MY_CHUNK=$(cat /var/run/my_ipv4_chunk)
        fi
        
        if [ "$MY_CHUNK" -gt 0 ]; then
            # Reconfigure dnsmasq and AP with correct chunk IPs
            IPV4_NETWORK=$(grep "^ipv4_network=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)
            MAX_EUDS=$(grep "^max_euds_per_node=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)
            
            CALC_OUTPUT=$(ipcalc "$IPV4_NETWORK" 2>/dev/null)
            FIRST_IP=$(echo "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')
            MIN_INT=$(ip_to_int "$FIRST_IP")
            CHUNK_SIZE=$((MAX_EUDS + 2))
            CHUNK_START_INT=$((MIN_INT + 5 + (MY_CHUNK * CHUNK_SIZE)))
            
            BR0_IP=$(int_to_ip "$CHUNK_START_INT")
            AP_IP=$(int_to_ip $((CHUNK_START_INT + 1)))
            DHCP_START=$(int_to_ip $((CHUNK_START_INT + 2)))
            DHCP_END=$(int_to_ip $((CHUNK_START_INT + CHUNK_SIZE - 1)))
            
            log "Updating AP configuration: gateway=$AP_IP, pool=$DHCP_START-$DHCP_END"
            
            # Remove AP from bridge if enslaved
            if ip link show "$AP_INTERFACE" | grep -q "master br0"; then
                log "Removing $AP_INTERFACE from br0 (must be routed, not bridged)"
                ip link set "$AP_INTERFACE" nomaster
            fi
            
            # Assign AP IP
            ip addr flush dev "$AP_INTERFACE" 2>/dev/null
            ip addr add "${AP_IP}/${IPV4_NETWORK#*/}" dev "$AP_INTERFACE"
            ip link set "$AP_INTERFACE" up
            
            # Enable routing between AP and mesh
            sysctl -q net.ipv4.conf.$AP_INTERFACE.proxy_arp=1
            sysctl -q net.ipv4.conf.br0.proxy_arp=1
            
            # Update dnsmasq config
            cat > /etc/dnsmasq.d/mesh-ap.conf <<EOF
# Listen only on AP interface
interface=$AP_INTERFACE
bind-interfaces

# Do not serve DHCP on br0
no-dhcp-interface=br0

# DHCP configuration from this node's chunk
dhcp-range=$DHCP_START,$DHCP_END,12h

# Gateway is this node's AP interface
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
        fi
        
        systemctl unmask dnsmasq.service 2>/dev/null
        systemctl enable hostapd.service 2>/dev/null
        systemctl start hostapd.service 2>/dev/null
        systemctl enable dnsmasq.service 2>/dev/null
        systemctl restart dnsmasq.service 2>/dev/null
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
DETECTION_METHOD=CARRIER_WITH_INTERNET
EOF
    
    log "Gateway configuration complete"

elif [ "$DETECTED_MODE" == "wired-eud" ]; then
    # ===================================
    # WIRED EUD MODE - No DHCP server, we provide DHCP
    # ===================================
    log "Configuring as wired EUD (routed mode, providing DHCP)..."
    
    # Load chunk assignment
    MY_CHUNK=0
    if [ -f /var/run/my_ipv4_chunk ]; then
        MY_CHUNK=$(cat /var/run/my_ipv4_chunk)
    fi
    
    if [ "$MY_CHUNK" -eq 0 ]; then
        log "WARNING: No chunk assigned yet, cannot configure wired EUD"
        exit 0
    fi
    
    # Source mesh config
    IPV4_NETWORK=$(grep "^ipv4_network=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)
    MAX_EUDS=$(grep "^max_euds_per_node=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)
    
    if [ -z "$IPV4_NETWORK" ] || [ -z "$MAX_EUDS" ]; then
        log "ERROR: Missing network config"
        exit 1
    fi
    
    # Calculate chunk IPs
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
    
    # Remove end0 from bridge config if it exists
    rm -f "$ACTIVE_CONFIG"
    
    # Configure end0 with its chunk IP (not bridged)
    ip addr flush dev "$ETH_IFACE" 2>/dev/null
    ip addr add "${AP_IP}/${IPV4_NETWORK#*/}" dev "$ETH_IFACE"
    ip link set "$ETH_IFACE" up
    
    # Enable proxy ARP for routing between end0 and mesh
    sysctl -q net.ipv4.conf.$ETH_IFACE.proxy_arp=1
    sysctl -q net.ipv4.conf.br0.proxy_arp=1
    
    # Configure dnsmasq for wired EUD
    cat > /etc/dnsmasq.d/mesh-wired-eud.conf <<EOF
# Listen only on wired EUD interface
interface=$ETH_IFACE
bind-interfaces

# Do not serve on br0
no-dhcp-interface=br0

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
    
    # Stop hostapd but keep dnsmasq for wired DHCP
    if [ "$EUD_MODE" == "auto" ] || [ "$EUD_MODE" == "wired" ]; then
        systemctl stop hostapd.service 2>/dev/null
        systemctl disable hostapd.service 2>/dev/null
    fi
    
    systemctl unmask dnsmasq.service 2>/dev/null
    systemctl enable dnsmasq.service 2>/dev/null
    systemctl restart dnsmasq.service 2>/dev/null
    
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
    
    # === AP CONTROL (for wireless mode) ===
    if [ "$EUD_MODE" == "wireless" ] && [ -n "$AP_INTERFACE" ]; then
        log "Wireless mode: Ensuring AP is enabled"
        systemctl unmask dnsmasq.service 2>/dev/null
        systemctl enable hostapd.service 2>/dev/null
        systemctl start hostapd.service 2>/dev/null
        systemctl start ap-txpower.service 2>/dev/null
    fi
    
    # Save state
    cat > /var/run/ethernet_detection_state <<EOF
ETH_MODE=WIRED_EUD
ETH_IP=$AP_IP
DETECTED_AT=$(date +%s)
DETECTION_METHOD=CARRIER_NO_DHCP
DHCP_POOL=$DHCP_START-$DHCP_END
EOF
    
    log "Wired EUD configuration complete"

else
    log "ERROR: Unknown mode: $DETECTED_MODE"
    exit 1
fi

exit 0
