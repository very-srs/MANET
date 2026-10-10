#!/bin/bash
. "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet-common.sh" || exit 1
# Restore configuration locally if mesh_ssid, mesh_key or ipv4_network
# changes prevent reconnection. State persists in /var/lib across reboots.
# Commands: arm <version> snapshots files, peer count and deadline, check
# commits or restores after expiry, commit clears state, status prints it.
# Use boot-clock deadlines tied to boot ID, unaffected by wall-clock steps.
# The first reboot grants full grace to re-form the mesh. Later boots check
# immediately so repeated reboots cannot indefinitely postpone rollback.
# A node with no prior peers commits because it has no reconnection baseline.

usage() {
    printf '%s\n' \
        'usage: mesh-config-rollback.sh {arm VERSION|check|commit|status}' >&2
}

case "${1:-check}" in
    arm) [ $# -eq 2 ] && [ -n "$2" ] || { usage; exit 1; } ;;
    check|commit|status) [ $# -le 1 ] || { usage; exit 1; } ;;
    *) usage; exit 1 ;;
esac

STATE_DIR="${MANET_ROLLBACK_DIR:-/var/lib/manet-config-rollback}"
STATE_FILE="$STATE_DIR/state"
MESH_CONF="${MANET_MESH_CONF:-/etc/mesh.conf}"
WPA_DIR="${MANET_WPA_DIR:-/etc/wpa_supplicant}"
BATCTL="${BATCTL:-/usr/sbin/batctl}"
RUN_DIR="${MANET_RUN_DIR:-/var/run}"
SUPPLICANT_HELPER="${MANET_SUPPLICANT_HELPER:-$(dirname "$0")/manet_supplicant.py}"
LOG_TAG="CONFIG-ROLLBACK"
# Grace seconds for supplicant restart, SAE and batman re-discovery.
GRACE_SECONDS="${MANET_ROLLBACK_GRACE:-300}"
BOOT_ID_FILE="${MANET_BOOT_ID_FILE:-/proc/sys/kernel/random/boot_id}"

# Whole seconds on the boot clock, and this boot's ID.


boot_id() {
    local id
    read -r id < "$BOOT_ID_FILE" || return 1
    [[ "$id" =~ ^[0-9a-f-]+$ ]] || return 1
    printf '%s\n' "$id"
}

log() {
    printf '%s\n' "$LOG_TAG: $1" >&2
}

peer_count() {
    # Multiple routes to one originator count once, and a failed query must
    # never mean "solo node": mesh-peer-count.sh exits non-zero instead.
    "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/mesh-peer-count.sh" --batctl "$BATCTL"
}

