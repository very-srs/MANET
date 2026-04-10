#!/bin/bash
# led-info.sh
# One-shot. Called by button-monitor.sh on button press.
# Displays neighbor count via LED blink sequence, then exits.

# ── Hardware config (update after wiring test) ───────────────────────
GPIO_CHIP="gpiochip3"
LED_R=20
LED_G=21
LED_B=22
# ─────────────────────────────────────────────────────────────────────

BLINK_ON=0.3        # seconds LED on per blink
BLINK_OFF=0.4       # seconds LED off between blinks
NO_PEER_SOLID=3     # seconds of solid red if no neighbors

# ── LED control ───────────────────────────────────────────────────────

led_set() { gpioset "${GPIO_CHIP}" "${LED_R}=$1" "${LED_G}=$2" "${LED_B}=$3"; }
led_off()  { led_set 0 0 0; }

cleanup() { led_off; }
trap cleanup EXIT

# ── Neighbor count ────────────────────────────────────────────────────

get_neighbor_count() {
    batctl neighbors 2>/dev/null \
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
