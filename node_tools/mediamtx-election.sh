#!/bin/bash
#
# mediamtx-election.sh
# This script runs an election based on mesh centrality (TQ) to determine
# which node should host the MediaMTX service. It assigns static VIPs
# (IPv4 and IPv6), updates the config, and manages the service.
#
# Improvements:
# - 50% TQ threshold to prevent flapping between similar nodes
# - Uses is_mediamtx_server flag in registry to identify current leader
# - Only restarts service when taking over leadership
# - Improved IPv6 VIP detection

# --- Configuration ---
REGISTRY_STATE_FILE="/var/run/mesh_node_registry"
MEDIAMTX_CONFIG_FILE="/etc/mediamtx/mediamtx.yml"
MEDIAMTX_SERVICE_NAME="mediamtx.service"
CONTROL_IFACE="br0"
MY_MAC=$(cat "/sys/class/net/${CONTROL_IFACE}/address")
MTX_IPV6_SCRIPT="/usr/local/bin/mtx-ip.sh"

# Threshold: challenger must have 50% better TQ than current leader
TQ_THRESHOLD_MULTIPLIER="1.5"

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
source /etc/mesh_ipv4.conf # Provides IPV4_NETWORK
MEDIAMTX_IPV4_VIP=$(get_mediamtx_ipv4_vip "$IPV4_NETWORK")
MEDIAMTX_IPV6_VIP_WITH_MASK=$("$MTX_IPV6_SCRIPT") # e.g., fd5a:..::64/128
MEDIAMTX_IPV6_VIP=${MEDIAMTX_IPV6_VIP_WITH_MASK%/*} # Just the address part

if [ -z "$MEDIAMTX_IPV4_VIP" ] || [ -z "$MEDIAMTX_IPV6_VIP" ]; then
    log "Error: Could not determine valid IPv4 or IPv6 VIPs. Exiting."
    exit 1
fi
IPV4_VIP_WITH_MASK="${MEDIAMTX_IPV4_VIP}/${IPV4_NETWORK#*/}"

# --- Check if we currently have the VIPs (are we the current leader?) ---
HAS_IPV4_VIP=false
HAS_IPV6_VIP=false

if ip addr show dev "$CONTROL_IFACE" | grep -q "inet $MEDIAMTX_IPV4_VIP/"; then
    HAS_IPV4_VIP=true
fi

if ip -6 addr show dev "$CONTROL_IFACE" | grep -qw "$MEDIAMTX_IPV6_VIP"; then
    HAS_IPV6_VIP=true
fi

I_AM_CURRENT_LEADER=false
if [ "$HAS_IPV4_VIP" = true ] && [ "$HAS_IPV6_VIP" = true ]; then
    I_AM_CURRENT_LEADER=true
    log "Currently holding leadership (have VIPs)"
fi

# --- Discover current leader from registry ---
CURRENT_LEADER_MAC=""
CURRENT_LEADER_TQ="0"

# Find the node advertising is_mediamtx_server=true
while read server_line; do
    # server_line will be like: NODE_..._IS_MEDIAMTX_SERVER='true'
    server_varname=$(echo "$server_line" | cut -d'=' -f1)
    server_value=$(echo "$server_line" | cut -d'=' -f2 | tr -d "'")
    
    if [ "$server_value" = "true" ]; then
        # Extract the sanitized MAC part
        MAC_SANITIZED=$(echo "$server_varname" | sed -n 's/NODE_\([0-9a-fA-F]\+\)_IS_MEDIAMTX_SERVER/\1/p')
        
        if [ -n "$MAC_SANITIZED" ]; then
            # Get the MAC address
            MAC_VAR="NODE_${MAC_SANITIZED}_MAC_ADDRESS"
            MAC_LINE=$(grep "^${MAC_VAR}=" "$REGISTRY_STATE_FILE")
            
            if [ -n "$MAC_LINE" ]; then
                CURRENT_LEADER_MAC=$(echo "$MAC_LINE" | cut -d'=' -f2 | tr -d "'")
                
                # Get the TQ for this node
                TQ_VAR="NODE_${MAC_SANITIZED}_TQ_AVERAGE"
                TQ_LINE=$(grep "^${TQ_VAR}=" "$REGISTRY_STATE_FILE")
                
                if [ -n "$TQ_LINE" ]; then
                    CURRENT_LEADER_TQ=$(echo "$TQ_LINE" | cut -d'=' -f2 | tr -d "'")
                    log "Found current leader in registry: $CURRENT_LEADER_MAC (TQ: $CURRENT_LEADER_TQ)"
                fi
                break
            fi
        fi
    fi
done < <(grep 'NODE_.*_IS_MEDIAMTX_SERVER=' "$REGISTRY_STATE_FILE")

# --- Run Election ---
log "Running MediaMTX election..."

BEST_CANDIDATE_MAC=""
HIGHEST_TQ="-1"
MY_TQ="-1"

