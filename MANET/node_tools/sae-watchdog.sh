#!/bin/bash
# Recover missing bat0 interfaces after MESH-SAE-AUTH-BLOCKED events.
# Four failed SAE handshakes block a peer for 300 s, which can prevent boot
# attachment. Restart wpa_supplicant to clear the block, then rerun
# batman-enslave to attach interfaces.
. "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet-common.sh" || exit 1

MESH_IF_FILE="${MANET_MESH_IF_FILE:-/var/lib/mesh_if}"
# Poll role readiness every 15 seconds unless overridden.
WAIT_SECS="${SAE_WATCHDOG_WAIT_SECS:-15}"
STANDARD_MESH_INTERFACES=""

# Write a prefixed message through the shared logging helper.
log() {
    manet_log SAE-WATCHDOG "$*"
}

# radio_iface_enabled: manet-common.sh

# Print the S1G or conventional supplicant unit for the interface argument.
service_unit_for_iface() {
    local iface="$1"
    if [ -s /var/lib/halow_if ] && grep -qx "$iface" /var/lib/halow_if 2>/dev/null; then
        printf '%s\n' "wpa_supplicant-s1g-${iface}.service"
        return 0
    fi
    printf '%s\n' "wpa_supplicant@${iface}.service"
}

# Log the reason, restart enabled STANDARD_MESH_INTERFACES, then reattach.
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

    # Wait for supplicant mesh point mode before batman-enslave.
    sleep 10

    log "Restarting batman-enslave to re-add interfaces to bat0..."
    systemctl restart batman-enslave.service 2>/dev/null && \
        log "batman-enslave restarted successfully" || \
        log "WARNING: failed to restart batman-enslave"
}

# Check that every enabled interface appears in the bat0 member list.
# Only restart if bat0 is actually missing mesh interfaces: avoids
# thrashing on a healthy node that just happens to see a blocked peer.
bat0_has_all_interfaces() {
    local members
    # Match interface-name prefixes in batctl if output, ignoring status text.
    members=$(batctl if 2>/dev/null) || return 1
    for iface in $STANDARD_MESH_INTERFACES; do
        radio_iface_enabled "$iface" || continue
        [[ $'\n'"$members" == *$'\n'"${iface}:"* ]] || return 1
    done
    return 0
}

# Wait for an enabled mesh interface before monitoring SAE events. Exiting
# would trigger Restart=always and pull in batman-enslave before configs
# exist, or while every mesh radio is disabled.
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

        # Restart only when bat0 lacks mesh interfaces. With all attached, a
        # blocked peer does not justify disrupting a healthy mesh.
        if bat0_has_all_interfaces; then
            log "bat0 has all expected interfaces: skipping restart (blocked peer may be genuinely unreachable)"
            continue
        fi

        log "bat0 is missing mesh interfaces: initiating recovery restart"
        restart_mesh "$line"
    fi
done
