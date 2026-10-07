#!/bin/bash
# Mesh IP Manager - Chunk-Based Allocation with Bridged EUD Architecture
# This script manages IPv4 address claiming using a chunk-based approach where
# each node claims a contiguous block of IPs for itself and its EUDs.
#
# Subnet IP Allocation Scheme:
#   IPs 1-5:    Reserved for mesh services (MediaMTX, Mumble, NTP, etc.)
#   IPs 6+:     Allocated in chunks
#
# Chunk Structure (example with max_euds=5):
#   Chunk size = max_euds + 2
#   - First IP in chunk: br0 primary (mesh interface)
#   - Second IP in chunk: br0 secondary (DHCP gateway for EUDs)
#   - Remaining IPs in chunk: DHCP pool for EUDs
#
# Bridged Architecture:
#   - All EUD interfaces (wlan1 when AP, end0 when wired) are bridged to br0
#   - nftables isolates DHCP at bat0, the mesh-facing br0 port
#   - dnsmasq listens on br0 for DHCP requests from EUDs
#   - Multicast works at L2 (bridge), no routing needed
#
# wlan1 Dual Purpose:
#   - When AP: wlan1 enslaved to br0 (not in bat0), DHCP allowed
#   - When mesh: wlan1 enslaved to bat0, DHCP blocked
#

# --- Configuration ---
# Request the existing agreement process; fall back to a one-shot when absent.
# Both share discovery, registry, isolation and address checks, calling back
# here only for reconciliation.
if [ "${MANET_IP_CHECKED:-0}" != 1 ]; then
    . "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet-runtime-client.sh"
    _runtime_rc=0
    manet_runtime_call ip || _runtime_rc=$?
    [ "$_runtime_rc" = 125 ] || exit "$_runtime_rc"
    exec python3 "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet_ip_runtime.py"
fi
CONTROL_IFACE="br0"
CLAIMED_CHUNKS_FILE="/tmp/claimed_chunks.txt"
PERSISTENT_STATE_FILE="/etc/mesh_ipv4_state"
STARTUP_HELPER="${MESH_IP_STARTUP_HELPER:-/usr/local/bin/mesh-ip-startup.py}"

# Source the network configuration
MAX_EUDS=1
IPV4_NETWORK=""

if [ -f /etc/mesh.conf ]; then
    while IFS='=' read -r key value; do
        [[ -z "$key" || "$key" =~ ^[[:space:]]*# ]] && continue
        case "$key" in
            max_euds_per_node) MAX_EUDS="$value" ;;
            ipv4_network)      IPV4_NETWORK="$value" ;;
            eud)               EUD_MODE="$value" ;;
        esac
    done < /etc/mesh.conf
fi
EUD_MODE=${EUD_MODE:-"none"}

# Calculate service VIPs from ipv4_network: same formula as election scripts
# MTX VIP = HostMin+1, Mumble VIP = HostMin+2
MTX_VIP=""
MUMBLE_VIP=""
_calc_service_vips() {
    local calc host_min
    calc=$(manet-ipcalc.sh "$IPV4_NETWORK" 2>/dev/null) || return 0
    host_min=$(echo "$calc" | awk '/HostMin/ {print $2}')
    [ -n "$host_min" ] || return 0
    MTX_VIP="${host_min%.*}.$((${host_min##*.} + 1))"
    MUMBLE_VIP="${host_min%.*}.$((${host_min##*.} + 2))"
}
_calc_service_vips

# If any EUD mode is active, we need at least 1 EUD IP
if [[ "$EUD_MODE" != "none" && "$MAX_EUDS" -lt 1 ]]; then
#    log "EUD mode is '$EUD_MODE' but max_euds=$MAX_EUDS. Forcing max_euds=1."
    MAX_EUDS=1
fi

# Sourced above
IPV4_NETWORK=${IPV4_NETWORK:-"10.43.1.0/16"}
MAX_EUDS=${MAX_EUDS:-1}
CHUNK_SIZE=$((MAX_EUDS + 2))  # br0 primary + br0 secondary (gateway) + EUDs
SERVICES_RESERVED=5  # IPs 1-5 for services

# --- State Variables ---
IPV4_STATE="UNCONFIGURED"
CURRENT_IPV4=""
CURRENT_CHUNK=""

PERSISTENT_IPV4=""
PERSISTENT_CHUNK=""
PERSISTENT_NETWORK=""

# --- Helper Functions ---
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - IP-MGR: $1" >&2
}

