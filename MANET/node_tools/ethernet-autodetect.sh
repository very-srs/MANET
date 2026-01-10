#!/bin/bash
# ==============================================================================
# Ethernet Auto-Detection Script - Bridged Architecture
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
#   - In gateway mode: wlan1 behavior depends on eud config
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
        systemctl start ap-txpower.service 2>/dev/null
        
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
    # In gateway mode, AP behavior depends on EUD mode
    if [ "$EUD_MODE" == "auto" ] && [ -n "$AP_INTERFACE" ]; then
        log "Auto mode + Gateway: Keeping AP enabled (dual role)"
        
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
        
        # Add wlan1 back to bat0
        if ! batctl if | grep -q "$AP_INTERFACE"; then
            log "Adding $AP_INTERFACE back to bat0 (wired mode)"
            systemctl restart batman-enslave.service
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

elif [ "$DETECTED_MODE" == "wired-eud" ]; then
    # ===================================
    # WIRED EUD MODE - Bridge to mesh
    # ===================================
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
                systemctl restart batman-enslave.service
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
