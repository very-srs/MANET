#!/bin/bash
# ==============================================================================
# Ethernet Auto-Detection Script
# ==============================================================================
exec > >(tee /var/log/ethernet-detect.log) 2>&1
set -x

ETH_IFACE="end0"
BRIDGE_IFACE="br0"
DETECTION_TIMEOUT=10
LOCK_FILE="/var/run/ethernet-autodetect.lock"

# Networkd config paths
NETWORKD_DIR="/etc/systemd/network"
GATEWAY_CONFIG="${NETWORKD_DIR}/20-end0-gateway.network.off"
EUD_CONFIG="${NETWORKD_DIR}/20-end0-eud.network.off"
ACTIVE_CONFIG="${NETWORKD_DIR}/20-end0.network"

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
    log "No carrier on $ETH_IFACE - cable not connected"
    
    # Clean up on cable unplug
    rm -f "$ACTIVE_CONFIG"
    rm -f /var/run/mesh-gateway.state
    rm -f /var/run/mesh-ntp.state
    rm -f /var/run/ethernet_detection_state
    systemctl restart systemd-networkd
    
    # In AUTO mode with no ethernet, enable AP
    EUD_MODE=$(grep "^eud=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)
    if [ "$EUD_MODE" == "auto" ] && [ -f /var/lib/ap_interface ]; then
        log "Auto mode: No ethernet detected, enabling AP for EUD connectivity"
        systemctl enable hostapd.service
        systemctl start hostapd.service
        systemctl start dnsmasq.service
        systemctl start ap-txpower.service
    fi
    
    exit 0
fi

log "Ethernet cable detected on $ETH_IFACE. Starting auto-detection..."

# Check for EUD mode in config (only affects AP behavior, NOT ethernet detection)
EUD_MODE=$(grep "^eud=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)

FORCE_WIRELESS=false
FORCE_WIRED=false
AUTO_MODE=false

case "$EUD_MODE" in
    "wireless")
        log "Wireless EUD mode from mesh.conf (AP always on)"
        FORCE_WIRELESS=true
        ;;
    "wired")
        log "Wired EUD mode from mesh.conf (AP disabled)"
        FORCE_WIRED=true
        ;;
    "auto")
        log "Auto-detection mode (AP based on ethernet detection)"
        AUTO_MODE=true
        ;;
    *)
        log "Unknown or no EUD mode, defaulting to auto"
        AUTO_MODE=true
        ;;
esac

# Read AP interface if configured
AP_INTERFACE=""
if [ -f /var/lib/ap_interface ]; then
    AP_INTERFACE=$(cat /var/lib/ap_interface)
    log "AP interface: $AP_INTERFACE"
fi

# ===================================================================
# DHCP DETECTION - NON-DISRUPTIVE METHOD
# ===================================================================
DHCP_DETECTED=false

# Check if interface already has an IP (from initial boot config)
EXISTING_IP=$(ip -4 addr show dev "$ETH_IFACE" | grep -oP 'inet \K[\d.]+' | head -1)

if [ -n "$EXISTING_IP" ]; then
    # Interface already has an IP - don't disrupt it!
    log "Interface already has IP: $EXISTING_IP from existing configuration"
    log "Testing connectivity without disrupting DHCP lease..."
    
    # Test internet connectivity to determine gateway vs EUD
    if ping -c 3 -W 2 -I "$ETH_IFACE" 8.8.8.8 > /dev/null 2>&1; then
        log "Internet reachable via $EXISTING_IP - treating as gateway"
        DHCP_DETECTED=true
        ETH_IP="$EXISTING_IP"
        
        # Verify we have the right networkd config active
        if [ ! -f "$ACTIVE_CONFIG" ]; then
            log "No active config found, creating gateway config"
            cp "$GATEWAY_CONFIG" "$ACTIVE_CONFIG"
            rm -f "${NETWORKD_DIR}/10-end0.network"
        fi
    else
        log "Has IP $EXISTING_IP but no internet connectivity - treating as EUD"
        DHCP_DETECTED=false
    fi
