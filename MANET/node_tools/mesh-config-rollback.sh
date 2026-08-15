#!/usr/bin/env bash
# ==============================================================================
# Config rollback safety net
# ==============================================================================
# Changing mesh_ssid, mesh_key or ipv4_network drops the mesh while every node
# reconnects. If the change was wrong, the mesh does not come back — and with
# it goes the only way to push a correction. Each node therefore has to be able
# to undo the change on its own, with no help from the network.
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
# commits — that is the solo bench case, where "the mesh did not come back"
# cannot be distinguished from "there was never anyone there".
# ==============================================================================

STATE_DIR="${MANET_ROLLBACK_DIR:-/var/lib/manet-config-rollback}"
STATE_FILE="$STATE_DIR/state"
MESH_CONF="${MANET_MESH_CONF:-/etc/mesh.conf}"
WPA_DIR="${MANET_WPA_DIR:-/etc/wpa_supplicant}"
BATCTL="${BATCTL:-/usr/sbin/batctl}"
LOG_TAG="CONFIG-ROLLBACK"
# How long the mesh gets to re-form before we give up on the change. Supplicant
# restart, SAE, and batman re-discovery all have to fit inside it.
GRACE_SECONDS="${MANET_ROLLBACK_GRACE:-300}"

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - $LOG_TAG: $1"
}

peer_count() {
    "$BATCTL" o 2>/dev/null | awk 'NR>2 && $1 ~ /^[0-9a-f:]{17}$/ {n++} END {print n+0}'
}

# ------------------------------------------------------------------ arm ------
do_arm() {
    local version="$1"
    [ -n "$version" ] || { log "ERROR: arm needs a version"; return 1; }

    rm -rf "$STATE_DIR"
    mkdir -p "$STATE_DIR/wpa" || { log "ERROR: cannot create $STATE_DIR"; return 1; }

    cp -a "$MESH_CONF" "$STATE_DIR/mesh.conf" 2>/dev/null || {
        log "ERROR: cannot snapshot $MESH_CONF"; return 1; }
    cp -a "$WPA_DIR"/wpa_supplicant-wlan*.conf "$STATE_DIR/wpa/" 2>/dev/null || true

    local peers deadline
    peers=$(peer_count)
    deadline=$(( $(date +%s) + GRACE_SECONDS ))

    {
        echo "VERSION='$version'"
        echo "PEERS_BEFORE=$peers"
        echo "DEADLINE=$deadline"
    } > "$STATE_FILE"

    log "Armed for version $version: $peers peer(s) before, deadline in ${GRACE_SECONDS}s"
}

# --------------------------------------------------------------- restore -----
do_restore() {
    log "Mesh did not re-form — restoring the previous configuration"

    cp -a "$STATE_DIR/mesh.conf" "$MESH_CONF" 2>/dev/null || {
        log "ERROR: cannot restore $MESH_CONF"; return 1; }
    for f in "$STATE_DIR"/wpa/wpa_supplicant-wlan*.conf; do
        [ -e "$f" ] || continue
        cp -a "$f" "$WPA_DIR/$(basename "$f")" 2>/dev/null || true
    done

    # The chunk is derived from ipv4_network, so it has to be recalculated
    # against the restored value rather than kept.
    rm -f /var/run/my_ipv4_chunk /var/run/mesh_ipv4_state 2>/dev/null

    for iface in $(cat /var/lib/mesh_if /var/lib/halow_if 2>/dev/null); do
        [ -z "$iface" ] && continue
        systemctl restart "wpa_supplicant@${iface}.service" 2>/dev/null || \
        systemctl restart "wpa_supplicant-s1g-${iface}.service" 2>/dev/null || true
    done
    systemctl restart batman-enslave.service 2>/dev/null || true

    # Forget that the rolled-back version was ever applied, so a later
    # re-broadcast of it is treated as new rather than skipped.
    rm -f /var/run/mesh_applied_config_version /var/run/mesh_pending_config.json \
          /var/run/mesh_config_ack_version 2>/dev/null

    rm -rf "$STATE_DIR"
    log "Restore complete"
}

# -------------------------------------------------------------- check --------
do_check() {
    [ -f "$STATE_FILE" ] || return 0

    VERSION=''; PEERS_BEFORE=0; DEADLINE=0
    # Our own file, written by do_arm — safe to source.
    . "$STATE_FILE"

    local now
    now=$(date +%s)
    [ "$now" -lt "$DEADLINE" ] && return 0

    if [ "${PEERS_BEFORE:-0}" -eq 0 ]; then
        log "Committing $VERSION: no peers before the change, nothing to compare"
        rm -rf "$STATE_DIR"
        return 0
    fi

    local peers_now
    peers_now=$(peer_count)
    if [ "$peers_now" -gt 0 ]; then
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
