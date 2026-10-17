#!/bin/bash
# Reconcile the node IPv4 block, local EUD pool and management names.
. "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet-common.sh" || exit 1
# Claim a contiguous IPv4 block for the node and its EUDs.
# IPs 1-5 are reserved for services. Chunks start at IP 6, sized max_euds + 2:
#   first: br0 primary for mesh traffic
#   second: br0 secondary for the EUD gateway
#   remainder: EUD DHCP pool
# AP/wired EUD interfaces bridge to br0. dnsmasq serves br0 while nftables
# blocks DHCP on bat0. Multicast stays at Layer 2. wlan1 in mesh mode joins
# bat0 with DHCP blocked, while AP mode joins br0 with DHCP allowed.

# --- Configuration ---
# Request the existing agreement process with a one-shot fallback. Both
# share discovery, registry, isolation and address checks, calling back
# here only for reconciliation.
if [ "${MANET_IP_CHECKED:-0}" != 1 ]; then
    . "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet-runtime-client.sh"
    _runtime_rc=0
    manet_runtime_call ip || _runtime_rc=$?
    [ "$_runtime_rc" = 125 ] || exit "$_runtime_rc"
    exec python3 "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet_ip_runtime.py"
fi
CONTROL_IFACE="br0"
CLAIMED_CHUNKS_FILE=/run/manet-registry/claimed-chunks.txt
CLAIMED_CHUNKS_FILE="${MESH_CLAIMED_CHUNKS_FILE:-$CLAIMED_CHUNKS_FILE}"
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
# Set MTX_VIP and MUMBLE_VIP from IPV4_NETWORK when ipcalc succeeds.
_calc_service_vips() {
    local calc host_min
    calc=$(manet-ipcalc.sh "$IPV4_NETWORK" 2>/dev/null) || return 0
    host_min=$(printf '%s\n' "$calc" | awk '/HostMin/ {print $2}')
    [ -n "$host_min" ] || return 0
    MTX_VIP="${host_min%.*}.$((${host_min##*.} + 1))"
    MUMBLE_VIP="${host_min%.*}.$((${host_min##*.} + 2))"
}
_calc_service_vips

# Active EUD modes require at least one EUD address.
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

PERSISTENT_IPV4=""
PERSISTENT_CHUNK=""
PERSISTENT_NETWORK=""

# --- Helper Functions ---
# Write a prefixed diagnostic to stderr.
log() {
    printf '%s\n' "IP-MGR: $1" >&2
}

# Chunk selection enumerates every free chunk and draws from $RANDOM
# (0-32767), so the mesh is at most a /16. The validator refuses larger
# networks; this catches a hand-edited mesh.conf.
if [[ "${IPV4_NETWORK#*/}" =~ ^[0-9]+$ ]] && [ "${IPV4_NETWORK#*/}" -lt 16 ]; then
    log "ipv4_network $IPV4_NETWORK is larger than a /16; not allocating"
    exit 1
fi

