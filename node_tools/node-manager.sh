#!/bin/bash
# ==============================================================================
# Mesh Node Manager - Main Orchestrator
# ==============================================================================
# This script runs as a persistent service to:
# 1. Periodically gather various node metrics.
# 2. Use encoder.py to create a Protobuf message.
# 3. Announce status via alfred.
# 4. Call modular scripts for IP management, registry building, and elections.
# 5. Accept --update arguments to update specific protobuf variables.
# ==============================================================================

# --- Configuration ---
CONTROL_IFACE="br0"
ALFRED_DATA_TYPE=68
DEFENSE_INTERVAL=300 # Republish status every 5 minutes
MONITOR_INTERVAL=15 # How often the main loop runs (seconds)
PERSISTENT_STATE_FILE="/etc/mesh_node_state"
BATCTL_PATH="/usr/sbin/batctl"

# Helper scripts
REGISTRY_BUILDER="/usr/local/bin/mesh-registry-builder.sh"
IP_MANAGER="/usr/local/bin/mesh-ip-manager.sh"
ELECTION_SCRIPTS_PATTERN="/usr/local/bin/*-election.sh"

# --- State Variables ---
LAST_PUBLISHED_PAYLOAD=""
LAST_PUBLISH_TIME=0
PROTOBUF_OVERRIDE=""
LAST_RUN_TIMESTAMP=0

# --- Helper Functions ---
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - NODE-MGR: $1"
}

# Save persistent state
save_persistent_state() {
    cat > "$PERSISTENT_STATE_FILE" <<- EOF
		# Persistent state for mesh node manager
		# Last updated: $(date)
		PROTOBUF_OVERRIDE='$PROTOBUF_OVERRIDE'
		LAST_RUN_TIMESTAMP=$(date +%s)
		LAST_UPDATE=$(date +%s)
	EOF
    chmod 644 "$PERSISTENT_STATE_FILE"
}

# --- Parse command line arguments ---
UPDATE_MODE=false
if [[ "$1" == "--update-protobuf" || "$1" == "-u" ]]; then
    UPDATE_MODE=true
fi

# --- Handle update mode ---
if [ "$UPDATE_MODE" = true ]; then
    # Read protobuf variable assignments from STDIN
    PROTOBUF_VARS=$(cat)
    if [ -z "$PROTOBUF_VARS" ]; then
        echo "Error: No protobuf variables provided on STDIN" >&2
        echo "Usage: echo 'VARIABLE=value' | $0 --update-protobuf" >&2
        exit 1
    fi

    # Load existing state
    if [ -f "$PERSISTENT_STATE_FILE" ]; then
        source "$PERSISTENT_STATE_FILE" 2>/dev/null
    fi

    # Update the PROTOBUF_OVERRIDE variable
    PROTOBUF_OVERRIDE="$PROTOBUF_VARS"
    save_persistent_state

    echo "Protobuf variables updated successfully."
    echo "The node-manager service will use these overrides on next cycle."
    exit 0
fi

# --- Normal operation mode continues below ---

log "Starting Mesh Node Manager."

# Get primary MAC address
MY_MAC=$(cat "/sys/class/net/${CONTROL_IFACE}/address" 2>/dev/null || echo "")
if [ -z "$MY_MAC" ]; then
    log "ERROR: Cannot read MAC address from $CONTROL_IFACE"
    exit 1
fi

log "Node MAC: ${MY_MAC}"

# Load persistent state if it exists
if [ -f "$PERSISTENT_STATE_FILE" ]; then
    source "$PERSISTENT_STATE_FILE" 2>/dev/null
    if [ -n "$PROTOBUF_OVERRIDE" ]; then
        log "Loaded protobuf overrides."
    fi
    if [ -n "$LAST_RUN_TIMESTAMP" ] && [ "$LAST_RUN_TIMESTAMP" -gt 0 ]; then
        log "Last run: $(date -d @${LAST_RUN_TIMESTAMP})"
    fi
fi

