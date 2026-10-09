#!/bin/bash
# Keep the timer entry point. Compare and publish hosts in one process.
TOOLS="${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}"
exec python3 "$TOOLS/manet_hosts.py" "$@"
