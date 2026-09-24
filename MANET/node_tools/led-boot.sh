#!/bin/bash
# External boot display; the service runs independently of boot completion.
source "$(dirname "${BASH_SOURCE[0]}")/manet-led-common.sh"

BLINK_HALF=0.5
POLL_TICKS=10
MESH_SOLID_SECS=10

mesh_ready() {
    systemctl is-active --quiet node-manager.service || return 1
    ip link show bat0 >/dev/null 2>&1
}

boot_blink() {
    # Button displays own the harness for their entire sequence; boot yields
    # between half-cycles without losing its state or blocking node services.
    if led_lock; then
        led_set "$@"
        led_sleep "$BLINK_HALF"
        led_unlock
    else
        sleep "$BLINK_HALF"
    fi
}

led_init
echo "led-boot: BOOTING"
toggle=0
while ! mesh_ready; do
    boot_blink "$toggle" 0 0
    toggle=$((1 - toggle))
done

echo "led-boot: WAITING_FOR_PEERS"
toggle=0
tick=0
unknown=0
while true; do
    # Green blink: confirmed zero peers / initial check pending.
    # Amber blink: query unavailable; never declare mesh formation on failure.
    boot_blink "$((unknown * toggle))" "$toggle" 0
    toggle=$((1 - toggle))
    tick=$((tick + 1))
    if (( tick % POLL_TICKS == 0 )); then
        if count=$(get_peer_count); then
            unknown=0
            echo "led-boot: peer poll = ${count}"
            (( count > 0 )) && break
        else
            result=$?
            if (( result == 3 )); then
                echo "led-boot: direct connection confirmed; identities pending"
                break
            fi
            unknown=1
            echo "led-boot: peer count unavailable; retrying"
        fi
    fi
done

# Finish the boot indication once a button sequence has released the harness.
while ! led_lock; do sleep "$BLINK_HALF"; done
echo "led-boot: MESH_FORMING"
led_set 0 1 0
led_sleep "$MESH_SOLID_SECS"
led_off
echo "led-boot: IDLE"
