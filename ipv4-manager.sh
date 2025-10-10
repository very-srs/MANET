#!/bin/bash

# ==============================================================================
#  Decentralized IPv4 Address Manager using B.A.T.M.A.N. alfred
# ==============================================================================
#
#  This script is for auto assigning IPv4 addresses to the bridge.  It picks up
#  the address range from a config variable, set on the config server, and saved
#  in /ec/mesh_ipv4.conf.  It listens to address gossip on the Alfred tool, and
#  keeps a log of which IPv4 addresses it has heard about.  The script selects a
#  random IP within the configured CIDR block then checks to see if it has
#  already heard about it from the Alfred tool.  If not, it announces this
#  address via Alfred and assigns the address to br0
#

# Source the configuration file if it exists
if [ -f /etc/mesh_ipv4.conf ]; then
    source /etc/mesh_ipv4.conf
fi

# Set defaults in case the config file doesn't exist
IPV4_SUBNET=${IPV4_SUBNET:-"10.30.1.0"}
IPV4_MASK=${IPV4_MASK:-"/24"}


### --- Configuration ---
CONTROL_IFACE="br0"
REGISTRY_FILE="/tmp/ipv4_registry.txt"
ALFRED_DATA_TYPE=64
MY_MAC=$(cat "/sys/class/net/${CONTROL_IFACE}/address")

### --- State Variables ---
IPV4_STATE="UNCONFIGURED"
CURRENT_IPV4=""
LAST_DEFENSE_TIME=$(date +%s)

#Remove the existing IP at start
ip -4 addr flush dev br0

# --- Helper Functions ---

# This one takes in a cidr notation network and outputs a random ip in it
get_random_ip_from_cidr() {
    local CIDR="$1"

    ip_to_int() {
        local a b c d
        IFS=. read -r a b c d <<<"$1"
        echo "$(( (a << 24) + (b << 16) + (c << 8) + d ))"
    }

    int_to_ip() {
        local ip_int=$1
        echo "$(( (ip_int >> 24) & 255 )).$(( (ip_int >> 16) & 255 )).$(( (ip_int >> 8) & 255 )).$(( ip_int & 255 ))"
    }

    # --- Main Logic ---
    # Use ipcalc to get the valid host range
    local CALC_OUTPUT
    CALC_OUTPUT=$(ipcalc "$CIDR" 2>/dev/null)
    if [ -z "$CALC_OUTPUT" ]; then
        echo "Error: Invalid CIDR notation provided: $CIDR" >&2
        return 1
    fi

    local HOST_MIN=$(echo "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')
    local HOST_MAX=$(echo "$CALC_OUTPUT" | awk '/HostMax/ {print $2}')

    # Convert the range to integers
    local MIN_INT=$(ip_to_int "$HOST_MIN")
    local MAX_INT=$(ip_to_int "$HOST_MAX")

    # Handle single-host networks (/31) or smaller, though unlikely
    if [ "$MIN_INT" -ge "$MAX_INT" ]; then
        echo "$HOST_MIN"
        return 0
    fi

    # Pick a random integer within the valid range
    local RANDOM_INT=$(shuf -i "${MIN_INT}-${MAX_INT}" -n 1)

    # Convert the random integer back to an IP and print it
    int_to_ip "$RANDOM_INT"
}


publish_claim() {
    local ip_to_claim=$1
    local payload="${ip_to_claim},${MY_MAC}"
    # Use alfred to set our data for the mesh to see
    echo "$payload" | alfred -s $ALFRED_DATA_TYPE
    log "Published claim for ${ip_to_claim} via alfred"
}

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - $1"
}

# ==============================================================================
#  Main Logic
# ==============================================================================

log "Starting IPv4 Manager for ${MY_MAC} using alfred."
log "State is UNCONFIGURED."

### --- Main State Machine Loop ---
while true; do

    # Get the latest registry data from the mesh using alfred.
    # Parse the "MAC: PAYLOAD" output to get just the "IP,MAC" payload.
    # alfred handles data timeouts internally, so no pruning is needed.
    alfred -r $ALFRED_DATA_TYPE | awk '{print $2}' > "$REGISTRY_FILE"

    case $IPV4_STATE in

        "UNCONFIGURED")
            log "State: UNCONFIGURED. Proposing a new IP."
            PROPOSED_IPV4="${IPV4_SUBNET}.$(shuf -i 1-254 -n 1)"

            if grep -q "^${PROPOSED_IPV4}," "$REGISTRY_FILE"; then
                log "Proposed IP ${PROPOSED_IPV4} is already in use. Retrying."
                sleep 1
                continue
            fi

            # IP is free, claim it immediately.
            log "Claiming ${PROPOSED_IPV4}..."
            ip addr add "${PROPOSED_IPV4}${IPV4_MASK}" dev "$CONTROL_IFACE"
            publish_claim "$PROPOSED_IPV4"

            CURRENT_IPV4="$PROPOSED_IPV4"
            IPV4_STATE="CONFIGURED"
            LAST_DEFENSE_TIME=$(date +%s)
            log "State -> CONFIGURED with ${CURRENT_IPV4}"
            ;;

        "CONFIGURED")
            # Scan for conflicts
            CONFLICTING_MAC=$(grep "^${CURRENT_IPV4}," "$REGISTRY_FILE" | grep -v ",${MY_MAC}$" | cut -d',' -f2)

            if [[ -n "$CONFLICTING_MAC" ]]; then
                log "CONFLICT DETECTED for ${CURRENT_IPV4}! Owner: ${CONFLICTING_MAC}"

                # Apply tie-breaker
                if [[ "$MY_MAC" > "$CONFLICTING_MAC" ]]; then
                    log "Lost tie-breaker to ${CONFLICTING_MAC}. Releasing IP."
                    ip addr del "${CURRENT_IPV4}${IPV4_MASK}" dev "$CONTROL_IFACE"
                    CURRENT_IPV4=""
                    IPV4_STATE="UNCONFIGURED"
                    log "State -> UNCONFIGURED"
                else
                    log "Won tie-breaker against ${CONFLICTING_MAC}. Defending IP."
                    publish_claim "$CURRENT_IPV4" # Re-assert our claim
                    LAST_DEFENSE_TIME=$(date +%s)
                fi
            fi

            # Periodically defend the IP (every 2 minutes)
            time_since_defense=$(( $(date +%s) - LAST_DEFENSE_TIME ))
            if (( time_since_defense > 120 )); then
                log "Defending claim for ${CURRENT_IPV4}"
                publish_claim "$CURRENT_IPV4"
                LAST_DEFENSE_TIME=$(date +%s)
            fi
            ;;
    esac

    # Main loop polls the network every 15 seconds
    sleep 15
done
