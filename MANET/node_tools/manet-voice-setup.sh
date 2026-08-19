#!/bin/bash
# One-shot voice bring-up for a node updated over the air.
#
# The tools tarball can carry anything that is a file — scripts, units, udev
# rules, modules — but it cannot run apt, and mesh-voice needs the GStreamer
# runtime. This is the piece that closes that gap on a fielded board: it is
# dropped in by the update, runs once, and removes itself.
#
# Safe to run on a node that needs nothing: it checks first, changes nothing,
# and still tidies up after itself. A fresh flash lands here with the packages
# already installed by firstrun.sh, so it is a no-op there by design.

set -u
LOG="/var/log/manet-voice-setup.log"
exec >>"$LOG" 2>&1
echo "=== manet-voice-setup $(date) ==="

PKGS="gstreamer1.0-alsa gstreamer1.0-plugins-base gstreamer1.0-plugins-good python3-gi gir1.2-gstreamer-1.0"

missing=""
for p in $PKGS; do
    dpkg -s "$p" >/dev/null 2>&1 || missing="$missing $p"
done

if [ -n "$missing" ]; then
    # No uplink means no apt. Leave the unit in place so the next boot retries
    # rather than marking voice permanently unavailable on a node that was
    # simply offline at the wrong moment.
    if ! ping -c 1 -W 3 8.8.8.8 >/dev/null 2>&1; then
        echo "no uplink; leaving setup enabled to retry next boot"
        exit 0
    fi
    echo "installing:$missing"
    export DEBIAN_FRONTEND=noninteractive
    if ! apt-get install -y --no-install-recommends $missing; then
        apt-get update -y || true
        if ! apt-get install -y --no-install-recommends $missing; then
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

# Only start voice if this node is configured for it; the daemon exits 0 on
# voice=n anyway, but starting it would be noise.
if grep -qE '^voice=(y|yes|true|1|on)$' /etc/mesh.conf 2>/dev/null; then
    echo "voice=y — starting mesh-voice"
    systemctl enable --now mesh-voice.service 2>/dev/null || true
else
    echo "voice not enabled in /etc/mesh.conf — unit left enabled but idle"
fi

echo "done; removing self"
systemctl disable manet-voice-setup.service 2>/dev/null || true
rm -f /etc/systemd/system/multi-user.target.wants/manet-voice-setup.service
rm -f /etc/systemd/system/manet-voice-setup.service
systemctl daemon-reload 2>/dev/null || true
exit 0
