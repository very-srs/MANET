#!/bin/sh
for rfkill in /sys/class/rfkill/rfkill*; do
    [ -e "$rfkill/type" ] || continue
    [ "$(cat "$rfkill/type" 2>/dev/null)" = "wlan" ] || continue
    [ "$(cat "$rfkill/hard" 2>/dev/null)" = "1" ] && continue
    echo 0 > "$rfkill/soft" 2>/dev/null || true
done
