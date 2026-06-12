#!/bin/bash
# Watchdog: re-enslaves HaLow (and standard mesh) interfaces into bat0 if they fall out.
# Runs continuously after batman-enslave.service. Safe to restart.

log() { echo "[$(date +'%Y-%m-%d %H:%M:%S')] ENSLAVE-WATCH: $*" | systemd-cat -t batman-enslave-watch; }

radio_iface_enabled() {
    python3 - "$1" <<'PY'
import json, sys
iface = sys.argv[1]
try:
    with open('/var/lib/mesh_radio_state.json') as f:
        state = json.load(f).get('desired', {}).get(iface, 'up')
except Exception:
    state = 'up'
sys.exit(1 if state == 'down' else 0)
PY
}

batman_mainif() {
    batctl bat0 o 2>/dev/null | sed -n '1s/.*MainIF\/MAC: \([^/]*\)\/.*/\1/p'
}

restore_halow_primary_if_needed() {
    local halow mainif now last cooldown_file

    halow="$(echo "$HALOW_IFS" | awk 'NF {print $1; exit}')"
    [ -n "$halow" ] || return 0
    radio_iface_enabled "$halow" || return 0
    ip link show "$halow" >/dev/null 2>&1 || return 0
    batctl bat0 if 2>/dev/null | grep -q "^${halow}:" || return 0

    mainif="$(batman_mainif)"
    [ -n "$mainif" ] || return 0
    [ "$mainif" = "$halow" ] && return 0

    cooldown_file="/run/batman-enslave-watch-halow-primary-reset"
    now="$(date +%s)"
    last="$(cat "$cooldown_file" 2>/dev/null || echo 0)"
    if [ $((now - last)) -lt 60 ]; then
        return 0
    fi
    echo "$now" > "$cooldown_file"

    log "WARNING: bat0 MainIF is $mainif, expected HaLow $halow; rebuilding bat0 membership"
    /usr/local/bin/batman-if-setup.sh stop 2>/dev/null || true
    sleep 2
    /usr/local/bin/batman-if-setup.sh start 2>/dev/null || true
}

while true; do
    sleep 8

    HALOW_IFS="$(cat /var/lib/halow_if 2>/dev/null)"
    MESH_IFS="$(cat /var/lib/mesh_if 2>/dev/null)"

    for IFACE in $HALOW_IFS $MESH_IFS; do
        # Skip if radio-state says down
        radio_iface_enabled "$IFACE" || continue

        # Skip if interface doesn't exist
        ip link show "$IFACE" >/dev/null 2>&1 || continue

        # Check if already in bat0
        if batctl bat0 if 2>/dev/null | grep -q "^${IFACE}:"; then
            continue
        fi

        # Interface should be in bat0 but isn't — re-enslave
        log "WARNING: $IFACE not in bat0, re-enslaving..."
        ip link set "$IFACE" up 2>/dev/null || true
        ip link set "$IFACE" mtu 1532 2>/dev/null || true

        for attempt in 1 2 3; do
            if batctl bat0 if add "$IFACE" 2>/dev/null; then
                sleep 1
                if batctl bat0 if 2>/dev/null | grep -q "^${IFACE}:"; then
                    log "OK: $IFACE re-enslaved to bat0 (attempt $attempt)"
                    break
                fi
            fi
            sleep 2
        done
    done

    restore_halow_primary_if_needed
done
