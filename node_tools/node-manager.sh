#!/bin/bash

# ==============================================================================
#  Mesh Node Manager
# ==============================================================================
# This script runs as a persistent service to:
# 1. Gather various node metrics (hostname, TQ, gateway status, etc.).
# 2. Use encoder.py to create a Protobuf message.
# 3. Announce this status to the mesh via alfred.
# 4. Decode all peer statuses and write them to the global state file
#    at /var/run/mesh_node_registry for other services to use.
# 5. Manage a decentralized, conflict-free IPv4 address for this node,
#    using the registry file for conflict detection.
# ==============================================================================

# --- Parse command line arguments ---
UPDATE_MODE=false
if [[ "$1" == "--update-protobuf" || "$1" == "-u" ]]; then
    UPDATE_MODE=true
fi

# --- Handle update mode ---
if [ "$UPDATE_MODE" = true ]; then
    # Read protobuf variable assignments from STDIN
    # Expected format: VARIABLE=value (one per line or semicolon-separated)
    PROTOBUF_VARS=$(cat)

    if [ -z "$PROTOBUF_VARS" ]; then
        echo "Error: No protobuf variables provided on STDIN" >&2
        echo "Usage: echo 'VARIABLE=value' | $0 --update-protobuf" >&2
        exit 1
    fi

    # Load the persistent state file
    PERSISTENT_STATE_FILE="/etc/mesh_ipv4_state"
    PERSISTENT_IPV4=""
    if [ -f "$PERSISTENT_STATE_FILE" ]; then
        source "$PERSISTENT_STATE_FILE"
    fi

    # Update the PROTOBUF_OVERRIDE variable
    cat > "$PERSISTENT_STATE_FILE" << EOF
# Persistent state for mesh node manager
# Last updated: $(date)
PERSISTENT_IPV4="$PERSISTENT_IPV4"
PROTOBUF_OVERRIDE='$PROTOBUF_VARS'
LAST_UPDATE=$(date +%s)
EOF
    chmod 644 "$PERSISTENT_STATE_FILE"

    echo "Protobuf variables updated successfully."
    echo "The mesh-node-manager service will use these overrides on next cycle."
    exit 0
fi

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
PERSISTENT_STATE_FILE="/etc/mesh_ipv4_state"  # Local persistent state
BATCTL_PATH="/usr/sbin/batctl" # Explicit path

### --- State Variables ---
IPV4_STATE="UNCONFIGURED"
CURRENT_IPV4=""
LAST_PUBLISHED_PAYLOAD=""
LAST_PUBLISH_TIME=0
PERSISTENT_IPV4=""
PROTOBUF_OVERRIDE=""

# --- Helper Functions ---
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - NODE-MGR: $1"
}

get_random_ip_from_cidr() {
    local CIDR="$1"; ip_to_int() { local a b c d; IFS=. read -r a b c d <<<"$1"; echo "$(( (a << 24) + (b << 16) + (c << 8) + d ))"; }; int_to_ip() { local ip_int=$1; echo "$(( (ip_int >> 24) & 255 )).$(( (ip_int >> 16) & 255 )).$(( (ip_int >> 8) & 255 )).$(( ip_int & 255 ))"; }; local CALC_OUTPUT; CALC_OUTPUT=$(ipcalc "$CIDR" 2>/dev/null); if [ -z "$CALC_OUTPUT" ]; then echo "Error: Invalid CIDR: $CIDR" >&2; return 1; fi; local HOST_MIN=$(echo "$CALC_OUTPUT" | awk '/HostMin/ {print $2}'); local HOST_MAX=$(echo "$CALC_OUTPUT" | awk '/HostMax/ {print $2}'); if [ -z "$HOST_MIN" ] || [ -z "$HOST_MAX" ]; then echo "Error: ipcalc parsing failed." >&2; return 1; fi; local MIN_INT=$(ip_to_int "$HOST_MIN"); local MAX_INT=$(ip_to_int "$HOST_MAX"); if [ "$MIN_INT" -gt "$MAX_INT" ]; then return 1; elif [ "$MIN_INT" -eq "$MAX_INT" ]; then echo "$HOST_MIN"; return 0; fi; local RANDOM_INT=$(shuf -i "${MIN_INT}-${MAX_INT}" -n 1); int_to_ip "$RANDOM_INT";
}

save_persistent_state() {
    cat > "$PERSISTENT_STATE_FILE" << EOF
# Persistent state for mesh node manager
# Last updated: $(date)
PERSISTENT_IPV4="$PERSISTENT_IPV4"
PROTOBUF_OVERRIDE='$PROTOBUF_OVERRIDE'
LAST_UPDATE=$(date +%s)
EOF
    chmod 644 "$PERSISTENT_STATE_FILE"
}

# ==============================================================================
#  Main Logic
# ==============================================================================

log "Starting Mesh Node Manager for ${MY_MAC}."