else
    # No existing IP - need to probe for DHCP
    log "No existing IP detected - probing for DHCP server..."
    
    # Ensure interface is up but don't flush any state
    ip link set "$ETH_IFACE" up
    
    # Wait for link to come up
    log "Waiting for ethernet link..."
    for i in {1..20}; do
        CARRIER=$(cat /sys/class/net/$ETH_IFACE/carrier 2>/dev/null || echo 0)
        OPERSTATE=$(cat /sys/class/net/$ETH_IFACE/operstate 2>/dev/null || echo "down")
        
        if [ "$CARRIER" = "1" ] && [ "$OPERSTATE" = "up" ]; then
            log "Link ready (carrier detected, operstate: $OPERSTATE)"
            break
        fi
        sleep 0.5
    done
    
    # Give link negotiation time to complete
    log "Waiting for link negotiation..."
    sleep 3
    
    # Try dhcping first (doesn't disrupt anything)
    log "Attempting DHCP detection with dhcping..."
    if timeout 8 dhcping -i "$ETH_IFACE" -s 255.255.255.255 2>&1 | tee /var/log/dhcping.log | grep -q "Got answer from"; then
        DHCP_DETECTED=true
        DHCP_SERVER=$(grep "Got answer from" /var/log/dhcping.log | awk '{print $4}' | tr -d ':')
        log "DHCP server detected via dhcping: $DHCP_SERVER"
    else
        log "dhcping found no response, trying nmap as fallback..."
        
        # Fallback to nmap method (also non-disruptive)
        DHCP_PROBE=$(timeout 10 nmap --script broadcast-dhcp-discover -e "$ETH_IFACE" 2>/dev/null | tee /var/log/nmap-dhcp.log)
        
        if echo "$DHCP_PROBE" | grep -q "Server Identifier\|DHCP Message Type: DHCPOFFER"; then
            DHCP_DETECTED=true
            log "DHCP server detected via nmap!"
            log "nmap output: $DHCP_PROBE"
        else
            log "No DHCP server found with either method"
            log "nmap output: $DHCP_PROBE"
        fi
    fi
fi

# Configure based on detection result
if [ "$DHCP_DETECTED" = true ]; then
    # ===================================
    # GATEWAY MODE
    # ===================================
    log "Configuring as gateway/uplink..."
    
    # Install gateway networkd config
    if [ ! -f "$GATEWAY_CONFIG" ]; then
        log "ERROR: Gateway config template not found at $GATEWAY_CONFIG"
        exit 1
    fi
    
    # Only reconfigure if we don't already have the right setup
    if [ ! -f "$ACTIVE_CONFIG" ] || ! grep -q "DHCP=yes" "$ACTIVE_CONFIG" 2>/dev/null; then
        log "Applying gateway network configuration..."
        cp "$GATEWAY_CONFIG" "$ACTIVE_CONFIG"
        rm -f "${NETWORKD_DIR}/10-end0.network"
        systemctl restart systemd-networkd
        
        # Wait for DHCP to complete (up to 15 seconds)
        log "Waiting for DHCP lease acquisition..."
        for i in {1..30}; do
            ETH_IP=$(ip -4 addr show dev "$ETH_IFACE" | grep -oP 'inet \K[\d.]+' | head -1)
            if [ -n "$ETH_IP" ]; then
                break
            fi
            sleep 0.5
        done
    else
        log "Gateway config already active with IP: ${ETH_IP:-$EXISTING_IP}"
        ETH_IP="${ETH_IP:-$EXISTING_IP}"
    fi
    
    if [ -n "$ETH_IP" ]; then
        log "Active IP: $ETH_IP"
        
        # Mark as gateway
        touch /var/run/mesh-gateway.state
        
        # Configure NAT/masquerading
        log "Configuring NAT..."
        nft add table ip nat 2>/dev/null || true
        nft add chain ip nat postrouting { type nat hook postrouting priority 100 \; } 2>/dev/null || true
        nft flush chain ip nat postrouting 2>/dev/null || true
        nft add rule ip nat postrouting oifname "$ETH_IFACE" masquerade
        
        # Enable IP forwarding
        sysctl -q net.ipv4.ip_forward=1
        
        # Get default gateway
        DEFAULT_GW=$(ip route show dev "$ETH_IFACE" | grep default | awk '{print $3}')
        if [ -n "$DEFAULT_GW" ]; then
            log "Default gateway: $DEFAULT_GW"
        fi
        
        # Enable BATMAN gateway mode
        if command -v batctl &>/dev/null; then
            batctl gw_mode server 2>/dev/null || log "BATMAN not ready yet"
            log "Enabled BATMAN gateway mode"
        fi
        
        # Update router advertisements to announce default route
        cp /etc/radvd-gateway.conf /etc/radvd.conf
        systemctl restart radvd
        
        # === NTP SERVER SETUP ===
        log "Attempting to sync time with external NTP source..."
        cp /etc/chrony/chrony-test.conf /etc/chrony/chrony.conf
        systemctl restart chrony.service
        sleep 3
        
        if timeout 30 chronyc -a 'burst 4/4' && sleep 5 && chronyc sources | grep -q '^\*'; then
            log "Time sync successful. Promoting to mesh NTP server."
            touch /var/run/mesh-ntp.state
            systemctl stop chrony.service
            cp /etc/chrony/chrony-server.conf /etc/chrony/chrony.conf
            systemctl start chrony.service
        else
            log "Failed to sync time. Will not become an NTP server."
            rm -f /var/run/mesh-ntp.state
            systemctl stop chrony.service
            cp /etc/chrony/chrony-default.conf /etc/chrony/chrony.conf
        fi
        
        # === AP CONTROL BASED ON EUD MODE ===
        if [ "$AUTO_MODE" = true ] && [ -n "$AP_INTERFACE" ]; then
            log "Auto mode + Gateway: Keeping AP enabled for dual gateway/AP role"
            systemctl enable hostapd.service
            systemctl start hostapd.service
            systemctl start dnsmasq.service
            systemctl start ap-txpower.service
        elif [ "$FORCE_WIRELESS" = true ] && [ -n "$AP_INTERFACE" ]; then
            log "Wireless mode: Ensuring AP is enabled"
            systemctl enable hostapd.service
            systemctl start hostapd.service
            systemctl start dnsmasq.service
            systemctl start ap-txpower.service
        elif [ "$FORCE_WIRED" = true ] && [ -n "$AP_INTERFACE" ]; then
            log "Wired mode: Disabling AP"
            systemctl stop hostapd.service
            systemctl stop dnsmasq.service
            systemctl stop ap-txpower.service
            systemctl disable hostapd.service
        fi
        
        # Save state
        cat > /var/run/ethernet_detection_state <<EOF
