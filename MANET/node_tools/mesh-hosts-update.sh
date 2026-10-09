#!/bin/bash
# Keep the timer's entry point; compare and publish hosts in one process.
TOOLS="${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}"
exec python3 "$TOOLS/manet_hosts.py" "$@"
