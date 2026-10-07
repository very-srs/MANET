#!/bin/bash
#
# sae-watchdog.sh: monitors wpa_supplicant for MESH-SAE-AUTH-BLOCKED events
# and automatically restarts wpa_supplicant + batman-enslave to recover.
#
# Background: when SAE handshake fails 4 times, wpa_supplicant blocks the peer
# for 300 seconds. If this happens at boot before any peers are established,
# batman-enslave ends up with no slaves and the node never joins the mesh.
# A restart of wpa_supplicant clears the block state; batman-enslave re-run
# re-adds the interfaces.
#
. "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet-common.sh" || exit 1

MESH_IF_FILE="${MANET_MESH_IF_FILE:-/var/lib/mesh_if}"
WAIT_SECS="${SAE_WATCHDOG_WAIT_SECS:-15}"
STANDARD_MESH_INTERFACES=""

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] - SAE-WATCHDOG: $*"
}

# radio_iface_enabled: manet-common.sh

service_unit_for_iface() {
    local iface="$1"
    if [ -s /var/lib/halow_if ] && grep -qx "$iface" /var/lib/halow_if 2>/dev/null; then
        echo "wpa_supplicant-s1g-${iface}.service"
        return 0
    fi
    echo "wpa_supplicant@${iface}.service"
}

restart_mesh() {
    local reason="$1"
    log "Triggered by: $reason"
    log "Restarting wpa_supplicant for all standard mesh interfaces..."

    for iface in $STANDARD_MESH_INTERFACES; do
        if ! radio_iface_enabled "$iface"; then
            log "Skipping wpa_supplicant@${iface}.service (radio-state says down)"
            continue
        fi
        systemctl restart "wpa_supplicant@${iface}.service" 2>/dev/null && \
            log "Restarted wpa_supplicant@${iface}.service" || \
            log "WARNING: failed to restart wpa_supplicant@${iface}.service"
    done

    # Give wpa_supplicant time to re-establish mesh point mode before batman-enslave
    sleep 10

    log "Restarting batman-enslave to re-add interfaces to bat0..."
    systemctl restart batman-enslave.service 2>/dev/null && \
        log "batman-enslave restarted successfully" || \
        log "WARNING: failed to restart batman-enslave"
}

# Track which interfaces already have all bat0 slaves active.
# Only restart if bat0 is actually missing mesh interfaces: avoids
# thrashing on a healthy node that just happens to see a blocked peer.
bat0_has_all_interfaces() {
    local members
    members=$(batctl if 2>/dev/null) || return 1
    for iface in $STANDARD_MESH_INTERFACES; do
        radio_iface_enabled "$iface" || continue
        [[ $'\n'"$members" == *$'\n'"${iface}:"* ]] || return 1
    done
    return 0
}

# Monitor journald for SAE block events across enabled mesh interfaces.
# Until there is one (first boot before radio-setup assigns roles, or every
# mesh radio turned off), wait here. Exiting instead made Restart=always
# re-run the unit, and each start pulled in batman-enslave, which starts the
# supplicants before they have a config.
JOURNAL_ARGS=()
waiting=""
while :; do
    STANDARD_MESH_INTERFACES=""
    [ -s "$MESH_IF_FILE" ] && STANDARD_MESH_INTERFACES=$(<"$MESH_IF_FILE")
    JOURNAL_ARGS=()
    for iface in $STANDARD_MESH_INTERFACES; do
        radio_iface_enabled "$iface" || continue
        JOURNAL_ARGS+=("-fu" "$(service_unit_for_iface "$iface")")
    done
    [ ${#JOURNAL_ARGS[@]} -gt 0 ] && break
    if [ -z "$waiting" ]; then
        log "No enabled mesh interfaces yet; waiting for them"
        waiting=1
    fi
    sleep "$WAIT_SECS"
done

log "Starting SAE watchdog (monitoring: ${STANDARD_MESH_INTERFACES% })"

journalctl "${JOURNAL_ARGS[@]}" \
    --output=cat 2>/dev/null | \
while IFS= read -r line; do
    if [[ "$line" == *MESH-SAE-AUTH-BLOCKED* ]]; then
        log "Detected: $line"

        # Only react if bat0 is missing interfaces: if we already have
        # all mesh interfaces in bat0 the block is on a genuinely bad peer
        # and restarting would cause unnecessary disruption.
        if bat0_has_all_interfaces; then
            log "bat0 has all expected interfaces: skipping restart (blocked peer may be genuinely unreachable)"
            continue
        fi

        log "bat0 is missing mesh interfaces: initiating recovery restart"
        restart_mesh "$line"
    fi
done
