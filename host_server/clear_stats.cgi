#!/bin/sh

# This script clears the historical stats for a given configuration file.

echo "Content-Type: application/json"
echo ""

url_decode() {
    printf '%b' "${1//%/\\x}"
}

# Accept data from either GET or POST
FORM_DATA=""
if [ "$REQUEST_METHOD" = "POST" ]; then
    if [ -n "$CONTENT_LENGTH" ]; then
        read -n "$CONTENT_LENGTH" FORM_DATA
    fi
elif [ "$REQUEST_METHOD" = "GET" ]; then
    FORM_DATA="$QUERY_STRING"
else
    echo "{\"status\":\"error\", \"message\":\"Invalid request method.\"}"
    exit 0
fi

STATS_FILE="/data/stats.json"

# --- parse the filename ---
FILENAME=""
FILENAME_LINE=$(echo "$FORM_DATA" | grep -o 'file=[^&]*')
if [ -n "$FILENAME_LINE" ]; then
    RAW_VALUE=$(echo "$FILENAME_LINE" | cut -d'=' -f2)
    if [ -n "$RAW_VALUE" ]; then
        FILENAME=$(url_decode "$RAW_VALUE")
    fi
fi

# --- Perform Security and Sanity Checks ---
if [ -z "$FILENAME" ] || echo "$FILENAME" | grep -q '[^a-zA-Z0-9._-]' || echo "$FILENAME" | grep -q '/'; then
    echo "{\"status\":\"error\", \"message\":\"Invalid or missing filename.\"}"
    exit 0
fi

# Ensure the stats file exists and is valid JSON, or create it.
if [ ! -s "$STATS_FILE" ] || ! jq -e . >/dev/null 2>&1 < "$STATS_FILE"; then
    echo "{}" > "$STATS_FILE"
fi

# Atomically update the stats file
jq --arg name "$FILENAME" '.[$name] = 0' "$STATS_FILE" > "${STATS_FILE}.tmp"
if [ $? -eq 0 ]; then
    # If jq succeeds, replace the old file with the new one.
    mv "${STATS_FILE}.tmp" "$STATS_FILE"
    echo "{\"status\":\"success\", \"message\":\"Stats cleared successfully for $FILENAME.\"}"
else
    # If jq fails, clean up the temporary file and report an error.
    rm -f "${STATS_FILE}.tmp"
    echo "{\"status\":\"error\", \"message\":\"Failed to update stats file.\"}"
fi

