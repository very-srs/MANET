#!/bin/bash
# Every hostapd start withdraws active mesh roles before preparing the radio.
set -euo pipefail
exec python3 /usr/local/bin/manet_ap_mesh.py prepare-ap
