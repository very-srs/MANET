#!/bin/bash
# Synchronous root-only requests to the existing channel-agreement process.
# 125 means unavailable/busy, no request sent: use the one-shot fallback.
# After submitting, failures/timeouts never replay a possibly applied action.
# Send a helper name and optional argument, then print its result file.
# The subshell closes FIFO and lock descriptors when the request finishes.
manet_runtime_call() (
    local directory="${MANET_RUNTIME_DIR:-/run/manet-runtime}" pid generation token reply rc output
    [ -r "$directory/ready" ] && [ -p "$directory/request" ] && [ -p "$directory/reply" ] || return 125
    IFS=' ' read -r pid generation < "$directory/ready" || return 125
    [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null || return 125
    exec 8>"$directory/client.lock" || return 125
    # Skip a busy helper to preserve dispatcher/election latency.
    # Existing helper locks still serialize mutations.
    flock -n 8 || return 125
    # Check again after taking the lock or a daemon restart.
    [ "$(<"$directory/ready")" = "$pid $generation" ] || return 125
    # Match this request's token to ignore leftover FIFO replies.
    token="${BASHPID}_${RANDOM}_${RANDOM}"
    exec 6<>"$directory/reply" 7<>"$directory/request" || return 125
    printf '%s %s %s\n' "$token" "$1" "${2:--}" >&7 || return 124
    # Each reply read waits up to MANET_RUNTIME_WAIT seconds, default 90.
    # A submitted request that times out returns 124, never the fallback code.
    while IFS=' ' read -r -t "${MANET_RUNTIME_WAIT:-90}" reply rc <&6; do
        [ "$reply" = "$token" ] || continue
        # Accept only decimal shell exit codes from 0 through 255.
        [[ "$rc" =~ ^[0-9]{1,3}$ ]] && [ "$rc" -le 255 ] || return 124
        # Reserve 125 for unsubmitted requests, even if a helper returns it.
        [ "$rc" != 125 ] || rc=1
        [ -f "$directory/result.$token" ] || return 1
        [ "$rc" = 0 ] || return "$rc"
        output=$(<"$directory/result.$token")
        case "$1" in
            ipv4|mcs|interfaces|ap-mesh|election)
                [[ "$output" == *[![:space:]]* ]] || return 1 ;;
            # IP is status-only. Boot permits a missing Syncthing certificate.
        esac
        printf '%s' "$output"
        return "$rc"
    done
    return 124
)
