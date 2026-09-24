#!/bin/bash
# The service also owns GPS/uplink chrony so role changes cannot race a client
# shutdown. Ordinary nodes stop mesh polling between bounded clock refreshes.
exec python3 "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/mesh-time-sync.py" "$@"
