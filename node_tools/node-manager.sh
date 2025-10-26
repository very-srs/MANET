#!/bin/bash

# ==============================================================================
#  Mesh Node Manager
# ==============================================================================
# ... (script description remains the same) ...
# 5. Manages decentralized IPv4, reserving the first few IPs for services.
# ==============================================================================

# --- Parse command line arguments ---
# ... (update mode logic remains the same) ...

# --- Normal operation mode continues below ---

# Source the configuration file if it exists
if [ -f /etc/mesh_ipv4.conf ]; then
    source /etc/mesh_ipv4.conf
fi

# Set defaults
IPV4_NETWORK=${IPV4_NETWORK:-"10.30.1.0/24"}

### --- Configuration ---
CONTROL_IFACE="br0"
ALFRED_DATA_TYPE=68
MY_MAC=$(cat "/sys/class/net/${CONTROL_IFACE}/address")
DEFENSE_INTERVAL=300 # Republish status every 5 minutes
MONITOR_INTERVAL=20  # How often the main loop runs (seconds)
REGISTRY_STATE_FILE="/var/run/mesh_node_registry" # Global state file
PERSISTENT_STATE_FILE="/etc/mesh_ipv4_state"      # Local persistent state
BATCTL_PATH="/usr/sbin/batctl" # Explicit path

# --- NEW: Configuration for Reserved Service IPs ---
RESERVED_IP_COUNT=5 # How many IPs to reserve for services (e.g., mediaMTX, Mumble)
RESERVED_START_INT=0 # Will be calculated later based on network
RESERVED_END_INT=0   # Will be calculated later

### --- State Variables ---
# ... (state variables remain the same) ...

# --- Helper Functions ---
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - NODE-MGR: $1"
}

