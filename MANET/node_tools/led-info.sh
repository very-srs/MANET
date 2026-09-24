#!/bin/bash
# One-shot external LED display, invoked by button-monitor.sh.
# Count directly connected nodes across BATMAN radios, excluding multihop peers.
source "$(dirname "${BASH_SOURCE[0]}")/manet-led-common.sh"

BLINK_ON=0.3
BLINK_OFF=0.4
STATUS_SOLID=3

led_init
# Wait through a boot blink or its ten-second success indication. Never
# interleave two counts or block the button monitor indefinitely.
flock -w 12 9 || exit 0

count=$(get_peer_count)
result=$?
if (( result == 3 )); then
    echo "led-info: connected; neighbor count pending identity discovery"
    led_set 0 1 0  # A connection is confirmed even before radio aliases arrive.
    led_sleep "$STATUS_SOLID"
elif (( result != 0 )); then
    echo "led-info: peer count unavailable"
    led_set 1 1 0  # Amber: unknown, distinct from confirmed isolation.
    led_sleep "$STATUS_SOLID"
elif (( count == 0 )); then
    echo "led-info: peer count = 0"
    led_set 1 0 0
    led_sleep "$STATUS_SOLID"
else
    echo "led-info: peer count = ${count}"
    led_off
    led_sleep 0.3
    for (( i = 0; i < count; i++ )); do
        led_set 0 1 0
        led_sleep "$BLINK_ON"
        led_set 0 0 0
        led_sleep "$BLINK_OFF"
    done
fi
led_off
