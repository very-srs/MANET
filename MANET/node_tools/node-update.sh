#!/bin/bash
# Python loads the updater before any installed source files are replaced.
exec python3 "$(dirname "$(readlink -f "$0")")/node-update.py" "$@"
