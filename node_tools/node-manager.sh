#!/bin/bash
# ==============================================================================
# Mesh Node Manager - Main Orchestrator
# ==============================================================================
# Coordinates timing and delegates complex tasks to specialized scripts
# ==============================================================================

# --- Configuration ---
CONTROL_IFACE="br0"
ALFRED_DATA_TYPE=68
ALFRED_HELPER_TYPE=69
MONITOR_INTERVAL=15

# Lobby channels
LOBBY_FREQ_2_4=2412
LOBBY_FREQ_5_0=5180

# Radio Config
WPA_CONF_2_4="/etc/wpa_supplicant/wpa_supplicant-wlan0.conf"
WPA_CONF_5_0="/etc/wpa_supplicant/wpa_supplicant-wlan1.conf"

# Scan frequencies
SCAN_FREQS_2_4="2412 2437 2462"
SCAN_FREQS_5_0="5180 5200 5220 5240 5745 5765 5785 5805 5825"

# Helper scripts
REGISTRY_BUILDER="/usr/local/bin/mesh-registry-builder.sh"
IP_MANAGER="/usr/local/bin/mesh-ip-manager.sh"
CHANNEL_ELECTION="/usr/local/bin/channel-election.sh"
TOURGUIDE_MANAGER="/usr/local/bin/tourguide-manager.sh"
QUORUM_CHECKER="/usr/local/bin/quorum-checker.sh"
LIMP_MODE_MANAGER="/usr/local/bin/limp-mode-manager.sh"
ELECTION_OUTPUT_FILE="/var/run/mesh_channel_election"
REGISTRY_STATE_FILE="/var/run/mesh_node_registry"
ENCODER_PATH="/usr/local/bin/encoder.py"
BATCTL_PATH="/usr/sbin/batctl"

# --- State Variables ---
LAST_PUBLISHED_PAYLOAD=""
LAST_PUBLISH_TIME=0
CACHED_SCAN_REPORT_JSON="{}"
LAST_SCAN_COMPLETE_TIME=0

# Window tracking
declare -A LAST_ACTION_WINDOW

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - NODE-MGR: $1"
}

