#!/bin/bash

# ==============================================================================
#  Mesh Node Manager (Polling Version with IP Reservation)
# ==============================================================================
# This script runs as a persistent service to:
# 1. Periodically gather various node metrics.
# 2. Use encoder.py to create a Protobuf message.
# 3. Announce status via alfred.
# 4. Write decoded peer statuses to /var/run/mesh_node_registry.
# 5. Manage a decentralized IPv4 address, reserving the first few IPs.
# 6. Trigger external election scripts (e.g., mediamtx-election.sh).
# 7. Be used by other scripts to update Alfred data
# ==============================================================================

# --- Parse command line arguments ---
UPDATE_MODE=false
if [[ "$1" == "--update-protobuf" || "$1" == "-u" ]]; then
    UPDATE_MODE=true
fi


# Set defaults
IPV4_NETWORK=${IPV4_NETWORK:-"10.43.1.0/16"}

### --- Configuration ---
CONTROL_IFACE="br0"
ALFRED_DATA_TYPE=68
MY_MAC=$(cat "/sys/class/net/${CONTROL_IFACE}/address")
DEFENSE_INTERVAL=300 # Republish status every 5 minutes
MONITOR_INTERVAL=30  # How often the main loop runs (seconds)
REGISTRY_STATE_FILE="/var/run/mesh_node_registry" # Global state file (tmpfs ok)
PERSISTENT_STATE_FILE="/etc/mesh_ipv4_state"      # Local persistent state (/etc)
BATCTL_PATH="/usr/sbin/batctl" # Explicit path (use compiled if needed /usr/local/sbin)

# Configuration for Reserved Service IPs
RESERVED_IP_COUNT=5 # How many IPs to reserve for services
RESERVED_START_INT=0 # Will be calculated
RESERVED_END_INT=0   # Will be calculated

### --- State Variables ---
IPV4_STATE="UNCONFIGURED"
CURRENT_IPV4=""
LAST_PUBLISHED_PAYLOAD=""
LAST_PUBLISH_TIME=0
PERSISTENT_IPV4=""
PROTOBUF_OVERRIDE=""


#Election services
MEDIAMTX_ELECTION_SCRIPT="/usr/local/bin/mediamtx-election.sh"


# --- Handle update mode ---
if [ "$UPDATE_MODE" = true ]; then
    # Read protobuf variable assignments from STDIN
    PROTOBUF_VARS=$(cat)

    if [ -z "$PROTOBUF_VARS" ]; then
        echo "Error: No protobuf variables provided on STDIN" >&2
        echo "Usage: echo 'VARIABLE=value' | $0 --update-protobuf" >&2
        exit 1
    fi

    # Load the persistent state file
    PERSISTENT_STATE_FILE="/etc/mesh_ipv4_state" # Used /etc for reboot persistence
    PERSISTENT_IPV4=""
    PROTOBUF_OVERRIDE="" # Clear old override before loading
    if [ -f "$PERSISTENT_STATE_FILE" ]; then
        source "$PERSISTENT_STATE_FILE"
    fi

    # Update the PROTOBUF_OVERRIDE variable in the file
	cat > "$PERSISTENT_STATE_FILE" <<- EOF
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


# --- Helper Functions ---
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - NODE-MGR: $1"
}

# --- UPDATED: get_random_ip_from_cidr with Reservation ---
get_random_ip_from_cidr() {
    local CIDR="$1";
    # Helper functions for IP integer conversion
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
    local MAX_RETRIES=10
    local RETRY_COUNT=0
    while [ "$RETRY_COUNT" -lt "$MAX_RETRIES" ]; do
      local RANDOM_INT=$(shuf -i "${USABLE_MIN_INT}-${MAX_INT}" -n 1)
      # Basic check: Ensure it's not the network or broadcast if calculation was somehow odd
      # Although USABLE_MIN/MAX should prevent this.
      # A more robust check might involve ipcalc again, but keep it simple for now.
      int_to_ip "$RANDOM_INT"
      return 0 # Return the first valid random IP found
      ((RETRY_COUNT++))
    done
    echo "Error: Failed to find a suitable random IP after $MAX_RETRIES attempts." >&2
    return 1

}

