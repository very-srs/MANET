#!/bin/bash
# ==============================================================================
# Bridged Architecture Verification Script
# ==============================================================================
# Verifies that the bridged EUD architecture is correctly configured
# with proper handling of wlan1's dual-purpose role
# ==============================================================================

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

ERRORS=0
WARNINGS=0

log_pass() {
    echo -e "${GREEN}✓${NC} $1"
}

log_fail() {
    echo -e "${RED}✗${NC} $1"
    ((ERRORS++))
}

log_warn() {
    echo -e "${YELLOW}⚠${NC} $1"
    ((WARNINGS++))
}

echo "========================================"
echo "Bridged Architecture Verification"
echo "========================================"
echo ""

AP_INTERFACE=$(cat /var/lib/ap_interface 2>/dev/null)
mapfile -t MESH_INTERFACES < /var/lib/mesh_if 2>/dev/null || MESH_INTERFACES=()
mapfile -t HALOW_INTERFACES < /var/lib/halow_if 2>/dev/null || HALOW_INTERFACES=()

# --- 1. Check Bridge Membership ---
echo "=== Bridge Membership ==="

if ! ip link show br0 &>/dev/null; then
    log_fail "br0 interface does not exist"
else
    log_pass "br0 interface exists"
    
    # Get members
    BR0_MEMBERS=$(bridge link | grep "master br0" | awk '{print $2}' | cut -d: -f1)
    
    if echo "$BR0_MEMBERS" | grep -q "bat0"; then
        log_pass "bat0 is enslaved to br0"
    else
        log_fail "bat0 is NOT enslaved to br0"
    fi
    
    # Check for EUD interfaces
    if echo "$BR0_MEMBERS" | grep -q "end0"; then
        log_pass "end0 is bridged (wired EUD mode)"
    else
        log_warn "end0 is not bridged (normal if no wired EUD)"
    fi
    
    # Check AP interface - should be in br0 and not bat0.
    if [ -n "$AP_INTERFACE" ]; then
        if echo "$BR0_MEMBERS" | grep -q "^${AP_INTERFACE}$"; then
            log_pass "$AP_INTERFACE is in br0 (AP mode, as configured)"
        else
            log_warn "$AP_INTERFACE should be in br0 when configured as AP"
        fi
        
        if batctl if | awk '{print $1}' | cut -d: -f1 | grep -qx "$AP_INTERFACE"; then
            log_fail "$AP_INTERFACE is in BOTH br0 and bat0 (invalid configuration!)"
        else
            log_pass "$AP_INTERFACE is NOT in bat0 (correct for AP mode)"
        fi
    fi
fi

echo ""

# --- 2. Check bat0 Membership ---
echo "=== bat0 (BATMAN-adv) Membership ==="

if ! batctl if &>/dev/null; then
    log_fail "batctl not working or bat0 not configured"
else
    BAT0_MEMBERS=$(batctl if | awk '{print $1}' | cut -d: -f1)
    
    for iface in "${MESH_INTERFACES[@]}"; do
        [ -z "$iface" ] && continue
        if echo "$BAT0_MEMBERS" | grep -qx "$iface"; then
            log_pass "$iface is in bat0 (mesh)"
        else
            log_fail "$iface is NOT in bat0 (mesh)"
        fi
    done

    for iface in "${HALOW_INTERFACES[@]}"; do
        [ -z "$iface" ] && continue
        if echo "$BAT0_MEMBERS" | grep -qx "$iface"; then
            log_pass "$iface is in bat0 (HaLow)"
        else
            log_warn "$iface is NOT in bat0 (HaLow may still be initializing)"
        fi
    done
fi

echo ""

# --- 3. Check IP Configuration ---
echo "=== IP Configuration on br0 ==="

BR0_IPS=$(ip addr show dev br0 | grep -oP 'inet \K[\d.]+')
IP_COUNT=$(echo "$BR0_IPS" | wc -l)

if [ "$IP_COUNT" -ge 2 ]; then
    log_pass "br0 has multiple IPs (expected for chunk allocation)"
    PRIMARY_IP=$(echo "$BR0_IPS" | head -1)
    SECONDARY_IP=$(echo "$BR0_IPS" | head -2 | tail -1)
    echo "    Primary: $PRIMARY_IP"
    echo "    Secondary (gateway): $SECONDARY_IP"
elif [ "$IP_COUNT" -eq 1 ]; then
    log_warn "br0 has only one IP (chunk allocation may not be complete)"
    echo "    IP: $(echo "$BR0_IPS" | head -1)"
else
    log_fail "br0 has no IPv4 address"
fi

echo ""

# --- 4. Check ebtables Rules ---
echo "=== ebtables DHCP Isolation ==="

if ! command -v ebtables &>/dev/null; then
    log_fail "ebtables not installed"
