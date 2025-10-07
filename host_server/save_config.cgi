#!/bin/sh

# This script takes form data and saves it to a named configuration file.

# --- START DEBUGGING ---
# Log everything to a temp file to see what the script is receiving.
DEBUG_LOG="/tmp/save_debug.log"
echo "--- New Save Request: $(date) ---" > "$DEBUG_LOG"
# --- END DEBUGGING ---

echo "Content-Type: application/json"
echo ""

# Robustly read the POST data
if [ "$REQUEST_METHOD" = "POST" ]; then
    if [ -n "$CONTENT_LENGTH" ]; then
        read -n "$CONTENT_LENGTH" POST_DATA
    else
        read POST_DATA
    fi
else
    echo "{\"status\":\"error\", \"message\":\"Invalid request method.\"}"
    exit 0
fi

# --- MORE DEBUGGING ---
echo "RAW POST_DATA: $POST_DATA" >> "$DEBUG_LOG"
# --- END DEBUGGING ---

# A robust url-decode function for busybox shell
url_decode() {
    printf '%b' "${1//%/\\x}"
}

# --- Parse config_name from the POST data ---
CONFIG_NAME=""
CONFIG_NAME_LINE=$(echo "$POST_DATA" | grep -o 'config_name=[^&]*')
if [ -n "$CONFIG_NAME_LINE" ]; then
    RAW_VALUE=$(echo "$CONFIG_NAME_LINE" | cut -d'=' -f2)
    if [ -n "$RAW_VALUE" ]; then
        CONFIG_NAME=$(url_decode "$RAW_VALUE")
    fi
fi

# If no name was provided, we can't save.
if [ -z "$CONFIG_NAME" ]; then
    echo "{\"status\":\"error\", \"message\":\"Configuration name cannot be empty.\"}"
    exit 0
fi

# --- Sanitize filename ---
SAFE_NAME=$(echo "$CONFIG_NAME" | sed 's/\.conf$//' | tr -cd 'a-zA-Z0-9_-')
if [ -z "$SAFE_NAME" ]; then
    echo "{\"status\":\"error\", \"message\":\"Invalid configuration name. Use only letters, numbers, underscore, and hyphen.\"}"
    exit 0
fi
FILENAME="${SAFE_NAME}.conf"
FILE_PATH="/data/$FILENAME"

# --- Save ALL fields from the form ---
echo "$POST_DATA" | tr '&' '\n' | while read -r pair; do
    key=$(echo "$pair" | cut -d'=' -f1)
    if [ "$key" != "config_name" ]; then
        echo "$pair"
    fi
done > "$FILE_PATH"

if [ $? -eq 0 ]; then
    echo "{\"status\":\"success\", \"message\":\"'$FILENAME' saved successfully.\"}"
else
    echo "{\"status\":\"error\", \"message\":\"Server error: Could not save configuration file.\"}"
fi