# Arm rollback
do_arm() (
    local version="$1"
    [[ "$version" =~ ^[0-9a-f]{6,64}$ ]] || { log "ERROR: arm needs a valid version"; return 1; }
    [[ "$GRACE_SECONDS" =~ ^[0-9]+$ ]] || { log "ERROR: invalid rollback grace period"; return 1; }
    [ ! -f "$STATE_FILE" ] || { log "ERROR: a rollback trial is already armed"; return 1; }

    local peers deadline snapshot f now boot
    peers=$(peer_count) || { log "ERROR: cannot establish peers before the change"; return 1; }
    now=$(uptime_now) && boot=$(boot_id) || { log "ERROR: cannot read the boot clock"; return 1; }
    deadline=$(( now + 10#$GRACE_SECONDS ))

    # Publish only a complete snapshot. Failed copies/writes leave no armed
    # state, and a second change cannot replace an ongoing trial's backup.
    umask 077
    mkdir -p "$(dirname "$STATE_DIR")" || return 1
    snapshot=$(mktemp -d "${STATE_DIR}.new.XXXXXX") || return 1
    trap 'rm -rf "$snapshot"' EXIT
    mkdir "$snapshot/wpa" || return 1
    cp -aL "$MESH_CONF" "$snapshot/mesh.conf" 2>/dev/null || {
        log "ERROR: cannot snapshot $MESH_CONF"; return 1; }
    for f in "$WPA_DIR"/wpa_supplicant-wlan*.conf; do
        [[ "$f" == *-uplink.conf ]] && continue
        [ -e "$f" ] || [ -L "$f" ] || continue
        cp -aL "$f" "$snapshot/wpa/" 2>/dev/null || {
            log "ERROR: cannot snapshot $f"; return 1; }
    done

    {
        printf '%s\n' "VERSION='$version'"
        printf '%s\n' "PEERS_BEFORE=$peers"
        printf '%s\n' "BOOT_ID='$boot'"
        printf '%s\n' "DEADLINE=$deadline"
        echo "REARMED=0"
    } > "$snapshot/state" || { log "ERROR: cannot write rollback state"; return 1; }
    rm -rf "$STATE_DIR" || return 1
    mv -T "$snapshot" "$STATE_DIR" || return 1

    log "Armed for version $version: $peers peer(s) before, deadline in ${GRACE_SECONDS}s"
)

# Restore configuration
do_restore() {
    log "Mesh did not re-form: restoring the previous configuration"
    # Once restoration starts, finish it even if a partial restore brings a
    # peer back. Otherwise the next check could discard an unfinished backup.
    touch "$STATE_DIR/restoring" || return 1

    cp -a "$STATE_DIR/mesh.conf" "$MESH_CONF" 2>/dev/null || {
        log "ERROR: cannot restore $MESH_CONF"; return 1; }
    for f in "$STATE_DIR"/wpa/wpa_supplicant-wlan*.conf; do
        [[ "$f" == *-uplink.conf ]] && continue
        [ -e "$f" ] || continue
        cp -a "$f" "$WPA_DIR/$(basename "$f")" 2>/dev/null || {
            log "ERROR: cannot restore $f; keeping snapshot for retry"; return 1; }
    done

    # Recalculate the chunk from the restored ipv4_network.
    rm -f "$RUN_DIR/my_ipv4_chunk" "$RUN_DIR/my_ipv4_chunk_size" "$RUN_DIR/mesh_ipv4_state" 2>/dev/null

    local restart_failed=0
    python3 "$SUPPLICANT_HELPER" restart || restart_failed=1
    systemctl restart batman-enslave.service 2>/dev/null || restart_failed=1
    [ "$restart_failed" -eq 0 ] || {
        log "ERROR: cannot restart mesh services; keeping snapshot for retry"; return 1; }

    # Clear volatile UI/staging state. Persistent admin replay history stays
    # intact: retrying a rolled-back change requires a fresh admin transaction.
    rm -f "$RUN_DIR/mesh_applied_config_version" "$RUN_DIR/mesh_pending_config.json" \
          "$RUN_DIR/mesh_config_ack_version" 2>/dev/null

    rm -rf "$STATE_DIR"
    log "Restore complete"
}

# Restart the grace on this boot's clock, once per trial.
rearm() {
    local now="$1" boot="$2" tmp
    [[ "$GRACE_SECONDS" =~ ^[0-9]+$ ]] || return 1
    tmp=$(mktemp "$STATE_DIR/state.XXXXXX") || return 1
    {
        printf '%s\n' "VERSION='$VERSION'"
        printf '%s\n' "PEERS_BEFORE=$PEERS_BEFORE"
        printf '%s\n' "BOOT_ID='$boot'"
        printf '%s\n' "DEADLINE=$(( now + 10#$GRACE_SECONDS ))"
        echo "REARMED=1"
    } > "$tmp" && mv -f "$tmp" "$STATE_FILE" || { rm -f "$tmp"; return 1; }
}

# Check trial outcome
do_check() {
    [ -f "$STATE_FILE" ] || return 0

    VERSION=''; PEERS_BEFORE=''; DEADLINE=''; BOOT_ID=''; REARMED=''
    # Source the state file written by do_arm.
    . "$STATE_FILE" || return 1
    if [[ ! "$PEERS_BEFORE" =~ ^[0-9]+$ || ! "$DEADLINE" =~ ^[0-9]+$ ]]; then
        log "ERROR: incomplete rollback state; keeping snapshot"
        return 1
    fi
    if [ -f "$STATE_DIR/restoring" ]; then
        do_restore
        return $?
    fi

    local now boot
    now=$(uptime_now) && boot=$(boot_id) || { log "ERROR: cannot read the boot clock"; return 1; }
    if [ "$BOOT_ID" != "$boot" ]; then
        # A deadline from another boot is invalid. Missing BOOT_ID means
        # the deadline uses wall seconds and also needs replacement.
        if [ "${REARMED:-0}" = 0 ]; then
            rearm "$now" "$boot" || return 1
            log "Rebooted during the trial for $VERSION: deadline in ${GRACE_SECONDS}s"
            return 0
        fi
        log "Rebooted again during the trial for $VERSION: checking now"
    elif [ "$now" -lt "$DEADLINE" ]; then
        return 0
    fi

    if [ "${PEERS_BEFORE:-0}" -eq 0 ]; then
        log "Committing $VERSION: no peers before the change, nothing to compare"
        rm -rf "$STATE_DIR"
        return 0
    fi

    local peers_now
    if ! peers_now=$(peer_count); then
        log "Cannot verify peer recovery at the deadline; restoring the saved configuration"
    elif [ "$peers_now" -gt 0 ]; then
        log "Committing $VERSION: mesh re-formed ($peers_now peer(s))"
        rm -rf "$STATE_DIR"
        return 0
    fi

    do_restore
}

case "${1:-check}" in
    arm)    do_arm "$2" ;;
    check)  do_check ;;
    commit) rm -rf "$STATE_DIR"; log "Armed state cleared; change kept" ;;
    status)
        if [ -f "$STATE_FILE" ]; then
            cat "$STATE_FILE"
            echo "ARMED=true"
        else
            echo "ARMED=false"
        fi
        ;;
    *)
        usage
        exit 1
        ;;
esac
