#!/bin/bash
#
# route-manager.sh: A persistent service to manage the default internet route
# based on B.A.T.M.A.N.-adv gateway selection using kernel events.
#

# How often to run a safety check even if no events are received
EVENT_TIMEOUT=60

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - ROUTE-MGR: $1"
}

update_default_route() {
    log "Checking gateway status..."
    if /usr/sbin/batctl gwl -H | grep -q "=>"; then
        log "Gateway found. Ensuring default routes point to bat0."
        ip route replace default dev bat0
        ip -6 route replace default dev bat0
    else
        log "No gateway available. Removing default routes."
        ip route del default dev bat0 2>/dev/null
        ip -6 route del default dev bat0 2>/dev/null
    fi
}

# --- Main Event Loop ---
log "Starting Route Manager (Event-Driven Mode)."
# Perform an initial check on startup
update_default_route

# Continuously listen for events from batctl's kernel interface
/usr/local/sbin/batctl event | while read -t $EVENT_TIMEOUT event_line; do

    # Check if the read timed out (exit code > 128)
    # OR if the event line contains a gateway change notification.
    if [[ $? -gt 128 || "$event_line" == *"Gateway event"* ]]; then
        update_default_route
    fi
done

log "batctl event command exited unexpectedly. Restarting..."
exit 1

