#!/bin/bash
# Helpers using node-manager scheduling and interface globals.

. "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet-service-common.sh"

# Throttle uplink reconciliation using manager-owned gateway state.
detect_and_update_gateway_state() {
    local mono
    mono=$(uptime_now) || return

    # LAST_GW_CHECK and GW_CHECK_INTERVAL use boot-clock seconds.
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
    # Record launch time before starting the independent election jobs.
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

# Check MediaMTX listeners and its local VIP for serving telemetry.
is_hosting_service() {
    service_election_hosting mediamtx
}

# Check Mumble listeners and its local VIP for serving telemetry.
is_hosting_mumble_service() {
    service_election_hosting mumble
}
