#!/bin/bash

# ==============================================================================
#  Mesh Node Manager
# ==============================================================================
# This script runs as a persistent service to:
# 1. Gather various node metrics (hostname, TQ, gateway status, etc.).
# 2. Use encoder.py to create a Protobuf message.
# 3. Announce this status to the mesh via alfred, but only on change or
#    periodically to keep the data fresh.
# 4. Manage a decentralized, conflict-free IPv4 address for this node.
# ==============================================================================

# Source the configuration file if it exists, from radio-setup.sh
if [ -f /etc/mesh_ipv4.conf ]; then
    source /etc/mesh_ipv4.conf
fi

# Set defaults in case the config file doesn't exist
IPV4_NETWORK=${IPV4_NETWORK:-"10.30.1.0/24"}

### --- Configuration ---
CONTROL_IFACE="br0"
ALFRED_DATA_TYPE=68 # Data type for the full NodeInfo protobuf message
MY_MAC=$(cat "/sys/class/net/${CONTROL_IFACE}/address")
DEFENSE_INTERVAL=300 # How often to re-publish status to prevent timeout (5 minutes)
MONITOR_INTERVAL=20   # How often to wake up and check for local changes

### --- State Variables ---
IPV4_STATE="UNCONFIGURED"
CURRENT_IPV4=""
LAST_PUBLISHED_PAYLOAD=""
LAST_PUBLISH_TIME=0

# --- Helper Functions ---
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - $1"
}

