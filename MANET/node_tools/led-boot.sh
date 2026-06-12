#!/bin/bash
# led-boot.sh
# One-shot LED state machine. Runs at boot, exits when LED goes dark.
# Managed by led-boot.service

# ── Hardware config (update after wiring test) ───────────────────────
GPIO_CHIP="gpiochip0"
LED_R=20
LED_G=21
LED_B=22
GPIO_CONSUMER="manet-led"
# ─────────────────────────────────────────────────────────────────────

MESH_READY_FLAG="/run/mesh-ready"
BLINK_HALF=0.5          # seconds per half-cycle → 1Hz blink
POLL_TICKS=10           # poll batctl every N half-cycles (10 × 0.5s = 5s)
MESH_SOLID_SECS=10      # solid green duration before going dark

# ── LED control ───────────────────────────────────────────────────────

release_led_lines() {
    pkill -f "gpioset.*${GPIO_CONSUMER}" 2>/dev/null || true
}

led_set() {
    # Usage: led_set <r> <g> <b>   (1=on, 0=off)
    release_led_lines
    gpioset -z -C "${GPIO_CONSUMER}" -c "${GPIO_CHIP}" "${LED_R}=$1" "${LED_G}=$2" "${LED_B}=$3"
    sleep 0.02
}

led_off() { led_set 0 0 0; }

# ── BATMAN neighbor count ─────────────────────────────────────────────

get_neighbor_count() {
    # Skip header lines and blank lines; count what remains
    /usr/sbin/batctl neighbors 2>/dev/null \
        | grep -v -e '^$' -e 'B.A.T.M.A.N' -e 'No batman' \
        | wc -l
}

mesh_ready() {
    [[ -f "$MESH_READY_FLAG" ]] && return 0
    systemctl is-active --quiet node-manager.service || return 1
    ip link show bat0 >/dev/null 2>&1
}

# ── State: BOOTING ────────────────────────────────────────────────────
# Blink red until node-manager touches MESH_READY_FLAG

state_booting() {
    local toggle=0
    echo "led-boot: BOOTING"
    while ! mesh_ready; do
        led_set $toggle 0 0
        toggle=$(( 1 - toggle ))
        sleep "$BLINK_HALF"
    done
}

# ── State: NO_PEERS ───────────────────────────────────────────────────
# Blink green, check for neighbors every POLL_TICKS half-cycles
# Returns when at least one neighbor appears

state_no_peers() {
    local toggle=0
    local tick=0
    local count=0
    echo "led-boot: NO_PEERS"
    while true; do
        led_set 0 $toggle 0
        toggle=$(( 1 - toggle ))
        sleep "$BLINK_HALF"

        tick=$(( tick + 1 ))
        if (( tick % POLL_TICKS == 0 )); then
            count=$(get_neighbor_count)
            echo "led-boot: neighbor poll → ${count}"
            (( count > 0 )) && return 0
        fi
    done
}

# ── State: MESH_FORMING ───────────────────────────────────────────────
# Solid green for MESH_SOLID_SECS, then go dark and exit

state_mesh_forming() {
    echo "led-boot: MESH_FORMING"
    led_set 0 1 0
    sleep "$MESH_SOLID_SECS"
    led_off
    echo "led-boot: IDLE (LED off, exiting)"
}

# ── Cleanup on unexpected exit ────────────────────────────────────────

cleanup() {
    led_off
    release_led_lines
}
trap cleanup EXIT

# ── Main ──────────────────────────────────────────────────────────────

state_booting
state_no_peers
state_mesh_forming