# Read TQ values directly from the registry file
while read tq_line; do
    tq_varname=$(echo "$tq_line" | cut -d'=' -f1)
    CURRENT_TQ=$(echo "$tq_line" | cut -d'=' -f2 | tr -d "'")
    
    MAC_SANITIZED=$(echo "$tq_varname" | sed -n 's/NODE_\([0-9a-fA-F]\+\)_TQ_AVERAGE/\1/p')
    
    if [ -n "$MAC_SANITIZED" ]; then
        MAC_VAR="NODE_${MAC_SANITIZED}_MAC_ADDRESS"
        MAC_LINE=$(grep "^${MAC_VAR}=" "$REGISTRY_STATE_FILE")
        
        if [ -n "$MAC_LINE" ]; then
            CURRENT_MAC=$(echo "$MAC_LINE" | cut -d'=' -f2 | tr -d "'")
            
            # Track our own TQ
            if [ "$CURRENT_MAC" = "$MY_MAC" ]; then
                MY_TQ="$CURRENT_TQ"
            fi
            
            # Find highest TQ
            if (( $(echo "$CURRENT_TQ > $HIGHEST_TQ" | bc -l) )); then
                HIGHEST_TQ=$CURRENT_TQ
                BEST_CANDIDATE_MAC=$CURRENT_MAC
            # Tie-breaker (lower MAC wins)
            elif (( $(echo "$CURRENT_TQ == $HIGHEST_TQ" | bc -l) )) && [[ "$CURRENT_MAC" < "$BEST_CANDIDATE_MAC" ]]; then
                BEST_CANDIDATE_MAC=$CURRENT_MAC
            fi
        fi
    fi
done < <(grep 'NODE_.*_TQ_AVERAGE=' "$REGISTRY_STATE_FILE")

# --- Apply threshold logic ---
# If there's a current leader and they're not us, apply 50% threshold
WINNER_MAC=""

if [ -n "$CURRENT_LEADER_MAC" ] && [ "$CURRENT_LEADER_MAC" != "$MY_MAC" ]; then
    # There's an existing leader (not us). Challenger needs 50% better TQ
    REQUIRED_TQ=$(echo "$CURRENT_LEADER_TQ * $TQ_THRESHOLD_MULTIPLIER" | bc -l)
    
    if (( $(echo "$HIGHEST_TQ >= $REQUIRED_TQ" | bc -l) )); then
        WINNER_MAC="$BEST_CANDIDATE_MAC"
        log "Challenger $BEST_CANDIDATE_MAC (TQ: $HIGHEST_TQ) exceeds threshold ($REQUIRED_TQ) to take over from $CURRENT_LEADER_MAC (TQ: $CURRENT_LEADER_TQ)"
    else
        # Current leader retains position due to threshold
        WINNER_MAC="$CURRENT_LEADER_MAC"
        log "Current leader $CURRENT_LEADER_MAC (TQ: $CURRENT_LEADER_TQ) retains position. Best challenger: $BEST_CANDIDATE_MAC (TQ: $HIGHEST_TQ, needs: $REQUIRED_TQ)"
    fi
elif [ "$I_AM_CURRENT_LEADER" = true ]; then
    # We're the current leader, check if someone beat us by 50%
    REQUIRED_TQ=$(echo "$MY_TQ * $TQ_THRESHOLD_MULTIPLIER" | bc -l)
    
    if [ "$BEST_CANDIDATE_MAC" != "$MY_MAC" ] && (( $(echo "$HIGHEST_TQ >= $REQUIRED_TQ" | bc -l) )); then
        WINNER_MAC="$BEST_CANDIDATE_MAC"
        log "Challenger $BEST_CANDIDATE_MAC (TQ: $HIGHEST_TQ) exceeds threshold ($REQUIRED_TQ) to take over from us (TQ: $MY_TQ)"
    else
        WINNER_MAC="$MY_MAC"
        if [ "$BEST_CANDIDATE_MAC" != "$MY_MAC" ]; then
            log "Retaining leadership (our TQ: $MY_TQ). Best challenger: $BEST_CANDIDATE_MAC (TQ: $HIGHEST_TQ, needs: $REQUIRED_TQ)"
        else
            log "Retaining leadership (our TQ: $MY_TQ). No other candidates."
        fi
    fi
else
    # No current leader exists, highest TQ wins
    WINNER_MAC="$BEST_CANDIDATE_MAC"
    log "No current leader. Highest TQ wins: $BEST_CANDIDATE_MAC (TQ: $HIGHEST_TQ)"
fi

# --- Decide and Act ---
if [ -z "$WINNER_MAC" ]; then
    log "No suitable winner determined."
    # Ensure we're not running if we shouldn't be
    if [ "$I_AM_CURRENT_LEADER" = true ]; then
        log "Removing VIPs and stopping service (no valid winner)."
        ip addr del "$IPV4_VIP_WITH_MASK" dev "$CONTROL_IFACE" 2>/dev/null
        ip addr del "$MEDIAMTX_IPV6_VIP_WITH_MASK" dev "$CONTROL_IFACE" 2>/dev/null
        systemctl stop "$MEDIAMTX_SERVICE_NAME" 2>/dev/null
    fi
    exit 0
