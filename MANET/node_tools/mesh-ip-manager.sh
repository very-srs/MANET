#!/bin/bash
# ==============================================================================
# Mesh IP Manager - Chunk-Based Allocation with Bridged EUD Architecture
# ==============================================================================
# This script manages IPv4 address claiming using a chunk-based approach where
# each node claims a contiguous block of IPs for itself and its EUDs.
#
# Subnet IP Allocation Scheme:
#   IPs 1-5:    Reserved for mesh services (MediaMTX, Mumble, NTP, etc.)
#   IPs 6+:     Allocated in chunks
#
# Chunk Structure (example with max_euds=5):
#   Chunk size = max_euds + 2
#   - First IP in chunk: br0 primary (mesh interface)
#   - Second IP in chunk: br0 secondary (DHCP gateway for EUDs)
#   - Remaining IPs in chunk: DHCP pool for EUDs
#
# Bridged Architecture:
#   - All EUD interfaces (wlan1 when AP, end0 when wired) are bridged to br0
#   - ebtables blocks DHCP on mesh interfaces (bat0, wlan0, wlan2, and wlan1 if mesh)
#   - dnsmasq listens on br0 for DHCP requests from EUDs
#   - Multicast works at L2 (bridge), no routing needed
#
# wlan1 Dual Purpose:
#   - When AP: wlan1 enslaved to br0 (not in bat0), DHCP allowed
#   - When mesh: wlan1 enslaved to bat0, DHCP blocked
#
# ==============================================================================

# --- Configuration ---
CONTROL_IFACE="br0"
CLAIMED_CHUNKS_FILE="/tmp/claimed_chunks.txt"
PERSISTENT_STATE_FILE="/etc/mesh_ipv4_state"

# Source the network configuration
MAX_EUDS=1
IPV4_NETWORK=""

if [ -f /etc/mesh.conf ]; then
    while IFS='=' read -r key value; do
        [[ -z "$key" || "$key" =~ ^[[:space:]]*# ]] && continue
        case "$key" in
            max_euds_per_node) MAX_EUDS="$value" ;;
            ipv4_network)      IPV4_NETWORK="$value" ;;
            eud)               EUD_MODE="$value" ;;
        esac
    done < /etc/mesh.conf
fi
EUD_MODE=${EUD_MODE:-"none"}

# Calculate service VIPs from ipv4_network — same formula as election scripts
# MTX VIP = HostMin+1, Mumble VIP = HostMin+2
MTX_VIP=""
MUMBLE_VIP=""
_calc_service_vips() {
    local calc host_min
    calc=$(ipcalc "$IPV4_NETWORK" 2>/dev/null) || return 0
    host_min=$(echo "$calc" | awk '/HostMin/ {print $2}')
    [ -n "$host_min" ] || return 0
    MTX_VIP="${host_min%.*}.$((${host_min##*.} + 1))"
    MUMBLE_VIP="${host_min%.*}.$((${host_min##*.} + 2))"
}
_calc_service_vips

# If any EUD mode is active, we need at least 1 EUD IP
if [[ "$EUD_MODE" != "none" && "$MAX_EUDS" -lt 1 ]]; then
#    log "EUD mode is '$EUD_MODE' but max_euds=$MAX_EUDS. Forcing max_euds=1."
    MAX_EUDS=1
fi

# Sourced above
IPV4_NETWORK=${IPV4_NETWORK:-"10.43.1.0/16"}
MAX_EUDS=${MAX_EUDS:-1}
CHUNK_SIZE=$((MAX_EUDS + 2))  # br0 primary + br0 secondary (gateway) + EUDs
SERVICES_RESERVED=5  # IPs 1-5 for services

# --- State Variables ---
IPV4_STATE="UNCONFIGURED"
CURRENT_IPV4=""
CURRENT_CHUNK=""
FORCE_CONF="/etc/manet/mesh-ip-force.conf"
FORCED_IPV4=""
FORCED_CHUNK=""
FORCED_GW=""
FORCED_DNS=""

PERSISTENT_IPV4=""
PERSISTENT_CHUNK=""
PERSISTENT_NETWORK=""

# --- Helper Functions ---
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - IP-MGR: $1" >&2
}


