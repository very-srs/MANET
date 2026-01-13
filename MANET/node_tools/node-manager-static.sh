#!/bin/bash
# ==============================================================================
# Mesh Node Manager - Static Channel Mode
# ==============================================================================
# Simplified version for static channel operation
# - No RF scanning or channel selection
# - No tourguide (all nodes on same channels always)
# - Just publishes status and manages services
# ==============================================================================

# --- Configuration ---
CONTROL_IFACE="br0"
ALFRED_DATA_TYPE=68
MONITOR_INTERVAL=15

# Static channels (lobby channels used as permanent data channels)
STATIC_FREQ_2_4=2412
STATIC_FREQ_5_0=5180

# Radio Config
WPA_CONF_2_4="/etc/wpa_supplicant/wpa_supplicant-wlan0.conf"
WPA_CONF_5_0="/etc/wpa_supplicant/wpa_supplicant-wlan1.conf"

# Helper scripts
REGISTRY_BUILDER="/usr/local/bin/mesh-registry-builder.sh"
IP_MANAGER="/usr/local/bin/mesh-ip-manager.sh"
REGISTRY_STATE_FILE="/var/run/mesh_node_registry"
ENCODER_PATH="/usr/local/bin/encoder.py"
BATCTL_PATH="/usr/sbin/batctl"

# --- State Variables ---
LAST_PUBLISHED_PAYLOAD=""
LAST_PUBLISH_TIME=0
PUBLISH_INTERVAL=180  # Publish every 3 minutes

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - NODE-MGR-STATIC: $1" >&2
}

get_current_freq() {
    local conf_file=$1
    grep -oP 'frequency=\K[0-9]+' "$conf_file" 2>/dev/null | head -1
}

ensure_static_channels() {
    local freq_2_4=$(get_current_freq "$WPA_CONF_2_4")
    local freq_5_0=$(get_current_freq "$WPA_CONF_5_0")
    
    local needs_restart=false
    
    if [ "$freq_2_4" != "$STATIC_FREQ_2_4" ]; then
        log "Correcting 2.4 GHz to static channel $STATIC_FREQ_2_4"
        sed -i "s/frequency=.*/frequency=${STATIC_FREQ_2_4}/" "$WPA_CONF_2_4"
        needs_restart=true
    fi
    
    if [ "$freq_5_0" != "$STATIC_FREQ_5_0" ]; then
        log "Correcting 5 GHz to static channel $STATIC_FREQ_5_0"
        sed -i "s/frequency=.*/frequency=${STATIC_FREQ_5_0}/" "$WPA_CONF_5_0"
        needs_restart=true
    fi
    
    if [ "$needs_restart" = true ]; then
        log "Restarting wpa_supplicant services..."
        systemctl restart wpa_supplicant@wlan0.service
        systemctl restart wpa_supplicant@wlan1.service
        sleep 5
    fi
}