# Load persistent state if it exists
if [ -f "$PERSISTENT_STATE_FILE" ]; then
    source "$PERSISTENT_STATE_FILE"
    if [ -n "$PERSISTENT_IPV4" ]; then
        log "Loaded persistent IPv4: ${PERSISTENT_IPV4}"
    fi
fi

ip -4 addr flush dev "$CONTROL_IFACE"
log "Initial IPv4 address flush on ${CONTROL_IFACE}."

### --- Main Loop ---
while true; do

    # Reload persistent state to pick up updates from --update-protobuf
    if [ -f "$PERSISTENT_STATE_FILE" ]; then
        source "$PERSISTENT_STATE_FILE"
    fi

    # --- 1. GATHER LOCAL METRICS ---
    HOSTNAME=$(hostname)
    SYNCTHING_ID=$(runuser -u radio -- syncthing --device-id 2>/dev/null || echo "")
    TQ_AVG=$("$BATCTL_PATH" o | awk 'NR>1 {sum+=$3} END {if (NR>1) printf "%.2f", sum/(NR-1); else print 0}')
    IS_GATEWAY_FLAG=""
    [ -f /var/run/mesh-gateway.state ] && IS_GATEWAY_FLAG="--is-internet-gateway"
    IS_NTP_FLAG=""
    # Check if chrony server config is active AND service is running
    if systemctl is-active --quiet chrony.service && grep -q "allow fd5a" /etc/chrony/chrony.conf; then
         IS_NTP_FLAG="--is-ntp-server"
    fi

    # --- 2. ENCODE & PUBLISH OWN STATUS ---
    ENCODER_ARGS=( "--hostname" "$HOSTNAME" "--mac-address" "$MY_MAC" "--tq-average" "$TQ_AVG" "--syncthing-id" "$SYNCTHING_ID" )
    [ "$IPV4_STATE" == "CONFIGURED" ] && ENCODER_ARGS+=("--ipv4-address" "$CURRENT_IPV4")
    [ -n "$IS_GATEWAY_FLAG" ] && ENCODER_ARGS+=("$IS_GATEWAY_FLAG")
    [ -n "$IS_NTP_FLAG" ] && ENCODER_ARGS+=("$IS_NTP_FLAG")

    # Apply protobuf overrides if present
    # These can add additional encoder arguments dynamically
    if [ -n "$PROTOBUF_OVERRIDE" ]; then
        log "Applying protobuf overrides"
        # Evaluate the override string to set variables
        eval "$PROTOBUF_OVERRIDE"

        # Map protobuf field names to encoder.py flags
        # Based on NodeInfo.proto fields:
        [ -n "$IS_MUMBLE_SERVER" ] && [ "$IS_MUMBLE_SERVER" = "true" ] && ENCODER_ARGS+=("--is-mumble-server")
        [ -n "$IS_TAK_SERVER" ] && [ "$IS_TAK_SERVER" = "true" ] && ENCODER_ARGS+=("--is-tak-server")
        [ -n "$UPTIME_SECONDS" ] && ENCODER_ARGS+=("--uptime-seconds" "$UPTIME_SECONDS")
        [ -n "$BATTERY_PERCENTAGE" ] && ENCODER_ARGS+=("--battery-percentage" "$BATTERY_PERCENTAGE")
        [ -n "$CPU_LOAD_AVERAGE" ] && ENCODER_ARGS+=("--cpu-load-average" "$CPU_LOAD_AVERAGE")
        [ -n "$GPS_LATITUDE" ] && [ -n "$GPS_LONGITUDE" ] && ENCODER_ARGS+=("--gps-latitude" "$GPS_LATITUDE" "--gps-longitude" "$GPS_LONGITUDE")
        [ -n "$GPS_ALTITUDE" ] && ENCODER_ARGS+=("--gps-altitude" "$GPS_ALTITUDE")
        [ -n "$ATAK_USER" ] && ENCODER_ARGS+=("--atak-user" "$ATAK_USER")
    fi

    CURRENT_PAYLOAD=$(/usr/local/bin/encoder.py "${ENCODER_ARGS[@]}")

    time_since_publish=$(( $(date +%s) - LAST_PUBLISH_TIME ))
    if [[ "$CURRENT_PAYLOAD" != "$LAST_PUBLISHED_PAYLOAD" || $time_since_publish -gt $DEFENSE_INTERVAL ]]; then
        log "Change detected or timer expired. Publishing status..."
        if [ -n "$CURRENT_PAYLOAD" ]; then
            echo -n "$CURRENT_PAYLOAD" | alfred -s $ALFRED_DATA_TYPE
            LAST_PUBLISHED_PAYLOAD="$CURRENT_PAYLOAD"
            LAST_PUBLISH_TIME=$(date +%s)
        else
             log "Error: Encoder produced empty payload."
        fi
    fi

    # --- 3. DISCOVER PEERS & BUILD GLOBAL REGISTRY ---
    mapfile -t PEER_PAYLOADS < <(alfred -r $ALFRED_DATA_TYPE | cut -d' ' -f2-)
    REGISTRY_TMP=$(mktemp)

    # Add header to the registry file
    echo "# Mesh Node Registry - Generated $(date)" > "$REGISTRY_TMP"
    echo "# Sourced by other scripts to get network state." >> "$REGISTRY_TMP"
    echo "" >> "$REGISTRY_TMP"

    for B64_PAYLOAD in "${PEER_PAYLOADS[@]}"; do
        TRIMMED_PAYLOAD=$(echo "$B64_PAYLOAD" | xargs)
        if [ -z "$TRIMMED_PAYLOAD" ]; then continue; fi

        DECODED_DATA=$(/usr/local/bin/decoder.py "$TRIMMED_PAYLOAD" 2>/dev/null)
        if [ -n "$DECODED_DATA" ]; then
            # Write specified decoded data to the registry file
            (
                eval "$DECODED_DATA"
                if [[ -n "$MAC_ADDRESS" ]]; then
                    PREFIX="NODE_$(echo "$MAC_ADDRESS" | tr -d ':')"
                    echo "${PREFIX}_HOSTNAME='$HOSTNAME'"
                    echo "${PREFIX}_MAC_ADDRESS='$MAC_ADDRESS'"
                    echo "${PREFIX}_IPV4_ADDRESS='$IPV4_ADDRESS'"
                    echo "${PREFIX}_SYNCTHING_ID='$SYNCTHING_ID'"
                    echo "${PREFIX}_TQ_AVERAGE='$TQ_AVERAGE'"
                    # Map the python bool 'true'/'false' to bash usable strings
                    echo "${PREFIX}_IS_GATEWAY='$IS_INTERNET_GATEWAY'"
                    echo "${PREFIX}_IS_NTP_SERVER='$IS_NTP_SERVER'"
                    echo "${PREFIX}_IS_MUMBLE_SERVER='$IS_MUMBLE_SERVER'"
                    echo "${PREFIX}_IS_TAK_SERVER='$IS_TAK_SERVER'"
                    echo "${PREFIX}_UPTIME_SECONDS='$UPTIME_SECONDS'"
                    echo "${PREFIX}_BATTERY_PERCENTAGE='$BATTERY_PERCENTAGE'"
                    echo "${PREFIX}_CPU_LOAD_AVERAGE='$CPU_LOAD_AVERAGE'"
                    echo "${PREFIX}_ATAK_USER='$ATAK_USER'"
                    # Add echo lines here for any other fields you want in the registry
                    echo ""
                fi
            ) >> "$REGISTRY_TMP"
        fi
    done

    # Replace the old registry with the new one
    mv "$REGISTRY_TMP" "$REGISTRY_STATE_FILE"
    chmod 644 "$REGISTRY_STATE_FILE" # Ensure readable

    # --- 4. MANAGE IPV4 ADDRESS (Using the registry file) ---
    CLAIMED_IPS=()
    if [ -f "$REGISTRY_STATE_FILE" ]; then
         # Source the registry in a subshell to avoid polluting the main script
         # Extract IP,MAC pairs
         CLAIMED_IPS=($(
             source "$REGISTRY_STATE_FILE" > /dev/null 2>&1 # Suppress output
             # List all IPV4_ADDRESS variables, extract IP and MAC prefix
             compgen -A variable | grep 'NODE_.*_IPV4_ADDRESS' | while read varname; do
                 ip_val="${!varname}"
                 mac_prefix="${varname%%_IPV4_ADDRESS}"
                 mac_var="${mac_prefix}_MAC_ADDRESS"
                 mac_val="${!mac_var}"
                 # Output only if both IP and MAC are non-empty
                 if [[ -n "$ip_val" && -n "$mac_val" ]]; then
                     echo "$ip_val,$mac_val"
                 fi
             done | sort # Sort for consistency
         ))
    fi


    case $IPV4_STATE in
        "UNCONFIGURED")
            # First, try to reuse persistent IP if we have one
            if [ -n "$PERSISTENT_IPV4" ]; then
                log "State: UNCONFIGURED. Attempting to reclaim previous IP: ${PERSISTENT_IPV4}..."
                PROPOSED_IPV4="$PERSISTENT_IPV4"
            else
                log "State: UNCONFIGURED. Proposing new IP from ${IPV4_NETWORK}..."
                PROPOSED_IPV4=$(get_random_ip_from_cidr "${IPV4_NETWORK}")
            fi

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
                    PERSISTENT_IPV4=""  # Clear persistent IP, will get new one next iteration
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
                    PERSISTENT_IPV4=""  # Clear persistent IP since we lost it
                    IPV4_STATE="UNCONFIGURED"
                    save_persistent_state
                    LAST_PUBLISH_TIME=0 # Trigger immediate publish
                else
                    log "Won tie-breaker against ${CONFLICTING_MAC}. Defending IP."
                    LAST_PUBLISH_TIME=0 # Re-publish immediately
                fi
            fi
            ;;
    esac

    # --- Wait for next cycle ---
    sleep "$MONITOR_INTERVAL"
done

log "mesh-node-manager loop exited unexpectedly. Restarting..."
exit 1
