#!/bin/bash
# Elect a MediaMTX server by mean mesh throughput. Assign IPv4/IPv6 VIPs,
# update configuration and manage the service.

. "${MANET_TOOLS_DIR:-${BASH_SOURCE[0]%/*}}/manet-service-common.sh"
service_election_preflight mediamtx
case $? in
    0) exit 0 ;;
    125) ;;  # Unsettled, or no request submitted to an available worker.
    *) exit 1 ;;
esac

# --- Configuration ---
REGISTRY_STATE_FILE="/var/run/mesh_node_registry"
MEDIAMTX_CONFIG_FILE="/etc/mediamtx/mediamtx.yml"
MEDIAMTX_SERVICE_NAME="mediamtx.service"
CONTROL_IFACE="br0"
MY_MAC=$(cat "/sys/class/net/${CONTROL_IFACE}/address" 2>/dev/null || true)
MTX_IPV6_SCRIPT="/usr/local/bin/mtx-ip.sh"

# The shared ranker adds a 10 Mbit/s incumbent bias to reduce service churn.

# Send a prefixed diagnostic to the MediaMTX election journal tag.
log() {
    printf '%s\n' "MEDIAMTX-ELECTION: $1" | systemd-cat -t mediamtx-election
}

# Return the second usable CIDR address, reserved for MediaMTX.
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
MEDIAMTX_IPV6_VIP_WITH_MASK=$(service_election_ipv6 mediamtx \
    "$MTX_IPV6_SCRIPT") || exit 1
MEDIAMTX_IPV6_VIP=${MEDIAMTX_IPV6_VIP_WITH_MASK%/*}


if [ -z "$MEDIAMTX_IPV4_VIP" ] || [ -z "$MEDIAMTX_IPV6_VIP" ]; then
    log "Error: Could not determine valid IPv4 or IPv6 VIPs. Exiting."
    exit 1
fi
IPV4_VIP_WITH_MASK="${MEDIAMTX_IPV4_VIP}/${IPV4_NETWORK#*/}"

# Prepare wildcard listeners before either VIP is local. This also serves IPv6.
prepare_mediamtx_service() {
    yq -i '.rtspAddress = ":8554" | .webrtcAddress = ":8889"' "$MEDIAMTX_CONFIG_FILE" || return 1
    systemctl restart "$MEDIAMTX_SERVICE_NAME"
}

LOCK_FILE="${MEDIAMTX_ELECTION_LOCK:-/var/run/mediamtx-election.lock}"
SERVICE_KIND=mediamtx
SERVICE_UNIT="$MEDIAMTX_SERVICE_NAME"
SERVICE_TCP_PORTS="8554 8889"
SERVICE_UDP_PORTS=""
SERVICE_IPV6_VIP_WITH_MASK="$MEDIAMTX_IPV6_VIP_WITH_MASK"
SERVICE_PREPARE_HOOK=prepare_mediamtx_service
SERVICE_VERIFY_HOOK=true
SERVICE_RELEASE_HOOK=true
service_election_run
