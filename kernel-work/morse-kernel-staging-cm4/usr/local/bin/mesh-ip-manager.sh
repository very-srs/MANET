#!/bin/bash
# ==============================================================================
# Mesh IP Manager - Chunk-Based Allocation
# ==============================================================================
# This script manages IPv4 address claiming using a chunk-based approach where
# each node claims a contiguous block of IPs for itself and its EUDs.
#
# IP Allocation Scheme:
#   IPs 1-5:    Reserved for mesh services (MediaMTX, Mumble, NTP, etc.)
#   IPs 6+:     Allocated in chunks
#
# Chunk Structure (example with max_euds=5):
#   Chunk size = max_euds + 2
#   - IP 0 in chunk: br0 (mesh interface)
#   - IP 1 in chunk: wlan2 (AP gateway - if wireless/auto mode)
#   - IPs 2+ in chunk: DHCP pool for EUDs
#
# ==============================================================================

# --- Configuration ---
CONTROL_IFACE="br0"
CLAIMED_CHUNKS_FILE="/tmp/claimed_chunks.txt"
PERSISTENT_STATE_FILE="/etc/mesh_ipv4_state"

# Source the network configuration
MAX_EUDS=0
IPV4_NETWORK=""

if [ -f /etc/mesh.conf ]; then
    while IFS='=' read -r key value; do
        [[ -z "$key" || "$key" =~ ^[[:space:]]*# ]] && continue
        case "$key" in
            max_euds_per_node)
                MAX_EUDS="$value"
                ;;
            ipv4_network)
                IPV4_NETWORK="$value"
                ;;
        esac
    done < /etc/mesh.conf
fi

IPV4_NETWORK=${IPV4_NETWORK:-"10.43.1.0/16"}
MAX_EUDS=${MAX_EUDS:-0}
CHUNK_SIZE=$((MAX_EUDS + 2))  # node br0 + AP interface + EUDs
SERVICES_RESERVED=5  # IPs 1-5 for services

# --- State Variables ---
IPV4_STATE="UNCONFIGURED"
CURRENT_IPV4=""
CURRENT_CHUNK=""
PERSISTENT_IPV4=""
PERSISTENT_CHUNK=""
PERSISTENT_NETWORK=""

# --- Helper Functions ---
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - IP-MGR: $1" >&2
}

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
    
    # First IP in chunk (for br0)
    local BR0_IP=$(int_to_ip "$CHUNK_START_INT")
    
    # Second IP in chunk (for AP interface)
    local AP_IP=$(int_to_ip $((CHUNK_START_INT + 1)))
    
    # DHCP pool starts at third IP
    local DHCP_START=$(int_to_ip $((CHUNK_START_INT + 2)))
    local DHCP_END=$(int_to_ip $((CHUNK_START_INT + CHUNK_SIZE - 1)))
    
    echo "${BR0_IP}:${AP_IP}:${DHCP_START}:${DHCP_END}"
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

# Configure AP interface with its chunk IP
configure_ap_interface() {
    local ap_ip=$1
    local ap_iface=$(cat /var/lib/ap_interface 2>/dev/null)
    
    if [ -z "$ap_iface" ]; then
        return 0  # No AP configured
    fi
    
    # Check if IP already assigned
    if ip addr show dev "$ap_iface" | grep -q "inet $ap_ip/"; then
        log "AP interface $ap_iface already has $ap_ip"
        return 0
    fi
    
    log "Configuring AP interface $ap_iface with $ap_ip"
    ip addr flush dev "$ap_iface" 2>/dev/null
    ip addr add "${ap_ip}/${IPV4_NETWORK#*/}" dev "$ap_iface"
    ip link set "$ap_iface" up
}

