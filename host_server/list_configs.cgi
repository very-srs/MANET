#!/bin/sh

# This script finds all .conf files in the /data directory, reads the stats,
# and returns everything as a JSON object.

echo "Content-Type: application/json"
echo ""

# Ensure the primary stats file exists and is a valid JSON object.
# If not, create an empty one.
if [ ! -s "/data/stats.json" ] || ! jq -e . >/dev/null 2>&1 < "/data/stats.json"; then
    echo "{}" > /data/stats.json
fi

# Ensure the live stats file exists and is a valid JSON object.
# If not, create a default one
if [ ! -s "/data/live_stats.json" ] || ! jq -e . >/dev/null 2>&1 < "/data/live_stats.json"; then
    echo "{\"count\":0, \"name\":\"\"}" > /data/live_stats.json
fi

# read stats.
STATS=$(cat /data/stats.json)
LIVE_STATS=$(cat /data/live_stats.json)
ACTIVE_NAME=$(cat /data/active.name 2>/dev/null || echo "")


# --- Build the JSON Response ---
first=true
printf "{\"configs\":["

# Use find to handle filenames.
find /data -maxdepth 1 -type f -name "*.conf" -not -name "active.conf" | while read -r file; do
    filename=$(basename "$file")
    # Get the count for the current filename from the JSON stats object using jq.
    # If the key doesn't exist, default to 0.
    count=$(echo "$STATS" | jq --arg name "$filename" '.[$name] // 0')

    if [ "$first" = "false" ]; then
        printf ","
    fi
    # Print a JSON object for each config file found.
    printf "{\"name\":\"%s\",\"stats\":%s}" "$filename" "$count"
    first=false
done

# Now, add the 'active_name' and 'live_stats' to the main JSON object for the UI.
printf "],\"active_name\":\"%s\",\"live_stats\":%s}" "$ACTIVE_NAME" "$LIVE_STATS"

