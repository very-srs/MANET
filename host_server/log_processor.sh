#!/bin/sh

# This script runs in the background, watching the access log for downloads
# of the active configuration file and updating stats accordingly.

LOG_FILE="/var/log/lighttpd/access.log"
STATS_FILE="/data/stats.json"
LIVE_STATS_FILE="/data/live_stats.json"
ACTIVE_NAME_FILE="/data/active.name"

# Ensure the log file exists before we try to tail it.
touch "$LOG_FILE"
chown lighttpd:lighttpd "$LOG_FILE"

# Use tail to follow the log file, and pipe the output to our processing loop.
tail -n 0 -F "$LOG_FILE" | awk '$7 == "/data/active.conf" { print; fflush(); }' | while read -r line; do
    # This loop only runs when awk finds a matching line.

    # Ensure the primary stats file exists and is valid JSON.
    if [ ! -s "$STATS_FILE" ] || ! jq -e . >/dev/null 2>&1 < "$STATS_FILE"; then
        echo "{}" > "$STATS_FILE"
    fi

    # Ensure the live stats file exists and is valid JSON.
    if [ ! -s "$LIVE_STATS_FILE" ] || ! jq -e . >/dev/null 2>&1 < "$LIVE_STATS_FILE"; then
        echo "{\"count\":0, \"name\":\"\"}" > "$LIVE_STATS_FILE"
    fi

    # Read the name of the config that is currently active.
    if [ -f "$ACTIVE_NAME_FILE" ]; then
        CONFIG_NAME=$(cat "$ACTIVE_NAME_FILE")

        if [ -n "$CONFIG_NAME" ]; then
            # --- Update Total Stats ---
            # Use jq to read the current count, add 1, and write it back.
            # This is an atomic and safe way to update the JSON file.
            jq --arg name "$CONFIG_NAME" '.[$name] = (.[$name] // 0) + 1' "$STATS_FILE" > "${STATS_FILE}.tmp" && mv "${STATS_FILE}.tmp" "$STATS_FILE"

            # --- Update Live Stats ---
            jq '.count += 1' "$LIVE_STATS_FILE" > "${LIVE_STATS_FILE}.tmp" && mv "${LIVE_STATS_FILE}.tmp" "$LIVE_STATS_FILE"
        fi
    fi
done