### --- Main Loop ---
while true; do
    CYCLE_START=$(date +%s)

    # Reload persistent state if it was recently updated by --update-protobuf
    if [ -f "$PERSISTENT_STATE_FILE" ]; then
        STATE_FILE_MTIME=$(stat -c %Y "$PERSISTENT_STATE_FILE")
        TEMP_LAST_UPDATE=0
        eval "$(grep '^LAST_UPDATE=' "$PERSISTENT_STATE_FILE" 2>/dev/null || echo 'LAST_UPDATE=0')"
        TEMP_LAST_UPDATE=${LAST_UPDATE:-0}

        if [[ "$STATE_FILE_MTIME" -gt "$TEMP_LAST_UPDATE" ]]; then
            log "Detected external update to state file. Reloading..."
            source "$PERSISTENT_STATE_FILE" 2>/dev/null
        fi
    fi

    # --- 1. BUILD REGISTRY (discover peers) ---
    if [ -x "$REGISTRY_BUILDER" ]; then
        log "Building mesh registry..."
        "$REGISTRY_BUILDER"
    else
        log "WARNING: Registry builder not found or not executable: $REGISTRY_BUILDER"
    fi

    # --- 2. MANAGE IP ADDRESS ---
    if [ -x "$IP_MANAGER" ]; then
        "$IP_MANAGER"
    else
        log "WARNING: IP manager not found or not executable: $IP_MANAGER"
    fi

    # --- 3. GATHER LOCAL METRICS ---
    HOSTNAME=$(hostname)
    SYNCTHING_ID=$(runuser -u radio -- syncthing --device-id 2>/dev/null || echo "")
    TQ_AVG=$("$BATCTL_PATH" o | awk 'NR>1 {sum+=$3} END {if (NR>1) printf "%.2f", sum/(NR-1); else print 0}')

    # Check for gateway status
    IS_GATEWAY_FLAG=""
    [ -f /var/run/mesh-gateway.state ] && IS_GATEWAY_FLAG="--is-internet-gateway"

    # Check for NTP server status
    IS_NTP_FLAG=""
    if systemctl is-active --quiet chrony.service && grep -q "allow fd5a" /etc/chrony/chrony.conf 2>/dev/null; then
         IS_NTP_FLAG="--is-ntp-server"
    fi

    # Gather all MAC addresses, with primary (br0) first
    ALL_MACS=("$MY_MAC")
    for iface_dir in /sys/class/net/*; do
        iface=$(basename "$iface_dir")
        if [ "$iface" != "$CONTROL_IFACE" ] && [ -f "$iface_dir/address" ]; then
            mac=$(cat "$iface_dir/address" | tr -d '\n')
            if [ -n "$mac" ] && [ "$mac" != "$MY_MAC" ]; then
                ALL_MACS+=("$mac")
            fi
        fi
    done

    # Get current IPv4 address if configured (excluding service VIPs in reserved range)
    CURRENT_IPV4=""

    # Source network config to get reserved range
    if [ -f /etc/mesh_ipv4.conf ]; then
        source /etc/mesh_ipv4.conf
    fi
    IPV4_NETWORK=${IPV4_NETWORK:-"10.43.1.0/16"}
    RESERVED_IP_COUNT=${RESERVED_IP_COUNT:-5}

    # Calculate reserved range
    CALC_OUTPUT=$(ipcalc "$IPV4_NETWORK" 2>/dev/null)
    if [ -n "$CALC_OUTPUT" ]; then
        HOST_MIN=$(echo "$CALC_OUTPUT" | awk '/HostMin:/ {print $2}')
        if [ -n "$HOST_MIN" ]; then
            # Convert to integer
            IFS=. read -r a b c d <<<"$HOST_MIN"
            MIN_INT=$(( (a << 24) + (b << 16) + (c << 8) + d ))
            RESERVED_END_INT=$(( MIN_INT + RESERVED_IP_COUNT - 1 ))

            # Get all IPs on interface, filter out reserved range
            while IFS= read -r ip; do
                IFS=. read -r a b c d <<<"$ip"
                ip_int=$(( (a << 24) + (b << 16) + (c << 8) + d ))

                # Check if NOT in reserved range
                if [ "$ip_int" -lt "$MIN_INT" ] || [ "$ip_int" -gt "$RESERVED_END_INT" ]; then
                    CURRENT_IPV4="$ip"
                    break
                fi
            done < <(ip addr show dev "$CONTROL_IFACE" | grep -oP 'inet \K[\d.]+')
        fi
    fi

    # Fallback if calculation failed - just get first IP
    if [ -z "$CURRENT_IPV4" ]; then
        CURRENT_IPV4=$(ip addr show dev "$CONTROL_IFACE" | grep -oP 'inet \K[\d.]+' | head -1)
    fi

    # --- 4. ENCODE & PUBLISH OWN STATUS ---
    ENCODER_ARGS=(
        "--hostname" "$HOSTNAME"
        "--mac-addresses" "${ALL_MACS[@]}"
        "--tq-average" "$TQ_AVG"
        "--syncthing-id" "$SYNCTHING_ID"
    )

    # Add IPv4 if present
    [ -n "$CURRENT_IPV4" ] && ENCODER_ARGS+=("--ipv4-address" "$CURRENT_IPV4")

    # Add flags
    [ -n "$IS_GATEWAY_FLAG" ] && ENCODER_ARGS+=("$IS_GATEWAY_FLAG")
    [ -n "$IS_NTP_FLAG" ] && ENCODER_ARGS+=("$IS_NTP_FLAG")

    # Apply protobuf overrides if present
    if [ -n "$PROTOBUF_OVERRIDE" ]; then
        log "Applying protobuf overrides"

        # Parse overrides in subshell to extract flags
        OVERRIDE_ARGS=()
        (
            # Clear variables
            IS_MUMBLE_SERVER="" IS_TAK_SERVER="" UPTIME_SECONDS="" BATTERY_PERCENTAGE=""
            CPU_LOAD_AVERAGE="" GPS_LATITUDE="" GPS_LONGITUDE="" GPS_ALTITUDE="" ATAK_USER=""

            # Evaluate override string
            eval "$PROTOBUF_OVERRIDE"

            # Build argument list
            [[ "$IS_MUMBLE_SERVER" == "true" ]] && OVERRIDE_ARGS+=("--is-mumble-server")
            [[ "$IS_TAK_SERVER" == "true" ]] && OVERRIDE_ARGS+=("--is-tak-server")
            [ -n "$UPTIME_SECONDS" ] && [[ "$UPTIME_SECONDS" =~ ^[0-9]+$ ]] && OVERRIDE_ARGS+=("--uptime-seconds=$UPTIME_SECONDS")
            [ -n "$BATTERY_PERCENTAGE" ] && [[ "$BATTERY_PERCENTAGE" =~ ^[0-9]+$ ]] && OVERRIDE_ARGS+=("--battery-percentage=$BATTERY_PERCENTAGE")
            [ -n "$CPU_LOAD_AVERAGE" ] && [[ "$CPU_LOAD_AVERAGE" =~ ^[0-9]+(\.[0-9]+)?$ ]] && OVERRIDE_ARGS+=("--cpu-load-average=$CPU_LOAD_AVERAGE")

            if [ -n "$GPS_LATITUDE" ] && [ -n "$GPS_LONGITUDE" ]; then
                OVERRIDE_ARGS+=("--latitude=$GPS_LATITUDE")
                OVERRIDE_ARGS+=("--longitude=$GPS_LONGITUDE")
                [ -n "$GPS_ALTITUDE" ] && OVERRIDE_ARGS+=("--altitude=$GPS_ALTITUDE")
            fi

            [ -n "$ATAK_USER" ] && OVERRIDE_ARGS+=("--atak-user=$ATAK_USER")

            # Print args for parent shell
            printf "%s\n" "${OVERRIDE_ARGS[@]}"
        ) | while IFS= read -r arg; do
            ENCODER_ARGS+=("$arg")
        done
    fi

    # Encode the protobuf message
    CURRENT_PAYLOAD=$(/usr/local/bin/encoder.py "${ENCODER_ARGS[@]}" 2>/dev/null)

    # Publish if changed or timer expired
    time_since_publish=$(( $(date +%s) - LAST_PUBLISH_TIME ))
    if [[ "$CURRENT_PAYLOAD" != "$LAST_PUBLISHED_PAYLOAD" || $time_since_publish -gt $DEFENSE_INTERVAL ]]; then
        if [ -n "$CURRENT_PAYLOAD" ]; then
            log "Publishing status to Alfred..."
            echo -n "$CURRENT_PAYLOAD" | alfred -s $ALFRED_DATA_TYPE
            LAST_PUBLISHED_PAYLOAD="$CURRENT_PAYLOAD"
            LAST_PUBLISH_TIME=$(date +%s)
        else
            log "ERROR: Encoder produced empty payload."
        fi
    fi

    # --- 5. TRIGGER ELECTION SCRIPTS ---
    for election_script in $ELECTION_SCRIPTS_PATTERN; do
        if [ -f "$election_script" ] && [ -x "$election_script" ]; then
            log "Triggering election script: $(basename $election_script)"
            "$election_script" &
        fi
    done

    # --- 6. UPDATE TIMESTAMP ---
    LAST_RUN_TIMESTAMP=$(date +%s)
    save_persistent_state

    # --- Wait for next cycle ---
    sleep "$MONITOR_INTERVAL"
done

log "node-manager loop exited unexpectedly. Restarting..."
exit 1