# --- Clock-synchronized action checker ---
should_perform_action() {
    local action_name=$1
    local interval_seconds=$2
    local offset_seconds=$3

    local NOW=$(date +%s)
    local SECONDS_INTO_INTERVAL=$((NOW % interval_seconds))

    local DIFF=$((SECONDS_INTO_INTERVAL - offset_seconds))
    DIFF=${DIFF#-}

    if [ $DIFF -le 2 ]; then
        local CURRENT_WINDOW=$((NOW / interval_seconds))

        if [ "${LAST_ACTION_WINDOW[$action_name]}" != "$CURRENT_WINDOW" ]; then
            LAST_ACTION_WINDOW[$action_name]=$CURRENT_WINDOW
            return 0
        fi
    fi

    return 1
}

get_current_freq() {
    local conf_file=$1
    grep -oP 'frequency=\K[0-9]+' "$conf_file" 2>/dev/null | head -1
}

is_in_lobby() {
    local freq_2_4=$(get_current_freq "$WPA_CONF_2_4")
    local freq_5_0=$(get_current_freq "$WPA_CONF_5_0")

    if [[ "$freq_2_4" == "$LOBBY_FREQ_2_4" && "$freq_5_0" == "$LOBBY_FREQ_5_0" ]]; then
        echo "true"
    else
        echo "false"
    fi
}

return_to_lobby() {
    log "Returning to lobby channels..."
    sed -i "s/frequency=.*/frequency=${LOBBY_FREQ_2_4}/" "$WPA_CONF_2_4"
    sed -i "s/frequency=.*/frequency=${LOBBY_FREQ_5_0}/" "$WPA_CONF_5_0"
    systemctl restart wpa_supplicant@wlan0.service
    systemctl restart wpa_supplicant@wlan1.service
    sleep 5
}

perform_scan() {
    local json_out='{"results": ['
    local first_entry=true

    for iface in "wlan0" "wlan1"; do
        local freqs_to_scan=""
        [ "$iface" == "wlan0" ] && freqs_to_scan=$SCAN_FREQS_2_4
        [ "$iface" == "wlan1" ] && freqs_to_scan=$SCAN_FREQS_5_0

        (iw dev "$iface" scan freqs $freqs_to_scan > /dev/null 2>&1) &
        SCAN_PID=$!

        for i in {1..10}; do
            kill -0 $SCAN_PID 2>/dev/null || break
            sleep 0.5
        done
        kill $SCAN_PID 2>/dev/null || true

        local survey_data=$(iw dev "$iface" survey dump 2>/dev/null)
        local scan_data=$(iw dev "$iface" scan dump 2>/dev/null)

        for freq in $freqs_to_scan; do
            local noise=$(echo "$survey_data" | awk -v f=$freq '$1=="freq:" && $2==f {getline; if ($1=="noise:") print $2}' | head -1)
            noise=${noise:--100}
            local bss_count=$(echo "$scan_data" | grep -c "freq: $freq" || echo "0")

            [ "$first_entry" = true ] && first_entry=false || json_out+=","
            json_out+="{\"channel\": ${freq}, \"noise_floor\": ${noise}, \"bss_count\": ${bss_count}}"
        done
    done

    json_out+=']}'
    echo "$json_out"
}

is_hosting_service() {
    if systemctl is-active --quiet mediamtx.service; then
        source /etc/mesh_ipv4.conf 2>/dev/null
        local CALC_OUTPUT=$(ipcalc "$IPV4_NETWORK" 2>/dev/null)
        local FIRST_IP=$(echo "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')
        local MEDIAMTX_IPV4_VIP="${FIRST_IP%.*}.$((${FIRST_IP##*.} + 1))"
        ip addr show dev "$CONTROL_IFACE" | grep -q "inet $MEDIAMTX_IPV4_VIP/" && return 0
    fi
    return 1
}

should_perform_tourguide() {
    local NOW=$(date +%s)
    local MINUTE_OF_HOUR=$(( (NOW % 3600) / 60 ))

    [ $((MINUTE_OF_HOUR % 2)) -ne 0 ] && return 1

    local SECOND_OF_MINUTE=$((NOW % 60))

    if [ $SECOND_OF_MINUTE -ge 30 ] && [ $SECOND_OF_MINUTE -lt 50 ]; then
        local CURRENT_WINDOW=$((NOW / 120))
        if [ "${LAST_ACTION_WINDOW[TOURGUIDE]}" != "$CURRENT_WINDOW" ]; then
            LAST_ACTION_WINDOW[TOURGUIDE]=$CURRENT_WINDOW
            return 0
        fi
    fi

    return 1
}

# === MAIN SETUP ===
log "Starting Mesh Node Manager."
MY_MAC=$(cat "/sys/class/net/${CONTROL_IFACE}/address")
log "Node MAC: ${MY_MAC}"

# === MAIN LOOP ===
while true; do
    NOW=$(date +%s)

    # === CHECK STATE: LOBBY OR DATA ===
    IS_IN_LOBBY=$(is_in_lobby)

    if [ "$IS_IN_LOBBY" = "true" ]; then
        # ===================================
        # === LOBBY STATE ===
        # ===================================
        HELPER_PAYLOAD=$(timeout $MONITOR_INTERVAL alfred -r $ALFRED_HELPER_TYPE 2>/dev/null | grep -oP '"\K[^"]+(?="\s*\},?)' | head -1)

        if [ -n "$HELPER_PAYLOAD" ]; then
            eval $("/usr/local/bin/decoder.py" "$HELPER_PAYLOAD" 2>/dev/null | grep "DATA_CHANNEL_")

            if [[ -n "$DATA_CHANNEL_2_4" && -n "$DATA_CHANNEL_5_0" ]]; then
                log "Helper beacon received. Migrating to data channels: 2.4=${DATA_CHANNEL_2_4}, 5=${DATA_CHANNEL_5_0}"

                sed -i "s/frequency=.*/frequency=${DATA_CHANNEL_2_4}/" "$WPA_CONF_2_4"
                sed -i "s/frequency=.*/frequency=${DATA_CHANNEL_5_0}/" "$WPA_CONF_5_0"

                systemctl restart wpa_supplicant@wlan0.service
                systemctl restart wpa_supplicant@wlan1.service
                sleep 5
            fi
        fi

    else
        # ===================================
        # === DATA CHANNEL STATE ===
        # ===================================

        # === STAGE 1: RF SCAN (every 3 min at :10) ===
        if should_perform_action "SCAN" 180 10; then
            log "=== SCAN ($(date +'%H:%M:%S')) ==="
            SCAN_REPORT_JSON=$(perform_scan)
            LAST_SCAN_COMPLETE_TIME=$NOW
            SCAN_DATA_AVAILABLE=true
            CACHED_SCAN_REPORT_JSON="$SCAN_REPORT_JSON"
        else
            SCAN_DATA_AVAILABLE=false
            SCAN_REPORT_JSON="$CACHED_SCAN_REPORT_JSON"
        fi

        # === STAGE 2: PUBLISH (every 3 min at :15) ===
        if should_perform_action "PUBLISH" 180 15; then
            log "=== PUBLISH ($(date +'%H:%M:%S')) ==="

            HOSTNAME=$(hostname)
            SYNCTHING_ID=$(runuser -u radio -- syncthing --device-id 2>/dev/null || echo "")
            TQ_AVG=$("$BATCTL_PATH" o 2>/dev/null | awk 'NR>1 {sum+=$3} END {if (NR>1) printf "%.2f", sum/(NR-1); else print 0}')

            # Service flags
            IS_GATEWAY_FLAG=$([ -f /var/run/mesh-gateway.state ] && echo "--is-internet-gateway" || echo "")
            IS_NTP_FLAG=$([ -f /var/run/mesh-ntp.state ] && echo "--is-ntp-server" || echo "")
            IS_MEDIAMTX_FLAG=$(is_hosting_service && echo "--is-mediamtx-server" || echo "")

            # Gather MACs
            ALL_MACS=("$MY_MAC")
            for iface in wlan0 wlan1 end0; do
                if [ -d "/sys/class/net/$iface" ]; then
                    MAC=$(cat "/sys/class/net/$iface/address" 2>/dev/null)
                    [ -n "$MAC" ] && ALL_MACS+=("$MAC")
                fi
            done

            CURRENT_IPV4=$(ip addr show dev "$CONTROL_IFACE" 2>/dev/null | grep -oP 'inet \K[\d.]+' | head -1)

            # Limp mode flag
            LIMP_MODE_FLAG=""
            if [ -f "$ELECTION_OUTPUT_FILE" ]; then
                LIMP_MODE_DECISION=$(grep "LIMP_MODE" "$ELECTION_OUTPUT_FILE" 2>/dev/null | cut -d'=' -f2)
                [ "$LIMP_MODE_DECISION" == "true" ] && LIMP_MODE_FLAG="--is-in-limp-mode"
            fi

            # Load tourguide state
            LAST_TOURGUIDE_TIME=0
            LAST_TOURGUIDE_RADIO=""
            [ -f /var/run/tourguide_state ] && source /var/run/tourguide_state

            # Encode
            ENCODER_ARGS=(
                "--hostname" "$HOSTNAME"
                "--mac-addresses" "${ALL_MACS[@]}"
                "--tq-average" "$TQ_AVG"
                "--syncthing-id" "$SYNCTHING_ID"
                "--channel-report-json" "$SCAN_REPORT_JSON"
                "--timestamp" "$NOW"
                "--last-tourguide-timestamp" "$LAST_TOURGUIDE_TIME"
                "--last-tourguide-radio" "$LAST_TOURGUIDE_RADIO"
            )
            [ -n "$CURRENT_IPV4" ] && ENCODER_ARGS+=("--ipv4-address" "$CURRENT_IPV4")
            [ -n "$IS_GATEWAY_FLAG" ] && ENCODER_ARGS+=("$IS_GATEWAY_FLAG")
            [ -n "$IS_NTP_FLAG" ] && ENCODER_ARGS+=("$IS_NTP_FLAG")
            [ -n "$IS_MEDIAMTX_FLAG" ] && ENCODER_ARGS+=("$IS_MEDIAMTX_FLAG")
            [ -n "$LIMP_MODE_FLAG" ] && ENCODER_ARGS+=("$LIMP_MODE_FLAG")

            CURRENT_PAYLOAD=$("$ENCODER_PATH" "${ENCODER_ARGS[@]}" 2>/dev/null)

            if [ -n "$CURRENT_PAYLOAD" ]; then
                echo -n "$CURRENT_PAYLOAD" | alfred -s $ALFRED_DATA_TYPE
                LAST_PUBLISHED_PAYLOAD="$CURRENT_PAYLOAD"
                LAST_PUBLISH_TIME=$NOW
            fi
        fi

        # === STAGE 3: REGISTRY BUILD (every 3 min at :20) ===
        if should_perform_action "REGISTRY" 180 20; then
            log "=== REGISTRY BUILD ($(date +'%H:%M:%S')) ==="
            [ -x "$REGISTRY_BUILDER" ] && "$REGISTRY_BUILDER"
        fi

        # === STAGE 4: CHANNEL ELECTION (every 3 min at :25) ===
        if should_perform_action "ELECTION" 180 25; then
            log "=== CHANNEL ELECTION ($(date +'%H:%M:%S')) ==="
            [ -x "$CHANNEL_ELECTION" ] && "$CHANNEL_ELECTION"
        fi

        # === STAGE 5: IP MANAGEMENT ===
        [ -x "$IP_MANAGER" ] && "$IP_MANAGER"

        # === STAGE 6: QUORUM CHECK ===
        if [ -x "$QUORUM_CHECKER" ]; then
            if ! "$QUORUM_CHECKER"; then
                log "Quorum check failed. Returning to lobby."
                return_to_lobby
                continue
            fi
        fi

        # === STAGE 7: TOURGUIDE (every 2 min at :30) ===
        if should_perform_tourguide; then
            log "=== TOURGUIDE WINDOW ($(date +'%H:%M:%S')) ==="
            [ -x "$TOURGUIDE_MANAGER" ] && "$TOURGUIDE_MANAGER" &
        fi

        # === STAGE 8: LIMP MODE MANAGEMENT ===
        [ -x "$LIMP_MODE_MANAGER" ] && "$LIMP_MODE_MANAGER"

        # === STAGE 9: OTHER ELECTIONS ===
        for election_script in /usr/local/bin/*-election.sh; do
            if [[ -f "$election_script" && -x "$election_script" && "$election_script" != "$CHANNEL_ELECTION" ]]; then
                "$election_script" &
            fi
        done

    fi  # End of data channel state

    sleep "$MONITOR_INTERVAL"
done

log "Main loop exited unexpectedly. Restarting..."
exit 1
