#!/bin/bash
# Install GStreamer voice dependencies after a tools update. Freshly flashed
# nodes already receive them from firstrun.sh and skip installation.
# Disable and remove the setup unit after success, including when no package
# changes are needed.

set -u
LOG="/var/log/manet-voice-setup.log"
exec >>"$LOG" 2>&1
printf '%s\n' "=== manet-voice-setup $(date) ==="

PKGS="gstreamer1.0-alsa gstreamer1.0-plugins-base gstreamer1.0-plugins-good python3-gi gir1.2-gstreamer-1.0"

missing=""
for p in $PKGS; do
    dpkg -s "$p" >/dev/null 2>&1 || missing="$missing $p"
done

if [ -n "$missing" ]; then
    # Without an uplink, leave the unit installed so the next boot retries apt.
    if ! ping -c 1 -W 3 8.8.8.8 >/dev/null 2>&1; then
        echo "no uplink; leaving setup enabled to retry next boot"
        exit 0
    fi
    printf '%s\n' "installing:$missing"
    export DEBIAN_FRONTEND=noninteractive
    if ! apt-get install -y --no-install-recommends \
        -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold $missing; then
        apt-get update -y || true
        if ! apt-get install -y --no-install-recommends \
            -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold $missing; then
            echo "apt failed; leaving setup enabled to retry next boot"
            exit 0
        fi
    fi
else
    echo "gstreamer runtime already present"
fi

# The OpenVLM rule only applies to devices probed after it lands, so trigger it
# for a board that was already plugged in when the update arrived.
udevadm control --reload-rules 2>/dev/null || true
udevadm trigger --subsystem-match=hidraw 2>/dev/null || true

systemctl daemon-reload 2>/dev/null || true

# Lyra is the default. A missing plugin forces opus, which cannot talk to
# Lyra peers. Log the incompatible fallback.
if grep -qE '^voice_codec=lyra' /etc/mesh.conf 2>/dev/null; then
    if [ -f /usr/lib/aarch64-linux-gnu/gstreamer-1.0/libgstlyra.so ] \
       && [ -d /usr/local/share/lyra/model_coeffs ]; then
        echo "lyra codec present"
    else
        echo "WARNING: voice_codec=lyra but the plugin or model weights are missing;"
        echo "         this node falls back to opus and cannot hear a lyra mesh"
    fi
fi

# Start voice only when enabled, avoiding an immediate exit on voice=n.
if grep -qE '^voice=(y|yes|true|1|on)$' /etc/mesh.conf 2>/dev/null; then
    echo "voice=y: starting mesh-voice"
    systemctl enable --now mesh-voice.service 2>/dev/null || true
else
    echo "voice not enabled in /etc/mesh.conf: unit left enabled but idle"
fi

echo "done; removing self"
systemctl disable manet-voice-setup.service 2>/dev/null || true
rm -f /etc/systemd/system/multi-user.target.wants/manet-voice-setup.service
rm -f /etc/systemd/system/manet-voice-setup.service
systemctl daemon-reload 2>/dev/null || true
exit 0
