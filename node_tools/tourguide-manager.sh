#!/bin/bash
# ==============================================================================
# Tourguide Manager
# ==============================================================================
# Handles tourguide election, radio hopping, broadcasting, and partition detection
# Called by node-manager.sh during tourguide windows
# ==============================================================================

CONTROL_IFACE="br0"
ALFRED_HELPER_TYPE=69
REGISTRY_STATE_FILE="/var/run/mesh_node_registry"
WPA_CONF_2_4="/etc/wpa_supplicant/wpa_supplicant-wlan0.conf"
WPA_CONF_5_0="/etc/wpa_supplicant/wpa_supplicant-wlan1.conf"
LOBBY_FREQ_2_4=2412
LOBBY_FREQ_5_0=5180
ENCODER_PATH="/usr/local/bin/encoder.py"
BATCTL_PATH="/usr/sbin/batctl"
ELECTION_OUTPUT_FILE="/var/run/mesh_channel_election"

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - TOURGUIDE: $1" | systemd-cat -t tourguide-manager
}

get_current_freq() {
    local conf_file=$1
    if [ -f "$conf_file" ]; then
        grep -oP 'frequency=\K[0-9]+' "$conf_file" | head -1
    else
        echo ""
    fi
}

is_hosting_service() {
    if systemctl is-active --quiet mediamtx.service; then
        source /etc/mesh_ipv4.conf 2>/dev/null
        local MEDIAMTX_IPV4_VIP=$(get_mediamtx_ipv4_vip "$IPV4_NETWORK")
        if ip addr show dev "$CONTROL_IFACE" | grep -q "inet $MEDIAMTX_IPV4_VIP/"; then
            return 0
        fi
    fi
    return 1
}

get_mediamtx_ipv4_vip() {
    local CIDR="$1"
    local CALC_OUTPUT=$(ipcalc "$CIDR" 2>/dev/null)
    local FIRST_IP=$(echo "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')
    echo "${FIRST_IP%.*}.$((${FIRST_IP##*.} + 1))"
}

is_candidate_hosting_service() {
    local MAC_SANITIZED=$1
    [ ! -f "$REGISTRY_STATE_FILE" ] && return 1

    local MEDIAMTX_VAR="NODE_${MAC_SANITIZED}_IS_MEDIAMTX_SERVER"
    local IS_MEDIAMTX=$(grep "^${MEDIAMTX_VAR}=" "$REGISTRY_STATE_FILE" | cut -d'=' -f2 | tr -d "'")
    [ "$IS_MEDIAMTX" == "true" ] && return 0

    local MUMBLE_VAR="NODE_${MAC_SANITIZED}_IS_MUMBLE_SERVER"
    local IS_MUMBLE=$(grep "^${MUMBLE_VAR}=" "$REGISTRY_STATE_FILE" | cut -d'=' -f2 | tr -d "'")
    [ "$IS_MUMBLE" == "true" ] && return 0

    return 1
}

elect_tourguide() {
    local MY_MAC=$1
    local NEIGHBOR_MACS=($("$BATCTL_PATH" n | awk '/\[wlan[0-9]\]/ {print $2}' | sort -u))

    if [ ${#NEIGHBOR_MACS[@]} -eq 0 ]; then
        log "No neighbors (isolated). Electing self to find mesh."
        echo "$MY_MAC"
        return 0
    fi

    local CANDIDATES=("$MY_MAC")
    CANDIDATES+=("${NEIGHBOR_MACS[@]}")

    local WINNER_MAC=""
    local OLDEST_TIMESTAMP=$(date +%s)

    for CANDIDATE_MAC in "${CANDIDATES[@]}"; do
        local MAC_SANITIZED=$(echo "$CANDIDATE_MAC" | tr -d ':')

        if [ "$CANDIDATE_MAC" == "$MY_MAC" ]; then
            if is_hosting_service; then
                log "Skipping self (hosting service)"
                continue
            fi
        else
            if is_candidate_hosting_service "$MAC_SANITIZED"; then
                log "Skipping $CANDIDATE_MAC (hosting service)"
                continue
            fi
        fi

        local TIMESTAMP_VAR="NODE_${MAC_SANITIZED}_LAST_TOURGUIDE_TIMESTAMP"
        local CANDIDATE_TIMESTAMP=$(grep "^${TIMESTAMP_VAR}=" "$REGISTRY_STATE_FILE" 2>/dev/null | cut -d'=' -f2 | tr -d "'")
        CANDIDATE_TIMESTAMP=${CANDIDATE_TIMESTAMP:-0}

        if [ "$CANDIDATE_TIMESTAMP" -lt "$OLDEST_TIMESTAMP" ]; then
            OLDEST_TIMESTAMP=$CANDIDATE_TIMESTAMP
            WINNER_MAC=$CANDIDATE_MAC
        elif [ "$CANDIDATE_TIMESTAMP" -eq "$OLDEST_TIMESTAMP" ]; then
            if [[ -z "$WINNER_MAC" || "$CANDIDATE_MAC" < "$WINNER_MAC" ]]; then
                WINNER_MAC=$CANDIDATE_MAC
            fi
        fi
    done

    [ -z "$WINNER_MAC" ] && WINNER_MAC=$(printf "%s\n" "${CANDIDATES[@]}" | sort | head -1)

    log "Elected tourguide: $WINNER_MAC"
    echo "$WINNER_MAC"
}

select_tourguide_radio() {
    local MY_MAC=$1
    local MAC_SANITIZED=$(echo "$MY_MAC" | tr -d ':')
    local RADIO_VAR="NODE_${MAC_SANITIZED}_LAST_TOURGUIDE_RADIO"

    local LAST_RADIO=$(grep "^${RADIO_VAR}=" "$REGISTRY_STATE_FILE" 2>/dev/null | cut -d'=' -f2 | tr -d "'")

    case "$LAST_RADIO" in
        "wlan0") echo "wlan1" ;;
        "wlan1") echo "wlan0" ;;
        *) echo "wlan0" ;;
    esac
}

