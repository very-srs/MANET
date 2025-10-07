#!/bin/bash

# ==============================================================================
#  Decentralized IPv4 Address Manager using B.A.T.M.A.N. alfred
# ==============================================================================

### --- Configuration ---
IPV4_OCTET2=$(shuf -i 0-255 -n 1)
#IPV4_SUBNET="10.${IPV4_OCTET2}"
IPV4_SUBNET="10.30.1"
IPV4_MASK="/24"
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

### --- Helper Functions ---
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
    # We parse the "MAC: PAYLOAD" output to get just the "IP,MAC" payload.
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

            # IP is free, claim it immediately. The "PROPOSING" state is no
            # longer needed because alfred gives us a consistent snapshot
            # of the network, which avoids the race condition.
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
