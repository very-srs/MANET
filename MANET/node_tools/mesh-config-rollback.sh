#!/usr/bin/env bash
# Config rollback safety net
# Changing mesh_ssid, mesh_key or ipv4_network drops the mesh while every node
# reconnects. A bad setting can prevent remote correction, so each node must
# be able to restore its previous configuration without network access.
#
#   arm <version>   snapshot the files a dangerous apply rewrites, record how
#                   many batman peers we had, and set a deadline
#   check           called every node-manager cycle; a no-op until the deadline
#                   passes, then either commits or restores
#   commit          clear the armed state, keeping the new config
#   status          print the armed state as shell assignments
#
# State lives in /var/lib, not /var/run: a dangerous apply can end in a reboot,
# and the node still has to honour the deadline afterwards.
#
# A node with no peers before the change has nothing to compare against and
# commits: that is the solo bench case, where "the mesh did not come back"
# cannot be distinguished from "there was never anyone there".

STATE_DIR="${MANET_ROLLBACK_DIR:-/var/lib/manet-config-rollback}"
STATE_FILE="$STATE_DIR/state"
MESH_CONF="${MANET_MESH_CONF:-/etc/mesh.conf}"
WPA_DIR="${MANET_WPA_DIR:-/etc/wpa_supplicant}"
BATCTL="${BATCTL:-/usr/sbin/batctl}"
RUN_DIR="${MANET_RUN_DIR:-/var/run}"
IFACE_STATE_DIR="${MANET_IFACE_STATE_DIR:-/var/lib}"
LOG_TAG="CONFIG-ROLLBACK"
# How long the mesh gets to re-form before we give up on the change. Supplicant
# restart, SAE, and batman re-discovery all have to fit inside it.
GRACE_SECONDS="${MANET_ROLLBACK_GRACE:-300}"

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - $LOG_TAG: $1"
}

peer_count() {
    # Selected text-table routes start with '*', and multiple routes can name
    # the same originator. A failed query must never mean "solo node".
    python3 - "$BATCTL" <<'PY'
import json
import re
import subprocess
import sys

try:
    result = subprocess.run(
        [sys.argv[1], 'meshif', 'bat0', 'originators_json'],
        check=True, capture_output=True, text=True, timeout=5,
    )
    rows = json.loads(result.stdout)
    if not isinstance(rows, list):
        raise ValueError('originators response is not a list')
    peers = set()
    for row in rows:
        mac = row.get('orig_address') if isinstance(row, dict) else None
        if not isinstance(mac, str) or not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', mac):
            raise ValueError('invalid originator address')
        peers.add(mac.lower())
    print(len(peers))
except (OSError, subprocess.SubprocessError, ValueError) as exc:
    print(f'Cannot read BATMAN peers: {exc}', file=sys.stderr)
    sys.exit(1)
PY
}

# ------------------------------------------------------------------ arm ------
do_arm() (
    local version="$1"
    [[ "$version" =~ ^[0-9a-f]{6,64}$ ]] || { log "ERROR: arm needs a valid version"; return 1; }
    [[ "$GRACE_SECONDS" =~ ^[0-9]+$ ]] || { log "ERROR: invalid rollback grace period"; return 1; }
    [ ! -f "$STATE_FILE" ] || { log "ERROR: a rollback trial is already armed"; return 1; }

    local peers deadline snapshot f
    peers=$(peer_count) || { log "ERROR: cannot establish peers before the change"; return 1; }
    deadline=$(( $(date +%s) + 10#$GRACE_SECONDS ))

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
        [ -e "$f" ] || [ -L "$f" ] || continue
        cp -aL "$f" "$snapshot/wpa/" 2>/dev/null || {
            log "ERROR: cannot snapshot $f"; return 1; }
    done

    {
        echo "VERSION='$version'"
        echo "PEERS_BEFORE=$peers"
        echo "DEADLINE=$deadline"
    } > "$snapshot/state" || { log "ERROR: cannot write rollback state"; return 1; }
    rm -rf "$STATE_DIR" || return 1
    mv -T "$snapshot" "$STATE_DIR" || return 1

    log "Armed for version $version: $peers peer(s) before, deadline in ${GRACE_SECONDS}s"
)

# --------------------------------------------------------------- restore -----
do_restore() {
    log "Mesh did not re-form: restoring the previous configuration"
    # Once restoration starts, finish it even if a partial restore brings a
    # peer back. Otherwise the next check could discard an unfinished backup.
    touch "$STATE_DIR/restoring" || return 1

    cp -a "$STATE_DIR/mesh.conf" "$MESH_CONF" 2>/dev/null || {
        log "ERROR: cannot restore $MESH_CONF"; return 1; }
    for f in "$STATE_DIR"/wpa/wpa_supplicant-wlan*.conf; do
        [ -e "$f" ] || continue
        cp -a "$f" "$WPA_DIR/$(basename "$f")" 2>/dev/null || {
            log "ERROR: cannot restore $f; keeping snapshot for retry"; return 1; }
    done

    # The chunk is derived from ipv4_network, so it has to be recalculated
    # against the restored value rather than kept.
    rm -f "$RUN_DIR/my_ipv4_chunk" "$RUN_DIR/mesh_ipv4_state" 2>/dev/null

    local restart_failed=0
    for iface in $(cat "$IFACE_STATE_DIR/mesh_if" "$IFACE_STATE_DIR/halow_if" 2>/dev/null); do
        [ -z "$iface" ] && continue
        systemctl restart "wpa_supplicant@${iface}.service" 2>/dev/null || \
        systemctl restart "wpa_supplicant-s1g-${iface}.service" 2>/dev/null || restart_failed=1
    done
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

# -------------------------------------------------------------- check --------
do_check() {
    [ -f "$STATE_FILE" ] || return 0

    VERSION=''; PEERS_BEFORE=''; DEADLINE=''
    # Our own file, written by do_arm: safe to source.
    . "$STATE_FILE" || return 1
    if [[ ! "$PEERS_BEFORE" =~ ^[0-9]+$ || ! "$DEADLINE" =~ ^[0-9]+$ ]]; then
        log "ERROR: incomplete rollback state; keeping snapshot"
        return 1
    fi
    if [ -f "$STATE_DIR/restoring" ]; then
        do_restore
        return $?
    fi

    local now
    now=$(date +%s)
    [ "$now" -lt "$DEADLINE" ] && return 0

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
        echo "usage: $(basename "$0") {arm <version>|check|commit|status}" >&2
        exit 2
        ;;
esac
