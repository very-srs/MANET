#!/bin/bash
# Preserve the public entry point. Decode all records in one interpreter.
exec python3 "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet_registry_builder.py" "$@"