get_random_ip_from_cidr() {
    local CIDR="$1"
    ip_to_int() {
        local a b c d; IFS=. read -r a b c d <<<"$1"; echo "$(( (a << 24) + (b << 16) + (c << 8) + d ))"
    }
    int_to_ip() {
        local ip_int=$1; echo "$(( (ip_int >> 24) & 255 )).$(( (ip_int >> 16) & 255 )).$(( (ip_int >> 8) & 255 )).$(( ip_int & 255 ))"
    }
    local CALC_OUTPUT; CALC_OUTPUT=$(ipcalc "$CIDR" 2>/dev/null)
    if [ -z "$CALC_OUTPUT" ]; then echo "Error: Invalid CIDR: $CIDR" >&2; return 1; fi
    local HOST_MIN=$(echo "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')
    local HOST_MAX=$(echo "$CALC_OUTPUT" | awk '/HostMax/ {print $2}')
    if [ -z "$HOST_MIN" ] || [ -z "$HOST_MAX" ]; then echo "Error: ipcalc parsing failed." >&2; return 1; fi
    local MIN_INT=$(ip_to_int "$HOST_MIN")
    local MAX_INT=$(ip_to_int "$HOST_MAX")
    if [ "$MIN_INT" -gt "$MAX_INT" ]; then return 1; elif [ "$MIN_INT" -eq "$MAX_INT" ]; then echo "$HOST_MIN"; return 0; fi
    local RANDOM_INT=$(shuf -i "${MIN_INT}-${MAX_INT}" -n 1)
    int_to_ip "$RANDOM_INT"
}

# ==============================================================================
#  Main Logic
# ==============================================================================

log "Starting Mesh Node Manager for ${MY_MAC}."

# Clear any old IPv4 addresses on startup
ip -4 addr flush dev "$CONTROL_IFACE"
log "Initial IPv4 address flush on ${CONTROL_IFACE}."

### --- Main Loop ---
# This loop runs periodically to check for changes. 
while true; do

    # --- PUBLISH STATUS ---
    # Gather all current local metrics
    HOSTNAME=$(hostname)
    SYNCTHING_ID=$(runuser -u radio -- syncthing --device-id 2>/dev/null || echo "")
	# get the average TQ of the seen nodes
    TQ_AVG=$(batctl o | awk 'NR>1 {sum+=$3} END {if (NR>1) printf "%.2f", sum/(NR-1); else print 0}')
    IS_GATEWAY_FLAG=""
    [ -f /var/run/mesh-gateway.state ] && IS_GATEWAY_FLAG="--is-internet-gateway"
    IS_NTP_FLAG=""
    # check for virtual IP of the ntp server being on this node
    ip addr show dev br0 | grep -q "fd5a:1753:4340:1::123" && IS_NTP_FLAG="--is-ntp-server"

    # Build the arguments for the encoder
    ENCODER_ARGS=( "--hostname" "$HOSTNAME" "--mac-address" "$MY_MAC" "--tq-average" "$TQ_AVG" "--syncthing-id" "$SYNCTHING_ID" )
    [ "$IPV4_STATE" == "CONFIGURED" ] && ENCODER_ARGS+=("--ipv4-address" "$CURRENT_IPV4")
    [ -n "$IS_GATEWAY_FLAG" ] && ENCODER_ARGS+=("$IS_GATEWAY_FLAG")
    [ -n "$IS_NTP_FLAG" ] && ENCODER_ARGS+=("$IS_NTP_FLAG")

    # Generate the current payload
    CURRENT_PAYLOAD=$(/usr/local/bin/encoder.py "${ENCODER_ARGS[@]}")

    # Check if we need to publish
    time_since_publish=$(( $(date +%s) - LAST_PUBLISH_TIME ))
    if [[ "$CURRENT_PAYLOAD" != "$LAST_PUBLISHED_PAYLOAD" || $time_since_publish -gt $DEFENSE_INTERVAL ]]; then
        log "Change detected or defense timer expired. Publishing status..."
        if [ -n "$CURRENT_PAYLOAD" ]; then
            echo -n "$CURRENT_PAYLOAD" | alfred -s $ALFRED_DATA_TYPE
            LAST_PUBLISHED_PAYLOAD="$CURRENT_PAYLOAD"
            LAST_PUBLISH_TIME=$(date +%s)
        fi
    fi

    # --- 2. DISCOVER PEERS & MANAGE IPV4 ---
    mapfile -t PEER_PAYLOADS < <(alfred -r $ALFRED_DATA_TYPE | awk '{print $2}')

    CLAIMED_IPS=()
    for B64_PAYLOAD in "${PEER_PAYLOADS[@]}"; do
        (
            eval "$(/usr/local/bin/decoder.py "$B64_PAYLOAD" 2>/dev/null)"
            if [[ -n "$IPV4_ADDRESS" && -n "$MAC_ADDRESS" ]]; then
                echo "${IPV4_ADDRESS},${MAC_ADDRESS}"
            fi
        )
    done > /tmp/claimed_ips.txt

    mapfile -t CLAIMED_IPS < /tmp/claimed_ips.txt

    case $IPV4_STATE in
        "UNCONFIGURED")
            log "State: UNCONFIGURED. Proposing a new IP from ${IPV4_NETWORK}..."
            PROPOSED_IPV4=$(get_random_ip_from_cidr "${IPV4_NETWORK}")

            CONFLICT=false
            for entry in "${CLAIMED_IPS[@]}"; do
                if [[ "${entry%%,*}" == "$PROPOSED_IPV4" ]]; then
                    CONFLICT=true
                    break
                fi
            done

            if [ "$CONFLICT" = true ]; then
                log "Proposed IP ${PROPOSED_IPV4} is already in use. Retrying."
                sleep 0.5
            else
                log "Claiming ${PROPOSED_IPV4}..."
                ip addr add "${PROPOSED_IPV4}/${IPV4_NETWORK#*/}" dev "$CONTROL_IFACE"
                CURRENT_IPV4="$PROPOSED_IPV4"
                IPV4_STATE="CONFIGURED"
                log "State -> CONFIGURED with ${CURRENT_IPV4}"
            fi
            ;;

        "CONFIGURED")
            CONFLICTING_MAC=""
            for entry in "${CLAIMED_IPS[@]}"; do
                if [[ "${entry%%,*}" == "$CURRENT_IPV4" && "${entry##*,}" != "$MY_MAC" ]]; then
                    CONFLICTING_MAC="${entry##*,}"
                    break
                fi
            done

            if [[ -n "$CONFLICTING_MAC" ]]; then
                log "CONFLICT DETECTED for ${CURRENT_IPV4}! Owner: ${CONFLICTING_MAC}"
                if [[ "$MY_MAC" > "$CONFLICTING_MAC" ]]; then
                    log "Lost tie-breaker to ${CONFLICTING_MAC}. Releasing IP."
                    ip addr del "${CURRENT_IPV4}/${IPV4_NETWORK#*/}" dev "$CONTROL_IFACE"
                    CURRENT_IPV4=""
                    IPV4_STATE="UNCONFIGURED"
                else
                    log "Won tie-breaker against ${CONFLICTING_MAC}. Defending IP."
                fi
            fi
            ;;
    esac

    sleep $MONITOR_INTERVAL
done