if [ -f "$FORCE_CONF" ]; then
    # shellcheck disable=SC1090
    . "$FORCE_CONF"
    log "Loaded forced mesh IP config from $FORCE_CONF"
fi


# Converts an IP string to a 32-bit integer
ip_to_int() {
    local ip=$1
    if [[ -z "$ip" || ! "$ip" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
        return 1
    fi
    local a b c d
    IFS=. read -r a b c d <<<"$ip"
    echo "$(( (a << 24) + (b << 16) + (c << 8) + d ))"
}

# Converts a 32-bit integer to an IP string
int_to_ip() {
    local ip_int=$1
    echo "$(( (ip_int >> 24) & 255 )).$(( (ip_int >> 16) & 255 )).$(( (ip_int >> 8) & 255 )).$(( ip_int & 255 ))"
}

# Calculate chunk IPs given a chunk number
get_chunk_ips() {
    local chunk_num=$1
    local CALC_OUTPUT=$(ipcalc "$IPV4_NETWORK" 2>/dev/null)

    if [ -z "$CALC_OUTPUT" ]; then
        return 1
    fi

    local HOST_MIN=$(echo "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')
    local MIN_INT=$(ip_to_int "$HOST_MIN")

    # First chunk starts after services reservation
    local CHUNK_START_INT=$((MIN_INT + SERVICES_RESERVED + (chunk_num * CHUNK_SIZE)))

    # First IP in chunk (for br0 primary - mesh communication)
    local BR0_PRIMARY=$(int_to_ip "$CHUNK_START_INT")

    # Second IP in chunk (for br0 secondary - DHCP gateway)
    local BR0_SECONDARY=$(int_to_ip $((CHUNK_START_INT + 1)))

    # DHCP pool starts at third IP
    local DHCP_START=$(int_to_ip $((CHUNK_START_INT + 2)))
    local DHCP_END=$(int_to_ip $((CHUNK_START_INT + CHUNK_SIZE - 1)))

    echo "${BR0_PRIMARY}:${BR0_SECONDARY}:${DHCP_START}:${DHCP_END}"
}

# Check if an IP is in the usable range
ip_in_cidr() {
    local ip=$1
    local cidr=$2

    if [[ -z "$ip" || -z "$cidr" ]]; then
        return 1
    fi

    local CALC_OUTPUT=$(ipcalc "$cidr" 2>/dev/null)
    if [ -z "$CALC_OUTPUT" ]; then
        return 1
    fi

    local HOST_MIN=$(echo "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')
    local HOST_MAX=$(echo "$CALC_OUTPUT" | awk '/HostMax/ {print $2}')

    if [ -z "$HOST_MIN" ] || [ -z "$HOST_MAX" ]; then
        return 1
    fi

    local IP_INT=$(ip_to_int "$ip")
    local MIN_INT=$(ip_to_int "$HOST_MIN")
    local MAX_INT=$(ip_to_int "$HOST_MAX")

    if [ -z "$IP_INT" ] || [ -z "$MIN_INT" ] || [ -z "$MAX_INT" ]; then
        return 1
    fi

    if [ "$IP_INT" -ge "$MIN_INT" ] && [ "$IP_INT" -le "$MAX_INT" ]; then
        return 0
    else
        return 1
    fi
}

is_service_reserved_ip() {
    local ip="$1"
    local CALC_OUTPUT HOST_MIN MIN_INT IP_INT offset

    CALC_OUTPUT=$(ipcalc "$IPV4_NETWORK" 2>/dev/null)
    [ -n "$CALC_OUTPUT" ] || return 1

    HOST_MIN=$(echo "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')
    MIN_INT=$(ip_to_int "$HOST_MIN")
    IP_INT=$(ip_to_int "$ip")
    [ -n "$MIN_INT" ] && [ -n "$IP_INT" ] || return 1

    offset=$((IP_INT - MIN_INT))
    [ "$offset" -ge 0 ] && [ "$offset" -lt "$SERVICES_RESERVED" ]
}

# Get a random available chunk
get_random_chunk() {
    local CALC_OUTPUT=$(ipcalc "$IPV4_NETWORK" 2>/dev/null)
    
    if [ -z "$CALC_OUTPUT" ]; then
        log "Error: ipcalc failed for CIDR: $IPV4_NETWORK"
        return 1
    fi

    local HOST_MIN=$(echo "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')
    local HOST_MAX=$(echo "$CALC_OUTPUT" | awk '/HostMax/ {print $2}')
    local MIN_INT=$(ip_to_int "$HOST_MIN")
    local MAX_INT=$(ip_to_int "$HOST_MAX")

    # Calculate available IP space after services
    local AVAILABLE_IPS=$((MAX_INT - MIN_INT + 1 - SERVICES_RESERVED))
    local MAX_CHUNKS=$((AVAILABLE_IPS / CHUNK_SIZE))
    
    if [ "$MAX_CHUNKS" -lt 1 ]; then
        log "Error: Network too small for chunk size $CHUNK_SIZE"
        return 1
    fi
    
    log "Network supports $MAX_CHUNKS chunks (chunk_size=$CHUNK_SIZE, max_euds=$MAX_EUDS)"
    
    # Build list of claimed chunks
    declare -A claimed_chunks
    if [ -f "$CLAIMED_CHUNKS_FILE" ]; then
        while IFS=, read -r chunk mac; do
            claimed_chunks[$chunk]=1
        done < "$CLAIMED_CHUNKS_FILE"
    fi
    
    # Find available chunks
    local available_chunks=()
    for ((i=0; i<MAX_CHUNKS; i++)); do
        if [ -z "${claimed_chunks[$i]}" ]; then
            available_chunks+=($i)
        fi
    done
    
    if [ ${#available_chunks[@]} -eq 0 ]; then
        log "Error: No available chunks"
        return 1
    fi
    
    # Select random available chunk
    local random_index=$((RANDOM % ${#available_chunks[@]}))
    echo "${available_chunks[$random_index]}"
}

# Save persistent state
save_persistent_state() {
    cat > "$PERSISTENT_STATE_FILE" <<- EOF
# Persistent IPv4 state for mesh node
# Last updated: $(date)
PERSISTENT_IPV4="$PERSISTENT_IPV4"
PERSISTENT_CHUNK="$PERSISTENT_CHUNK"
PERSISTENT_NETWORK="$PERSISTENT_NETWORK"
EOF
    chmod 644 "$PERSISTENT_STATE_FILE"
}

mac_is_local() {
    local mac="$1"
    local local_mac
    local iface_path
    local iface

    [ -n "$mac" ] || return 1

    for iface_path in /sys/class/net/*; do
        iface=$(basename "$iface_path")
        [ -e "/sys/class/net/$iface/address" ] || continue
        local_mac=$(cat "/sys/class/net/$iface/address" 2>/dev/null)
        if [ "$mac" = "$local_mac" ]; then
            return 0
        fi
    done

    return 1
}

release_control_ips() {
    local prefix="${IPV4_NETWORK#*/}"
    local primary=""
    local secondary=""
    local _dhcp_start=""
    local _dhcp_end=""

    if [ -n "$PERSISTENT_CHUNK" ]; then
        IFS=: read -r primary secondary _dhcp_start _dhcp_end <<< "$(get_chunk_ips "$PERSISTENT_CHUNK")"
        [ -n "$primary" ] && ip addr del "${primary}/${prefix}" dev "$CONTROL_IFACE" 2>/dev/null || true
        [ -n "$secondary" ] && ip addr del "${secondary}/${prefix}" dev "$CONTROL_IFACE" 2>/dev/null || true
        log "Released br0 chunk addresses: ${primary:-unknown}, ${secondary:-unknown}"
    elif [ -n "$CURRENT_IPV4" ]; then
        ip addr del "${CURRENT_IPV4}/${prefix}" dev "$CONTROL_IFACE" 2>/dev/null || true
        log "Released current br0 IPv4 address: $CURRENT_IPV4"
    fi
}

cleanup_control_aliases() {
    local prefix="${IPV4_NETWORK#*/}"
    local primary=""
    local secondary=""
    local ip=""
    local keep=""
    local _dhcp_start=""
    local _dhcp_end=""

    [ -n "$PERSISTENT_CHUNK" ] || return 0

    IFS=: read -r primary secondary _dhcp_start _dhcp_end <<< "$(get_chunk_ips "$PERSISTENT_CHUNK")"
    keep=" $primary $secondary "

    while read -r ip; do
        [ -n "$ip" ] || continue
        ip_in_cidr "$ip" "$IPV4_NETWORK" || continue
        is_service_reserved_ip "$ip" && continue

        if [[ "$keep" != *" $ip "* ]]; then
            ip addr del "${ip}/${prefix}" dev "$CONTROL_IFACE" 2>/dev/null || true
            log "Removed stale $CONTROL_IFACE IPv4 alias: $ip"
        fi
    done < <(ip -4 -o addr show dev "$CONTROL_IFACE" 2>/dev/null | awk '{print $4}' | cut -d/ -f1)
}

# Configure ebtables to block DHCP on mesh interfaces
configure_ebtables_dhcp_isolation() {
    local br0_secondary=$1
    local iface_path
    local iface
    
    log "Configuring ebtables DHCP isolation..."
    
    # Flush existing rules
    ebtables -F FORWARD 2>/dev/null || true
    
    # Get AP interface if configured. DHCP is allowed only on the selected EUD
    # AP interface; all mesh radios and bat0 must block forwarded DHCP.
    local AP_INTERFACE=""
    if [ -f /var/lib/ap_interface ]; then
        AP_INTERFACE=$(cat /var/lib/ap_interface)
        log "AP interface: $AP_INTERFACE"
    fi
    
    # Block DHCP on bat0 (the BATMAN-adv backbone)
    if [ -d "/sys/class/net/bat0" ]; then
        ebtables -A FORWARD -o bat0 -p IPv4 --ip-protocol udp --ip-destination-port 67:68 -j DROP
        ebtables -A FORWARD -i bat0 -p IPv4 --ip-protocol udp --ip-destination-port 67:68 -j DROP
        log "Blocked DHCP on bat0"
    fi
    
    # Block DHCP on all wireless interfaces EXCEPT the AP. Do not assume a
    # specific wlanX mapping; Pi onboard, MT7915, and HaLow names vary by boot.
    for iface_path in /sys/class/net/wlan*; do
        [ -e "$iface_path" ] || continue
        iface=$(basename "$iface_path")
        if [ ! -d "/sys/class/net/$iface" ]; then
            continue
        fi
        
        # Skip if this is the AP interface
        if [ -n "$AP_INTERFACE" ] && [ "$iface" == "$AP_INTERFACE" ]; then
            log "Allowing DHCP on $iface (AP interface)"
            continue
        fi
        
        # Check if interface exists and has a master
        if ip link show "$iface" 2>/dev/null | grep -q "master"; then
            ebtables -A FORWARD -o "$iface" -p IPv4 --ip-protocol udp --ip-destination-port 67:68 -j DROP
            ebtables -A FORWARD -i "$iface" -p IPv4 --ip-protocol udp --ip-destination-port 67:68 -j DROP
            log "Blocked DHCP on $iface"
        fi
    done
    
    # Save rules for restore on boot
    ebtables-save > /etc/ebtables.rules
    log "ebtables rules saved to /etc/ebtables.rules"
}

# Configure dnsmasq for DHCP on br0
configure_dnsmasq() {
    local br0_primary=$1
    local br0_secondary=$2
    local dhcp_start=$3
    local dhcp_end=$4
    local old_gateway=""
    local old_primary=""
    local _MUMBLE_VIP_LINE=""
    local _MTX_VIP_LINE=""
    [ -n "$MUMBLE_VIP" ] && _MUMBLE_VIP_LINE="address=/mumble.local/$MUMBLE_VIP"
    [ -n "$MTX_VIP" ]    && _MTX_VIP_LINE="address=/mtx.local/$MTX_VIP"

    if [ -f /etc/dnsmasq.d/mesh-eud.conf ]; then
        old_gateway=$(awk -F, '$1 == "dhcp-option=3" {print $2; exit}' /etc/dnsmasq.d/mesh-eud.conf)
    fi

    if [ -n "$old_gateway" ] && [ "$old_gateway" != "$br0_secondary" ] && ip_in_cidr "$old_gateway" "$IPV4_NETWORK"; then
        old_primary=$(int_to_ip $(( $(ip_to_int "$old_gateway") - 1 )))
        log "EUD gateway changed from ${old_primary:-unknown}/$old_gateway to $br0_primary/$br0_secondary"
    fi

    log "Configuring dnsmasq: pool=$dhcp_start-$dhcp_end, gateway=$br0_secondary"

    cat > /etc/dnsmasq.d/mesh-eud.conf <<- EOF
# Listen only on br0 bridge
interface=br0
bind-interfaces

# DHCP configuration from this node's chunk
dhcp-range=$dhcp_start,$dhcp_end,4m

# Gateway is this node's br0 secondary address
dhcp-option=3,$br0_secondary

# DNS configuration
dhcp-option=6,$br0_secondary
domain=mesh.local
local=/mesh.local/

# manet.local and perf.local resolve to this node's IP so EUD clients can reach the admin panels
address=/manet.local/$br0_secondary
address=/perf.local/$br0_secondary

# Service VIPs — stable across the mesh regardless of which node is leader
${_MUMBLE_VIP_LINE}
${_MTX_VIP_LINE}

# Upstream DNS for EUD internet access through Ethernet
server=1.1.1.1
server=8.8.8.8

# Log for debugging
log-dhcp
EOF

    # Ensure dnsmasq is unmasked, enabled, and running
    systemctl unmask dnsmasq.service 2>/dev/null
#    systemctl enable dnsmasq.service 2>/dev/null

    if systemctl is-active --quiet dnsmasq.service; then
        systemctl restart dnsmasq.service
        log "dnsmasq restarted"
    else
        systemctl start dnsmasq.service
        log "dnsmasq started"
    fi
}

ensure_control_addr() {
    local ip="$1"
    local prefix="${IPV4_NETWORK#*/}"

    [ -n "$ip" ] || return 0

    if ! ip -4 addr show dev "$CONTROL_IFACE" | grep -qw "$ip"; then
        ip addr add "${ip}/${prefix}" dev "$CONTROL_IFACE" 2>/dev/null || true
        log "Restored $CONTROL_IFACE IPv4 address: $ip"
    fi
}

# --- Main Logic ---

# Get our MAC address
MY_MAC=$(cat "/sys/class/net/${CONTROL_IFACE}/address" 2>/dev/null || echo "")
if [ -z "$MY_MAC" ]; then
    log "ERROR: Cannot read MAC address from $CONTROL_IFACE"
    exit 1
fi

log "Chunk-based IP allocation: chunk_size=$CHUNK_SIZE (max_euds=$MAX_EUDS)"

# Load persistent state
if [ -f "$PERSISTENT_STATE_FILE" ]; then
    source "$PERSISTENT_STATE_FILE" 2>/dev/null
    if [ -n "$PERSISTENT_IPV4" ] && [ -n "$PERSISTENT_CHUNK" ]; then
        log "Loaded persistent state: chunk=$PERSISTENT_CHUNK, ip=$PERSISTENT_IPV4"
    fi

    if [ -n "${FORCED_IPV4:-}" ] && [ -n "${FORCED_CHUNK:-}" ]; then
        PERSISTENT_IPV4="$FORCED_IPV4"
        PERSISTENT_CHUNK="$FORCED_CHUNK"
        log "Forcing persistent state: chunk=$PERSISTENT_CHUNK, ip=$PERSISTENT_IPV4"
    fi
fi

cleanup_control_aliases

# Check if we already have an IP configured on br0
CURRENT_IPV4=$(ip addr show dev "$CONTROL_IFACE" | grep -oP 'inet \K[\d.]+' | head -1)
if [ -n "$CURRENT_IPV4" ]; then
    IPV4_STATE="CONFIGURED"
    log "Current IPv4 on br0: ${CURRENT_IPV4}"

    if [ -z "$PERSISTENT_CHUNK" ] && is_service_reserved_ip "$CURRENT_IPV4"; then
        log "Only service-reserved br0 IPv4 is present; selecting a node/EUD chunk"
        CURRENT_IPV4=""
        IPV4_STATE="UNCONFIGURED"
    fi
fi

# Load claimed chunks from registry
if [ -f "$CLAIMED_CHUNKS_FILE" ]; then
    mapfile -t CLAIMED_CHUNKS < "$CLAIMED_CHUNKS_FILE"
else
    CLAIMED_CHUNKS=()
    log "Warning: Claimed chunks file not found"
fi

# --- State Machine ---
case $IPV4_STATE in
    "UNCONFIGURED")
        PROPOSED_CHUNK=""
        SHOULD_USE_PERSISTENT=false

        # Check if we have a persistent chunk and if network has changed
        if [ -n "$PERSISTENT_CHUNK" ] && [ -n "$PERSISTENT_IPV4" ]; then
            # Check if network changed
            if [ -n "$PERSISTENT_NETWORK" ] && [ "$PERSISTENT_NETWORK" != "$IPV4_NETWORK" ]; then
                log "Network changed from ${PERSISTENT_NETWORK} to ${IPV4_NETWORK}. Selecting new chunk."
                PERSISTENT_IPV4=""
                PERSISTENT_CHUNK=""
                PERSISTENT_NETWORK=""
                save_persistent_state
            else
                # Verify persistent IP is in current network
                if ip_in_cidr "$PERSISTENT_IPV4" "$IPV4_NETWORK"; then
                    log "Attempting to reclaim previous chunk $PERSISTENT_CHUNK (IP: ${PERSISTENT_IPV4})"
                    PROPOSED_CHUNK="$PERSISTENT_CHUNK"
                    SHOULD_USE_PERSISTENT=true
                else
                    log "Persistent IP ${PERSISTENT_IPV4} not in network ${IPV4_NETWORK}. Selecting new chunk."
                    PERSISTENT_IPV4=""
                    PERSISTENT_CHUNK=""
                    save_persistent_state
                fi
            fi
        fi

        # Generate new chunk if needed
        if [ -z "$PROPOSED_CHUNK" ]; then
            log "Selecting new chunk from ${IPV4_NETWORK}..."
            PROPOSED_CHUNK=$(get_random_chunk)
        fi

        if [ -z "$PROPOSED_CHUNK" ]; then
            log "Failed to select chunk"
            exit 1
        fi

        # Get chunk IPs
        CHUNK_IPS=$(get_chunk_ips "$PROPOSED_CHUNK")
        IFS=: read -r BR0_PRIMARY BR0_SECONDARY DHCP_START DHCP_END <<< "$CHUNK_IPS"
        
        log "Proposed chunk $PROPOSED_CHUNK: primary=$BR0_PRIMARY, gateway=$BR0_SECONDARY, dhcp=$DHCP_START-$DHCP_END"

        # For persistent chunks, skip the claimed_chunks conflict check and assign directly.
        # claimed_chunks.txt lives in /tmp and is lost on reboot, so it routinely contains
        # stale entries from other nodes. A false conflict here clears persistent state and
        # causes the node to pick a random new chunk — exactly the churn we want to avoid.
        # Real conflicts (two live nodes with the same IPs) are resolved by the MAC tie-breaker
        # in the CONFIGURED branch, which operates on actual live ARP data.
        CONFLICT=false
        if [ "$SHOULD_USE_PERSISTENT" = false ]; then
            for entry in "${CLAIMED_CHUNKS[@]}"; do
                CLAIMED_CHUNK=$(echo "$entry" | cut -d',' -f1)
                if [[ "$CLAIMED_CHUNK" == "$PROPOSED_CHUNK" ]]; then
                    CONFLICT=true
                    break
                fi
            done
        fi

        if [ "$CONFLICT" = true ]; then
            log "Proposed chunk ${PROPOSED_CHUNK} is in use. Will retry next cycle."
        else
            log "Claiming chunk ${PROPOSED_CHUNK} with br0 IPs ${BR0_PRIMARY} and ${BR0_SECONDARY}..."
            
            # Assign both IPs to br0
            ip addr add "${BR0_PRIMARY}/${IPV4_NETWORK#*/}" dev "$CONTROL_IFACE"
            ip addr add "${BR0_SECONDARY}/${IPV4_NETWORK#*/}" dev "$CONTROL_IFACE"
            log "Assigned br0 primary: $BR0_PRIMARY, secondary (gateway): $BR0_SECONDARY"
            
            # Configure ebtables DHCP isolation
            configure_ebtables_dhcp_isolation "$BR0_SECONDARY"
            
            # Configure dnsmasq
            configure_dnsmasq "$BR0_PRIMARY" "$BR0_SECONDARY" "$DHCP_START" "$DHCP_END"

            # Save persistent state
            PERSISTENT_IPV4="$BR0_PRIMARY"
            PERSISTENT_CHUNK="$PROPOSED_CHUNK"
            PERSISTENT_NETWORK="$IPV4_NETWORK"
            save_persistent_state
            
            log "Successfully claimed chunk ${PROPOSED_CHUNK}"
            
            # Write chunk to temp file for encoder to pick up
            echo "$PROPOSED_CHUNK" > /var/run/my_ipv4_chunk
        fi
        ;;

    "CONFIGURED")
        # Check for conflicts
        CONFLICTING_MAC=""
        CONFLICTING_CHUNK=""
        
        for entry in "${CLAIMED_CHUNKS[@]}"; do
            IFS=, read -r CLAIMED_CHUNK CLAIMED_MAC <<< "$entry"
            
            # Get this chunk's br0 primary IP
            CHUNK_IPS=$(get_chunk_ips "$CLAIMED_CHUNK")
            IFS=: read -r CHUNK_BR0_PRIMARY _ _ _ <<< "$CHUNK_IPS"
            
            # Check if someone else claimed our IP
            if [[ "$CHUNK_BR0_PRIMARY" == "$CURRENT_IPV4" ]] && ! mac_is_local "$CLAIMED_MAC"; then
                CONFLICTING_MAC="$CLAIMED_MAC"
                CONFLICTING_CHUNK="$CLAIMED_CHUNK"
                break
            fi
        done

        if [[ -n "$CONFLICTING_MAC" ]]; then
            log "CONFLICT DETECTED for ${CURRENT_IPV4}! Conflicting MAC: ${CONFLICTING_MAC} (chunk ${CONFLICTING_CHUNK})"

            # Tie-breaker: higher MAC wins
            if [[ "$MY_MAC" > "$CONFLICTING_MAC" ]]; then
                log "Won tie-breaker. Defending chunk."
            else
                log "Lost tie-breaker. Releasing chunk and IPs."
                release_control_ips
                
                PERSISTENT_IPV4=""
                PERSISTENT_CHUNK=""
                PERSISTENT_NETWORK=""
                save_persistent_state
                rm -f /var/run/my_ipv4_chunk
            fi
        else
            # No conflict, only reconfigure if something actually changed
            if [ -n "$PERSISTENT_CHUNK" ]; then
                echo "$PERSISTENT_CHUNK" > /var/run/my_ipv4_chunk

                # Get current chunk IPs
                CHUNK_IPS=$(get_chunk_ips "$PERSISTENT_CHUNK")
                IFS=: read -r BR0_PRIMARY BR0_SECONDARY DHCP_START DHCP_END <<< "$CHUNK_IPS"

                ensure_control_addr "$BR0_PRIMARY"
                ensure_control_addr "$BR0_SECONDARY"

                # Only reconfigure dnsmasq if the config has changed
                DNSMASQ_CONF="/etc/dnsmasq.d/mesh-eud.conf"
                NEEDS_DNSMASQ_UPDATE=false

                if [ ! -f "$DNSMASQ_CONF" ]; then
                    NEEDS_DNSMASQ_UPDATE=true
                elif ! grep -q "dhcp-range=$DHCP_START,$DHCP_END" "$DNSMASQ_CONF" 2>/dev/null; then
                    NEEDS_DNSMASQ_UPDATE=true
                elif ! grep -q "dhcp-option=3,$BR0_SECONDARY" "$DNSMASQ_CONF" 2>/dev/null; then
                    NEEDS_DNSMASQ_UPDATE=true
                elif grep -q "^no-resolv" "$DNSMASQ_CONF" 2>/dev/null; then
                    NEEDS_DNSMASQ_UPDATE=true
                elif ! grep -q "^server=" "$DNSMASQ_CONF" 2>/dev/null; then
                    NEEDS_DNSMASQ_UPDATE=true
                elif ! grep -q "^address=/mumble.local/" "$DNSMASQ_CONF" 2>/dev/null; then
                    NEEDS_DNSMASQ_UPDATE=true
                fi

                if [ "$NEEDS_DNSMASQ_UPDATE" = true ]; then
                    log "DHCP config changed, reconfiguring..."
                    configure_ebtables_dhcp_isolation "$BR0_SECONDARY"
                    configure_dnsmasq "$BR0_PRIMARY" "$BR0_SECONDARY" "$DHCP_START" "$DHCP_END"
                fi
            fi
        fi
        ;;
esac

exit 0