# --- UPDATED: get_random_ip_from_cidr with Reservation ---
get_random_ip_from_cidr() {
    local CIDR="$1";
    # --- Helper functions for IP integer conversion ---
    ip_to_int() { local a b c d; IFS=. read -r a b c d <<<"$1"; echo "$(( (a << 24) + (b << 16) + (c << 8) + d ))"; }
    int_to_ip() { local ip_int=$1; echo "$(( (ip_int >> 24) & 255 )).$(( (ip_int >> 16) & 255 )).$(( (ip_int >> 8) & 255 )).$(( ip_int & 255 ))"; }

    # --- Main Logic ---
    local CALC_OUTPUT; CALC_OUTPUT=$(ipcalc "$CIDR" 2>/dev/null);
    if [ -z "$CALC_OUTPUT" ]; then echo "Error: Invalid CIDR or ipcalc not found: $CIDR" >&2; return 1; fi
    local HOST_MIN=$(echo "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')
    local HOST_MAX=$(echo "$CALC_OUTPUT" | awk '/HostMax/ {print $2}')
    if [ -z "$HOST_MIN" ] || [ -z "$HOST_MAX" ]; then echo "Error: ipcalc parsing failed." >&2; return 1; fi
    local MIN_INT=$(ip_to_int "$HOST_MIN")
    local MAX_INT=$(ip_to_int "$HOST_MAX")

    # Calculate the reserved range (only once, if not already set)
    if [ "$RESERVED_START_INT" -eq 0 ]; then
        RESERVED_START_INT=$MIN_INT
        RESERVED_END_INT=$(( MIN_INT + RESERVED_IP_COUNT - 1 ))
        # Sanity check: ensure reserved range doesn't exceed the network's max
        if [ "$RESERVED_END_INT" -gt "$MAX_INT" ]; then
            RESERVED_END_INT=$MAX_INT
            log "Warning: Reserved IP count exceeds available hosts. Reserving up to $HOST_MAX"
        fi
        log "Calculated reserved range: $(int_to_ip $RESERVED_START_INT) - $(int_to_ip $RESERVED_END_INT)"
    fi

    # Define the first usable IP *after* the reserved block
    local USABLE_MIN_INT=$(( RESERVED_END_INT + 1 ))

    # Check if there are any non-reserved IPs available
    if [ "$USABLE_MIN_INT" -gt "$MAX_INT" ]; then
        echo "Error: No assignable dynamic IPs available after reserving $RESERVED_IP_COUNT IPs." >&2
        return 1
    fi
    # Handle single available dynamic IP case
    if [ "$USABLE_MIN_INT" -eq "$MAX_INT" ]; then
        int_to_ip "$USABLE_MIN_INT"
        return 0
    fi

    # Pick a random integer from the *usable* range
    local RANDOM_INT=$(shuf -i "${USABLE_MIN_INT}-${MAX_INT}" -n 1)
    int_to_ip "$RANDOM_INT"
}

save_persistent_state() {
    # ... (save_persistent_state remains the same) ...
}

# ==============================================================================
#  Main Logic
# ==============================================================================

log "Starting Mesh Node Manager for ${MY_MAC}."

# Load persistent state if it exists
# ... (loading persistent state remains the same) ...

ip -4 addr flush dev "$CONTROL_IFACE"
log "Initial IPv4 address flush on ${CONTROL_IFACE}."

### --- Main Loop ---
while true; do

    # Reload persistent state
    # ... (reloading persistent state remains the same) ...

    # --- 1. GATHER LOCAL METRICS ---
    # ... (metric gathering remains the same) ...

    # --- 2. ENCODE & PUBLISH OWN STATUS ---
    # ... (encoding and publishing remain the same) ...

    # --- 3. DISCOVER PEERS & BUILD GLOBAL REGISTRY ---
    # ... (peer discovery and registry building remain the same) ...

    # --- 4. MANAGE IPV4 ADDRESS (Using the registry file and reserved range) ---
    # ... (logic inside the case statement remains the same,
    #      as get_random_ip_from_cidr now automatically excludes reserved IPs) ...

    case $IPV4_STATE in
        "UNCONFIGURED")
            # First, try to reuse persistent IP if we have one AND it's not reserved
            if [ -n "$PERSISTENT_IPV4" ]; then
                PERSISTENT_IPV4_INT=$(ip_to_int "$PERSISTENT_IPV4")
                # Calculate reserved range here if not done yet by get_random_ip_from_cidr
                if [ "$RESERVED_START_INT" -eq 0 ]; then
                    # Need to run ipcalc once to get MIN_INT for reservation calculation
                    TEMP_IP=$(get_random_ip_from_cidr "${IPV4_NETWORK}")
                    if [ -z "$TEMP_IP" ]; then
                       log "Cannot determine network range. Skipping persistent IP check."
                       PERSISTENT_IPV4="" # Clear it to force new random IP later
                    fi
                fi
                # Check if persistent IP falls within reserved range
                if [ "$PERSISTENT_IPV4_INT" -ge "$RESERVED_START_INT" ] && [ "$PERSISTENT_IPV4_INT" -le "$RESERVED_END_INT" ]; then
                    log "Persistent IP ${PERSISTENT_IPV4} is within the reserved range. Ignoring."
                    PERSISTENT_IPV4="" # Clear it to force new random IP
                    save_persistent_state
                    PROPOSED_IPV4="" # Ensure we generate a new random one below
                else
                    log "State: UNCONFIGURED. Attempting to reclaim previous IP: ${PERSISTENT_IPV4}..."
                    PROPOSED_IPV4="$PERSISTENT_IPV4"
                fi
            fi

            # If we don't have a valid persistent IP proposal, generate a random one
            if [ -z "$PROPOSED_IPV4" ]; then
                log "State: UNCONFIGURED. Proposing new IP from ${IPV4_NETWORK} (excluding reserved)..."
                PROPOSED_IPV4=$(get_random_ip_from_cidr "${IPV4_NETWORK}")
            fi

            # Check if IP generation failed (e.g., no usable IPs left)
            if [ -z "$PROPOSED_IPV4" ]; then
                log "Failed to generate IP. Retrying."
                sleep 5
                continue
            fi

            CONFLICT=false
            for entry in "${CLAIMED_IPS[@]}"; do
                if [[ "${entry%%,*}" == "$PROPOSED_IPV4" ]]; then
                    CONFLICT=true
                    break
                fi
            done

            if [ "$CONFLICT" = true ]; then
                if [ "$PROPOSED_IPV4" = "$PERSISTENT_IPV4" ]; then
                    log "Previous IP ${PROPOSED_IPV4} is now in use. Selecting new IP."
                    PERSISTENT_IPV4=""
                    save_persistent_state
                else
                    log "Proposed IP ${PROPOSED_IPV4} is in use. Retrying."
                fi
            else
                log "Claiming ${PROPOSED_IPV4}..."
                ip addr add "${PROPOSED_IPV4}/${IPV4_NETWORK#*/}" dev "$CONTROL_IFACE"
                CURRENT_IPV4="$PROPOSED_IPV4"
                PERSISTENT_IPV4="$PROPOSED_IPV4"
                IPV4_STATE="CONFIGURED"
                save_persistent_state
                LAST_PUBLISH_TIME=0 # Trigger immediate publish
                log "Successfully claimed and persisted ${CURRENT_IPV4}"
            fi
            ;;

        "CONFIGURED")
            # ... (conflict detection logic remains the same) ...
            ;;
    esac


    # --- 5: TRIGGER ELECTION SCRIPTS ---
    # ... (triggering election scripts remains the same) ...

    # --- Wait for next cycle ---
    sleep "$MONITOR_INTERVAL"
done

log "mesh-node-manager loop exited unexpectedly. Restarting..."
exit 1
