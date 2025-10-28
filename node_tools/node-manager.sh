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


#Election services will be going here
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
RESERVED_IP_COUNT=5 # How many IPs to reserve (e.g., .1 to .5)
RESERVED_START_INT=0 # Will be calculated
RESERVED_END_INT=0   # Will be calculated

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

# Converts an IP string (e.g., 192.168.1.1) to a 32-bit integer.
ip_to_int() {
    local ip=$1
    # Basic validation before processing
    if [[ -z "$ip" || ! "$ip" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
       # Log error but return empty string, let caller handle check
       log "Error: Invalid IP format passed to ip_to_int: '$ip'"
       echo ""
       return 1
    fi
    local a b c d; IFS=. read -r a b c d <<<"$ip"; echo "$(( (a << 24) + (b << 16) + (c << 8) + d ))"
}
# Correctly converts a 32-bit integer back to an IP string.
int_to_ip() {
    local ip_int=$1; echo "$(( (ip_int >> 24) & 255 )).$(( (ip_int >> 16) & 255 )).$(( (ip_int >> 8) & 255 )).$(( ip_int & 255 ))"
}

# --- get_random_ip_from_cidr with Reservation ---
get_random_ip_from_cidr() {
    local CIDR="$1";
    # --- Main Logic ---
    # Validate CIDR format before passing to ipcalc
    if [[ -z "$CIDR" || ! "$CIDR" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}/[0-9]{1,2}$ ]]; then
        echo "Error: Invalid CIDR format passed to get_random_ip_from_cidr: '$CIDR'" >&2
        return 1
    fi
    local CALC_OUTPUT; CALC_OUTPUT=$(ipcalc "$CIDR" 2>/dev/null);
    if [ -z "$CALC_OUTPUT" ]; then echo "Error: ipcalc failed for CIDR: $CIDR" >&2; return 1; fi
    local HOST_MIN=$(echo "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')
    local HOST_MAX=$(echo "$CALC_OUTPUT" | awk '/HostMax/ {print $2}')
    if [ -z "$HOST_MIN" ] || [ -z "$HOST_MAX" ]; then echo "Error: ipcalc parsing failed." >&2; return 1; fi
    local MIN_INT=$(ip_to_int "$HOST_MIN") # Uses global function now
    local MAX_INT=$(ip_to_int "$HOST_MAX") # Uses global function now
    # Check conversion results
    if [ -z "$MIN_INT" ] || [ -z "$MAX_INT" ]; then echo "Error: Failed to convert HostMin/Max to integer." >&2; return 1; fi

    # Calculate the reserved range (only once, if not already set).
    # Ensure this runs only if calculation hasn't happened yet AND range is valid.
    if [[ "$RESERVED_START_INT" -eq 0 && "$MIN_INT" -le "$MAX_INT" ]]; then
        RESERVED_START_INT=$MIN_INT
        RESERVED_END_INT=$(( MIN_INT + RESERVED_IP_COUNT - 1 ))
        if [ "$RESERVED_END_INT" -gt "$MAX_INT" ]; then
            RESERVED_END_INT=$MAX_INT
            log "Warning: Reserved IP count exceeds available hosts. Reserving up to $(int_to_ip $HOST_MAX)" >&2
        fi
        # Check if int_to_ip succeeded before logging (redirect to stderr)
        local range_start_ip=$(int_to_ip $RESERVED_START_INT)
        local range_end_ip=$(int_to_ip $RESERVED_END_INT)
        if [ -n "$range_start_ip" ] && [ -n "$range_end_ip" ]; then
            log "Calculated reserved range: $range_start_ip - $range_end_ip" >&2
        else
            log "Error converting reserved range integers back to IP for logging." >&2
        fi
    elif [ "$RESERVED_START_INT" -eq 0 ]; then
         echo "Error: Cannot calculate reserved range due to invalid network range." >&2
         return 1
    fi

    local USABLE_MIN_INT=$(( RESERVED_END_INT + 1 ))

    if [ "$USABLE_MIN_INT" -gt "$MAX_INT" ]; then
        echo "Error: No assignable dynamic IPs available after reserving $RESERVED_IP_COUNT IPs." >&2
        return 1
    fi
    if [ "$USABLE_MIN_INT" -eq "$MAX_INT" ]; then
        int_to_ip "$USABLE_MIN_INT" # Uses global function now
        return 0
    fi

    # Pick a random integer from the *usable* range
    local MAX_RETRIES=10
    local RETRY_COUNT=0
    while [ "$RETRY_COUNT" -lt "$MAX_RETRIES" ]; do
      local RANDOM_INT=$(shuf -i "${USABLE_MIN_INT}-${MAX_INT}" -n 1)
      local RANDOM_IP=$(int_to_ip "$RANDOM_INT") # Uses global function now
      # Check if conversion succeeded
      if [ -n "$RANDOM_IP" ]; then
        echo "$RANDOM_IP" # Output the valid IP
        return 0 # Return success
      fi
      log "Warning: int_to_ip failed for $RANDOM_INT. Retrying random selection." >&2
      ((RETRY_COUNT++))
    done
    echo "Error: Failed to find/convert a suitable random IP after $MAX_RETRIES attempts." >&2
    return 1
}

save_persistent_state() {
    # Use <<- EOF and ensure tabs for indentation
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
    # Source in a subshell to avoid polluting main script if file has errors
    ( source "$PERSISTENT_STATE_FILE" 2>/dev/null && \
      echo "PERSISTENT_IPV4='${PERSISTENT_IPV4:-}'" && \
      echo "PROTOBUF_OVERRIDE='${PROTOBUF_OVERRIDE:-}'" ) | while IFS= read -r line; do
        eval "$line" # Safely import only expected vars
    done
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
    if [ -f "$PERSISTENT_STATE_FILE" ]; then
        STATE_FILE_MTIME=$(stat -c %Y "$PERSISTENT_STATE_FILE")
        TEMP_LAST_UPDATE=0
        # Source specific variable safely
        eval "$(grep '^LAST_UPDATE=' "$PERSISTENT_STATE_FILE" 2>/dev/null || echo 'LAST_UPDATE=0')"
        TEMP_LAST_UPDATE=${LAST_UPDATE:-0} # Use default if grep failed

        if [[ "$STATE_FILE_MTIME" -gt "$TEMP_LAST_UPDATE" ]]; then
            log "Detected external update to state file. Reloading..."
            # Source in subshell again
            ( source "$PERSISTENT_STATE_FILE" 2>/dev/null && \
              echo "PERSISTENT_IPV4='${PERSISTENT_IPV4:-}'" && \
              echo "PROTOBUF_OVERRIDE='${PROTOBUF_OVERRIDE:-}'" ) | while IFS= read -r line; do
                eval "$line"
            done
        fi
    fi


    # --- 1. GATHER LOCAL METRICS ---
    HOSTNAME=$(hostname)
    SYNCTHING_ID=$(runuser -u radio -- syncthing --device-id 2>/dev/null || echo "")
    # Use explicit path for batctl
    TQ_AVG=$("$BATCTL_PATH" o | awk 'NR>1 {sum+=$3} END {if (NR>1) printf "%.2f", sum/(NR-1); else print 0}')
    IS_GATEWAY_FLAG=""
    [ -f /var/run/mesh-gateway.state ] && IS_GATEWAY_FLAG="--is-internet-gateway"
    IS_NTP_FLAG=""
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
    # Ensure CURRENT_IPV4 is valid before adding
    [[ "$IPV4_STATE" == "CONFIGURED" && -n "$CURRENT_IPV4" ]] && ENCODER_ARGS+=("--ipv4-address" "$CURRENT_IPV4")
    [ -n "$IS_GATEWAY_FLAG" ] && ENCODER_ARGS+=("$IS_GATEWAY_FLAG")
    [ -n "$IS_NTP_FLAG" ] && ENCODER_ARGS+=("$IS_NTP_FLAG")

    # Apply protobuf overrides if present
    if [ -n "$PROTOBUF_OVERRIDE" ]; then
        log "Applying protobuf overrides: $PROTOBUF_OVERRIDE"
        # Source the overrides string in a subshell to parse and set temp vars
        OVERRIDE_ARGS=() # Array to hold override flags
        (
         # Clear vars before eval to avoid leakage if override doesn't set them
         IS_MUMBLE_SERVER="" IS_TAK_SERVER="" UPTIME_SECONDS="" BATTERY_PERCENTAGE=""
         CPU_LOAD_AVERAGE="" GPS_LATITUDE="" GPS_LONGITUDE="" GPS_ALTITUDE="" ATAK_USER=""
         eval "$PROTOBUF_OVERRIDE"
         # Add flags based on temp vars - ensure these match encoder.py args
         [[ "$IS_MUMBLE_SERVER" == "true" ]] && OVERRIDE_ARGS+=("--is-mumble-server")
         [[ "$IS_TAK_SERVER" == "true" ]] && OVERRIDE_ARGS+=("--is-tak-server")
         [ -n "$UPTIME_SECONDS" ] && [[ "$UPTIME_SECONDS" =~ ^[0-9]+$ ]] && OVERRIDE_ARGS+=("--uptime-seconds=$UPTIME_SECONDS")
         [ -n "$BATTERY_PERCENTAGE" ] && [[ "$BATTERY_PERCENTAGE" =~ ^[0-9]+$ ]] && OVERRIDE_ARGS+=("--battery-percentage=$BATTERY_PERCENTAGE")
         [ -n "$CPU_LOAD_AVERAGE" ] && [[ "$CPU_LOAD_AVERAGE" =~ ^[0-9]+(\.[0-9]+)?$ ]] && OVERRIDE_ARGS+=("--cpu-load-average=$CPU_LOAD_AVERAGE")
         # Handle GPS fields carefully - Add validation if needed
         if [ -n "$GPS_LATITUDE" ] && [ -n "$GPS_LONGITUDE" ]; then
             OVERRIDE_ARGS+=("--latitude=$GPS_LATITUDE")
             OVERRIDE_ARGS+=("--longitude=$GPS_LONGITUDE")
             [ -n "$GPS_ALTITUDE" ] && OVERRIDE_ARGS+=("--altitude=$GPS_ALTITUDE")
         fi
         [ -n "$ATAK_USER" ] && OVERRIDE_ARGS+=("--atak-user=$ATAK_USER")
        )
        # Append override args to main args list
        ENCODER_ARGS+=("${OVERRIDE_ARGS[@]}")
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
    mapfile -t PEER_PAYLOADS < <(alfred -r $ALFRED_DATA_TYPE | grep -oP '"\K[^"]+(?="\s*\},?)' )
    log "DEBUG: Found ${#PEER_PAYLOADS[@]} peer payloads from Alfred"
    REGISTRY_TMP=$(mktemp)
    CLAIMED_IPS_TMP=$(mktemp)

    echo "# Mesh Node Registry - Generated $(date)" > "$REGISTRY_TMP"
    echo "# Sourced by other scripts to get network state." >> "$REGISTRY_TMP"
    echo "" >> "$REGISTRY_TMP"

    for B64_PAYLOAD in "${PEER_PAYLOADS[@]}"; do
        if [ -z "$B64_PAYLOAD" ]; then 
            log "DEBUG: Skipping empty payload"
            continue
        fi

        log "DEBUG: Decoding payload: ${B64_PAYLOAD:0:20}..."
        DECODED_DATA=$(/usr/local/bin/decoder.py "${B64_PAYLOAD}" 2>&1)
        DECODER_EXIT=$?

        if [ $DECODER_EXIT -ne 0 ]; then
            log "Warning: decoder.py failed with exit code $DECODER_EXIT"
            log "Decoder output: $DECODED_DATA"
            continue
        fi

        if [ -z "$DECODED_DATA" ]; then
            log "Warning: decoder.py returned empty data for payload"
            continue
        fi

        log "DEBUG: Decoded data: $DECODED_DATA"

        # Filter for valid variable assignments (with or without quotes)
		FILTERED_DATA=$(echo "$DECODED_DATA" | grep -E "^[A-Z0-9_]+=")
        if [ -z "$FILTERED_DATA" ]; then
            log "Warning: No valid variable assignments in decoded data"
            continue
        fi

        log "DEBUG: Filtered data has $(echo "$FILTERED_DATA" | wc -l) lines"

        # Evaluate in current shell to capture variables
        eval "$FILTERED_DATA"

        if [[ -n "$MAC_ADDRESS" ]]; then
            PREFIX="NODE_$(echo "$MAC_ADDRESS" | tr -d ':')"
            # Write to registry
            {
                printf "%s_HOSTNAME='%s'\n" "$PREFIX" "$HOSTNAME"
                printf "%s_MAC_ADDRESS='%s'\n" "$PREFIX" "$MAC_ADDRESS"
                printf "%s_IPV4_ADDRESS='%s'\n" "$PREFIX" "$IPV4_ADDRESS"
                printf "%s_SYNCTHING_ID='%s'\n" "$PREFIX" "$SYNCTHING_ID"
                printf "%s_TQ_AVERAGE='%s'\n" "$PREFIX" "$TQ_AVERAGE"
                printf "%s_IS_GATEWAY='%s'\n" "$PREFIX" "$IS_INTERNET_GATEWAY"
                printf "%s_IS_NTP_SERVER='%s'\n" "$PREFIX" "$IS_NTP_SERVER"
                printf "%s_IS_MUMBLE_SERVER='%s'\n" "$PREFIX" "$IS_MUMBLE_SERVER"
                printf "%s_IS_TAK_SERVER='%s'\n" "$PREFIX" "$IS_TAK_SERVER"
                printf "%s_UPTIME_SECONDS='%s'\n" "$PREFIX" "$UPTIME_SECONDS"
                printf "%s_BATTERY_PERCENTAGE='%s'\n" "$PREFIX" "$BATTERY_PERCENTAGE"
                printf "%s_CPU_LOAD_AVERAGE='%s'\n" "$PREFIX" "$CPU_LOAD_AVERAGE"
                printf "%s_ATAK_USER='%s'\n" "$PREFIX" "$ATAK_USER"
                echo ""
            } >> "$REGISTRY_TMP"

            # Track claimed IPs
            if [[ -n "$IPV4_ADDRESS" ]]; then
                echo "${IPV4_ADDRESS},${MAC_ADDRESS}" >> "$CLAIMED_IPS_TMP"
            fi
        else
            log "Warning: MAC_ADDRESS not found in decoded data"
        fi

        # Clear variables for next iteration
        unset HOSTNAME MAC_ADDRESS IPV4_ADDRESS SYNCTHING_ID TQ_AVERAGE IS_INTERNET_GATEWAY \
              IS_NTP_SERVER IS_MUMBLE_SERVER IS_TAK_SERVER UPTIME_SECONDS BATTERY_PERCENTAGE \
              CPU_LOAD_AVERAGE ATAK_USER
    done

    sort "$CLAIMED_IPS_TMP" > /tmp/claimed_ips.txt
    rm "$CLAIMED_IPS_TMP"
    mv "$REGISTRY_TMP" "$REGISTRY_STATE_FILE"
    chmod 644 "$REGISTRY_STATE_FILE"
    mapfile -t CLAIMED_IPS < /tmp/claimed_ips.txt

    # --- 4. MANAGE IPV4 ADDRESS (Using the registry file and reserved range) ---
    case $IPV4_STATE in
        "UNCONFIGURED")
            PROPOSED_IPV4=""
            if [ -n "$PERSISTENT_IPV4" ]; then
                # Use global function ip_to_int
                PERSISTENT_IPV4_INT=$(ip_to_int "$PERSISTENT_IPV4")
                # Check function succeeded before proceeding
                if [ -n "$PERSISTENT_IPV4_INT" ]; then
                    # Ensure reserved range is calculated before checking persistent IP
                    if [ "$RESERVED_START_INT" -eq 0 ]; then
                        # Temporarily call get_random_ip to force range calculation
                        get_random_ip_from_cidr "${IPV4_NETWORK}" > /dev/null
                        # Recheck if calculation succeeded
                        if [ "$RESERVED_START_INT" -eq 0 ]; then
                            log "Cannot determine network range. Skipping persistent IP check this cycle."
                            PERSISTENT_IPV4="" # Clear invalid persistent IP
                        fi
                    fi
                    # Check against reserved range (only if range is valid)
                    if [ "$RESERVED_START_INT" -ne 0 ] && [ "$PERSISTENT_IPV4_INT" -ge "$RESERVED_START_INT" ] && [ "$PERSISTENT_IPV4_INT" -le "$RESERVED_END_INT" ]; then
                        log "Persistent IP ${PERSISTENT_IPV4} is reserved. Ignoring."
                        PERSISTENT_IPV4=""
                        save_persistent_state
                    else
                        log "Attempting to reclaim previous IP: ${PERSISTENT_IPV4}..."
                        PROPOSED_IPV4="$PERSISTENT_IPV4"
                    fi
                else
                    log "Failed to convert persistent IP $PERSISTENT_IPV4 to integer. Ignoring."
                    PERSISTENT_IPV4="" # Clear invalid persistent IP
                    save_persistent_state
                fi
            fi


            if [ -z "$PROPOSED_IPV4" ]; then
                log "Proposing new IP from ${IPV4_NETWORK} (excluding reserved)..."
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
                    PERSISTENT_IPV4=""
                    save_persistent_state
                else
                    log "Proposed IP ${PROPOSED_IPV4} is in use. Retrying."
                fi
                sleep 1
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
        if [ -x "$MEDIAMTX_ELECTION_SCRIPT" ]; then
             "$MEDIAMTX_ELECTION_SCRIPT" &
        else
            # Log warning only once per state file update to avoid spam
            if [[ ! -v MEDIAMTX_SCRIPT_WARNING_LOGGED || $(stat -c %Y "$REGISTRY_STATE_FILE") -gt ${MEDIAMTX_SCRIPT_WARNING_LOGGED:-0} ]]; then
                log "Warning: MediaMTX election script not found or not executable at $MEDIAMTX_ELECTION_SCRIPT"
                MEDIAMTX_SCRIPT_WARNING_LOGGED=$(stat -c %Y "$REGISTRY_STATE_FILE")
            fi
        fi
        # Add calls to other election scripts here...
    else
        # Log warning only periodically to avoid spam
        if [[ ! -v REGISTRY_WARNING_LOGGED || $(date +%s) -gt $(( ${REGISTRY_WARNING_LOGGED:-0} + 60 )) ]]; then
             log "Registry file not found ($REGISTRY_STATE_FILE). Skipping election triggers."
             REGISTRY_WARNING_LOGGED=$(date +%s)
        fi
    fi

    # --- Wait for next cycle ---
    sleep "$MONITOR_INTERVAL"
done

log "mesh-node-manager loop exited unexpectedly. Restarting..."
exit 1
