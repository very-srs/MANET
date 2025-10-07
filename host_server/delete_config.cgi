#!/bin/sh

# This script deletes a configuration file and removes its historical stats.

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
    echo "{\"status\":\"error\", \"message\":\"Invalid or missing filename. Data received: '$FORM_DATA'\"}"
    exit 0
fi

if [ "$FILENAME" = "active.conf" ]; then
    echo "{\"status\":\"error\", \"message\":\"Cannot delete the active configuration file.\"}"
    exit 0
fi

FILE_PATH="/data/$FILENAME"

# --- Delete the configuration file ---
if [ -f "$FILE_PATH" ]; then
    rm "$FILE_PATH"
    if [ $? -ne 0 ]; then
        echo "{\"status\":\"error\", \"message\":\"Failed to delete configuration file.\"}"
        exit 0
    fi
else
    echo "{\"status\":\"error\", \"message\":\"Configuration file not found.\"}"
    exit 0
fi

# --- Update the stats file ---
if [ ! -s "$STATS_FILE" ] || ! jq -e . >/dev/null 2>&1 < "$STATS_FILE"; then
    echo "{}" > "$STATS_FILE"
fi

# Atomically update the JSON file, removing the entry for the deleted config.
jq --arg name "$FILENAME" 'del(.[$name])' "$STATS_FILE" > "${STATS_FILE}.tmp"
if [ $? -eq 0 ]; then
    mv "${STATS_FILE}.tmp" "$STATS_FILE"
    echo "{\"status\":\"success\", \"message\":\"$FILENAME deleted successfully.\"}"
else
    rm -f "${STATS_FILE}.tmp"
    echo "{\"status\":\"error\", \"message\":\"Failed to update stats file after deletion.\"}"
fi

