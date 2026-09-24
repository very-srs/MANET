#!/bin/bash
# button-monitor.sh
# Persistent service. Blocks on button GPIO interrupt, launches led-info.sh
# on each press. Near-zero CPU when idle.
# Managed by button-monitor.service
# Requires libgpiod v2 tools (gpiomon --chip <chip> <line>).

source "$(dirname "${BASH_SOURCE[0]}")/manet-led-common.sh"

LED_INFO_SCRIPT="$LED_TOOLS_DIR/led-info.sh"
DEBOUNCE_MS=50      # gpiomon debounce in milliseconds

# Exit 0 (not failure) when the hardware isn't there: the unit uses
# Restart=on-failure, so a clean exit stops the service instead of
# spinning the relaunch loop on nodes without button/LED wiring.
if ! led_hardware_ready; then
    exit 0
fi

echo "button-monitor: watching ${GPIO_CHIP} line ${BTN_LINE}"

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
        echo "button-monitor: gpiomon failed on ${GPIO_CHIP} line ${BTN_LINE}; exiting"
        exit 0
    fi

    echo "button-monitor: button pressed, launching led-info"
    bash "$LED_INFO_SCRIPT"
done