save_persistent_state() {
    cat > "$PERSISTENT_STATE_FILE" <<- EOF
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
    if [ -n "$PROTOBUF_OVERRIDE" ]; then
        log "Loaded protobuf overrides."
    fi
fi

# Clear any old IPv4 addresses on startup - ensures a clean state
ip -4 addr flush dev "$CONTROL_IFACE"
log "Initial IPv4 address flush on ${CONTROL_IFACE}."

### --- Main Loop ---
while true; do

    # Reload persistent state ONLY if it was recently updated by --update-protobuf
    # This avoids constantly re-reading the file unnecessarily.
    if [ -f "$PERSISTENT_STATE_FILE" ]; then
        # Get modification time of state file
        STATE_FILE_MTIME=$(stat -c %Y "$PERSISTENT_STATE_FILE")
        # Get the LAST_UPDATE timestamp stored inside the file
        TEMP_LAST_UPDATE=0
        eval "$(grep '^LAST_UPDATE=' "$PERSISTENT_STATE_FILE")"
        TEMP_LAST_UPDATE=$LAST_UPDATE # Assign sourced value to temp var

        # If file's modification time is newer than internal timestamp, reload
        if [[ "$STATE_FILE_MTIME" -gt "$TEMP_LAST_UPDATE" ]]; then
            log "Detected external update to state file. Reloading..."
            source "$PERSISTENT_STATE_FILE"
        fi
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
    ENCODER_ARGS=(
        "--hostname" "$HOSTNAME"
        "--mac-address" "$MY_MAC"
        "--tq-average" "$TQ_AVG"
        "--syncthing-id" "$SYNCTHING_ID"
    )
    [ "$IPV4_STATE" == "CONFIGURED" ] && ENCODER_ARGS+=("--ipv4-address" "$CURRENT_IPV4")
    [ -n "$IS_GATEWAY_FLAG" ] && ENCODER_ARGS+=("$IS_GATEWAY_FLAG")
    [ -n "$IS_NTP_FLAG" ] && ENCODER_ARGS+=("$IS_NTP_FLAG")

    # Apply protobuf overrides if present
    if [ -n "$PROTOBUF_OVERRIDE" ]; then
        log "Applying protobuf overrides: $PROTOBUF_OVERRIDE"
        # Source the overrides string in a subshell to parse and set temp vars
        (
         eval "$PROTOBUF_OVERRIDE"
         # Add flags based on temp vars - ensure these match encoder.py args
         [ "$IS_MUMBLE_SERVER" == "true" ] && echo "--is-mumble-server"
         [ "$IS_TAK_SERVER" == "true" ] && echo "--is-tak-server"
         [ -n "$UPTIME_SECONDS" ] && echo "--uptime-seconds=$UPTIME_SECONDS"
         [ -n "$BATTERY_PERCENTAGE" ] && echo "--battery-percentage=$BATTERY_PERCENTAGE"
         [ -n "$CPU_LOAD_AVERAGE" ] && echo "--cpu-load-average=$CPU_LOAD_AVERAGE"
         # Handle GPS fields carefully
         if [ -n "$GPS_LATITUDE" ] && [ -n "$GPS_LONGITUDE" ]; then
             echo "--latitude=$GPS_LATITUDE"
             echo "--longitude=$GPS_LONGITUDE"
             [ -n "$GPS_ALTITUDE" ] && echo "--altitude=$GPS_ALTITUDE"
         fi
         [ -n "$ATAK_USER" ] && echo "--atak-user=$ATAK_USER"
        ) | while read -r flag; do ENCODER_ARGS+=("$flag"); done # Append flags
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
    # Get payload list, skipping the first field (originator MAC)
    mapfile -t PEER_PAYLOADS < <(alfred -r $ALFRED_DATA_TYPE | cut -d' ' -f2-)

    REGISTRY_TMP=$(mktemp)
    CLAIMED_IPS_TMP=$(mktemp)

    # Add header to the registry file
    echo "# Mesh Node Registry - Generated $(date)" > "$REGISTRY_TMP"
    echo "# Sourced by other scripts to get network state." >> "$REGISTRY_TMP"
    echo "" >> "$REGISTRY_TMP"

    for B64_PAYLOAD in "${PEER_PAYLOADS[@]}"; do
        TRIMMED_PAYLOAD=$(echo "$B64_PAYLOAD" | xargs) # Trim whitespace
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
                    echo "${PREFIX}_IS_GATEWAY='$IS_INTERNET_GATEWAY'"
                    echo "${PREFIX}_IS_NTP_SERVER='$IS_NTP_SERVER'"
                    echo "${PREFIX}_IS_MUMBLE_SERVER='$IS_MUMBLE_SERVER'"
                    echo "${PREFIX}_IS_TAK_SERVER='$IS_TAK_SERVER'"
                    echo "${PREFIX}_UPTIME_SECONDS='$UPTIME_SECONDS'"
                    echo "${PREFIX}_BATTERY_PERCENTAGE='$BATTERY_PERCENTAGE'"
                    echo "${PREFIX}_CPU_LOAD_AVERAGE='$CPU_LOAD_AVERAGE'"
                    echo "${PREFIX}_ATAK_USER='$ATAK_USER'"
                    echo ""
                fi
            ) >> "$REGISTRY_TMP"

            # Populate CLAIMED_IPS array for internal IPv4 logic
            (
                eval "$DECODED_DATA"
                if [[ -n "$IPV4_ADDRESS" && -n "$MAC_ADDRESS" ]]; then
                    echo "${IPV4_ADDRESS},${MAC_ADDRESS}"
                fi
            ) >> "$CLAIMED_IPS_TMP"
        fi
    done

    # Sort IPs for stable comparison later
    sort "$CLAIMED_IPS_TMP" > /tmp/claimed_ips.txt
    rm "$CLAIMED_IPS_TMP"

    # Replace the old registry with the new one
    mv "$REGISTRY_TMP" "$REGISTRY_STATE_FILE"
    chmod 644 "$REGISTRY_STATE_FILE" # Ensure readable

    mapfile -t CLAIMED_IPS < /tmp/claimed_ips.txt

    # --- 4. MANAGE IPV4 ADDRESS (Using the registry file and reserved range) ---
    case $IPV4_STATE in
        "UNCONFIGURED")
            PROPOSED_IPV4="" # Reset proposal
            # First, try to reuse persistent IP if we have one AND it's not reserved
            if [ -n "$PERSISTENT_IPV4" ]; then
                PERSISTENT_IPV4_INT=$(ip_to_int "$PERSISTENT_IPV4")
                # Calculate reserved range here if not done yet by get_random_ip_from_cidr
                if [ "$RESERVED_START_INT" -eq 0 ]; then
                    TEMP_IP=$(get_random_ip_from_cidr "${IPV4_NETWORK}")
                    if [ -z "$TEMP_IP" ]; then
                       log "Cannot determine network range. Skipping persistent IP check."
                       PERSISTENT_IPV4=""
                    fi
                fi
                # Check if persistent IP falls within reserved range (only if range calculated)
                if [ "$RESERVED_START_INT" -ne 0 ] && [ "$PERSISTENT_IPV4_INT" -ge "$RESERVED_START_INT" ] && [ "$PERSISTENT_IPV4_INT" -le "$RESERVED_END_INT" ]; then
                    log "Persistent IP ${PERSISTENT_IPV4} is within the reserved range. Ignoring."
                    PERSISTENT_IPV4=""
                    save_persistent_state
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

            # Check if IP generation failed
            if [ -z "$PROPOSED_IPV4" ]; then
                log "Failed to generate IP. Retrying."
                sleep 5
                continue # Skip rest of loop and retry
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
                sleep 1 # Brief pause before next loop iteration
            else
                log "Claiming ${PROPOSED_IPV4}..."
                # Extract CIDR suffix (e.g., /24) from IPV4_NETWORK variable
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
                    PERSISTENT_IPV4="" # Clear persistent IP since we lost it
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

    # --- 5: TRIGGER ELECTION SCRIPTS ---
    if [ -f "$REGISTRY_STATE_FILE" ]; then
        # Check if the election script exists and is executable
        if [ -x "$MEDIAMTX_ELECTION_SCRIPT" ]; then
             # Execute in background to avoid blocking manager loop
             "$MEDIAMTX_ELECTION_SCRIPT" &
        else
            # Log warning only once per state file update to avoid spam
            if [[ ! -v MEDIAMTX_SCRIPT_WARNING_LOGGED || $(stat -c %Y "$REGISTRY_STATE_FILE") -gt $MEDIAMTX_SCRIPT_WARNING_LOGGED ]]; then
                log "Warning: MediaMTX election script not found or not executable at $MEDIAMTX_ELECTION_SCRIPT"
                MEDIAMTX_SCRIPT_WARNING_LOGGED=$(stat -c %Y "$REGISTRY_STATE_FILE")
            fi
        fi
        # other election scripts will go here
    else
        # Log warning only once per state file update
        if [[ ! -v REGISTRY_WARNING_LOGGED || $(date +%s) -gt $((REGISTRY_WARNING_LOGGED + 60)) ]]; then
             log "Registry file not found ($REGISTRY_STATE_FILE). Skipping election triggers."
             REGISTRY_WARNING_LOGGED=$(date +%s)
        fi
    fi


    # --- Wait for next cycle ---
    sleep "$MONITOR_INTERVAL"
done

log "mesh-node-manager loop exited unexpectedly. Restarting..."
exit 1