# Converts an IP string to a 32-bit integer
ip_to_int() {
    local ip=$1
    if [[ -z "$ip" || ! "$ip" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
        return 1
    fi
    local a b c d
    IFS=. read -r a b c d <<<"$ip"
    printf '%s\n' "$(( (a << 24) + (b << 16) + (c << 8) + d ))"
}

# Converts a 32-bit integer to an IP string


# Calculate chunk IPs given a chunk number
get_chunk_ips() {
    # Zero-based chunks use IPV4_NETWORK, SERVICES_RESERVED and CHUNK_SIZE.
    # Output fields are primary:gateway:pool_start:pool_end.
    local chunk_num=$1
    local CALC_OUTPUT=$(manet-ipcalc.sh "$IPV4_NETWORK" 2>/dev/null)

    if [ -z "$CALC_OUTPUT" ]; then
        return 1
    fi

    local HOST_MIN=$(printf '%s\n' "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')
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

    printf '%s\n' "${BR0_PRIMARY}:${BR0_SECONDARY}:${DHCP_START}:${DHCP_END}"
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

    local HOST_MIN=$(printf '%s\n' "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')
    local HOST_MAX=$(printf '%s\n' "$CALC_OUTPUT" | awk '/HostMax/ {print $2}')

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

# Peer claims use absolute ranges because nodes can provision different
# block sizes. Claims file fields: chunk,mac,start_int,size.
# A missing size makes the snapshot incomplete and defers allocation. The
# known primary still participates in conflict checks. Guessing a local
# size could overlap a larger peer block.
MAX_PEER_CHUNK_SIZE=255   # max_euds_per_node is at most 253
# Load peer ranges into CLAIM_STARTS/ENDS/MACS and INCOMPLETE_CLAIMS.
# Read CLAIMED_CHUNKS_FILE once and mark CLAIMS_LOADED for later checks.
load_claims() {
    local chunk mac start size
    CLAIM_STARTS=(); CLAIM_ENDS=(); CLAIM_MACS=(); INCOMPLETE_CLAIMS=()
    CLAIMS_LOADED=1
    [ -f "$CLAIMED_CHUNKS_FILE" ] || return 0
    while IFS=, read -r chunk mac start size; do
        [[ "$chunk" =~ ^[0-9]+$ && -n "$mac" ]] || continue
        mac_is_local "$mac" && continue
        [[ "$size" =~ ^[0-9]+$ ]] || size=0
        # IPv4 integer addresses fit in 32 bits, up to 4294967295.
        if ! [[ "$start" =~ ^[0-9]{1,10}$ ]] || [ "$start" -gt 4294967295 ]; then
            INCOMPLETE_CLAIMS+=("$mac")
            continue
        fi
        if [ "$size" -eq 0 ]; then
            INCOMPLETE_CLAIMS+=("$mac")
            size=1
        elif [ "$size" -gt "$MAX_PEER_CHUNK_SIZE" ]; then
            # Reject implausible ranges to avoid allocating over them.
            log "Implausible claim from $mac: $size addresses"
            INCOMPLETE_CLAIMS+=("$mac")
            size=1
        fi
        CLAIM_STARTS+=("$start")
        CLAIM_ENDS+=($((start + size - 1)))
        CLAIM_MACS+=("$mac")
    done < "$CLAIMED_CHUNKS_FILE"
}

# Return the absolute first/last integer addresses of a local chunk.
chunk_range() {
    local ips primary
    ips=$(get_chunk_ips "$1") || return 1
    primary=$(ip_to_int "${ips%%:*}") || return 1
    printf '%s\n' "$primary $((primary + CHUNK_SIZE - 1))"
}

# Print the peer MAC overlapping [start, end] and succeed if found.
# Exclude this node's identity cached elsewhere in the mesh.
range_claimed_by_peer() {
    local start="$1" end="$2" i
    [ -n "${CLAIMS_LOADED:-}" ] || load_claims
    for i in "${!CLAIM_STARTS[@]}"; do
        if [ "${CLAIM_STARTS[$i]}" -le "$end" ] && [ "${CLAIM_ENDS[$i]}" -ge "$start" ]; then
            printf '%s\n' "${CLAIM_MACS[$i]}"
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

    local HOST_MIN=$(printf '%s\n' "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')
    local HOST_MAX=$(printf '%s\n' "$CALC_OUTPUT" | awk '/HostMax/ {print $2}')
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
    printf '%s\n' "${available_chunks[$random_index]}"
}

# Durably replace allocation intent before changing its live addresses.
save_persistent_state() {
    local temporary
    temporary=$(mktemp "${PERSISTENT_STATE_FILE}.XXXXXX") || return 1
    if ! cat > "$temporary" <<- EOF
# Persistent IPv4 state for mesh node
# Last updated: $(date)
PERSISTENT_IPV4="$PERSISTENT_IPV4"
PERSISTENT_CHUNK="$PERSISTENT_CHUNK"
PERSISTENT_NETWORK="$PERSISTENT_NETWORK"
EOF
    then
        rm -f "$temporary"
        return 1
    fi
    chmod 644 "$temporary" && sync -f "$temporary" &&
        mv -f "$temporary" "$PERSISTENT_STATE_FILE" &&
        sync -f "${PERSISTENT_STATE_FILE%/*}"
}

# Restore the saved files and owned addresses before resuming old DHCP.
# The caller holds the allocator and both service-election locks.
retirement_restore() {
    local address live path temporary i failed=0
    live=$(ip -4 -o addr show dev "$CONTROL_IFACE") || return 1
    while IFS= read -r address; do
        if ! printf '%s\n' "$live" | awk '{print $4}' |
                grep -qxF "$address"; then
            ip addr add "$address" dev "$CONTROL_IFACE" || failed=1
        fi
    done < "$retirement_backup/addresses"
    for i in "${!retirement_files[@]}"; do
        [ -f "$retirement_backup/files-changed" ] || break
        path=${retirement_files[$i]}
        if [ -f "$retirement_backup/$i" ]; then
            # A failed unlink may leave the correct file in place. Reuse it
            # even when its permissions now forbid atomic replacement.
            cmp -s "$retirement_backup/$i" "$path" && continue
            temporary=$(mktemp "${path}.XXXXXX") || return 1
            cp -p "$retirement_backup/$i" "$temporary" &&
                mv -f "$temporary" "$path" || failed=1
        else
            rm -f "$path" || failed=1
        fi
    done
    # Keep the checkpoint if restoration is incomplete. The next pass must
    # finish it before attempting another retirement, including after SIGKILL.
    [ "$failed" = 0 ] || return 1
    if [ -f "$retirement_backup/dnsmasq-active" ]; then
        systemctl start dnsmasq.service || return 1
    fi
    if systemctl is-active --quiet avahi-daemon.service; then
        systemctl reload avahi-daemon.service ||
            touch /run/manet-avahi-host-reload-needed
    fi
    if [ -f "$retirement_backup/state-temp" ]; then
        read -r temporary < "$retirement_backup/state-temp"
        rm -f "$temporary" || return 1
    fi
    rm -rf "$retirement_backup"
}

# Roll back any failed disruptive step, retaining a checkpoint on failure.
retirement_finish() {
    local status=$1
    trap - EXIT HUP INT TERM
    if [ -f "$retirement_backup/state-temp" ]; then
        local state_temp
        read -r state_temp < "$retirement_backup/state-temp"
        rm -f "$state_temp"
    fi
    if [ "$status" != 0 ]; then
        log "CIDR retirement deferred: $retirement_step"
        if [ -f "$retirement_backup/armed" ]; then
            retirement_restore ||
                log "CIDR retirement recovery pending; old DHCP not restored"
        else
            rm -rf "$retirement_backup"
        fi
    else
        rm -rf "$retirement_backup"
    fi
    exit "$status"
}

# Preflight, checkpoint and retire exact MANET addresses in a subshell.
# No tunable writes: this runs inside mesh-channel-agreement's sandbox.
retirement_transaction() (
    local retirement_backup=/run/manet-ip-retirement
    local retirement_step="election lock busy or inaccessible"
    local retirement_files=("$PERSISTENT_STATE_FILE"
        /etc/dnsmasq.d/mesh-eud.conf /var/lib/misc/dnsmasq.leases
        /run/dnsmasq.leases /tmp/dnsmasq.leases /etc/avahi/hosts
        /var/run/my_ipv4_chunk /var/run/my_ipv4_chunk_size
        /run/manet-avahi-host-reload-needed)
    # Elections never acquire the allocator lock while holding theirs.
    exec 198>"${MEDIAMTX_ELECTION_LOCK:-/var/run/mediamtx-election.lock}" ||
        { log "$retirement_step"; exit 1; }
    flock -n 198 || { log "$retirement_step"; exit 1; }
    exec 199>"${MUMBLE_ELECTION_LOCK:-/var/run/mumble-election.lock}" ||
        { log "$retirement_step"; exit 1; }
    flock -n 199 || { log "$retirement_step"; exit 1; }

    if [ -f "$retirement_backup/armed" ]; then
        retirement_restore ||
            { log "CIDR retirement recovery pending"; exit 1; }
        source "$PERSISTENT_STATE_FILE" || exit 1
    fi
    [ -n "$PERSISTENT_NETWORK" ] || exit 0
    [ "$PERSISTENT_NETWORK" != "$IPV4_NETWORK" ] || exit 0
    trap 'retirement_finish $?' EXIT
    trap 'exit 1' HUP INT TERM
    retirement_step="invalid saved address ownership"
    local old_prefix="${PERSISTENT_NETWORK#*/}" calc first primary gateway
    local address live line path temporary caps i active=0 promote all policy
    local owned=() secondaries=() primaries=() unowned=()
    calc=$(manet-ipcalc.sh "$PERSISTENT_NETWORK") || exit 1
    first=$(printf '%s\n' "$calc" | awk '/HostMin/ {print $2}')
    first=$(ip_to_int "$first") || exit 1
    primary=$(ip_to_int "$PERSISTENT_IPV4") || exit 1
    gateway=$(int_to_ip "$((primary + 1))")
    ip_in_cidr "$PERSISTENT_IPV4" "$PERSISTENT_NETWORK" || exit 1
    ip_in_cidr "$gateway" "$PERSISTENT_NETWORK" || exit 1
    [ "$primary" -gt "$((first + 4))" ] || exit 1
    owned=("$PERSISTENT_IPV4/$old_prefix" "$gateway/$old_prefix"
           "$(int_to_ip "$((first + 1))")/$old_prefix"
           "$(int_to_ip "$((first + 2))")/$old_prefix")

    retirement_step="new network has no safely claimable allocation"
    load_claims
    [ "${#INCOMPLETE_CLAIMS[@]}" = 0 ] || exit 1
    get_random_chunk >/dev/null 2>&1 || exit 1

    retirement_step="CAP_NET_ADMIN or live br0 inspection unavailable"
    caps=$(awk '/^CapEff:/ {print $2}' /proc/self/status)
    [[ "$caps" =~ ^[0-9a-fA-F]+$ ]] || exit 1
    (( (16#$caps & 4096) != 0 )) || exit 1
    live=$(ip -4 -o addr show dev "$CONTROL_IFACE") || exit 1
    retirement_step="cannot read secondary promotion policy"
    policy="/proc/sys/net/ipv4/conf/$CONTROL_IFACE/promote_secondaries"
    read -r promote < "$policy" || exit 1
    read -r all < /proc/sys/net/ipv4/conf/all/promote_secondaries || exit 1
    [[ "$promote" =~ ^[01]$ && "$all" =~ ^[01]$ ]] || exit 1
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        address=$(awk '{print $4}' <<< "$line")
        if printf '%s\n' "${owned[@]}" | grep -qxF "$address"; then
            if [[ " $line " == *" secondary "* ]]; then
                secondaries+=("$address")
            else
                primaries+=("$address")
            fi
        elif [ "${address#*/}" = "$old_prefix" ] &&
                ip_in_cidr "${address%/*}" "$PERSISTENT_NETWORK"; then
            unowned+=("$address")
        fi
    done <<< "$live"
    # With promotion off, deleting a primary removes all its secondaries.
    # Delete owned secondaries first. If an operator alias would be lost,
    # leave the entire old allocation serving until policy/aliases change.
    retirement_step="promotion disabled with unowned old-prefix aliases"
    if [ "$promote" = 0 ] && [ "$all" = 0 ] &&
            [ "${#primaries[@]}" != 0 ] && [ "${#unowned[@]}" != 0 ]; then
        exit 1
    fi

    retirement_step="cannot inspect dnsmasq state"
    systemctl is-active --quiet dnsmasq.service || active=$?
    [ "$active" = 0 ] || [ "$active" = 3 ] || exit 1
    retirement_step="cannot checkpoint retirement files or write their parents"
    rm -rf "$retirement_backup" || exit 1
    mkdir -m 700 "$retirement_backup" || exit 1
    for i in "${!retirement_files[@]}"; do
        path=${retirement_files[$i]}
        retirement_step="cannot checkpoint or replace $path"
        [ ! -L "$path" ] || exit 1
        if [ -e "$path" ]; then
            [ -f "$path" ] && [ -r "$path" ] && [ -w "$path" ] || exit 1
            cp -p "$path" "$retirement_backup/$i" || exit 1
        fi
        [ -d "${path%/*}" ] || continue
        temporary=$(mktemp "${path%/*}/.manet-retirement.XXXXXX") || exit 1
        chmod 600 "$temporary" && sync -f "$temporary" &&
            rm -f "$temporary" || exit 1
    done
    # Restore the original kernel primary first if any deletion fails.
    printf '%s\n' "${primaries[@]}" "${secondaries[@]}" |
        sed '/^$/d' > "$retirement_backup/addresses" || exit 1
    [ "$active" != 0 ] || touch "$retirement_backup/dnsmasq-active" ||
        exit 1
    # Prepare the empty durable state before stopping DHCP. Rename only
    # after cleanup, and restore its checkpoint if synchronization fails.
    retirement_step="cannot stage retired allocation"
    temporary=$(mktemp "${PERSISTENT_STATE_FILE}.XXXXXX") || exit 1
    printf '%s\n' "$temporary" > "$retirement_backup/state-temp" || exit 1
    printf 'PERSISTENT_IPV4=""\nPERSISTENT_CHUNK=""\nPERSISTENT_NETWORK=""\n' \
        > "$temporary" || exit 1
    chmod 644 "$temporary" && sync -f "$temporary" || exit 1
    touch "$retirement_backup/armed" || exit 1

    retirement_step="cannot stop old DHCP"
    systemctl stop dnsmasq.service || exit 1
    # DHCP may have granted leases during preflight. Refresh their snapshot
    # after the stop; until withdrawal, rollback leaves these files in place.
    retirement_step="cannot checkpoint stopped DHCP leases"
    for i in 2 3 4; do
        path=${retirement_files[$i]}
        if [ -f "$path" ]; then
            cp -p "$path" "$retirement_backup/$i" || exit 1
        else
            rm -f "$retirement_backup/$i" || exit 1
        fi
    done
    for address in "${secondaries[@]}" "${primaries[@]}"; do
        retirement_step="cannot delete owned address $address"
        ip addr del "$address" dev "$CONTROL_IFACE" || exit 1
    done
    retirement_step="cannot withdraw old DHCP files or management name"
    touch "$retirement_backup/files-changed" || exit 1
    rm -f /etc/dnsmasq.d/mesh-eud.conf /var/lib/misc/dnsmasq.leases \
        /run/dnsmasq.leases /tmp/dnsmasq.leases \
        /var/run/my_ipv4_chunk /var/run/my_ipv4_chunk_size || exit 1
    update_avahi_host "" || exit 1
    retirement_step="cannot commit retired allocation"
    mv -f "$temporary" "$PERSISTENT_STATE_FILE" &&
        sync -f "${PERSISTENT_STATE_FILE%/*}" || exit 1
    rm -f "$retirement_backup/armed" || exit 1
    # Linux owns connected routes. An operator alias retains its route.
)

# Bound retries even when discovery polls once a second with no chunk marker.
# Keep one diagnostic per distinct failure for this old/new CIDR pair.
retire_previous_network() {
    local retry=/run/manet-ip-retirement.retry now key reason previous=""
    local saved_key="" deadline=0 temporary
    if [ ! -f /run/manet-ip-retirement/armed ] &&
            { [ -z "$PERSISTENT_NETWORK" ] ||
              [ "$PERSISTENT_NETWORK" = "$IPV4_NETWORK" ]; }; then
        rm -f "$retry"
        return 0
    fi
    key="$PERSISTENT_NETWORK $PERSISTENT_IPV4 $IPV4_NETWORK"
    read -r now _ < /proc/uptime || return 1
    now=${now%%.*}
    if [ -f "$retry" ]; then
        { read -r saved_key; read -r deadline; previous=$(cat); } < "$retry"
        if [ "$saved_key" = "$key" ] && [[ "$deadline" =~ ^[0-9]+$ ]] &&
                [ "$now" -lt "$deadline" ]; then
            return 1
        fi
    fi
    if reason=$(retirement_transaction 2>&1); then
        rm -f "$retry"
        source "$PERSISTENT_STATE_FILE"
        return 0
    fi
    # Command diagnostics can contain random temporary names. Deduplicate the
    # stable failed step, not those incidental strings, across fresh attempts.
    reason=$(sed -n '/^IP-MGR: /p' <<< "$reason")
    reason=${reason:-"IP-MGR: CIDR retirement interrupted; recovery pending"}
    if [ "$key" != "$saved_key" ] || [ "$reason" != "$previous" ]; then
        printf '%s\n' "$reason" >&2
    fi
    read -r now _ < /proc/uptime || return 1
    temporary=$(mktemp "${retry}.XXXXXX") || return 1
    printf '%s\n' "$key" "$((${now%%.*} + 15))" "$reason" > "$temporary" &&
        mv -f "$temporary" "$retry"
    return 1
}

# Compare the supplied MAC with every local sysfs interface address.
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

# Withdraw the management name and remove the saved chunk from CONTROL_IFACE.
# Use CURRENT_IPV4 alone when PERSISTENT_CHUNK is unavailable.
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

# Test for a live forwarding br0 port other than bat0 before serving DHCP.
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
            # Bridge state 3 is forwarding, and carrier 1 confirms a live port.
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
    # Require canonical dotted decimal, adjacent gateway/pool addresses,
    # nonloopback gateway and a pool below 224.0.0.0 (3758096384).
    [ "$(int_to_ip "$address_int")" = "$address" ] &&
        [ "$(int_to_ip "$start_int")" = "$start" ] &&
        [ "$address_int" -gt 0 ] && [ "$start_int" -lt 3758096384 ] &&
        [ "$((address_int >> 24))" -ne 127 ] &&
        [ "$start_int" -eq "$((address_int + 1))" ] &&
        [ "$address" != "$MTX_VIP" ] && [ "$address" != "$MUMBLE_VIP" ]
}

# Publish gateway $1 for pool start $2 as manet.local, or withdraw it.
# Retain a failed reload's marker for the next pass while Avahi is active.
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
    # Preserve operator entries and comments. Replace only the management name.
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
    # Arguments are primary, EUD gateway, DHCP start and DHCP end.
    # The generated pool uses four-minute leases and the node's upstream DNS.
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
    # Reclaiming the same allocation can reach this function again during an
    # uplink/EUD transition. Identical content needs neither restart nor lease
    # deletion. DNS server changes arrive through resolv-file automatically.
    if cmp -s "$temporary" /etc/dnsmasq.d/mesh-eud.conf; then
        rm -f "$temporary"
        ensure_dnsmasq_running || return 1
        update_avahi_host "$br0_secondary" "$dhcp_start"
        return $?
    fi
    # Only a changed pool/gateway invalidates leases. DNS/format corrections
    # still require a restart (SIGHUP does not reload dnsmasq configuration).
    if [ "$old_gateway" != "$br0_secondary" ] ||
            ! grep -Fxq "dhcp-range=$dhcp_start,$dhcp_end,4m" /etc/dnsmasq.d/mesh-eud.conf; then
        rm -f /var/lib/misc/dnsmasq.leases /run/dnsmasq.leases /tmp/dnsmasq.leases 2>/dev/null || true
    fi
    if ! chmod 0644 "$temporary" || ! mv -f "$temporary" /etc/dnsmasq.d/mesh-eud.conf; then
        rm -f "$temporary"
        return 1
    fi
    # Unmask dnsmasq only when masked. Each unmask reloads systemd.
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

# Restore the supplied address on CONTROL_IFACE using IPV4_NETWORK's prefix.
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

# Read the local MAC address.
MY_MAC=$(cat "/sys/class/net/${CONTROL_IFACE}/address" 2>/dev/null || echo "")
if [ -z "$MY_MAC" ]; then
    log "ERROR: Cannot read MAC address from $CONTROL_IFACE"
    update_avahi_host ""
    exit 1
fi

# Load persistent state
if [ -f "$PERSISTENT_STATE_FILE" ]; then
    source "$PERSISTENT_STATE_FILE" 2>/dev/null
fi

retire_previous_network || exit 1

log "Chunk-based IP allocation: chunk_size=$CHUNK_SIZE (max_euds=$MAX_EUDS)"
if [ -n "$PERSISTENT_IPV4" ] && [ -n "$PERSISTENT_CHUNK" ]; then
    log "Loaded persistent state: chunk=$PERSISTENT_CHUNK, ip=$PERSISTENT_IPV4"
fi

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
                retire_previous_network || exit 1
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
                    save_persistent_state || exit 1
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

            # Persist intent first so interruption after either address or
            # dnsmasq install reuses the same allocation on the next pass.
            PERSISTENT_IPV4="$BR0_PRIMARY"
            PERSISTENT_CHUNK="$PROPOSED_CHUNK"
            PERSISTENT_NETWORK="$IPV4_NETWORK"
            save_persistent_state || exit 1
            
            # Assign both IPs to br0
            ip addr add "${BR0_PRIMARY}/${IPV4_NETWORK#*/}" dev "$CONTROL_IFACE"
            ip addr add "${BR0_SECONDARY}/${IPV4_NETWORK#*/}" dev "$CONTROL_IFACE"
            log "Assigned br0 primary: $BR0_PRIMARY, secondary (gateway): $BR0_SECONDARY"
            
            
            # Configure dnsmasq
            configure_dnsmasq "$BR0_PRIMARY" "$BR0_SECONDARY" "$DHCP_START" "$DHCP_END" || exit 1

            log "Successfully claimed chunk ${PROPOSED_CHUNK}"
            
            # Write chunk and block size for the identity publisher to pick up
            printf '%s\n' "$CHUNK_SIZE" > /var/run/my_ipv4_chunk_size
            printf '%s\n' "$PROPOSED_CHUNK" > /var/run/my_ipv4_chunk
        fi
        ;;

    "CONFIGURED")
        if [ -z "$PERSISTENT_CHUNK" ]; then
            update_avahi_host "" || exit 1
        fi
        # Check for peer overlap with the whole block, even with different
        # primary addresses.
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
                save_persistent_state || exit 1
                rm -f /var/run/my_ipv4_chunk /var/run/my_ipv4_chunk_size
            fi
        else
            # No conflict, only reconfigure if something actually changed
            if [ -n "$PERSISTENT_CHUNK" ]; then
                printf '%s\n' "$CHUNK_SIZE" > /var/run/my_ipv4_chunk_size
                printf '%s\n' "$PERSISTENT_CHUNK" > /var/run/my_ipv4_chunk

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

                # Rebuild on pool/name mismatches, extra address overrides,
                # fixed binding or DNS settings that bypass resolved.
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
                # Rules accept pool source addresses without checking leases.
                [ -x /usr/local/bin/manet-ui-firewall.sh ] && \
                    /usr/local/bin/manet-ui-firewall.sh >/dev/null 2>&1 || true
            fi
        fi
        ;;
esac

exit 0