hop_to_lobby_frequency() {
    local iface=$1
    local freq=$2
    local conf=$3

    sed -i "s/frequency=.*/frequency=${freq}/" "$conf"
    wpa_cli -i "$iface" reconfigure >/dev/null 2>&1

    for i in {1..20}; do
        CURRENT_FREQ=$(iw dev "$iface" info 2>/dev/null | grep -oP 'channel.*\((\K[0-9]+)' || echo "0")
        if [ "$CURRENT_FREQ" == "$freq" ]; then
            break
        fi
        sleep 0.5
    done

    # Set lobby bitrates
    if [ "$freq" == "$LOBBY_FREQ_2_4" ]; then
        iw dev "$iface" set bitrates legacy-2.4 1 2 5.5 11
    elif [ "$freq" == "$LOBBY_FREQ_5_0" ]; then
        iw dev "$iface" set bitrates legacy-5 6 9 12 18
    fi
}

hop_to_data_frequency() {
    local iface=$1
    local freq=$2
    local conf=$3

    sed -i "s/frequency=.*/frequency=${freq}/" "$conf"
    iw dev "$iface" set bitrates
    wpa_cli -i "$iface" reconfigure >/dev/null 2>&1
}

analyze_partition_data() {
    local payloads="$1"
    local MY_MAC=$(cat "/sys/class/net/${CONTROL_IFACE}/address")
    local MY_CHAN_2_4=$(get_current_freq "$WPA_CONF_2_4")
    local MY_CHAN_5_0=$(get_current_freq "$WPA_CONF_5_0")

    declare -A CHANNEL_CONFIGS
    local FOREIGN_NODES_FOUND=false

    while IFS= read -r payload; do
        [ -z "$payload" ] && continue

        DECODED=$("/usr/local/bin/decoder.py" "$payload" 2>/dev/null || true)
        [ -z "$DECODED" ] && continue

        eval $(echo "$DECODED" | grep -E "^(MAC_ADDRESS|DATA_CHANNEL_)")

        [ "$MAC_ADDRESS" == "$MY_MAC" ] && { unset MAC_ADDRESS DATA_CHANNEL_2_4 DATA_CHANNEL_5_0; continue; }

        FOREIGN_NODES_FOUND=true

        if [[ -n "$DATA_CHANNEL_2_4" && -n "$DATA_CHANNEL_5_0" ]]; then
            CONFIG_KEY="${DATA_CHANNEL_2_4}-${DATA_CHANNEL_5_0}"
            CHANNEL_CONFIGS[$CONFIG_KEY]=$((${CHANNEL_CONFIGS[$CONFIG_KEY]:-0} + 1))
        fi

        unset MAC_ADDRESS DATA_CHANNEL_2_4 DATA_CHANNEL_5_0
    done <<< "$payloads"

    [ "$FOREIGN_NODES_FOUND" = false ] && return

    MY_CONFIG="${MY_CHAN_2_4}-${MY_CHAN_5_0}"

    for config in "${!CHANNEL_CONFIGS[@]}"; do
        if [ "$config" != "$MY_CONFIG" ]; then
            count=${CHANNEL_CONFIGS[$config]}
            log "!!! PARTITION: $count nodes on $config !!!"

            local my_count=$(batctl o | awk 'NR>1' | wc -l)

            if [ $count -gt $((my_count + 2)) ]; then
                log "Other partition larger. Triggering migration..."
                IFS='-' read -r new_2_4 new_5_0 <<< "$config"

                cat > "$ELECTION_OUTPUT_FILE" <<-EOF
					WINNER_2_4=$new_2_4
					WINNER_5_0=$new_5_0
					LIMP_MODE=false
					PARTITION_MERGE=true
				EOF
            fi
            return
        fi
    done
}