is_hosting_service() {
	if systemctl is-active --quiet mediamtx.service; then

	# Source the network configuration
	if [ -f /etc/mesh.conf ]; then
	    # Parse the config file
	    while IFS='=' read -r key value; do
	        [[ -z "$key" || "$key" =~ ^[[:space:]]*# ]] && continue
	        case "$key" in
	            ipv4_network)
	                IPV4_NETWORK="$value"
	                ;;
	        esac
	    done < /etc/mesh.conf
	fi

        local CALC_OUTPUT=$(ipcalc "$IPV4_NETWORK" 2>/dev/null)
        local FIRST_IP=$(echo "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')
        local MEDIAMTX_IPV4_VIP="${FIRST_IP%.*}.$((${FIRST_IP##*.} + 1))"
        ip addr show dev "$CONTROL_IFACE" | grep -q "inet $MEDIAMTX_IPV4_VIP/" && return 0
    fi
    return 1
}

# === MAIN SETUP ===
log "Starting Mesh Node Manager (Static Channel Mode)."
log "Static channels: 2.4G=${STATIC_FREQ_2_4}, 5G=${STATIC_FREQ_5_0}"
MY_MAC=$(cat "/sys/class/net/${CONTROL_IFACE}/address")
log "Node MAC: ${MY_MAC}"

# Ensure we're on static channels at startup
ensure_static_channels

# === MAIN LOOP ===
while true; do
    NOW=$(date +%s)

    # Load current chunk assignment from IP manager
    MY_CHUNK=0
    if [ -f /var/run/my_ipv4_chunk ]; then
        MY_CHUNK=$(cat /var/run/my_ipv4_chunk)
    fi
    # === PERIODIC CHANNEL CHECK ===
    # Verify we haven't drifted from static channels (safety check)
    ensure_static_channels
    
    # === REGISTRY BUILD ===
    [ -x "$REGISTRY_BUILDER" ] && "$REGISTRY_BUILDER"
    
    # === IP MANAGEMENT ===
    [ -x "$IP_MANAGER" ] && "$IP_MANAGER"
    
    # === PUBLISH STATUS ===
    time_since_publish=$((NOW - LAST_PUBLISH_TIME))
    
    if [ $time_since_publish -ge $PUBLISH_INTERVAL ]; then
        log "Publishing status to Alfred..."
        
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
        
        # Encode (no scan data, no limp mode, no tourguide in static mode)
        ENCODER_ARGS=(
            "--hostname" "$HOSTNAME"
            "--mac-addresses" "${ALL_MACS[@]}"
            "--tq-average" "$TQ_AVG"
            "--syncthing-id" "$SYNCTHING_ID"
            "--ipv4-chunk" "$MY_CHUNK"
            "--timestamp" "$NOW"
            "--data-channel-2-4" "$STATIC_FREQ_2_4"
            "--data-channel-5-0" "$STATIC_FREQ_5_0"
        )
        [ -n "$CURRENT_IPV4" ] && ENCODER_ARGS+=("--ipv4-address" "$CURRENT_IPV4")
        [ -n "$IS_GATEWAY_FLAG" ] && ENCODER_ARGS+=("$IS_GATEWAY_FLAG")
        [ -n "$IS_NTP_FLAG" ] && ENCODER_ARGS+=("$IS_NTP_FLAG")
        [ -n "$IS_MEDIAMTX_FLAG" ] && ENCODER_ARGS+=("$IS_MEDIAMTX_FLAG")
        
        CURRENT_PAYLOAD=$("$ENCODER_PATH" "${ENCODER_ARGS[@]}" 2>/dev/null)
        
        if [ -n "$CURRENT_PAYLOAD" ]; then
            echo -n "$CURRENT_PAYLOAD" | alfred -s $ALFRED_DATA_TYPE
            LAST_PUBLISHED_PAYLOAD="$CURRENT_PAYLOAD"
            LAST_PUBLISH_TIME=$NOW
        fi
    fi
	# === RUN SERVICE ELECTIONS ===
	for election_script in /usr/local/bin/*-election.sh; do
	    if [[ -f "$election_script" && -x "$election_script" ]]; then
	        # Skip channel-election.sh since we're static
	        [[ "$election_script" =~ channel-election ]] && continue
	        # Skip mediamtx-election.sh if MTX not enabled
	        if [[ "$election_script" =~ mediamtx-election ]]; then
	            MTX_ENABLED=$(grep "^mtx=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)
	            [[ "$MTX_ENABLED" != "y" ]] && continue
	        fi
	        if [[ "$election_script" =~ mumble-election ]]; then
	            MUMBLE_ENABLED=$(grep "^mumble=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)
	            [[ "$MUMBLE_ENABLED" != "y" ]] && continue
	        fi
	        "$election_script" &    # ← NOW runs only if checks pass
	    fi
	done
    sleep "$MONITOR_INTERVAL"
done

log "Main loop exited unexpectedly. Restarting..."
exit 1
