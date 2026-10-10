#!/bin/bash
# Helpers using node-manager scheduling and interface globals.

# Throttle uplink reconciliation using manager-owned gateway state.
detect_and_update_gateway_state() {
    local mono
    mono=$(uptime_now) || return

    local time_since_check=$(( mono - LAST_GW_CHECK ))
    if [ "$time_since_check" -lt "$GW_CHECK_INTERVAL" ] &&
            [ -f "$GATEWAY_STATE_FILE" ]; then
        return
    fi

    LAST_GW_CHECK=$mono
    [ -x /usr/local/bin/manet-uplink-dispatch.sh ] &&
        /usr/local/bin/manet-uplink-dispatch.sh reconcile \
            >/dev/null 2>&1 || true
}

# Launch enabled service elections once per manager interval.
run_service_elections() {
    local now election_script
    [ -s /var/run/my_ipv4_chunk ] || return 0
    read -r now _ < "${MESH_UPTIME_FILE:-/proc/uptime}"
    now=${now%.*}
    if [ -n "$LAST_ELECTION_RUN" ] &&
            [ $((now - LAST_ELECTION_RUN)) -lt "$ELECTION_INTERVAL" ]; then
        return 0
    fi
    LAST_ELECTION_RUN=$now
    for election_script in "${ELECTION_DIR:-/usr/local/bin}"/*-election.sh; do
        [[ -f "$election_script" && -x "$election_script" ]] || continue
        # ACS schedules channel elections separately; static skips them.
        [[ "$election_script" =~ channel-election ]] && continue
        if [[ "$election_script" =~ mediamtx-election ]]; then
            [[ "$(grep "^mtx=" /etc/mesh.conf 2>/dev/null |
                cut -d'=' -f2)" == "y" ]] || continue
        fi
        if [[ "$election_script" =~ mumble-election ]]; then
            [[ "$(grep "^mumble=" /etc/mesh.conf 2>/dev/null |
                cut -d'=' -f2)" == "y" ]] || continue
        fi
        "$election_script" &
    done
}

# Check the active MediaMTX service and its local virtual address.
is_hosting_service() {
    if systemctl is-active --quiet mediamtx.service; then
        local IPV4_NETWORK=""
        if [ -f /etc/mesh.conf ]; then
            while IFS='=' read -r key value; do
                [[ -z "$key" || "$key" =~ ^[[:space:]]*# ]] && continue
                case "$key" in
                    ipv4_network)
                        IPV4_NETWORK="$value"
                        ;;
                esac
            done < /etc/mesh.conf
        fi
        [ -z "$IPV4_NETWORK" ] && return 1

        local CALC_OUTPUT=$(manet-ipcalc.sh "$IPV4_NETWORK" 2>/dev/null)
        local FIRST_IP=$(printf '%s\n' "$CALC_OUTPUT" |
            awk '/HostMin/ {print $2}')
        local MEDIAMTX_IPV4_VIP="${FIRST_IP%.*}.$((${FIRST_IP##*.} + 1))"
        ip addr show dev "$CONTROL_IFACE" |
            grep -q "inet $MEDIAMTX_IPV4_VIP/" && return 0
    fi
    return 1
}

# Check the active Mumble service and its local virtual address.
is_hosting_mumble_service() {
    if systemctl is-active --quiet mumble-server.service; then
        if [ -f /etc/mesh.conf ]; then
            while IFS='=' read -r key value; do
                [[ -z "$key" || "$key" =~ ^[[:space:]]*# ]] && continue
                case "$key" in
                    ipv4_network)
                        IPV4_NETWORK="$value"
                        ;;
                esac
            done < /etc/mesh.conf
        fi

        local CALC_OUTPUT
        CALC_OUTPUT=$(manet-ipcalc.sh "$IPV4_NETWORK" 2>/dev/null)
        local FIRST_IP
        FIRST_IP=$(printf '%s\n' "$CALC_OUTPUT" |
            awk '/HostMin/ {print $2}')
        local MUMBLE_IPV4_VIP="${FIRST_IP%.*}.$((${FIRST_IP##*.} + 2))"
        ip addr show dev "$CONTROL_IFACE" |
            grep -q "inet $MUMBLE_IPV4_VIP/" && return 0
    fi
    return 1
}
