#!/bin/bash
# led-info.sh
# One-shot. Called by button-monitor.sh on button press.
# Displays neighbor count via LED blink sequence, then exits.
# Requires libgpiod v2 tools (gpioset --chip <chip> <line>=<value>).

# ── Hardware config (update after wiring test) ───────────────────────
GPIO_CHIP="gpiochip0"
LED_R=20
LED_G=21
LED_B=22
GPIO_CONSUMER="manet-led"
# ─────────────────────────────────────────────────────────────────────

BLINK_ON=0.3        # seconds LED on per blink
BLINK_OFF=0.4       # seconds LED off between blinks
NO_PEER_SOLID=3     # seconds of solid red if no neighbors

if ! gpioinfo --chip "$GPIO_CHIP" >/dev/null 2>&1; then
    echo "led-info: GPIO chip ${GPIO_CHIP} not present/usable; exiting"
    exit 0
fi

# ── LED control ───────────────────────────────────────────────────────
# libgpiod v2 gpioset only holds line values while it runs, so keep one
# holder process alive and replace it on each state change.

LED_HOLD_PID=""

led_set() {
    if [ -n "$LED_HOLD_PID" ]; then
        kill "$LED_HOLD_PID" 2>/dev/null
        wait "$LED_HOLD_PID" 2>/dev/null
    fi
    gpioset --chip "$GPIO_CHIP" \
        "${LED_R}=$1" "${LED_G}=$2" "${LED_B}=$3" 2>/dev/null &
    LED_HOLD_PID=$!
}

led_off() { led_set 0 0 0; }

cleanup() {
    [ -n "$LED_HOLD_PID" ] && kill "$LED_HOLD_PID" 2>/dev/null
}
trap cleanup EXIT

# ── Neighbor count ────────────────────────────────────────────────────

get_neighbor_count() {
    /usr/sbin/batctl neighbors 2>/dev/null \
        | grep -v -e '^$' -e 'B.A.T.M.A.N' -e 'No batman' \
        | wc -l
}

# ── Main ──────────────────────────────────────────────────────────────

count=$(get_neighbor_count)
echo "led-info: neighbor count = ${count}"

if (( count == 0 )); then
    # No peers: solid red for NO_PEER_SOLID seconds
    led_set 1 0 0
    sleep "$NO_PEER_SOLID"
    led_off
else
    # N peers: blink green N times
    led_off
    sleep 0.3   # brief pause before sequence starts
    for (( i = 0; i < count; i++ )); do
        led_set 0 1 0
        sleep "$BLINK_ON"
        led_off
        sleep "$BLINK_OFF"
    done
fi