# Converts an IP string to a 32-bit integer
ip_to_int() {
    local ip=$1
    if [[ -z "$ip" || ! "$ip" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
        return 1
    fi
    local a b c d
    IFS=. read -r a b c d <<<"$ip"
    echo "$(( (a << 24) + (b << 16) + (c << 8) + d ))"
}

# Converts a 32-bit integer to an IP string
int_to_ip() {
    local ip_int=$1
    echo "$(( (ip_int >> 24) & 255 )).$(( (ip_int >> 16) & 255 )).$(( (ip_int >> 8) & 255 )).$(( ip_int & 255 ))"
}

# Calculate chunk IPs given a chunk number
get_chunk_ips() {
    local chunk_num=$1
    local CALC_OUTPUT=$(manet-ipcalc.sh "$IPV4_NETWORK" 2>/dev/null)

    if [ -z "$CALC_OUTPUT" ]; then
        return 1
    fi

    local HOST_MIN=$(echo "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')
    local MIN_INT=$(ip_to_int "$HOST_MIN")

    # First chunk starts after services reservation
    local CHUNK_START_INT=$((MIN_INT + SERVICES_RESERVED + (chunk_num * CHUNK_SIZE)))

    # First IP in chunk (for br0 primary - mesh communication)
    local BR0_PRIMARY=$(int_to_ip "$CHUNK_START_INT")

    # Second IP in chunk (for br0 secondary - DHCP gateway)
    local BR0_SECONDARY=$(int_to_ip $((CHUNK_START_INT + 1)))

    # DHCP pool starts at third IP
    local DHCP_START=$(int_to_ip $((CHUNK_START_INT + 2)))
    local DHCP_END=$(int_to_ip $((CHUNK_START_INT + CHUNK_SIZE - 1)))

    echo "${BR0_PRIMARY}:${BR0_SECONDARY}:${DHCP_START}:${DHCP_END}"
}

# Check if an IP is in the usable range
ip_in_cidr() {
    local ip=$1
    local cidr=$2

    if [[ -z "$ip" || -z "$cidr" ]]; then
        return 1
    fi

    local CALC_OUTPUT=$(manet-ipcalc.sh "$cidr" 2>/dev/null)
    if [ -z "$CALC_OUTPUT" ]; then
        return 1
    fi

    local HOST_MIN=$(echo "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')
    local HOST_MAX=$(echo "$CALC_OUTPUT" | awk '/HostMax/ {print $2}')

    if [ -z "$HOST_MIN" ] || [ -z "$HOST_MAX" ]; then
        return 1
    fi

    local IP_INT=$(ip_to_int "$ip")
    local MIN_INT=$(ip_to_int "$HOST_MIN")
    local MAX_INT=$(ip_to_int "$HOST_MAX")

    if [ -z "$IP_INT" ] || [ -z "$MIN_INT" ] || [ -z "$MAX_INT" ]; then
        return 1
    fi

    if [ "$IP_INT" -ge "$MIN_INT" ] && [ "$IP_INT" -le "$MAX_INT" ]; then
        return 0
    else
        return 1
    fi
}

is_service_reserved_ip() {
    local ip="$1"
    local CALC_OUTPUT HOST_MIN MIN_INT IP_INT offset

    CALC_OUTPUT=$(manet-ipcalc.sh "$IPV4_NETWORK" 2>/dev/null)
    [ -n "$CALC_OUTPUT" ] || return 1

    HOST_MIN=$(echo "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')
    MIN_INT=$(ip_to_int "$HOST_MIN")
    IP_INT=$(ip_to_int "$ip")
    [ -n "$MIN_INT" ] && [ -n "$IP_INT" ] || return 1

    offset=$((IP_INT - MIN_INT))
    [ "$offset" -ge 0 ] && [ "$offset" -lt "$SERVICES_RESERVED" ]
}

# Peer claims as absolute address ranges. Block sizes are provisioned per node,
# so a peer's chunk NUMBER means nothing here: only its advertised address and
# size do. Claims file lines are chunk,mac,start_int,size.
#
# A claim without a size cannot be placed. Guessing our own size would recreate
# the mixed-size overlap this format exists to prevent, so such a claim marks
# the snapshot incomplete: new allocation waits for a complete identity, while
# its known primary address still counts for conflict checks.
MAX_PEER_CHUNK_SIZE=255   # max_euds_per_node is at most 253
load_claims() {
    local chunk mac start size
    CLAIM_STARTS=(); CLAIM_ENDS=(); CLAIM_MACS=(); INCOMPLETE_CLAIMS=()
    CLAIMS_LOADED=1
    [ -f "$CLAIMED_CHUNKS_FILE" ] || return 0
    while IFS=, read -r chunk mac start size; do
        [[ "$chunk" =~ ^[0-9]+$ && -n "$mac" ]] || continue
        mac_is_local "$mac" && continue
        [[ "$size" =~ ^[0-9]+$ ]] || size=0
        if ! [[ "$start" =~ ^[0-9]{1,10}$ ]] || [ "$start" -gt 4294967295 ]; then
            INCOMPLETE_CLAIMS+=("$mac")
            continue
        fi
        if [ "$size" -eq 0 ]; then
            INCOMPLETE_CLAIMS+=("$mac")
            size=1
        elif [ "$size" -gt "$MAX_PEER_CHUNK_SIZE" ]; then
            # Implausible, so unusable as a range; never allocate over it.
            log "Implausible claim from $mac: $size addresses"
            INCOMPLETE_CLAIMS+=("$mac")
            size=1
        fi
        CLAIM_STARTS+=("$start")
        CLAIM_ENDS+=($((start + size - 1)))
        CLAIM_MACS+=("$mac")
    done < "$CLAIMED_CHUNKS_FILE"
}

# Absolute first and last address (as integers) of one of OUR chunks.
chunk_range() {
    local ips primary
    ips=$(get_chunk_ips "$1") || return 1
    primary=$(ip_to_int "${ips%%:*}") || return 1
    echo "$primary $((primary + CHUNK_SIZE - 1))"
}

# Print the MAC of a peer whose claim overlaps [start, end]; succeed if found.
# Our own identity cached elsewhere in the mesh is not a peer.
range_claimed_by_peer() {
    local start="$1" end="$2" i
    [ -n "${CLAIMS_LOADED:-}" ] || load_claims
    for i in "${!CLAIM_STARTS[@]}"; do
        if [ "${CLAIM_STARTS[$i]}" -le "$end" ] && [ "${CLAIM_ENDS[$i]}" -ge "$start" ]; then
            echo "${CLAIM_MACS[$i]}"
            return 0
        fi
    done
    return 1
}

# A saved allocation is a preference, not ownership. Check the fresh registry
# before restoring it.
chunk_claimed_by_peer() {
    local range
    range=$(chunk_range "$1") || return 1
    range_claimed_by_peer ${range} >/dev/null
}

# Get a random available chunk
get_random_chunk() {
    local CALC_OUTPUT=$(manet-ipcalc.sh "$IPV4_NETWORK" 2>/dev/null)
    
    if [ -z "$CALC_OUTPUT" ]; then
        log "Error: ipcalc failed for CIDR: $IPV4_NETWORK"
        return 1
    fi

    local HOST_MIN=$(echo "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')
    local HOST_MAX=$(echo "$CALC_OUTPUT" | awk '/HostMax/ {print $2}')
    local MIN_INT=$(ip_to_int "$HOST_MIN")
    local MAX_INT=$(ip_to_int "$HOST_MAX")

    # Calculate available IP space after services
    local AVAILABLE_IPS=$((MAX_INT - MIN_INT + 1 - SERVICES_RESERVED))
    local MAX_CHUNKS=$((AVAILABLE_IPS / CHUNK_SIZE))
    
    if [ "$MAX_CHUNKS" -lt 1 ]; then
        log "Error: Network too small for chunk size $CHUNK_SIZE"
        return 1
    fi
    
    log "Network supports $MAX_CHUNKS chunks (chunk_size=$CHUNK_SIZE, max_euds=$MAX_EUDS)"
    
    # Mark every local chunk index a peer range touches, then collect the
    # rest. One pass over claims plus one over chunks, no per-chunk subshells.
    [ -n "${CLAIMS_LOADED:-}" ] || load_claims
    local base=$((MIN_INT + SERVICES_RESERVED)) first last j c
    local -A blocked=()
    for j in "${!CLAIM_STARTS[@]}"; do
        [ "${CLAIM_ENDS[$j]}" -ge "$base" ] || continue
        first=$(( (CLAIM_STARTS[j] - base) / CHUNK_SIZE ))
        [ "${CLAIM_STARTS[$j]}" -ge "$base" ] || first=0
        last=$(( (CLAIM_ENDS[j] - base) / CHUNK_SIZE ))
        [ "$last" -lt "$MAX_CHUNKS" ] || last=$((MAX_CHUNKS - 1))
        for ((c=first; c<=last; c++)); do
            blocked[$c]=1
        done
    done
    local available_chunks=()
    for ((i=0; i<MAX_CHUNKS; i++)); do
        [ -n "${blocked[$i]:-}" ] || available_chunks+=($i)
    done
    
    if [ ${#available_chunks[@]} -eq 0 ]; then
        log "Error: No available chunks"
        return 1
    fi
    
    # Select random available chunk
    local random_index=$((RANDOM % ${#available_chunks[@]}))
    echo "${available_chunks[$random_index]}"
}

# Save persistent state
save_persistent_state() {
    cat > "$PERSISTENT_STATE_FILE" <<- EOF
# Persistent IPv4 state for mesh node
# Last updated: $(date)
PERSISTENT_IPV4="$PERSISTENT_IPV4"
PERSISTENT_CHUNK="$PERSISTENT_CHUNK"
PERSISTENT_NETWORK="$PERSISTENT_NETWORK"
EOF
    chmod 644 "$PERSISTENT_STATE_FILE"
}

mac_is_local() {
    local mac="$1"
    local local_mac
    local iface_path
    local iface

    [ -n "$mac" ] || return 1

    for iface_path in /sys/class/net/*; do
        iface=${iface_path##*/}
        [ -e "/sys/class/net/$iface/address" ] || continue
        read -r local_mac < "/sys/class/net/$iface/address" || continue
        if [ "$mac" = "$local_mac" ]; then
            return 0
        fi
    done

    return 1
}

release_control_ips() {
    update_avahi_host "" || return 1
    local prefix="${IPV4_NETWORK#*/}"
    local primary=""
    local secondary=""
    local _dhcp_start=""
    local _dhcp_end=""

    if [ -n "$PERSISTENT_CHUNK" ]; then
        IFS=: read -r primary secondary _dhcp_start _dhcp_end <<< "$(get_chunk_ips "$PERSISTENT_CHUNK")"
        [ -n "$primary" ] && ip addr del "${primary}/${prefix}" dev "$CONTROL_IFACE" 2>/dev/null || true
        [ -n "$secondary" ] && ip addr del "${secondary}/${prefix}" dev "$CONTROL_IFACE" 2>/dev/null || true
        log "Released br0 chunk addresses: ${primary:-unknown}, ${secondary:-unknown}"
    elif [ -n "$CURRENT_IPV4" ]; then
        ip addr del "${CURRENT_IPV4}/${prefix}" dev "$CONTROL_IFACE" 2>/dev/null || true
        log "Released current br0 IPv4 address: $CURRENT_IPV4"
    fi
}

cleanup_control_aliases() {
    local prefix="${IPV4_NETWORK#*/}"
    local primary=""
    local secondary=""
    local ip=""
    local keep=""
    local _dhcp_start=""
    local _dhcp_end=""

    [ -n "$PERSISTENT_CHUNK" ] || return 0

    IFS=: read -r primary secondary _dhcp_start _dhcp_end <<< "$(get_chunk_ips "$PERSISTENT_CHUNK")"
    keep=" $primary $secondary "

    while read -r ip; do
        [ -n "$ip" ] || continue
        ip_in_cidr "$ip" "$IPV4_NETWORK" || continue
        is_service_reserved_ip "$ip" && continue

        if [[ "$keep" != *" $ip "* ]]; then
            ip addr del "${ip}/${prefix}" dev "$CONTROL_IFACE" 2>/dev/null || true
            log "Removed stale $CONTROL_IFACE IPv4 alias: $ip"
        fi
    done < <(ip -4 -o addr show dev "$CONTROL_IFACE" 2>/dev/null | awk '{print $4}' | cut -d/ -f1)
}

# bat0 is the only mesh-facing bridge port. The rule set does not depend on
# which physical radio currently serves as AP or mesh.
ensure_dhcp_isolation() {
    if ! { if [ "${MANET_IP_CHECKED:-0}" = 1 ]; then
               [ "$MANET_IP_ISOLATED" = 1 ]
           else python3 /usr/local/bin/manet-dhcp-isolation.py ensure; fi; }; then
        log "ERROR: DHCP isolation failed; stopping dnsmasq to prevent foreign offers"
        systemctl stop dnsmasq.service 2>/dev/null || true
        return 1
    fi
}

eud_ready() {
    if [ "${MANET_IP_CHECKED:-0}" = 1 ]; then
        # Recheck at the point of a DHCP start/restart: a port can disappear
        # while allocation runs. Sysfs reads need no second interpreter.
        local sysnet="${MANET_SYS_NET:-/sys/class/net}" port state carrier
        for port in "$sysnet"/br0/brif/*; do
            [ "${port##*/}" != bat0 ] || continue
            state=""; carrier=""
            [ ! -r "$port/state" ] || read -r state < "$port/state"
            [ ! -r "$sysnet/${port##*/}/carrier" ] || read -r carrier < "$sysnet/${port##*/}/carrier"
            [ "$state" != 3 ] || [ "$carrier" != 1 ] || return 0
        done
        return 1
    else
        python3 /usr/local/bin/manet-dhcp-isolation.py eud-ready
    fi
}

# Reuse the validated pool and leases after a temporary isolation failure.
# Called only after this pass has checked the node's allocation/configuration.
ensure_dnsmasq_running() {
    if ! eud_ready; then
        if systemctl is-active --quiet dnsmasq.service; then
            systemctl stop dnsmasq.service || return 1
            log "dnsmasq stopped: no forwarding EUD port on br0"
        fi
        return 0
    fi
    if ! systemctl is-active --quiet dnsmasq.service; then
        if ! systemctl start dnsmasq.service; then
            log "ERROR: dnsmasq recovery failed; retrying next allocation pass"
            return 1
        fi
        log "dnsmasq resumed with existing pool and leases"
    fi
}

# The internal address is immediately before this chunk's EUD pool. Do not
# publish a mesh address, service VIP, malformed address or unknown allocation.
valid_eud_gateway() {
    local address="${1:-}" start="${2:-}" address_int start_int
    address_int=$(ip_to_int "$address") || return 1
    start_int=$(ip_to_int "$start") || return 1
    [ "$(int_to_ip "$address_int")" = "$address" ] &&
        [ "$(int_to_ip "$start_int")" = "$start" ] &&
        [ "$address_int" -gt 0 ] && [ "$start_int" -lt 3758096384 ] &&
        [ "$((address_int >> 24))" -ne 127 ] &&
        [ "$start_int" -eq "$((address_int + 1))" ] &&
        [ "$address" != "$MTX_VIP" ] && [ "$address" != "$MUMBLE_VIP" ]
}

update_avahi_host() {
    local address="${1:-}" start="${2:-}"
    local hosts=/etc/avahi/hosts pending=/run/manet-avahi-host-reload-needed
    local source temporary
    valid_eud_gateway "$address" "$start" || address=""
    [ -n "$address" ] || [ -f "$hosts" ] || [ -f "$pending" ] || return 0
    mkdir -p "${hosts%/*}" || return 1
    temporary=$(mktemp "${hosts}.XXXXXX") || return 1
    source="$hosts"
    [ -f "$source" ] || source=/dev/null
    # Preserve operator entries and comments; replace only our management name.
    if ! awk -v address="$address" '
        { name=tolower($2); sub(/\.$/, "", name) }
        $1 !~ /^#/ && name == "manet.local" { next }
        { print }
        END { if (address != "") print address " manet.local" }
    ' "$source" > "$temporary"; then
        rm -f "$temporary"
        return 1
    fi
    if cmp -s "$temporary" "$hosts"; then
        rm -f "$temporary"
    else
        if ! chmod 0644 "$temporary" || ! touch "$pending" || ! mv -f "$temporary" "$hosts"; then
            rm -f "$temporary"
            return 1
        fi
    fi
    [ -f "$pending" ] || return 0
    # Inactive Avahi reads the file when started. A failed reload is retried
    # on the next allocation pass, without rewriting an unchanged hosts file.
    if systemctl is-active --quiet avahi-daemon.service; then
        systemctl reload avahi-daemon.service || return 1
    fi
    rm -f "$pending"
}

# Configure DNS and mDNS together from the same allocated internal address.
configure_dnsmasq() {
    local br0_primary=$1
    local br0_secondary=$2
    local dhcp_start=$3
    local dhcp_end=$4
    local old_gateway=""
    local old_primary=""
    local _MUMBLE_VIP_LINE=""
    local _MTX_VIP_LINE=""
    local temporary
    if ! valid_eud_gateway "$br0_secondary" "$dhcp_start"; then
        update_avahi_host "" || return 1
        log "ERROR: EUD gateway unknown or inconsistent with the pool; management name withdrawn"
        return 1
    fi
    [ -n "$MUMBLE_VIP" ] && _MUMBLE_VIP_LINE="address=/mumble.local/$MUMBLE_VIP"
    [ -n "$MTX_VIP" ]    && _MTX_VIP_LINE="address=/mtx.local/$MTX_VIP"

    if [ -f /etc/dnsmasq.d/mesh-eud.conf ]; then
        old_gateway=$(awk -F, '$1 == "dhcp-option=3" {print $2; exit}' /etc/dnsmasq.d/mesh-eud.conf)
    fi

    if [ -n "$old_gateway" ] && [ "$old_gateway" != "$br0_secondary" ] && ip_in_cidr "$old_gateway" "$IPV4_NETWORK"; then
        old_primary=$(int_to_ip $(( $(ip_to_int "$old_gateway") - 1 )))
        log "EUD gateway changed from ${old_primary:-unknown}/$old_gateway to $br0_primary/$br0_secondary"
    fi

    log "Configuring dnsmasq: pool=$dhcp_start-$dhcp_end, gateway=$br0_secondary"
    # Pool/gateway changes invalidate old EUD leases. Keeping them can make the
    # dashboard or downstream clients try stale EUD IPs such as old ATAK peers.
    rm -f /var/lib/misc/dnsmasq.leases /run/dnsmasq.leases /tmp/dnsmasq.leases 2>/dev/null || true

    temporary=$(mktemp /etc/dnsmasq.d/.mesh-eud.conf.XXXXXX) || return 1
    if ! cat > "$temporary" <<- EOF
# Listen only on br0 bridge
interface=br0
# br0 and its IPv4 addresses can appear after dnsmasq starts during boot.
bind-dynamic

# DHCP configuration from this node's chunk
dhcp-range=$dhcp_start,$dhcp_end,4m

# Gateway is this node's br0 secondary address
dhcp-option=3,$br0_secondary

# DNS configuration
dhcp-option=6,$br0_secondary
domain=mesh.local
local=/mesh.local/

# The management name uses this node's internal EUD address.
address=/manet.local/$br0_secondary

# Service VIPs: stable across the mesh regardless of which node is leader
${_MUMBLE_VIP_LINE}
${_MTX_VIP_LINE}

# Follow DHCP/RA DNS changes without restarting DHCP or discarding leases.
# This is resolved's upstream list, never its localhost stub (no DNS loop).
resolv-file=/run/systemd/resolve/resolv.conf
clear-on-reload

# Log for debugging
log-dhcp
EOF
    then
        rm -f "$temporary"
        return 1
    fi
    if ! chmod 0644 "$temporary" || ! mv -f "$temporary" /etc/dnsmasq.d/mesh-eud.conf; then
        rm -f "$temporary"
        return 1
    fi
    # Ensure dnsmasq is unmasked, enabled, and running.
    # unmask triggers a full systemd daemon-reload even when nothing is
    # masked: only call it when the unit is actually masked.
    if [ "$(systemctl is-enabled dnsmasq.service 2>/dev/null)" = "masked" ]; then
        systemctl unmask dnsmasq.service 2>/dev/null
    fi
#    systemctl enable dnsmasq.service 2>/dev/null

    if eud_ready &&
            systemctl is-active --quiet dnsmasq.service; then
        systemctl restart dnsmasq.service
        log "dnsmasq restarted"
    else
        ensure_dnsmasq_running
    fi
    update_avahi_host "$br0_secondary" "$dhcp_start"
}

ensure_control_addr() {
    local ip="$1"
    local prefix="${IPV4_NETWORK#*/}"

    [ -n "$ip" ] || return 0

    if ! ip -4 addr show dev "$CONTROL_IFACE" | grep -qw "$ip"; then
        ip addr add "${ip}/${prefix}" dev "$CONTROL_IFACE" 2>/dev/null || true
        log "Restored $CONTROL_IFACE IPv4 address: $ip"
    fi
}

# --- Main Logic ---

# This refreshes the registry on every pass and returns promptly while boot
# discovery is pending. Leave node-manager free to publish over IPv6. Run it
# before restoring even a saved chunk or changing any interface/DHCP state.
if ! { if [ "${MANET_IP_CHECKED:-0}" = 1 ]; then
           [ "$MANET_IP_STARTUP_READY" = 1 ]
       else python3 "$STARTUP_HELPER"; fi; }; then
    update_avahi_host "" || exit 1
    exit 0
fi

if ! ensure_dhcp_isolation; then
    update_avahi_host ""
    exit 1
fi

# Get our MAC address
MY_MAC=$(cat "/sys/class/net/${CONTROL_IFACE}/address" 2>/dev/null || echo "")
if [ -z "$MY_MAC" ]; then
    log "ERROR: Cannot read MAC address from $CONTROL_IFACE"
    update_avahi_host ""
    exit 1
fi

log "Chunk-based IP allocation: chunk_size=$CHUNK_SIZE (max_euds=$MAX_EUDS)"

# Load persistent state
if [ -f "$PERSISTENT_STATE_FILE" ]; then
    source "$PERSISTENT_STATE_FILE" 2>/dev/null
    if [ -n "$PERSISTENT_IPV4" ] && [ -n "$PERSISTENT_CHUNK" ]; then
        log "Loaded persistent state: chunk=$PERSISTENT_CHUNK, ip=$PERSISTENT_IPV4"
    fi
fi

cleanup_control_aliases

# Check if we already have an IP configured on br0
read_current_ipv4() {
    if [ "${MANET_IP_CHECKED:-0}" = 1 ]; then
        [ "$MANET_IP_ADDRESS_OK" = 1 ] || return 1
        printf '%s\n' "$MANET_IP_PRIMARY"
    else
        python3 /usr/local/bin/manet_node_ipv4.py "$CONTROL_IFACE"
    fi
}
if ! CURRENT_IPV4=$(read_current_ipv4); then
    log "Cannot inspect the assigned node address; deferring allocation"
    update_avahi_host "" || exit 1
    exit 0
fi
if [ -n "$CURRENT_IPV4" ]; then
    IPV4_STATE="CONFIGURED"
    log "Current IPv4 on br0: ${CURRENT_IPV4}"
fi

# Load claimed ranges from registry
[ -f "$CLAIMED_CHUNKS_FILE" ] || log "Warning: Claimed chunks file not found"
load_claims

# --- State Machine ---
case $IPV4_STATE in
    "UNCONFIGURED")
        update_avahi_host "" || exit 1
        PROPOSED_CHUNK=""

        if [ "${#INCOMPLETE_CLAIMS[@]}" -gt 0 ]; then
            log "Deferring allocation: peer claim without a block size from ${INCOMPLETE_CLAIMS[*]} (identity incomplete or outdated software)"
            exit 0
        fi

        # Check if we have a persistent chunk and if network has changed
        if [ -n "$PERSISTENT_CHUNK" ] && [ -n "$PERSISTENT_IPV4" ]; then
            # Check if network changed
            if [ -n "$PERSISTENT_NETWORK" ] && [ "$PERSISTENT_NETWORK" != "$IPV4_NETWORK" ]; then
                log "Network changed from ${PERSISTENT_NETWORK} to ${IPV4_NETWORK}. Selecting new chunk."
                PERSISTENT_IPV4=""
                PERSISTENT_CHUNK=""
                PERSISTENT_NETWORK=""
                save_persistent_state
            else
                # Verify persistent IP is in current network
                if ip_in_cidr "$PERSISTENT_IPV4" "$IPV4_NETWORK"; then
                    if chunk_claimed_by_peer "$PERSISTENT_CHUNK"; then
                        log "Previous chunk $PERSISTENT_CHUNK is claimed by a peer. Selecting a free chunk."
                    else
                        log "Reclaiming previous chunk $PERSISTENT_CHUNK (IP: ${PERSISTENT_IPV4})"
                        PROPOSED_CHUNK="$PERSISTENT_CHUNK"
                    fi
                else
                    log "Persistent IP ${PERSISTENT_IPV4} not in network ${IPV4_NETWORK}. Selecting new chunk."
                    PERSISTENT_IPV4=""
                    PERSISTENT_CHUNK=""
                    save_persistent_state
                fi
            fi
        fi

        # Generate new chunk if needed
        if [ -z "$PROPOSED_CHUNK" ]; then
            log "Selecting new chunk from ${IPV4_NETWORK}..."
            PROPOSED_CHUNK=$(get_random_chunk)
        fi

        if [ -z "$PROPOSED_CHUNK" ]; then
            log "Failed to select chunk"
            exit 1
        fi

        # Get chunk IPs
        CHUNK_IPS=$(get_chunk_ips "$PROPOSED_CHUNK")
        IFS=: read -r BR0_PRIMARY BR0_SECONDARY DHCP_START DHCP_END <<< "$CHUNK_IPS"
        if ! valid_eud_gateway "$BR0_SECONDARY" "$DHCP_START"; then
            update_avahi_host ""
            exit 1
        fi
        
        log "Proposed chunk $PROPOSED_CHUNK: primary=$BR0_PRIMARY, gateway=$BR0_SECONDARY, dhcp=$DHCP_START-$DHCP_END"

        # A fresh snapshot protects both new and remembered allocations.
        # Simultaneous claims / partition merges still need the MAC tie-break.
        if chunk_claimed_by_peer "$PROPOSED_CHUNK"; then
            log "Proposed chunk ${PROPOSED_CHUNK} is in use. Will retry next cycle."
        else
            log "Claiming chunk ${PROPOSED_CHUNK} with br0 IPs ${BR0_PRIMARY} and ${BR0_SECONDARY}..."
            
            # Assign both IPs to br0
            ip addr add "${BR0_PRIMARY}/${IPV4_NETWORK#*/}" dev "$CONTROL_IFACE"
            ip addr add "${BR0_SECONDARY}/${IPV4_NETWORK#*/}" dev "$CONTROL_IFACE"
            log "Assigned br0 primary: $BR0_PRIMARY, secondary (gateway): $BR0_SECONDARY"
            
            
            # Configure dnsmasq
            configure_dnsmasq "$BR0_PRIMARY" "$BR0_SECONDARY" "$DHCP_START" "$DHCP_END" || exit 1

            # Save persistent state
            PERSISTENT_IPV4="$BR0_PRIMARY"
            PERSISTENT_CHUNK="$PROPOSED_CHUNK"
            PERSISTENT_NETWORK="$IPV4_NETWORK"
            save_persistent_state
            
            log "Successfully claimed chunk ${PROPOSED_CHUNK}"
            
            # Write chunk and block size for the identity publisher to pick up
            echo "$CHUNK_SIZE" > /var/run/my_ipv4_chunk_size
            echo "$PROPOSED_CHUNK" > /var/run/my_ipv4_chunk
        fi
        ;;

    "CONFIGURED")
        if [ -z "$PERSISTENT_CHUNK" ]; then
            update_avahi_host "" || exit 1
        fi
        # Check for conflicts: any peer range overlapping our whole block,
        # not only a peer whose primary equals ours.
        CONFLICTING_MAC=""
        MY_START=$(ip_to_int "$CURRENT_IPV4")
        if [ -n "$MY_START" ]; then
            CONFLICTING_MAC=$(range_claimed_by_peer "$MY_START" $((MY_START + CHUNK_SIZE - 1))) || true
        fi

        if [[ -n "$CONFLICTING_MAC" ]]; then
            log "CONFLICT DETECTED for ${CURRENT_IPV4}/${CHUNK_SIZE} addresses! Overlapping claim from ${CONFLICTING_MAC}"

            # Tie-breaker: higher MAC wins
            if [[ "$MY_MAC" > "$CONFLICTING_MAC" ]]; then
                log "Won tie-breaker. Defending chunk."
            else
                log "Lost tie-breaker. Releasing chunk and IPs."
                release_control_ips || exit 1
                
                PERSISTENT_IPV4=""
                PERSISTENT_CHUNK=""
                PERSISTENT_NETWORK=""
                save_persistent_state
                rm -f /var/run/my_ipv4_chunk /var/run/my_ipv4_chunk_size
            fi
        else
            # No conflict, only reconfigure if something actually changed
            if [ -n "$PERSISTENT_CHUNK" ]; then
                echo "$CHUNK_SIZE" > /var/run/my_ipv4_chunk_size
                echo "$PERSISTENT_CHUNK" > /var/run/my_ipv4_chunk

                # Get current chunk IPs
                CHUNK_IPS=$(get_chunk_ips "$PERSISTENT_CHUNK")
                IFS=: read -r BR0_PRIMARY BR0_SECONDARY DHCP_START DHCP_END <<< "$CHUNK_IPS"
                if ! valid_eud_gateway "$BR0_SECONDARY" "$DHCP_START"; then
                    update_avahi_host ""
                    exit 1
                fi

                ensure_control_addr "$BR0_PRIMARY"
                ensure_control_addr "$BR0_SECONDARY"

                # Only reconfigure dnsmasq if the config has changed
                DNSMASQ_CONF="/etc/dnsmasq.d/mesh-eud.conf"
                NEEDS_DNSMASQ_UPDATE=false

                if [ ! -f "$DNSMASQ_CONF" ]; then
                    NEEDS_DNSMASQ_UPDATE=true
                elif ! grep -q "dhcp-range=$DHCP_START,$DHCP_END" "$DNSMASQ_CONF" 2>/dev/null; then
                    NEEDS_DNSMASQ_UPDATE=true
                elif ! grep -q "dhcp-option=3,$BR0_SECONDARY" "$DNSMASQ_CONF" 2>/dev/null; then
                    NEEDS_DNSMASQ_UPDATE=true
                elif ! grep -Fxq "address=/manet.local/$BR0_SECONDARY" "$DNSMASQ_CONF"; then
                    NEEDS_DNSMASQ_UPDATE=true
                elif grep '^address=/' "$DNSMASQ_CONF" | grep -Evq '^address=/(manet|mumble|mtx)\.local/'; then
                    NEEDS_DNSMASQ_UPDATE=true
                elif ! grep -Fxq 'bind-dynamic' "$DNSMASQ_CONF" || grep -q '^bind-interfaces' "$DNSMASQ_CONF"; then
                    NEEDS_DNSMASQ_UPDATE=true
                elif grep -q "^no-resolv" "$DNSMASQ_CONF" 2>/dev/null; then
                    NEEDS_DNSMASQ_UPDATE=true
                elif ! grep -Fxq 'resolv-file=/run/systemd/resolve/resolv.conf' "$DNSMASQ_CONF"; then
                    NEEDS_DNSMASQ_UPDATE=true
                elif grep -q '^server=' "$DNSMASQ_CONF"; then
                    NEEDS_DNSMASQ_UPDATE=true
                elif ! grep -Fxq 'clear-on-reload' "$DNSMASQ_CONF"; then
                    NEEDS_DNSMASQ_UPDATE=true
                elif [ -n "$MUMBLE_VIP" ] && ! grep -Fxq "address=/mumble.local/$MUMBLE_VIP" "$DNSMASQ_CONF"; then
                    NEEDS_DNSMASQ_UPDATE=true
                elif [ -n "$MTX_VIP" ] && ! grep -Fxq "address=/mtx.local/$MTX_VIP" "$DNSMASQ_CONF"; then
                    NEEDS_DNSMASQ_UPDATE=true
                fi

                if [ "$NEEDS_DNSMASQ_UPDATE" = true ]; then
                    log "DHCP config changed, reconfiguring..."
                    configure_dnsmasq "$BR0_PRIMARY" "$BR0_SECONDARY" "$DHCP_START" "$DHCP_END" || exit 1
                else
                    update_avahi_host "$BR0_SECONDARY" "$DHCP_START" || exit 1
                    ensure_dnsmasq_running || exit 1
                fi

                # The web UI is restricted to whoever holds a lease from this
                # node, so the rule has to follow the pool whenever it moves.
                [ -x /usr/local/bin/manet-ui-firewall.sh ] && \
                    /usr/local/bin/manet-ui-firewall.sh >/dev/null 2>&1 || true
            fi
        fi
        ;;
esac

exit 0
