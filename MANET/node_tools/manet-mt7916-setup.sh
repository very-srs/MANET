#!/bin/sh
# Installation/update integration; no driver load, removal or reload here.
set -eu
systemctl enable manet-mt7916-firmware.service
systemctl enable --now manet-mt7916-firmware.path
/usr/bin/python3 /usr/local/bin/manet-mt7916-firmware.py || true