# Configure dnsmasq for chunk DHCP pool
configure_dnsmasq() {
    local ap_ip=$1
    local dhcp_start=$2
    local dhcp_end=$3
    local ap_iface=$(cat /var/lib/ap_interface 2>/dev/null)
    
    if [ -z "$ap_iface" ]; then
        return 0  # No AP configured
    fi
    
    log "Configuring dnsmasq: pool=$dhcp_start-$dhcp_end, gateway=$ap_ip"
    
    cat > /etc/dnsmasq.d/mesh-ap.conf <<- EOF
# Listen only on AP interface
interface=$ap_iface
bind-interfaces

# Do not serve DHCP on br0
no-dhcp-interface=br0

# DHCP configuration from this node's chunk
dhcp-range=$dhcp_start,$dhcp_end,30m

# Gateway is this node's AP interface
dhcp-option=3,$ap_ip

# DNS configuration
domain=mesh.local
local=/mesh.local/

# Disable DNS upstream (offline mesh)
no-resolv
no-poll

# Log for debugging
log-dhcp
EOF
    
    # Restart dnsmasq if it's running
    if systemctl is-active --quiet dnsmasq.service; then
        systemctl restart dnsmasq.service
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
fi

# Check if we already have an IP configured on br0
CURRENT_IPV4=$(ip addr show dev "$CONTROL_IFACE" | grep -oP 'inet \K[\d.]+' | head -1)
if [ -n "$CURRENT_IPV4" ]; then
    IPV4_STATE="CONFIGURED"
    log "Current IPv4 on br0: ${CURRENT_IPV4}"
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
        IFS=: read -r BR0_IP AP_IP DHCP_START DHCP_END <<< "$CHUNK_IPS"
        
        log "Proposed chunk $PROPOSED_CHUNK: br0=$BR0_IP, ap=$AP_IP, dhcp=$DHCP_START-$DHCP_END"

        # Check for conflicts
        CONFLICT=false
        for entry in "${CLAIMED_CHUNKS[@]}"; do
            CLAIMED_CHUNK=$(echo "$entry" | cut -d',' -f1)
            if [[ "$CLAIMED_CHUNK" == "$PROPOSED_CHUNK" ]]; then
                CONFLICT=true
                break
            fi
        done

        if [ "$CONFLICT" = true ]; then
            if [ "$SHOULD_USE_PERSISTENT" = true ]; then
                log "Previous chunk ${PROPOSED_CHUNK} is now in use. Will select new chunk next cycle."
                PERSISTENT_IPV4=""
                PERSISTENT_CHUNK=""
                save_persistent_state
            else
                log "Proposed chunk ${PROPOSED_CHUNK} is in use. Will retry next cycle."
            fi
        else
            log "Claiming chunk ${PROPOSED_CHUNK} with br0 IP ${BR0_IP}..."
            
            # Assign IP to br0
            ip addr add "${BR0_IP}/${IPV4_NETWORK#*/}" dev "$CONTROL_IFACE"
            
            # Configure AP interface if present
            configure_ap_interface "$AP_IP"
            
            # Configure dnsmasq if AP present
            configure_dnsmasq "$AP_IP" "$DHCP_START" "$DHCP_END"
            
            # Save persistent state
            PERSISTENT_IPV4="$BR0_IP"
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
            
            # Get this chunk's br0 IP
            CHUNK_IPS=$(get_chunk_ips "$CLAIMED_CHUNK")
            IFS=: read -r CHUNK_BR0_IP _ _ _ <<< "$CHUNK_IPS"
            
            # Check if someone else claimed our IP
            if [[ "$CHUNK_BR0_IP" == "$CURRENT_IPV4" && "$CLAIMED_MAC" != "$MY_MAC" ]]; then
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
                log "Lost tie-breaker. Releasing chunk and IP."
                ip addr del "${CURRENT_IPV4}/${IPV4_NETWORK#*/}" dev "$CONTROL_IFACE" 2>/dev/null
                
                # Remove AP IP if configured
                local ap_iface=$(cat /var/lib/ap_interface 2>/dev/null)
                if [ -n "$ap_iface" ]; then
                    ip addr flush dev "$ap_iface" 2>/dev/null
                fi
                
                PERSISTENT_IPV4=""
                PERSISTENT_CHUNK=""
                PERSISTENT_NETWORK=""
                save_persistent_state
                rm -f /var/run/my_ipv4_chunk
            fi
        else
            # No conflict, write chunk info for encoder
            if [ -n "$PERSISTENT_CHUNK" ]; then
                echo "$PERSISTENT_CHUNK" > /var/run/my_ipv4_chunk
            fi
        fi
        ;;
esac

exit 0