# Ethernet auto-detection result
ETH_MODE=GATEWAY
ETH_IP=$ETH_IP
DEFAULT_GW=${DEFAULT_GW:-none}
DETECTED_AT=$(date +%s)
DETECTION_METHOD=$([ -n "$EXISTING_IP" ] && echo "EXISTING_LEASE" || echo "DHCP_PROBE")
EOF
        
        log "Gateway configuration complete"
    else
        log "ERROR: Failed to acquire IP address"
        DHCP_DETECTED=false
    fi
fi

if [ "$DHCP_DETECTED" = false ]; then
    # ===================================
    # EUD MODE
    # ===================================
    log "Configuring as EUD (bridge member)..."
    
    # Install EUD networkd config
    if [ ! -f "$EUD_CONFIG" ]; then
        log "ERROR: EUD config template not found at $EUD_CONFIG"
        exit 1
    fi
    
    # Activate EUD config
    cp "$EUD_CONFIG" "$ACTIVE_CONFIG"
    rm -f "${NETWORKD_DIR}/10-end0.network"
    
    # Flush any existing IP
    ip addr flush dev "$ETH_IFACE" 2>/dev/null
    
    # Restart networkd to apply bridge membership
    systemctl restart systemd-networkd
    
    log "Waiting for bridge attachment..."
    for i in {1..10}; do
        if bridge link show | grep -q end0; then
            log "Successfully added end0 to bridge br0"
            break
        fi
        sleep 0.5
    done
    
    # Final verification
    if ! bridge link show | grep -q end0; then
        log "WARNING: Failed to add end0 to bridge after 5 seconds"
        # Try manual add as fallback
        ip link set end0 master br0 2>/dev/null || true
    fi
    
    # Remove gateway state
    rm -f /var/run/mesh-gateway.state
    rm -f /var/run/mesh-ntp.state
    
    # Disable BATMAN gateway mode
    if command -v batctl &>/dev/null; then
        batctl gw_mode off 2>/dev/null || log "BATMAN not ready yet"
        log "Disabled BATMAN gateway mode"
    fi
    
    # Revert radvd to not announce default route
    cp /etc/radvd-mesh.conf /etc/radvd.conf
    systemctl restart radvd
    
    # Remove NAT rules
    nft flush chain ip nat postrouting 2>/dev/null || true
    
    # === AP CONTROL BASED ON EUD MODE ===
    if [ "$AUTO_MODE" = true ] && [ -n "$AP_INTERFACE" ]; then
        log "Auto mode + Wired EUD: Disabling AP (wired connection takes priority)"
        systemctl stop hostapd.service
        systemctl stop dnsmasq.service
        systemctl stop ap-txpower.service
        systemctl disable hostapd.service
    elif [ "$FORCE_WIRELESS" = true ] && [ -n "$AP_INTERFACE" ]; then
        log "Wireless mode: Ensuring AP is enabled"
        systemctl enable hostapd.service
        systemctl start hostapd.service
        systemctl start dnsmasq.service
        systemctl start ap-txpower.service
    elif [ "$FORCE_WIRED" = true ] && [ -n "$AP_INTERFACE" ]; then
        log "Wired mode: Ensuring AP is disabled"
        systemctl stop hostapd.service
        systemctl stop dnsmasq.service
        systemctl stop ap-txpower.service
        systemctl disable hostapd.service
    fi
    
    # Save state
    cat > /var/run/ethernet_detection_state <<EOF
# Ethernet auto-detection result
ETH_MODE=EUD
ETH_IP=none
DETECTED_AT=$(date +%s)
DETECTION_METHOD=NO_DHCP
EOF
    
    log "EUD configuration complete"
fi

exit 0
