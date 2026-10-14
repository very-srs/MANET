#!/bin/bash
# Shared, serialized MediaMTX/Mumble handover. The caller supplies service
# settings and preparation/release hooks. Helpers never run as a daemon.

. "${MANET_TOOLS_DIR:-${BASH_SOURCE[0]%/*}}/manet-runtime-client.sh"

# Return 0 to exit a settled pass, 125 to reconcile, or a request error.
service_election_preflight() {
    local result rc
    result=$(MANET_RUNTIME_LOCK_WAIT=1 manet_runtime_call election "$1")
    rc=$?
    if [ "$rc" = 0 ]; then
        [ "$result" != skip ] || return 0
        return 125
    fi
    return "$rc"
}

# Reuse canonical VIPs in the resident worker, including during handover.
service_election_ipv6() {
    local result rc
    result=$(MANET_RUNTIME_LOCK_WAIT=1 manet_runtime_call service-vip "$1")
    rc=$?
    if [ "$rc" = 125 ]; then
        result=$("$2") || return 1
        python3 "${MANET_TOOLS_DIR:-/usr/local/bin}/manet_service_handover.py" \
            normalize "$result"
        return $?
    fi
    [ "$rc" = 0 ] || return "$rc"
    printf '%s\n' "$result"
}

# Serving telemetry uses the same live proc checks without starting ss.
service_election_hosting() {
    local result rc
    result=$(MANET_RUNTIME_LOCK_WAIT=1 manet_runtime_call hosting "$1")
    rc=$?
    if [ "$rc" = 125 ]; then
        result=$(python3 \
            "${MANET_TOOLS_DIR:-/usr/local/bin}/manet_service_handover.py" \
            hosting "$1")
        rc=$?
    fi
    [ "$rc" = 0 ] && [ "$result" = true ]
}

# Check unit activity and every required wildcard TCP/UDP listener.
service_election_ready() {
    local unit="$1" tcp_ports="$2" udp_ports="$3" port sockets
    systemctl is-active --quiet "$unit" || return 1
    for port in $tcp_ports; do
        sockets=$(ss -H -ltn "sport = :$port") || return 1
        printf '%s\n' "$sockets" | awk '{print $4}' |
            grep -qxE "(0\.0\.0\.0|\*|\[::\]|::):$port" || return 1
    done
    for port in $udp_ports; do
        sockets=$(ss -H -lun "sport = :$port") || return 1
        printf '%s\n' "$sockets" | awk '{print $4}' |
            grep -qxE "(0\.0\.0\.0|\*|\[::\]|::):$port" || return 1
    done
}

# Persist transition names under the election lock, logging each only once.
service_election_transition() {
    local previous=""
    [ ! -f "$LOCK_FILE.state" ] || read -r previous < "$LOCK_FILE.state"
    if [ "$previous" != "$1" ]; then
        printf '%s\n' "$1" > "$LOCK_FILE.state"
        log "$2"
    fi
}

# Cancel failed start/restart jobs until the next scheduled election.
service_election_start_failed() {
    service_election_transition start-failed "$1"
    systemctl stop "$SERVICE_UNIT" || return 1
    systemctl reset-failed "$SERVICE_UNIT" || return 1
    rm -f "$LOCK_FILE.prepared"
}

# Query exact address tokens, avoiding substring matches in IPv6 addresses.
service_election_has_vip() {
    ip -o addr show dev "$CONTROL_IFACE" | awk '{print $4}' | grep -qxF "$1"
}

# Release the addresses before stopping, then run the service's storage hook.
service_election_release() {
    if service_election_has_vip "$IPV4_VIP_WITH_MASK"; then
        ip addr del "$IPV4_VIP_WITH_MASK" dev "$CONTROL_IFACE" || return 1
    fi
    if service_election_has_vip "$SERVICE_IPV6_VIP_WITH_MASK"; then
        ip addr del "$SERVICE_IPV6_VIP_WITH_MASK" dev "$CONTROL_IFACE" || return 1
    fi
    if systemctl is-active --quiet "$SERVICE_UNIT"; then
        touch "$LOCK_FILE.release-pending" || return 1
        printf 'false\n' > "$LOCK_FILE.publish-pending" || return 1
        systemctl stop "$SERVICE_UNIT" || return 1
    fi
    if [ -f "$LOCK_FILE.release-pending" ]; then
        "$SERVICE_RELEASE_HOOK" || return 1
        rm -f "$LOCK_FILE.release-pending" || return 1
    fi
    systemctl reset-failed "$SERVICE_UNIT" 2>/dev/null || true
    rm -f "$LOCK_FILE.prepared"
}

