#!/bin/bash
#
# mediamtx-election.sh
# This script runs an election based on mesh centrality (TQ) to determine
# which node should host the MediaMTX service. It assigns static VIPs
# (IPv4 and IPv6), updates the config, and manages the service.
#

# --- Configuration ---
REGISTRY_STATE_FILE="/var/run/mesh_node_registry"
MEDIAMTX_CONFIG_FILE="/etc/mediamtx/mediamtx.yml"
MEDIAMTX_SERVICE_NAME="mediamtx.service"
CONTROL_IFACE="br0"
MY_MAC=$(cat "/sys/class/net/${CONTROL_IFACE}/address")
MTX_IPV6_SCRIPT="/usr/local/bin/mtx-ip.sh"

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - MEDIAMTX-ELECTION: $1" | systemd-cat -t mediamtx-election
}

# Function to get the first usable IP address from the CIDR (our reserved VIP)
get_mediamtx_ipv4_vip() {
    local CIDR="$1"
    local CALC_OUTPUT
    CALC_OUTPUT=$(ipcalc "$CIDR" 2>/dev/null)
    if [ -z "$CALC_OUTPUT" ]; then
        echo ""
        return 1
    fi
    echo "$CALC_OUTPUT" | awk '/HostMin/ {print $2}'
}

# --- Check Dependencies ---
if [ ! -f "$REGISTRY_STATE_FILE" ]; then
    log "Registry file not found ($REGISTRY_STATE_FILE). Exiting."
    exit 1
fi

# --- Determine the Static VIPs ---
# Source the IPv4 network range
source /etc/mesh_ipv4.conf # Provides IPV4_NETWORK
MEDIAMTX_IPV4_VIP=$(get_mediamtx_ipv4_vip "$IPV4_NETWORK")
MEDIAMTX_IPV6_VIP_WITH_MASK=$("$MTX_IPV6_SCRIPT") # e.g., fd5a:..::64/128
MEDIAMTX_IPV6_VIP=${MEDIAMTX_IPV6_VIP_WITH_MASK%/*} # Just the address part

if [ -z "$MEDIAMTX_IPV4_VIP" ] || [ -z "$MEDIAMTX_IPV6_VIP" ]; then
    log "Error: Could not determine valid IPv4 or IPv6 VIPs. Exiting."
    exit 1
fi
IPV4_VIP_WITH_MASK="${MEDIAMTX_IPV4_VIP}/${IPV4_NETWORK#*/}"

# --- Run Election ---
log "Running MediaMTX election..."
source "$REGISTRY_STATE_FILE" > /dev/null 2>&1 # Source in subshell to avoid polluting

BEST_CANDIDATE_MAC=""
HIGHEST_TQ="-1"
PREFIX_PATTERN="NODE_([0-9a-fA-F]+)"

# Use compgen to find all TQ variables defined in the registry
compgen -A variable | grep 'NODE_.*_TQ_AVERAGE' | while read tq_varname; do
    if [[ $tq_varname =~ ${PREFIX_PATTERN}_TQ_AVERAGE ]]; then
        MAC_SANITIZED="${BASH_REMATCH[1]}"
        MAC_VAR="NODE_${MAC_SANITIZED}_MAC_ADDRESS"

        # Check if corresponding MAC variable exists (sourced from registry)
        if declare -p "$MAC_VAR" &>/dev/null; then
            CURRENT_TQ=${!tq_varname}
            CURRENT_MAC=${!MAC_VAR}

            # Compare TQ (higher is better)
            if (( $(echo "$CURRENT_TQ > $HIGHEST_TQ" | bc -l) )); then
                HIGHEST_TQ=$CURRENT_TQ
                BEST_CANDIDATE_MAC=$CURRENT_MAC
            # Tie-breaker (lower MAC wins)
            elif (( $(echo "$CURRENT_TQ == $HIGHEST_TQ" | bc -l) )) && [[ "$CURRENT_MAC" < "$BEST_CANDIDATE_MAC" ]]; then
                BEST_CANDIDATE_MAC=$CURRENT_MAC
            fi
        fi
    fi
done

