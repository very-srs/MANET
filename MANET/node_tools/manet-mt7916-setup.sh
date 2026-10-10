#!/bin/sh
# Install/update integration without driver loading, removal or reload.
set -eu
systemctl enable manet-mt7916-firmware.service
systemctl enable manet-mt7916-firmware-survived.service
systemctl enable --now manet-mt7916-firmware.path
# Leave enabled units in place if the firmware command fails.
/usr/bin/python3 /usr/local/bin/manet-mt7916-firmware.py || true