else
    EBTABLES_OUTPUT=$(ebtables -L FORWARD 2>/dev/null)
    
    if echo "$EBTABLES_OUTPUT" | grep -q "DROP.*udp.*67:68.*bat0"; then
        log_pass "DHCP blocked on bat0"
    else
        log_fail "DHCP is NOT blocked on bat0"
    fi
    
    for iface in "${MESH_INTERFACES[@]}" "${HALOW_INTERFACES[@]}"; do
        [ -z "$iface" ] && continue
        if echo "$EBTABLES_OUTPUT" | grep -q "DROP.*udp.*67:68.*${iface}"; then
            log_pass "DHCP blocked on $iface"
        else
            log_warn "DHCP is NOT blocked on $iface"
        fi
    done

    if [ -n "$AP_INTERFACE" ]; then
        if echo "$EBTABLES_OUTPUT" | grep -q "DROP.*udp.*67:68.*${AP_INTERFACE}"; then
            log_fail "DHCP is blocked on $AP_INTERFACE (AP interface - should allow!)"
        else
            log_pass "DHCP is allowed on $AP_INTERFACE (AP interface)"
        fi
    fi
    
    if [ -f /etc/ebtables.rules ]; then
        log_pass "ebtables rules file exists (/etc/ebtables.rules)"
    else
        log_warn "ebtables rules file not found (won't persist across reboots)"
    fi
fi

echo ""

# --- 5. Check dnsmasq Configuration ---
echo "=== dnsmasq Configuration ==="

if [ -f /etc/dnsmasq.d/mesh-eud.conf ]; then
    log_pass "dnsmasq mesh configuration exists"
    
    if grep -q "interface=br0" /etc/dnsmasq.d/mesh-eud.conf; then
        log_pass "dnsmasq listening on br0"
    else
        log_fail "dnsmasq not configured to listen on br0"
    fi
    
    if grep -q "dhcp-range=" /etc/dnsmasq.d/mesh-eud.conf; then
        DHCP_RANGE=$(grep "dhcp-range=" /etc/dnsmasq.d/mesh-eud.conf | cut -d= -f2)
        log_pass "DHCP range configured: $DHCP_RANGE"
    else
        log_fail "No DHCP range configured"
    fi
else
    log_warn "dnsmasq mesh configuration not found (may be unconfigured)"
fi

if systemctl is-active --quiet dnsmasq.service; then
    log_pass "dnsmasq service is running"
else
    log_warn "dnsmasq service is not running"
fi

echo ""

# --- 6. Check Multicast Settings ---
echo "=== Multicast Configuration ==="

check_mc_forward() {
    local iface=$1
    local value=$(sysctl -n net.ipv4.conf.$iface.mc_forwarding 2>/dev/null)
    if [ "$value" == "1" ]; then
        log_pass "IPv4 mc_forwarding enabled on $iface"
    else
        log_warn "IPv4 mc_forwarding disabled on $iface"
    fi
}

check_mc_forward "br0"
check_mc_forward "bat0"

# Check bridge multicast snooping
if [ -f /sys/class/net/br0/bridge/multicast_snooping ]; then
    SNOOPING=$(cat /sys/class/net/br0/bridge/multicast_snooping)
    if [ "$SNOOPING" == "1" ]; then
        log_pass "Bridge multicast snooping enabled"
    else
        log_warn "Bridge multicast snooping disabled"
    fi
fi

echo ""

# --- 7. Check Service State ---
echo "=== Service States ==="

check_service() {
    local service=$1
    if systemctl is-active --quiet "$service"; then
        log_pass "$service is running"
    else
        log_warn "$service is not running"
    fi
}

check_service "batman-enslave.service"
check_service "alfred.service"

if [ -n "$AP_INTERFACE" ]; then
    check_service "hostapd.service"
else
    if systemctl is-active --quiet hostapd.service; then
        log_warn "hostapd running but no AP interface configured"
    fi
fi

echo ""

# --- 8. Configuration Summary ---
echo "=== Configuration Summary ==="

EUD_MODE=$(grep "^eud=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)
echo "EUD Mode: ${EUD_MODE:-unknown}"

if [ -n "$AP_INTERFACE" ]; then
    echo "AP Interface: $AP_INTERFACE"
    echo "  -> $AP_INTERFACE should be: in br0, NOT in bat0, DHCP allowed"
else
    echo "AP Interface: none configured"
fi

if [ -f /var/run/mesh-gateway.state ]; then
    echo "Gateway Mode: ACTIVE (internet uplink on end0)"
else
    echo "Gateway Mode: inactive"
fi

if [ -f /var/run/ethernet_detection_state ]; then
    source /var/run/ethernet_detection_state
    echo "Ethernet State: $ETH_MODE"
fi

echo ""

# --- Summary ---
echo "========================================"
if [ $ERRORS -eq 0 ] && [ $WARNINGS -eq 0 ]; then
    echo -e "${GREEN}All checks passed!${NC}"
elif [ $ERRORS -eq 0 ]; then
    echo -e "${YELLOW}Passed with $WARNINGS warnings${NC}"
else
    echo -e "${RED}Failed with $ERRORS errors and $WARNINGS warnings${NC}"
fi
echo "========================================"

exit $ERRORS