fi

if [ "$WINNER_MAC" = "$MY_MAC" ]; then
    # --- I AM THE LEADER ---
    if [ "$I_AM_CURRENT_LEADER" = true ]; then
        # Already leader with VIPs, check service status
        if systemctl is-active --quiet "$MEDIAMTX_SERVICE_NAME"; then
            log "Already leader and service running. No action needed."
        else
            log "Already leader but service not running. Starting service."
            systemctl start "$MEDIAMTX_SERVICE_NAME"
        fi
    else
        # Taking over leadership
        log "Won election. Taking over leadership (TQ: $MY_TQ)."
        
        # Assign IPv4 VIP
        if [ "$HAS_IPV4_VIP" = false ]; then
            log "Assigning IPv4 VIP: $MEDIAMTX_IPV4_VIP"
            ip addr add "$IPV4_VIP_WITH_MASK" dev "$CONTROL_IFACE"
            
            # Send Gratuitous ARP
            if command -v arping &> /dev/null; then
                log "Sending Gratuitous ARP for $MEDIAMTX_IPV4_VIP"
                arping -c 1 -A -I "$CONTROL_IFACE" "$MEDIAMTX_IPV4_VIP" 2>/dev/null &
            fi
        fi
        
        # Assign IPv6 VIP
        if [ "$HAS_IPV6_VIP" = false ]; then
            log "Assigning IPv6 VIP: $MEDIAMTX_IPV6_VIP"
            ip addr add "$MEDIAMTX_IPV6_VIP_WITH_MASK" dev "$CONTROL_IFACE" 2>/dev/null
        fi
        
        # Update config
        if command -v yq &> /dev/null; then
            log "Updating $MEDIAMTX_CONFIG_FILE listen addresses..."
            yq -i ".rtspAddress = \"$MEDIAMTX_IPV4_VIP:8554,$MEDIAMTX_IPV6_VIP:8554\"" "$MEDIAMTX_CONFIG_FILE"
            yq -i ".webrtcAddress = \"$MEDIAMTX_IPV4_VIP:8889,$MEDIAMTX_IPV6_VIP:8889\"" "$MEDIAMTX_CONFIG_FILE"
        else
            log "Warning: 'yq' not found. Cannot update $MEDIAMTX_CONFIG_FILE."
        fi
        
        # Start/restart service
        log "Starting $MEDIAMTX_SERVICE_NAME..."
        systemctl restart "$MEDIAMTX_SERVICE_NAME"
    fi
    
else
    # --- I AM NOT THE LEADER ---
    if [ "$I_AM_CURRENT_LEADER" = true ]; then
        log "Lost leadership to $WINNER_MAC. Stepping down."
        
        # Remove VIPs with error checking
        if ip addr show dev "$CONTROL_IFACE" | grep -q "inet $MEDIAMTX_IPV4_VIP/"; then
            log "Removing IPv4 VIP $MEDIAMTX_IPV4_VIP"
            if ip addr del "$IPV4_VIP_WITH_MASK" dev "$CONTROL_IFACE" 2>&1; then
                log "IPv4 VIP removed successfully"
            else
                log "ERROR: Failed to remove IPv4 VIP"
            fi
        fi
        
        if ip -6 addr show dev "$CONTROL_IFACE" | grep -qw "$MEDIAMTX_IPV6_VIP"; then
            log "Removing IPv6 VIP $MEDIAMTX_IPV6_VIP"
            if ip addr del "$MEDIAMTX_IPV6_VIP_WITH_MASK" dev "$CONTROL_IFACE" 2>&1; then
                log "IPv6 VIP removed successfully"
            else
                log "ERROR: Failed to remove IPv6 VIP"
            fi
        fi
        
        # Stop service
        if systemctl is-active --quiet "$MEDIAMTX_SERVICE_NAME"; then
            log "Stopping $MEDIAMTX_SERVICE_NAME"
            systemctl stop "$MEDIAMTX_SERVICE_NAME"
        fi
    else
        log "Not leader. Current leader: $WINNER_MAC"
        
        # Sanity check - if we somehow have the VIPs but shouldn't, remove them
        if [ "$HAS_IPV4_VIP" = true ] || [ "$HAS_IPV6_VIP" = true ]; then
            log "WARNING: Not leader but have VIPs assigned. Removing them."
            [ "$HAS_IPV4_VIP" = true ] && ip addr del "$IPV4_VIP_WITH_MASK" dev "$CONTROL_IFACE" 2>/dev/null
            [ "$HAS_IPV6_VIP" = true ] && ip addr del "$MEDIAMTX_IPV6_VIP_WITH_MASK" dev "$CONTROL_IFACE" 2>/dev/null
            systemctl stop "$MEDIAMTX_SERVICE_NAME" 2>/dev/null
        fi
    fi
fi

log "Election check complete."