# --- Decide and Act ---
if [ -z "$BEST_CANDIDATE_MAC" ]; then
    log "No suitable candidates found in registry."
    # Ensure service is stopped and VIPs removed if we previously held them
    if ip addr show dev "$CONTROL_IFACE" | grep -q "inet $MEDIAMTX_IPV4_VIP/"; then
        log "Removing IPv4 VIP."
        ip addr del "$IPV4_VIP_WITH_MASK" dev "$CONTROL_IFACE" 2>/dev/null
    fi
    if ip -6 addr show dev "$CONTROL_IFACE" | grep -q "$MEDIAMTX_IPV6_VIP/"; then
        log "Removing IPv6 VIP."
        ip addr del "$MEDIAMTX_IPV6_VIP_WITH_MASK" dev "$CONTROL_IFACE" 2>/dev/null
    fi
    if systemctl is-active --quiet "$MEDIAMTX_SERVICE_NAME"; then
        log "Stopping local service as no winner was found."
        systemctl stop "$MEDIAMTX_SERVICE_NAME"
    fi
elif [ "$MY_MAC" == "$BEST_CANDIDATE_MAC" ]; then
    # --- I AM THE LEADER ---
    log "Won election (TQ: $HIGHEST_TQ)."
    WAS_ALREADY_LEADER=true # Assume yes initially

    # Assign IPv4 VIP if not already present
    if ! ip addr show dev "$CONTROL_IFACE" | grep -q "inet $MEDIAMTX_IPV4_VIP/"; then
        WAS_ALREADY_LEADER=false
        log "Assigning IPv4 VIP: $MEDIAMTX_IPV4_VIP"
        ip addr add "$IPV4_VIP_WITH_MASK" dev "$CONTROL_IFACE"
        # Send Gratuitous ARP
        if command -v arping &> /dev/null; then
             log "Sending Gratuitous ARP for $MEDIAMTX_IPV4_VIP"
             arping -c 1 -A -I "$CONTROL_IFACE" "$MEDIAMTX_IPV4_VIP"
        fi
    fi

    # Assign IPv6 VIP if not already present
    if ! ip -6 addr show dev "$CONTROL_IFACE" | grep -q "$MEDIAMTX_IPV6_VIP/"; then
         WAS_ALREADY_LEADER=false
         log "Assigning IPv6 VIP: $MEDIAMTX_IPV6_VIP"
         ip addr add "$MEDIAMTX_IPV6_VIP_WITH_MASK" dev "$CONTROL_IFACE"
    fi

    # Update config and start service ONLY if we weren't already the leader
    # or if the service isn't currently running (covers initial startup)
    if [ "$WAS_ALREADY_LEADER" = false ] || ! systemctl is-active --quiet "$MEDIAMTX_SERVICE_NAME"; then
        if command -v yq &> /dev/null; then
            log "Updating $MEDIAMTX_CONFIG_FILE listen addresses..."
            # Update RTSP address (adjust path as needed)
            yq -i ".rtspAddress = \"$MEDIAMTX_IPV4_VIP:8554,$MEDIAMTX_IPV6_VIP:8554\"" "$MEDIAMTX_CONFIG_FILE"
            # Update WebRTC address (adjust path as needed)
            yq -i ".webrtcAddress = \"$MEDIAMTX_IPV4_VIP:8889,$MEDIAMTX_IPV6_VIP:8889\"" "$MEDIAMTX_CONFIG_FILE"
            # Add other protocols/ports as needed (e.g., SRT, RTMP)
        else
            log "Warning: 'yq' not found. Cannot update listen addresses in $MEDIAMTX_CONFIG_FILE. Service might bind incorrectly."
        fi

        log "Starting/Restarting $MEDIAMTX_SERVICE_NAME..."
        systemctl restart "$MEDIAMTX_SERVICE_NAME"
    else
        log "Already leader and service running. No action needed."
    fi

else
    # --- I AM NOT THE LEADER ---
    log "Lost election to ${BEST_CANDIDATE_MAC}."
    # Ensure service is stopped and VIPs removed if we previously held them
    if ip addr show dev "$CONTROL_IFACE" | grep -q "inet $MEDIAMTX_IPV4_VIP/"; then
        log "Removing IPv4 VIP."
        ip addr del "$IPV4_VIP_WITH_MASK" dev "$CONTROL_IFACE" 2>/dev/null
    fi
    if ip -6 addr show dev "$CONTROL_IFACE" | grep -q "$MEDIAMTX_IPV6_VIP/"; then
        log "Removing IPv6 VIP."
        ip addr del "$MEDIAMTX_IPV6_VIP_WITH_MASK" dev "$CONTROL_IFACE" 2>/dev/null
    fi
    if systemctl is-active --quiet "$MEDIAMTX_SERVICE_NAME"; then
        log "Stopping local service."
        systemctl stop "$MEDIAMTX_SERVICE_NAME"
    fi
fi

log "Election check complete."
