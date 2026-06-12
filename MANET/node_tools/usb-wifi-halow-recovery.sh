#!/bin/sh
# Restart HaLow (wpa_supplicant-s1g-wlan2) when a non-Morse USB WiFi adapter
# appears or disappears, to recover from USB bus contention.
set -u

ACTION="${1:-}"
IFACE="${2:-}"
HALOW_SVC="wpa_supplicant-s1g-wlan2.service"
DELAY=8

log() {
    echo "[usb-wifi-halow-recovery] $*" | systemd-cat -t usb-wifi-halow-recovery
}

# Ignore Morse adapter itself and non-wlan interfaces
case "$IFACE" in
    wlan2) exit 0 ;;
esac

# Only act if wlan2 exists
[ -e /sys/class/net/wlan2 ] || exit 0

log "$ACTION on $IFACE — scheduling HaLow recovery in ${DELAY}s"

# Run restart in background so udev isn't blocked
(
    sleep "$DELAY"
    # Check if wlan2 is already up — skip restart if it recovered on its own
    state="$(cat /sys/class/net/wlan2/operstate 2>/dev/null)"
    if [ "$ACTION" = "add" ] && [ "$state" = "up" ]; then
        log "wlan2 already up, skipping restart"
        exit 0
    fi
    log "restarting $HALOW_SVC (wlan2 operstate=$state)"
    systemctl restart "$HALOW_SVC"
) &
