#!/usr/bin/env bash
# ==============================================================================
# Provisioning status on the onboard LEDs
# ==============================================================================
# The verdict manet-provision-status.sh prints on login, shown on the board's
# two LEDs, so a node can be read across a bench without a terminal:
#
#   provisioned          green heartbeat, red off
#   did not complete     red heartbeat, green solid
#   still provisioning   left alone, so the board keeps the look it boots with
#
# Run by manet-led-status.service on every boot, and again by radio-setup.sh
# when the verdict is written. The state it reads lives in /var/lib, so the
# LEDs survive a reboot and only change when the status does.
#
# Never fails and never exits non-zero. A carrier board that wires neither LED
# is a board this has nothing to say about, not a board with a problem.
# ==============================================================================

STATE_FILE="${MANET_PROVISION_STATE:-/var/lib/manet-provision.state}"
DONE_FILE="${MANET_PROVISION_DONE:-/var/lib/radio-setup.done}"
LED_DIR="${MANET_LED_DIR:-/sys/class/leds}"

# Raspberry Pi and CM4 call them PWR and ACT. Other boards use their own names,
# so each is looked up in turn rather than assumed.
RED_NAMES="PWR led1 red:power power"
GREEN_NAMES="ACT led0 green:status status"

find_led() {
    local name
    for name in $1; do
        if [ -d "$LED_DIR/$name" ] && [ -w "$LED_DIR/$name/trigger" ]; then
            echo "$LED_DIR/$name"
            return 0
        fi
    done
    return 1
}

# mode: heartbeat | on | off
set_led() {
    local path="$1" mode="$2" max=1
    [ -n "$path" ] || return 0

    if [ "$mode" = heartbeat ]; then
        echo heartbeat > "$path/trigger" 2>/dev/null || true
        return 0
    fi

    # Solid on and off both need the trigger out of the way first, or whatever
    # the kernel had driving the LED simply overwrites the brightness again.
    echo none > "$path/trigger" 2>/dev/null || true
    [ -r "$path/max_brightness" ] && max=$(cat "$path/max_brightness" 2>/dev/null)
    case "$max" in ''|*[!0-9]*) max=1 ;; esac

    if [ "$mode" = on ]; then
        echo "$max" > "$path/brightness" 2>/dev/null || true
    else
        echo 0 > "$path/brightness" 2>/dev/null || true
    fi
    return 0
}

# The same read, with the same fallback, that manet-provision-status.sh uses.
# A node provisioned before the state file existed still has the marker
# radio-setup touches on success, and the LED must not disagree with the
# banner about that.
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
        # running, or nothing recorded at all. Nothing is known, so nothing is
        # claimed.
        ;;
esac

exit 0
