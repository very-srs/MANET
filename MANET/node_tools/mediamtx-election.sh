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

# Incumbent bias: current leader gets this many TQ points added to their score.
# Prevents service migration due to normal TQ fluctuation.
INCUMBENT_BIAS=10

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - MEDIAMTX-ELECTION: $1" | systemd-cat -t mediamtx-election
}

# Function to get the second usable IP address from the CIDR (our reserved VIP)
get_mediamtx_ipv4_vip() {
    local CIDR="$1"
    local CALC_OUTPUT
    CALC_OUTPUT=$(ipcalc "$CIDR" 2>/dev/null)
    if [ -z "$CALC_OUTPUT" ]; then
        echo "no CIDR supplied"
        return 1
    fi

    # Get the first usable IP (HostMin)
    local FIRST_IP=$(echo "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')

    # Increment the last octet to get the second IP
    echo "${FIRST_IP%.*}.$((${FIRST_IP##*.} + 1))"
}

# --- Check Dependencies ---
if [ ! -f "$REGISTRY_STATE_FILE" ]; then
    log "Registry file not found ($REGISTRY_STATE_FILE). Exiting."
    exit 1
fi

# --- Determine the Static VIPs ---
# Source the IPv4 network range
IPV4_NETWORK=$(grep "^ipv4_network=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)
if [ -z "$IPV4_NETWORK" ]; then
    log "Error: ipv4_network not found in /etc/mesh.conf"
    exit 1
fi
MEDIAMTX_IPV4_VIP=$(get_mediamtx_ipv4_vip "$IPV4_NETWORK")
MEDIAMTX_IPV6_VIP_WITH_MASK=$("$MTX_IPV6_SCRIPT") # e.g., fd5a:..::64/128
MEDIAMTX_IPV6_VIP=${MEDIAMTX_IPV6_VIP_WITH_MASK%/*} # Just the address part

#Normalize IPv6 to compressed form (remove :0000: before ::) ***
MEDIAMTX_IPV6_VIP=$(echo "$MEDIAMTX_IPV6_VIP" | sed 's/:0000::/::/g')
MEDIAMTX_IPV6_VIP_WITH_MASK="${MEDIAMTX_IPV6_VIP}/128"


if [ -z "$MEDIAMTX_IPV4_VIP" ] || [ -z "$MEDIAMTX_IPV6_VIP" ]; then
    log "Error: Could not determine valid IPv4 or IPv6 VIPs. Exiting."
    exit 1
fi
IPV4_VIP_WITH_MASK="${MEDIAMTX_IPV4_VIP}/${IPV4_NETWORK#*/}"

# --- Detect Current Incumbent ---
# Use the Alfred node registry as the authoritative source for incumbency.
# node-manager publishes IS_MEDIAMTX_SERVER=true only when the local node holds
# both the mediamtx service AND the MediaMTX VIP — all nodes read the same
# Alfred-propagated registry, so there is no per-node ARP/local-IP ambiguity.
CURRENT_LEADER_MAC=""
if [ -f "$REGISTRY_STATE_FILE" ]; then
    INCUMBENT_NODE_ID=$(grep "IS_MEDIAMTX_SERVER='true'" "$REGISTRY_STATE_FILE" \
        | head -1 | sed "s/NODE_\([^_]*\)_.*/\1/")
    if [ -n "$INCUMBENT_NODE_ID" ]; then
        INCUMBENT_MAC=$(grep "^NODE_${INCUMBENT_NODE_ID}_MAC_ADDRESS=" "$REGISTRY_STATE_FILE" \
            | cut -d"'" -f2)
        [ -n "$INCUMBENT_MAC" ] && CURRENT_LEADER_MAC="$INCUMBENT_MAC"
    fi
fi
if [ -n "$CURRENT_LEADER_MAC" ]; then
    log "Current incumbent: $CURRENT_LEADER_MAC (bias: +${INCUMBENT_BIAS} TQ)"
fi

# --- Run Election ---
log "Running MediaMTX election..."

BEST_CANDIDATE_MAC=""
HIGHEST_TQ="-1"
NOW=$(date +%s)
STALE_THRESHOLD=600 # 10 minutes (must match node-manager)

# Read TQ values directly from the registry file
while read tq_line; do
    tq_varname=$(echo "$tq_line" | cut -d'=' -f1)
    CURRENT_TQ=$(echo "$tq_line" | cut -d'=' -f2 | tr -d "'")
    MAC_SANITIZED=$(echo "$tq_varname" | sed -n 's/NODE_\([0-9a-fA-F]\+\)_TQ_AVERAGE/\1/p')

    if [ -n "$MAC_SANITIZED" ]; then
        # --- Check Timestamp ---
        TIMESTAMP_VAR="NODE_${MAC_SANITIZED}_LAST_SEEN_TIMESTAMP"
        TIMESTAMP_LINE=$(grep "^${TIMESTAMP_VAR}=" "$REGISTRY_STATE_FILE")
        TIMESTAMP_VAL=$(echo "$TIMESTAMP_LINE" | cut -d'=' -f2 | tr -d "'")
        
        if [ -z "$TIMESTAMP_VAL" ] || [ $((NOW - TIMESTAMP_VAL)) -gt $STALE_THRESHOLD ]; then
            log "Skipping stale candidate $MAC_SANITIZED"
            continue # Skip to the next node
        fi
        # --- End Timestamp Check ---

        MAC_VAR="NODE_${MAC_SANITIZED}_MAC_ADDRESS"
        MAC_LINE=$(grep "^${MAC_VAR}=" "$REGISTRY_STATE_FILE")

        if [ -n "$MAC_LINE" ]; then
            # Extract the MAC address and strip the single quotes
            CURRENT_MAC=$(echo "$MAC_LINE" | cut -d'=' -f2 | tr -d "'")

            # Apply incumbent bias: current leader gets a TQ bonus to prevent
            # unnecessary service migration from normal TQ fluctuation
            EFFECTIVE_TQ="$CURRENT_TQ"
            if [ -n "$CURRENT_LEADER_MAC" ] && [ "$CURRENT_MAC" == "$CURRENT_LEADER_MAC" ]; then
                EFFECTIVE_TQ=$(echo "$CURRENT_TQ + $INCUMBENT_BIAS" | bc -l)
            fi

            if (( $(echo "$EFFECTIVE_TQ > $HIGHEST_TQ" | bc -l) )); then
                HIGHEST_TQ=$EFFECTIVE_TQ
                BEST_CANDIDATE_MAC=$CURRENT_MAC
            # Tie-breaker (lower MAC wins)
            elif (( $(echo "$EFFECTIVE_TQ == $HIGHEST_TQ" | bc -l) )) && [[ "$CURRENT_MAC" < "$BEST_CANDIDATE_MAC" ]]; then
                BEST_CANDIDATE_MAC=$CURRENT_MAC
            fi
        else
             log "Warning: Found TQ for $MAC_SANITIZED but no matching MAC_ADDRESS."
        fi
    fi
# Use process substitution to read from grep, avoiding the subshell pipe problem
done < <(grep 'NODE_.*_TQ_AVERAGE=' "$REGISTRY_STATE_FILE")

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
    systemctl reset-failed "$MEDIAMTX_SERVICE_NAME" 2>/dev/null

elif [ "$MY_MAC" == "$BEST_CANDIDATE_MAC" ]; then
    # --- I AM THE LEADER ---
    log "Won election (TQ: $HIGHEST_TQ)."

    # Check if we already have both VIPs assigned
    HAS_IPV4_VIP=false
    HAS_IPV6_VIP=false

    if ip addr show dev "$CONTROL_IFACE" | grep -q "inet $MEDIAMTX_IPV4_VIP/"; then
        HAS_IPV4_VIP=true
    fi

    if ip addr show dev "$CONTROL_IFACE" | grep -q "inet6 $MEDIAMTX_IPV6_VIP/"; then
        HAS_IPV6_VIP=true
    fi

    # Assign IPv4 VIP if not already present
    if [ "$HAS_IPV4_VIP" = false ]; then
        log "Assigning IPv4 VIP: $MEDIAMTX_IPV4_VIP"
        ip addr add "$IPV4_VIP_WITH_MASK" dev "$CONTROL_IFACE"
        # Send Gratuitous ARP
        if command -v arping &> /dev/null; then
             log "Sending Gratuitous ARP for $MEDIAMTX_IPV4_VIP"
             arping -c 1 -A -I "$CONTROL_IFACE" "$MEDIAMTX_IPV4_VIP"
        fi
    fi

    # Assign IPv6 VIP if not already present
    if [ "$HAS_IPV6_VIP" = false ]; then
         log "Assigning IPv6 VIP: $MEDIAMTX_IPV6_VIP"
         ip addr add "$MEDIAMTX_IPV6_VIP_WITH_MASK" dev "$CONTROL_IFACE" 2>/dev/null || log "IPv6 VIP already exists or failed to add"
    fi

    # Determine if we were already the leader (had both VIPs)
    if [ "$HAS_IPV4_VIP" = true ] && [ "$HAS_IPV6_VIP" = true ]; then
        WAS_ALREADY_LEADER=true
    else
        WAS_ALREADY_LEADER=false
    fi

    # Update config and start service ONLY if we weren't already the leader
    # or if the service isn't currently running (covers initial startup)
    if [ "$WAS_ALREADY_LEADER" = false ] || ! systemctl is-active --quiet "$MEDIAMTX_SERVICE_NAME"; then
        if command -v yq &> /dev/null; then
            log "Updating $MEDIAMTX_CONFIG_FILE listen addresses..."
            yq -i ".rtspAddress = \"$MEDIAMTX_IPV4_VIP:8554\"" "$MEDIAMTX_CONFIG_FILE"
            yq -i ".webrtcAddress = \"$MEDIAMTX_IPV4_VIP:8889\"" "$MEDIAMTX_CONFIG_FILE"
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
    if ip addr show dev "$CONTROL_IFACE" | grep -q "inet6 $MEDIAMTX_IPV6_VIP/"; then
        log "Removing IPv6 VIP."
        ip addr del "$MEDIAMTX_IPV6_VIP_WITH_MASK" dev "$CONTROL_IFACE" 2>/dev/null
    fi
    if systemctl is-active --quiet "$MEDIAMTX_SERVICE_NAME"; then
        log "Stopping local service."
        systemctl stop "$MEDIAMTX_SERVICE_NAME"
    fi
    systemctl reset-failed "$MEDIAMTX_SERVICE_NAME" 2>/dev/null

fi
log "Election check complete."
