#!/bin/bash
# Shared configuration and ownership for the optional external LED harness.
# Sourced by the boot display, button monitor and info display.

LED_ENABLED=0
GPIO_CHIP=gpiochip0
LED_R=20
LED_G=21
LED_B=22
BTN_LINE=23
LED_CONFIG="${MANET_LED_CONFIG:-/etc/default/manet-led}"
[ ! -r "$LED_CONFIG" ] || source "$LED_CONFIG"

LED_TOOLS_DIR="$(dirname "${BASH_SOURCE[0]}")"
BATCTL_PATH="${BATCTL_PATH:-/usr/sbin/batctl}"
LED_LOCK_FILE="${MANET_LED_LOCK_FILE:-/run/manet-external-led.lock}"
LED_HOLD_PID=""

led_hardware_ready() {
    # The presence of a GPIO controller does not mean the harness is wired.
    [ "$LED_ENABLED" = 1 ] || return 1
    gpioinfo --chip "$GPIO_CHIP" >/dev/null 2>&1
}

led_stop_holder() {
    if [ -n "$LED_HOLD_PID" ]; then
        kill "$LED_HOLD_PID" 2>/dev/null || true
        wait "$LED_HOLD_PID" 2>/dev/null || true
        LED_HOLD_PID=""
    fi
}

led_init() {
    led_hardware_ready || exit 0
    exec 9>"$LED_LOCK_FILE" || exit 1
    trap led_stop_holder EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
}

led_lock() { flock -n 9; }

led_unlock() {
    led_stop_holder
    flock -u 9
}

led_set() {
    led_stop_holder
    gpioset --chip "$GPIO_CHIP" --consumer manet-led \
        "${LED_R}=$1" "${LED_G}=$2" "${LED_B}=$3" &
    LED_HOLD_PID=$!
}

led_sleep() {
    sleep "$1"
    # Busy/invalid pins must not leave a boot process looping forever,
    # claiming to display a pattern it cannot set.
    if ! kill -0 "$LED_HOLD_PID" 2>/dev/null; then
        wait "$LED_HOLD_PID" 2>/dev/null || true
        LED_HOLD_PID=""
        echo "LED GPIO request failed; stopping display" >&2
        exit 1
    fi
}

led_off() {
    led_set 0 0 0
    led_sleep 0.05
    # Releasing a GPIO does not guarantee its subsequent electrical state.
    # The harness must provide an inactive bias; verify this on the bench.
}

get_peer_count() {
    python3 "$LED_TOOLS_DIR/mesh-neighbor-count.py" --batctl "$BATCTL_PATH" \
        --registry "${REGISTRY_STATE_FILE:-/var/run/mesh_node_registry}"
}