# Retry a failed transition publication on the existing election schedule.
service_election_publish() {
    local helper="${MANET_TOOLS_DIR:-/usr/local/bin}/manet_service_handover.py"
    if python3 "$helper" publish "$SERVICE_KIND" "$MY_MAC" "$1" 2>"$LOCK_FILE.publish-error"; then
        rm -f "$LOCK_FILE.publish-pending" "$LOCK_FILE.publish-error" "$LOCK_FILE.publish-logged"
    else
        printf '%s\n' "$1" > "$LOCK_FILE.publish-pending"
        if [ ! -f "$LOCK_FILE.publish-logged" ]; then
            log 'Service status publication failed, retrying next election'
            touch "$LOCK_FILE.publish-logged"
        fi
    fi
}

# Run one election, or re-run it when a new scheduled invocation wakes a wait.
service_election_run() {
    local result winner had4 had6 previous since decision ready attempt
    local helper="${MANET_TOOLS_DIR:-/usr/local/bin}/manet_service_handover.py"
    exec 200>"$LOCK_FILE"
    if ! flock -n 200; then
        python3 "$helper" wake "$LOCK_FILE.wake"
        return 0
    fi
    while true; do
        # CIDR retirement holds this same lock. Never recreate a VIP computed
        # before retirement, including after a long successor wait.
        local current_cidr
        current_cidr=$(sed -n 's/^ipv4_network=//p' /etc/mesh.conf)
        if [ "$current_cidr" != "$IPV4_NETWORK" ]; then
            log 'CIDR changed during election; retrying with current VIPs'
            return 1
        fi
        if [ -f "$LOCK_FILE.release-pending" ]; then
            service_election_release || return 1
        fi
        result=$(python3 "$helper" rank "$SERVICE_KIND" "$REGISTRY_STATE_FILE") || return 1
        winner=${result%% *}
        had4=false
        had6=false
        service_election_has_vip "$IPV4_VIP_WITH_MASK" && had4=true
        service_election_has_vip "$SERVICE_IPV6_VIP_WITH_MASK" && had6=true
        if [ "$winner" = "$MY_MAC" ]; then
            previous=""
            [ ! -f "$LOCK_FILE.state" ] || read -r previous < "$LOCK_FILE.state"
            if { [ "$had4" != true ] && [ "$had6" != true ] && [ ! -f "$LOCK_FILE.prepared" ]; } ||
                    ! service_election_ready "$SERVICE_UNIT" "$SERVICE_TCP_PORTS" "$SERVICE_UDP_PORTS"; then
                if ! "$SERVICE_PREPARE_HOOK"; then
                    service_election_start_failed \
                        'Service preparation/start failed, no VIP claim'
                    return 1
                fi
                ready=false
                for ((attempt=0; attempt<5; attempt++)); do
                    if service_election_ready "$SERVICE_UNIT" "$SERVICE_TCP_PORTS" "$SERVICE_UDP_PORTS"; then
                        ready=true
                        break
                    fi
                    sleep 1
                done
                if [ "$ready" != true ]; then
                    service_election_start_failed \
                        'Service is not listening, no VIP claim'
                    return 1
                fi
                # Reuse a ready service if a subsequent VIP claim fails.
                touch "$LOCK_FILE.prepared"
            fi
            if { [ "$had4" != true ] || [ "$had6" != true ]; } && ! "$SERVICE_VERIFY_HOOK"; then
                service_election_transition storage-failed 'Working service data is not ready, no VIP claim'
                return 1
            fi
            # Preparation can take seconds. Never claim after losing rank.
            result=$(python3 "$helper" rank "$SERVICE_KIND" "$REGISTRY_STATE_FILE") || return 1
            [ "${result%% *}" = "$MY_MAC" ] || continue
            if [ "$had4" != true ]; then
                ip addr add "$IPV4_VIP_WITH_MASK" dev "$CONTROL_IFACE" || return 1
            fi
            if [ "$had6" != true ]; then
                # During overlap the incumbent still holds this VIP. DAD must
                # not reject the deliberate transfer as a duplicate address.
                if ! ip addr add "$SERVICE_IPV6_VIP_WITH_MASK" dev "$CONTROL_IFACE" nodad; then
                    # Do not publish half a new claim through telemetry.
                    [ "$had4" = true ] || ip addr del "$IPV4_VIP_WITH_MASK" dev "$CONTROL_IFACE"
                    return 1
                fi
            fi
            if [ "$had4" != true ] || [ "$had6" != true ] || [ "$previous" != leader ]; then
                read -r since _ < "${MESH_UPTIME_FILE:-/proc/uptime}"
                printf '%s\n' "$since" > "$LOCK_FILE.claim-uptime"
                python3 "$helper" announce "$CONTROL_IFACE" "${IPV4_VIP_WITH_MASK%/*}" "${SERVICE_IPV6_VIP_WITH_MASK%/*}" || {
                    service_election_publish true
                    service_election_transition announce-failed 'VIPs held, announcement failed, retry next election'
                    return 1
                }
                service_election_publish true
            elif [ -f "$LOCK_FILE.publish-pending" ]; then
                service_election_publish true
            fi
            rm -f "$LOCK_FILE.since" "$LOCK_FILE.ignored" "$LOCK_FILE.ignored.error"
            rm -f "$LOCK_FILE.ignored.probe-error"
            service_election_transition leader 'Serving on both VIPs'
            "${SERVICE_SERVING_HOOK:-true}" || return 1
            return 0
        fi
        if [ "$had4" != true ] && [ "$had6" != true ]; then
            service_election_release || return 1
            if [ -f "$LOCK_FILE.publish-pending" ]; then
                service_election_publish false
            fi
            service_election_transition follower 'Following elected service host'
            return 0
        fi
        if [ "$winner" = - ]; then
            service_election_transition no-winner 'No eligible successor, retaining service and VIPs'
            return 0
        fi
        previous=""
        [ ! -f "$LOCK_FILE.state" ] || read -r previous < "$LOCK_FILE.state"
        if [ "$previous" != "waiting:$winner" ]; then
            if [ -f "$LOCK_FILE.claim-uptime" ]; then
                read -r since < "$LOCK_FILE.claim-uptime"
            else
                read -r since _ < "${MESH_UPTIME_FILE:-/proc/uptime}"
            fi
            printf '%s\n' "$since" > "$LOCK_FILE.since"
            rm -f "$LOCK_FILE.ignored" "$LOCK_FILE.ignored.error"
            rm -f "$LOCK_FILE.ignored.probe-error"
        fi
        service_election_transition "waiting:$winner" "Waiting for successor $winner, retaining service and VIPs until it claims"
        read -r since < "$LOCK_FILE.since"
        decision=$(python3 "$helper" wait "$SERVICE_KIND" \
            "$REGISTRY_STATE_FILE" "$winner" "${IPV4_VIP_WITH_MASK%/*}" \
            "${SERVICE_IPV6_VIP_WITH_MASK%/*}" "$CONTROL_IFACE" "$since" \
            "$LOCK_FILE.wake" "$LOCK_FILE.ignored" \
            2> >(while IFS= read -r line; do log "$line"; done)) || return 1
        case "$decision" in
            recheck) continue ;;
            ready:ARP|ready:NA|ready:registry|"ready:NS probe")
                service_election_release || return 1
                service_election_publish false
                result="Successor $winner claimed via ${decision#ready:},"
                service_election_transition follower \
                    "$result released service and VIPs"
                return 0 ;;
            *) return 0 ;;
        esac
    done
}
