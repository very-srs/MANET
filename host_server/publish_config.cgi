#!/bin/sh

# This script takes the current form data, saves it as the active configuration,
# updates the active name, and resets the live counter.

echo "Content-Type: application/json"
echo ""

# Read the POST data using CONTENT_LENGTH
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

url_decode() {
    printf '%b' "${1//%/\\x}"
}

# --- Parse the configuration name from the form data ---
CONFIG_NAME="Unsaved" # Set a default value

# Use grep to find the specific key=value pair, then cut to get the value.
CONFIG_NAME_LINE=$(echo "$POST_DATA" | grep -o 'config_name=[^&]*')
if [ -n "$CONFIG_NAME_LINE" ]; then
    RAW_VALUE=$(echo "$CONFIG_NAME_LINE" | cut -d'=' -f2)
    if [ -n "$RAW_VALUE" ]; then
        CONFIG_NAME=$(url_decode "$RAW_VALUE")
    fi
fi

# Ensure the name used for stats tracking is the full filename.
# If the name is not "Unsaved" and does not end with ".conf", append it.
case "$CONFIG_NAME" in
  *.conf)
    # Name is already correct, do nothing.
    ;;
  Unsaved)
    # This is a special case, do nothing.
    ;;
  *)
    # Append .conf to the name.
    CONFIG_NAME="${CONFIG_NAME}.conf"
    ;;
esac

# Save the active.conf file.
echo "$POST_DATA" | sed 's/&/\n/g' | while read -r line; do
    key=$(echo "$line" | cut -d'=' -f1)
    value=$(echo "$line" | cut -d'=' -f2)
    printf "%s: %s\n" "$(url_decode "$key")" "$(url_decode "$value")"
done > /data/active.conf

if [ $? -ne 0 ]; then
    echo "{\"status\":\"error\", \"message\":\"Server error during publish.\"}"
    exit 0
fi

# Update active name and reset stats using the full, correct name.
echo "$CONFIG_NAME" > /data/active.name
# Atomically reset the live stats file to prevent race conditions.
jq -n --arg name "$CONFIG_NAME" '{"count":0, "name":$name}' > /data/live_stats.json.tmp && mv /data/live_stats.json.tmp /data/live_stats.json

# Send a success response.
echo "{\"status\":\"success\", \"message\":\"Configuration published successfully.\", \"published_name\":\"$CONFIG_NAME\"}"

