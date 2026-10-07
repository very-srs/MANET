#!/usr/bin/env bash
# Preserve the public entry point; all records are decoded in one interpreter.
exec python3 "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet_registry_builder.py" "$@"
