#!/bin/bash
# ==============================================================================
# Mesh IP Manager
# ==============================================================================
# This script manages IPv4 address claiming, defending, and conflict resolution
# for the mesh node. It uses a decentralized approach with tie-breaking based
# on MAC address comparisons.
# ==============================================================================

# --- Configuration ---
CONTROL_IFACE="br0"
CLAIMED_IPS_FILE="/tmp/claimed_ips.txt"
PERSISTENT_STATE_FILE="/etc/mesh_ipv4_state"

# Source the network configuration
if [ -f /etc/mesh_ipv4.conf ]; then
    source /etc/mesh_ipv4.conf
fi

IPV4_NETWORK=${IPV4_NETWORK:-"10.43.1.0/16"}
RESERVED_IP_COUNT=${RESERVED_IP_COUNT:-5}

# --- State Variables ---
IPV4_STATE="UNCONFIGURED"
CURRENT_IPV4=""
PERSISTENT_IPV4=""
PERSISTENT_NETWORK=""
RESERVED_START_INT=0
RESERVED_END_INT=0

# --- Helper Functions ---
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - IP-MGR: $1"
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

# Check if an IP is within a CIDR range
ip_in_cidr() {
    local ip=$1
    local cidr=$2

    if [[ -z "$ip" || -z "$cidr" ]]; then
        return 1
    fi

    local CALC_OUTPUT
    CALC_OUTPUT=$(ipcalc "$cidr" 2>/dev/null)
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

# Get a random IP from CIDR, excluding reserved range
get_random_ip_from_cidr() {
    local CIDR="$1"

    if [[ -z "$CIDR" || ! "$CIDR" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}/[0-9]{1,2}$ ]]; then
        log "Error: Invalid CIDR format: '$CIDR'"
        return 1
    fi

    local CALC_OUTPUT
    CALC_OUTPUT=$(ipcalc "$CIDR" 2>/dev/null)
    if [ -z "$CALC_OUTPUT" ]; then
        log "Error: ipcalc failed for CIDR: $CIDR"
        return 1
    fi

    local HOST_MIN=$(echo "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')
    local HOST_MAX=$(echo "$CALC_OUTPUT" | awk '/HostMax/ {print $2}')

    if [ -z "$HOST_MIN" ] || [ -z "$HOST_MAX" ]; then
        log "Error: ipcalc parsing failed"
        return 1
    fi

    local MIN_INT=$(ip_to_int "$HOST_MIN")
    local MAX_INT=$(ip_to_int "$HOST_MAX")

    if [ -z "$MIN_INT" ] || [ -z "$MAX_INT" ]; then
        log "Error: Failed to convert HostMin/Max to integer"
        return 1
    fi

    # Calculate reserved range if not already done
    if [[ "$RESERVED_START_INT" -eq 0 && "$MIN_INT" -le "$MAX_INT" ]]; then
        RESERVED_START_INT=$MIN_INT
        RESERVED_END_INT=$(( MIN_INT + RESERVED_IP_COUNT - 1 ))
        if [ "$RESERVED_END_INT" -gt "$MAX_INT" ]; then
            RESERVED_END_INT=$MAX_INT
        fi
        log "Reserved range: $(int_to_ip $RESERVED_START_INT) - $(int_to_ip $RESERVED_END_INT)"
    fi

    local USABLE_MIN_INT=$(( RESERVED_END_INT + 1 ))
    if [ "$USABLE_MIN_INT" -gt "$MAX_INT" ]; then
        log "Error: No assignable IPs after reserving $RESERVED_IP_COUNT IPs"
        return 1
    fi

    # Pick random IP from usable range
    local RANDOM_INT=$(shuf -i "${USABLE_MIN_INT}-${MAX_INT}" -n 1)
    local RANDOM_IP=$(int_to_ip "$RANDOM_INT")

    if [ -n "$RANDOM_IP" ]; then
        echo "$RANDOM_IP"
        return 0
    fi

    return 1
}

# Save persistent state
save_persistent_state() {
    cat > "$PERSISTENT_STATE_FILE" <<- EOF
# Persistent IPv4 state for mesh node
# Last updated: $(date)
PERSISTENT_IPV4="$PERSISTENT_IPV4"
PERSISTENT_NETWORK="$PERSISTENT_NETWORK"
EOF
    chmod 644 "$PERSISTENT_STATE_FILE"
}

# --- Main Logic ---

# Get our MAC address
MY_MAC=$(cat "/sys/class/net/${CONTROL_IFACE}/address" 2>/dev/null || echo "")
if [ -z "$MY_MAC" ]; then
    log "ERROR: Cannot read MAC address from $CONTROL_IFACE"
    exit 1
fi

# Load persistent state
if [ -f "$PERSISTENT_STATE_FILE" ]; then
    source "$PERSISTENT_STATE_FILE" 2>/dev/null
    if [ -n "$PERSISTENT_IPV4" ]; then
        log "Loaded persistent IPv4: ${PERSISTENT_IPV4}"
    fi
fi

# Check if we already have an IP configured
CURRENT_IPV4=$(ip addr show dev "$CONTROL_IFACE" | grep -oP 'inet \K[\d.]+' | head -1)
if [ -n "$CURRENT_IPV4" ]; then
    IPV4_STATE="CONFIGURED"
    log "Current IPv4: ${CURRENT_IPV4}"
fi

# Load claimed IPs from registry
if [ -f "$CLAIMED_IPS_FILE" ]; then
    mapfile -t CLAIMED_IPS < "$CLAIMED_IPS_FILE"
else
    CLAIMED_IPS=()
    log "Warning: Claimed IPs file not found"
fi

# --- State Machine ---
case $IPV4_STATE in
    "UNCONFIGURED")
        PROPOSED_IPV4=""
        SHOULD_USE_PERSISTENT=false

        # Check if we have a persistent IP and if network has changed
        if [ -n "$PERSISTENT_IPV4" ]; then
            # Check if network changed
            if [ -n "$PERSISTENT_NETWORK" ] && [ "$PERSISTENT_NETWORK" != "$IPV4_NETWORK" ]; then
                log "Network changed from ${PERSISTENT_NETWORK} to ${IPV4_NETWORK}. Selecting new IP."
                PERSISTENT_IPV4=""
                PERSISTENT_NETWORK=""
                save_persistent_state
            else
                # Verify persistent IP is in current network
                if ip_in_cidr "$PERSISTENT_IPV4" "$IPV4_NETWORK"; then
                    PERSISTENT_IPV4_INT=$(ip_to_int "$PERSISTENT_IPV4")

                    if [ -n "$PERSISTENT_IPV4_INT" ]; then
                        # Calculate reserved range if needed
                        if [ "$RESERVED_START_INT" -eq 0 ]; then
                            get_random_ip_from_cidr "${IPV4_NETWORK}" > /dev/null
                        fi

                        # Check if in reserved range
                        if [ "$RESERVED_START_INT" -ne 0 ] && \
                           [ "$PERSISTENT_IPV4_INT" -ge "$RESERVED_START_INT" ] && \
                           [ "$PERSISTENT_IPV4_INT" -le "$RESERVED_END_INT" ]; then
                            log "Persistent IP ${PERSISTENT_IPV4} is in reserved range. Selecting new IP."
                            PERSISTENT_IPV4=""
                            save_persistent_state
                        else
                            log "Attempting to reclaim previous IP: ${PERSISTENT_IPV4}"
                            PROPOSED_IPV4="$PERSISTENT_IPV4"
                            SHOULD_USE_PERSISTENT=true
                        fi
                    else
                        log "Failed to convert persistent IP. Selecting new IP."
                        PERSISTENT_IPV4=""
                        save_persistent_state
                    fi
                else
                    log "Persistent IP ${PERSISTENT_IPV4} not in network ${IPV4_NETWORK}. Selecting new IP."
                    PERSISTENT_IPV4=""
                    save_persistent_state
                fi
            fi
        fi

        # Generate new IP if needed
        if [ -z "$PROPOSED_IPV4" ]; then
            log "Selecting new IP from ${IPV4_NETWORK}..."
            PROPOSED_IPV4=$(get_random_ip_from_cidr "${IPV4_NETWORK}")
        fi

        if [ -z "$PROPOSED_IPV4" ]; then
            log "Failed to generate IP"
            exit 1
        fi

        # Check for conflicts
        CONFLICT=false
        for entry in "${CLAIMED_IPS[@]}"; do
            if [[ "${entry%%,*}" == "$PROPOSED_IPV4" ]]; then
                CONFLICT=true
                break
            fi
        done

        if [ "$CONFLICT" = true ]; then
            if [ "$SHOULD_USE_PERSISTENT" = true ]; then
                log "Previous IP ${PROPOSED_IPV4} is now in use. Will select new IP next cycle."
                PERSISTENT_IPV4=""
                save_persistent_state
            else
                log "Proposed IP ${PROPOSED_IPV4} is in use. Will retry next cycle."
            fi
        else
            log "Claiming ${PROPOSED_IPV4}..."
            ip addr add "${PROPOSED_IPV4}/${IPV4_NETWORK#*/}" dev "$CONTROL_IFACE"
            PERSISTENT_IPV4="$PROPOSED_IPV4"
            PERSISTENT_NETWORK="$IPV4_NETWORK"
            save_persistent_state
            log "Successfully claimed ${PROPOSED_IPV4}"
        fi
        ;;

    "CONFIGURED")
        # Check for conflicts
        CONFLICTING_MAC=""
        for entry in "${CLAIMED_IPS[@]}"; do
            if [[ "${entry%%,*}" == "$CURRENT_IPV4" && "${entry##*,}" != "$MY_MAC" ]]; then
                CONFLICTING_MAC="${entry##*,}"
                break
            fi
        done

        if [[ -n "$CONFLICTING_MAC" ]]; then
            log "CONFLICT DETECTED for ${CURRENT_IPV4}! Conflicting MAC: ${CONFLICTING_MAC}"

            # Tie-breaker: higher MAC wins
            if [[ "$MY_MAC" > "$CONFLICTING_MAC" ]]; then
                log "Won tie-breaker. Defending IP."
            else
                log "Lost tie-breaker. Releasing IP."
                ip addr del "${CURRENT_IPV4}/${IPV4_NETWORK#*/}" dev "$CONTROL_IFACE" 2>/dev/null
                PERSISTENT_IPV4=""
                PERSISTENT_NETWORK=""
                save_persistent_state
            fi
        fi
        ;;
esac

exit 0
