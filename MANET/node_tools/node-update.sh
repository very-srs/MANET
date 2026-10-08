#!/bin/bash
# Python loads the updater before any installed source files are replaced.
saved=/var/lib/manet-update/recovery/node-update.py
if [ -e /var/lib/manet-update/in-progress ] && [ -f "$saved" ]; then
    exec python3 "$saved" "$@"
fi
exec python3 "$(dirname "$(readlink -f "$0")")/node-update.py" "$@"
