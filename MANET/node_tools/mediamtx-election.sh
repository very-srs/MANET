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
MY_MAC=$(cat "/sys/class/net/${CONTROL_IFACE}/address" 2>/dev/null || true)
MTX_IPV6_SCRIPT="/usr/local/bin/mtx-ip.sh"

# Incumbent bias: current leader gets this many TQ points added to their score.
# Prevents service migration due to normal TQ fluctuation.

log() {
    printf '%s\n' "MEDIAMTX-ELECTION: $1" | systemd-cat -t mediamtx-election
}

# Function to get the second usable IP address from the CIDR (our reserved VIP)
get_mediamtx_ipv4_vip() {
    local CIDR="$1"
    local CALC_OUTPUT
    CALC_OUTPUT=$(manet-ipcalc.sh "$CIDR" 2>/dev/null)
    if [ -z "$CALC_OUTPUT" ]; then
        echo "no CIDR supplied"
        return 1
    fi

    # Get the first usable IP (HostMin)
    local FIRST_IP=$(printf '%s\n' "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')

    # Increment the last octet to get the second IP
    printf '%s\n' "${FIRST_IP%.*}.$((${FIRST_IP##*.} + 1))"
}

# --- Single run at a time ---
# The node manager starts elections in the background; an overlapping run could
# add and remove the VIP or restart the service underneath this one.
LOCK_FILE="${MEDIAMTX_ELECTION_LOCK:-/var/run/mediamtx-election.lock}"
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    log "Election already in progress, exiting"
    exit 0
fi

# Reuse the existing Python process before computing VIPs or spawning the
# one-shot ranker. It rechecks age, addresses and service state every request.
. "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet-runtime-client.sh"
ELECTION_RESULT=""
RUNTIME_RC=0
ELECTION_RESULT=$(manet_runtime_call election mediamtx) || RUNTIME_RC=$?
if [ "$RUNTIME_RC" = 0 ]; then
    [ "$ELECTION_RESULT" != skip ] || exit 0
elif [ "$RUNTIME_RC" != 125 ]; then
    log "Runtime election check failed; retrying next pass"
    exit "$RUNTIME_RC"
fi

# --- Check Dependencies ---
if [ -z "$MY_MAC" ]; then
    log "Cannot determine local MAC address (${CONTROL_IFACE} not up). Exiting."
    exit 1
fi
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
MEDIAMTX_IPV6_VIP=$(printf '%s\n' "$MEDIAMTX_IPV6_VIP" | sed 's/:0000::/::/g')
MEDIAMTX_IPV6_VIP_WITH_MASK="${MEDIAMTX_IPV6_VIP}/128"


if [ -z "$MEDIAMTX_IPV4_VIP" ] || [ -z "$MEDIAMTX_IPV6_VIP" ]; then
    log "Error: Could not determine valid IPv4 or IPv6 VIPs. Exiting."
    exit 1
fi
IPV4_VIP_WITH_MASK="${MEDIAMTX_IPV4_VIP}/${IPV4_NETWORK#*/}"

# --- Rank candidates ---
# mesh-service-election.py reads one registry snapshot and applies the shared
# rules: eligibility (state, observed age, valid metric), one deterministic
# incumbent with its +10 bias, highest score wins, lowest MAC breaks a tie.
ELECTION_HELPER="${MANET_TOOLS_DIR:-/usr/local/bin}/mesh-service-election.py"
if [ -z "$ELECTION_RESULT" ] && ! ELECTION_RESULT=$(python3 "$ELECTION_HELPER" mediamtx "$REGISTRY_STATE_FILE" 2> >(while IFS= read -r line; do log "$line"; done)); then
    log "Cannot rank candidates; leaving the service as it is"
    exit 1
fi
read -r BEST_CANDIDATE_MAC HIGHEST_TQ CURRENT_LEADER_MAC <<< "$ELECTION_RESULT"
[ "$BEST_CANDIDATE_MAC" = - ] && BEST_CANDIDATE_MAC=""
[ "$CURRENT_LEADER_MAC" = - ] && CURRENT_LEADER_MAC=""
if [ -n "$CURRENT_LEADER_MAC" ]; then
    log "Current incumbent: $CURRENT_LEADER_MAC (incumbent bias applied)"
fi

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
