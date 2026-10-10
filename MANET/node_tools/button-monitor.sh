#!/bin/bash
# Wait for button GPIO interrupts and launch led-info.sh on each press.
# button-monitor.service requires libgpiod v2 tools
# (gpiomon --chip <chip> <line>). Blocking on interrupts keeps idle CPU low.

source "$(dirname "${BASH_SOURCE[0]}")/manet-led-common.sh"

LED_INFO_SCRIPT="$LED_TOOLS_DIR/led-info.sh"
DEBOUNCE_MS=50      # gpiomon debounce in milliseconds

# Exit successfully when hardware is absent. Restart=on-failure would
# otherwise repeatedly launch the service on nodes without button wiring.
if ! led_hardware_ready; then
    exit 0
fi

printf '%s\n' "button-monitor: watching ${GPIO_CHIP} line ${BTN_LINE}"

while true; do
    # Block here until a falling edge (button press, active-low)
    # gpiomon exits after 1 event (--num-events 1)
    if ! gpiomon \
        --chip "$GPIO_CHIP" \
        --edges falling \
        --num-events 1 \
        --debounce-period "${DEBOUNCE_MS}ms" \
        --quiet \
        "$BTN_LINE"; then
        printf '%s%s\n' \
            "button-monitor: gpiomon failed on ${GPIO_CHIP} line " \
            "${BTN_LINE}; exiting"
        exit 0
    fi

    echo "button-monitor: button pressed, launching led-info"
    # Wait for the display to finish before arming the next button event.
    bash "$LED_INFO_SCRIPT"
done
