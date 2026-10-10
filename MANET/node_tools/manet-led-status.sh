#!/bin/bash
# Show manet-provision-status.sh verdicts on the onboard LEDs.
#   provisioned          green heartbeat, red off
#   did not complete     red heartbeat, green solid
#   still provisioning   retain the boot LED state
# manet-led-status.service runs at boot. radio-setup.sh reruns it when writing
# the verdict. State in /var/lib persists across reboots.
# Always exit successfully, including on boards without these LEDs.

STATE_FILE="${MANET_PROVISION_STATE:-/var/lib/manet-provision.state}"
DONE_FILE="${MANET_PROVISION_DONE:-/var/lib/radio-setup.done}"
LED_DIR="${MANET_LED_DIR:-/sys/class/leds}"

# Try board-specific LED names, including Raspberry Pi and CM4 PWR/ACT.
RED_NAMES="PWR led1 red:power power"
GREEN_NAMES="ACT led0 green:status status"

# Print the first writable LED path in LED_DIR from a space-separated list.
find_led() {
    local name
    for name in $1; do
        if [ -d "$LED_DIR/$name" ] && [ -w "$LED_DIR/$name/trigger" ]; then
            printf '%s\n' "$LED_DIR/$name"
            return 0
        fi
    done
    return 1
}

# Apply a trigger or brightness to the LED path in $1.
# mode: heartbeat | on | off
set_led() {
    local path="$1" mode="$2" max=1
    [ -n "$path" ] || return 0

    if [ "$mode" = heartbeat ]; then
        echo heartbeat > "$path/trigger" 2>/dev/null || true
        return 0
    fi

    # Disable the trigger before setting brightness so it cannot overwrite it.
    echo none > "$path/trigger" 2>/dev/null || true
    [ -r "$path/max_brightness" ] && max=$(cat "$path/max_brightness" 2>/dev/null)
    # Use binary brightness if the kernel limit is empty or nonnumeric.
    case "$max" in ''|*[!0-9]*) max=1 ;; esac

    if [ "$mode" = on ]; then
        printf '%s\n' "$max" > "$path/brightness" 2>/dev/null || true
    else
        echo 0 > "$path/brightness" 2>/dev/null || true
    fi
    return 0
}

# Match manet-provision-status.sh: fall back to radio-setup's success marker
# when the state file has no verdict, keeping LEDs and the banner consistent.
read_state() {
    local k v state=""
    if [ -r "$STATE_FILE" ]; then
        while IFS='=' read -r k v; do
            [ "$k" = STATE ] && state="$v"
        done < "$STATE_FILE"
    fi
    [ -z "$state" ] && [ -f "$DONE_FILE" ] && state=complete
    printf '%s' "$state"
}

RED="$(find_led "$RED_NAMES" || true)"
GREEN="$(find_led "$GREEN_NAMES" || true)"
STATE="$(read_state)"

case "$STATE" in
    complete)
        set_led "$GREEN" heartbeat
        set_led "$RED"   off
        ;;
    incomplete)
        set_led "$RED"   heartbeat
        set_led "$GREEN" on
        ;;
    *)
        # Leave LEDs unchanged when provisioning is running or has no verdict.
        ;;
esac

exit 0
