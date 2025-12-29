#!/bin/bash
#
#  Syncthing Peer Manager for a B.A.T.M.A.N. Mesh
#
# This script runs as a daemon to automatically discover and configure Syncthing
# peers by reading the central mesh node registry file.
#

# Configuration
SYNCTHING_USER="radio"
SYNCTHING_CONFIG_DIR="/home/${SYNCTHING_USER}/.config/syncthing"
SYNCTHING_CONFIG_FILE="${SYNCTHING_CONFIG_DIR}/config.xml"
REGISTRY_STATE_FILE="/var/run/mesh_node_registry" # Central registry file

# How often to check the registry for new peers (in seconds)
LOOP_INTERVAL=60 # Check every minute

log() {
    # Add script name for clarity
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - SYNC-MGR: $1"
}

# Gets the local Syncthing Device ID (needed for filtering)
get_local_device_id() {
    runuser -u "$SYNCTHING_USER" -- syncthing --device-id 2>/dev/null
}

# Checks if a given peer ID is already configured in config.xml
is_peer_configured() {
    local peer_id=$1
    # Check if config file exists before trying to grep it
    if [ ! -f "$SYNCTHING_CONFIG_FILE" ]; then
        return 1 # Not configured if file doesn't exist
    fi
    # Use -F for fixed string and -q for quiet mode
    if grep -Fq "<device id=\"${peer_id}\"" "$SYNCTHING_CONFIG_FILE"; then
        return 0 # Peer IS configured
    else
        return 1 # Peer is NOT configured
    fi
}

# Adds a new peer device to the config.xml
add_peer_to_config() {
    local peer_id=$1
    # Generate a simple name based on the first few chars of the ID
    local peer_name="mesh-peer-$(echo "$peer_id" | cut -c 1-7)"

    log "Adding new peer ${peer_name} (${peer_id}) to config..."

    # Use printf for safer XML generation, avoid complex sed injections
    local device_xml
    device_xml=$(printf '\n    <device id="%s" name="%s" compression="metadata" introducer="false" skipIntroductionRemovals="false" introducedBy="">\n        <address>dynamic</address>\n    </device>' "$peer_id" "$peer_name")

    # Insert the new <device> block just before the closing </configuration> tag using awk for robustness
    awk -v device_xml="$device_xml" '/<\/configuration>/ { print device_xml } { print }' "$SYNCTHING_CONFIG_FILE" > "$SYNCTHING_CONFIG_FILE.tmp" && \
    mv "$SYNCTHING_CONFIG_FILE.tmp" "$SYNCTHING_CONFIG_FILE"
}

# Shares the default folder ("default") with a newly added peer
share_default_folder_with_peer() {
    local peer_id=$1
    log "Sharing default folder with ${peer_id}..."

    # Define the XML snippet for the device share
    local share_xml
    share_xml=$(printf '\n        <device id="%s" introducedBy=""></device>' "$peer_id")

    # Insert the <device> share line within the default folder's definition using awk
    awk -v share_xml="$share_xml" '/<folder id="default"/ { in_folder=1 } in_folder && /<\/folder>/ { print share_xml; in_folder=0 } { print }' "$SYNCTHING_CONFIG_FILE" > "$SYNCTHING_CONFIG_FILE.tmp" && \
    mv "$SYNCTHING_CONFIG_FILE.tmp" "$SYNCTHING_CONFIG_FILE"

}

### --- Main Execution ---
log "Starting Syncthing Peer Manager."

if [[ $EUID -ne 0 ]]; then
   log "This script must be run as root."
   exit 1
fi

# Give Syncthing and node-manager a moment on fresh boot
sleep 30

while true; do
    # Check if Syncthing config and registry exist
    if [ ! -f "$SYNCTHING_CONFIG_FILE" ]; then
        log "Syncthing config ($SYNCTHING_CONFIG_FILE) not found. Waiting..."
        sleep 30
        continue
    fi
    if [ ! -s "$REGISTRY_STATE_FILE" ]; then
        log "Mesh registry ($REGISTRY_STATE_FILE) not found or empty. Waiting..."
        sleep 30
        continue
    fi

    LOCAL_ID=$(get_local_device_id)
    if [ -z "$LOCAL_ID" ]; then
        log "Could not get local Syncthing Device ID. Waiting..."
        sleep 30
        continue
    fi

    # Discover peer IDs by sourcing the registry file
    log "Querying mesh registry for peer Syncthing IDs..."
    # Source in a subshell to avoid polluting environment
    PEER_IDS=$( (
        source "$REGISTRY_STATE_FILE" > /dev/null 2>&1
        # List all variables starting with NODE_ and ending with _SYNCTHING_ID
        compgen -A variable | grep '^NODE_.*_SYNCTHING_ID$' | while read varname; do
            peer_id="${!varname}"
            # Exclude our own ID and any potentially empty entries
            if [[ -n "$peer_id" && "$peer_id" != "$LOCAL_ID" ]]; then
                echo "$peer_id"
            fi
        done
    ) | sort -u ) # Sort and get unique IDs

    NEEDS_RESTART=false
    if [ -n "$PEER_IDS" ]; then
        log "Found potential peers:"
        echo "$PEER_IDS" # Log the list for debugging

        # Loop through unique peer IDs found in the registry
        while IFS= read -r PEER_ID; do
            if ! is_peer_configured "$PEER_ID"; then
                add_peer_to_config "$PEER_ID"
                share_default_folder_with_peer "$PEER_ID"
                NEEDS_RESTART=true
            fi
        done <<< "$PEER_IDS"
    else
        log "No other peers found in the mesh registry."
    fi

    # If we added new peers, restart Syncthing to apply the changes
    if [ "$NEEDS_RESTART" = true ]; then
        log "New peers were added. Restarting Syncthing to apply changes."
        # Use runuser to ensure config file permissions are correct if Syncthing touches them on reload
        # Although systemctl restart should handle the user context correctly.
        systemctl restart "syncthing@${SYNCTHING_USER}.service"
    fi

    log "Check complete. Waiting for ${LOOP_INTERVAL} seconds."
    sleep "$LOOP_INTERVAL"
done