# === MAIN EXECUTION ===
MY_MAC=$(cat "/sys/class/net/${CONTROL_IFACE}/address")
NOW=$(date +%s)

# Read last tourguide state
LAST_TOURGUIDE_TIME=0
LAST_TOURGUIDE_RADIO=""
if [ -f /var/run/tourguide_state ]; then
    source /var/run/tourguide_state
fi

ELECTED_TOURGUIDE=$(elect_tourguide "$MY_MAC")

if [ "$ELECTED_TOURGUIDE" != "$MY_MAC" ]; then
    log "Tourguide is $ELECTED_TOURGUIDE. Standing by."
    exit 0
fi

log "=== I AM TOURGUIDE ==="

TOURGUIDE_RADIO=$(select_tourguide_radio "$MY_MAC")
HOSTNAME=$(hostname)

# Build helper payload
HELPER_PAYLOAD=$("$ENCODER_PATH" \
    "--hostname" "$HOSTNAME" \
    "--mac-addresses" "$MY_MAC" \
    "--data-channel-2-4" "$(get_current_freq $WPA_CONF_2_4)" \
    "--data-channel-5-0" "$(get_current_freq $WPA_CONF_5_0)" \
    "--timestamp" "$NOW" \
    2>/dev/null)

# Select lobby frequency based on radio
if [ "$TOURGUIDE_RADIO" == "wlan0" ]; then
    LOBBY_FREQ=$LOBBY_FREQ_2_4
    TOURGUIDE_CONF=$WPA_CONF_2_4
else
    LOBBY_FREQ=$LOBBY_FREQ_5_0
    TOURGUIDE_CONF=$WPA_CONF_5_0
fi

DATA_FREQ=$(get_current_freq "$TOURGUIDE_CONF")

log "Hopping $TOURGUIDE_RADIO to lobby ($LOBBY_FREQ)..."
hop_to_lobby_frequency "$TOURGUIDE_RADIO" "$LOBBY_FREQ" "$TOURGUIDE_CONF"

sleep 3

log "Broadcasting helper beacon..."
echo -n "$HELPER_PAYLOAD" | alfred -s $ALFRED_HELPER_TYPE

log "Listening for partitions (12s)..."
sleep 12

# Check for partition
OTHER_PARTITION_DATA=$(alfred -r $ALFRED_HELPER_TYPE 2>/dev/null | grep -oP '"\K[^"]+(?="\s*\},?)' || true)

if [ -n "$OTHER_PARTITION_DATA" ]; then
    analyze_partition_data "$OTHER_PARTITION_DATA"
fi

log "Returning to data channel ($DATA_FREQ)..."
hop_to_data_frequency "$TOURGUIDE_RADIO" "$DATA_FREQ" "$TOURGUIDE_CONF"

# Save state
cat > /var/run/tourguide_state <<EOF
LAST_TOURGUIDE_TIME=$NOW
LAST_TOURGUIDE_RADIO=$TOURGUIDE_RADIO
EOF

log "Tourguide duty complete."
exit 0
