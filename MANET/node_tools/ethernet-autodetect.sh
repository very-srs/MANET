#!/bin/bash
# ==============================================================================
# Ethernet Auto-Detection Script
# ==============================================================================

ETH_IFACE="end0"
BRIDGE_IFACE="br0"
DETECTION_TIMEOUT=2
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

# Check for forced mode in config
EUD_MODE=$(grep "^eud=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)

FORCE_WIRELESS=false
FORCE_WIRED=false
AUTO_MODE=false

case "$EUD_MODE" in
    "wireless")
        log "Forced wireless mode from mesh.conf"
        FORCE_WIRELESS=true
        ;;
    "wired")
        log "Forced wired EUD mode from mesh.conf"
        FORCE_WIRED=true
        ;;
    "auto")
        log "Auto-detection mode"
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

DHCP_DETECTED=false
log "Probing for DHCP server (timeout: ${DETECTION_TIMEOUT}s)..."

# Ensure interface is up for probing
ip link set "$ETH_IFACE" up
sleep 2

# Use nmap to detect DHCP server
DHCP_PROBE=$(timeout "$DETECTION_TIMEOUT" nmap --script broadcast-dhcp-discover -e "$ETH_IFACE" 2>/dev/null)

if echo "$DHCP_PROBE" | grep -q "Server Identifier\|DHCP Message Type: DHCPOFFER"; then
    DHCP_DETECTED=true
    log "DHCP server detected!"
else
    log "No DHCP server found"
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
    
    # Activate gateway config
    cp "$GATEWAY_CONFIG" "$ACTIVE_CONFIG"
	rm -f "${NETWORKD_DIR}/10-end0.network" 2>/dev/null
	
    # Restart networkd to apply DHCP
    systemctl restart systemd-networkd
    
    # Wait for DHCP to complete (up to 10 seconds)
    log "Waiting for DHCP lease acquisition..."
    for i in {1..20}; do
        ETH_IP=$(ip -4 addr show dev "$ETH_IFACE" | grep -oP 'inet \K[\d.]+' | head -1)
        if [ -n "$ETH_IP" ]; then
            break
        fi
        sleep 0.5
    done
    
    if [ -n "$ETH_IP" ]; then
        log "Acquired IP: $ETH_IP"
        
        # Verify internet connectivity before proceeding
        log "Testing internet connectivity..."
        if ! ping -c 3 -W 2 -I "$ETH_IFACE" 8.8.8.8 > /dev/null 2>&1; then
            log "WARNING: Got DHCP but no internet connectivity. Treating as EUD."
            DHCP_DETECTED=false  # Fall through to EUD mode
        else
            log "Internet confirmed working!"
            
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
                batctl gw_mode server
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
            
            # In AUTO mode when gateway, KEEP AP ENABLED (dual role)
            if [ "$AUTO_MODE" = true ] && [ -n "$AP_INTERFACE" ]; then
                log "Auto mode + Gateway: Keeping AP enabled for dual gateway/AP role"
                systemctl enable hostapd.service
                systemctl start hostapd.service
                systemctl start dnsmasq.service
                systemctl start ap-txpower.service
            fi
            
            # Save state
            cat > /var/run/ethernet_detection_state <<EOF
# Ethernet auto-detection result
ETH_MODE=GATEWAY
ETH_IP=$ETH_IP
DEFAULT_GW=${DEFAULT_GW:-none}
DETECTED_AT=$(date +%s)
DETECTION_METHOD=$([ "$FORCE_WIRELESS" = true ] && echo "FORCED" || echo "DHCP_PROBE")
EOF
            
            log "Gateway configuration complete"
        fi
    else
        log "WARNING: DHCP server detected but IP acquisition failed"
        # Fall back to EUD mode
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
    rm -f "${NETWORKD_DIR}/10-end0.network" 2>/dev/null

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
        batctl gw_mode off
        log "Disabled BATMAN gateway mode"
    fi
    
    # Revert radvd to not announce default route
    cp /etc/radvd-mesh.conf /etc/radvd.conf
    systemctl restart radvd
    
    # Remove NAT rules
    nft flush chain ip nat postrouting 2>/dev/null || true
    
    # In AUTO mode when EUD plugged in, DISABLE AP (wired EUD takes priority)
    if [ "$AUTO_MODE" = true ] && [ -n "$AP_INTERFACE" ]; then
        log "Auto mode + Wired EUD: Disabling AP (wired connection takes priority)"
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
DETECTION_METHOD=$([ "$FORCE_WIRED" = true ] && echo "FORCED" || echo "NO_DHCP")
EOF
    
    log "EUD configuration complete"
fi

exit 0
