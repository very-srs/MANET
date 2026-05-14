#!/bin/bash
# button-monitor.sh
# Persistent service. Blocks on button GPIO interrupt, launches led-info.sh
# on each press. Near-zero CPU when idle.
# Managed by button-monitor.service

# ── Hardware config (update after wiring test) ───────────────────────
GPIO_CHIP="gpiochip3"
BTN_LINE=23
# ─────────────────────────────────────────────────────────────────────

LED_INFO_SCRIPT="/usr/local/bin/led-info.sh"
DEBOUNCE_MS=50      # gpiomon debounce in milliseconds

echo "button-monitor: watching ${GPIO_CHIP} line ${BTN_LINE}"

while true; do
    # Block here until a falling edge (button press, active-low)
    # gpiomon exits after 1 event (-n 1)
    gpiomon \
        --num-events=1 \
        --falling-edge \
        --debounce-period="${DEBOUNCE_MS}ms" \
        "${GPIO_CHIP}" "${BTN_LINE}"

    echo "button-monitor: button pressed, launching led-info"
    bash "$LED_INFO_SCRIPT"
done
