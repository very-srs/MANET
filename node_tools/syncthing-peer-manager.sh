#!/bin/bash
#
#  Syncthing Peer Manager for a B.A.T.M.A.N. Mesh
#
# This script runs as a daemon to automatically discover and configure Syncthing
# peers on a mesh network using alfred for data exchange.
#

# Configuration
SYNCTHING_USER="radio"
SYNCTHING_CONFIG_DIR="/home/${SYNCTHING_USER}/.config/syncthing"
SYNCTHING_CONFIG_FILE="${SYNCTHING_CONFIG_DIR}/config.xml"

# Use a alfred data type for Syncthing Device IDs
ALFRED_DATA_TYPE=65

# How often to announce ID and check for new peers (in seconds)
# Alfred's default data timeout is around 10 minutes
LOOP_INTERVAL=300  #5 min

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - $1"
}

# Gets the local Syncthing Device ID
get_local_device_id() {
    runuser -u "$SYNCTHING_USER" -- syncthing --device-id 2>/dev/null
}

# Checks if a given peer ID is already configured in config.xml
is_peer_configured() {
    local peer_id=$1
    if grep -q "<device id=\"${peer_id}\"" "$SYNCTHING_CONFIG_FILE"; then
        return 0
    else
        return 1
    fi
}

# Adds a new peer device to the config.xml
add_peer_to_config() {
    local peer_id=$1
    local peer_name="mesh-peer-$(echo "$peer_id" | cut -c 1-6)"

    log "Adding new peer ${peer_name} (${peer_id}) to config..."

    local device_xml="\ \ \ \ <device id=\"${peer_id}\" name=\"${peer_name}\" compression=\"metadata\" introducer=\"false\">\n\ \ \ \ \ \ \ \ <address>dynamic</address>\n\ \ \ \ </device>"

    # Insert the new <device> block just before the closing </configuration> tag
    sed -i "/<\/configuration>/i ${device_xml}" "$SYNCTHING_CONFIG_FILE"
}

# Shares the default folder with a newly added peer
share_default_folder_with_peer() {
    local peer_id=$1
    log "Sharing default folder with ${peer_id}..."

    # Define the XML snippet for the device share
    local share_xml="\ \ \ \ \ \ \ \ <device id=\"${peer_id}\" introducedBy=\"\"></device>"

    # Insert the <device> share line inside the default folder's definition
    sed -i "/<folder id=\"default\"/a ${share_xml}" "$SYNCTHING_CONFIG_FILE"
}

### --- Main Execution ---
log "Starting Syncthing Peer Manager."

if [[ $EUID -ne 0 ]]; then
   log "This script must be run as root."
   exit 1
fi

# Give Syncthing a moment to start up on a fresh boot
sleep 20

while true; do
    # Ensure Syncthing is configured and get our own ID
    if [ ! -f "$SYNCTHING_CONFIG_FILE" ]; then
        log "Syncthing config not found. Waiting..."
        sleep 30
        continue
    fi

    LOCAL_ID=$(get_local_device_id)
    if [ -z "$LOCAL_ID" ]; then
        log "Could not get local Syncthing Device ID. Waiting..."
        sleep 30
        continue
    fi

    # Announce our own ID to the mesh to keep it fresh in alfred
    log "Announcing our Device ID: ${LOCAL_ID}"
    echo "$LOCAL_ID" | alfred -s $ALFRED_DATA_TYPE

    # Discover all other peer IDs on the mesh
    log "Querying for peer Device IDs..."
    PEER_IDS=$(alfred -r $ALFRED_DATA_TYPE | awk '{print $2}' | grep -v "$LOCAL_ID" || true)

    NEEDS_RESTART=false
    if [ -n "$PEER_IDS" ]; then
        for PEER_ID in $PEER_IDS; do
            if ! is_peer_configured "$PEER_ID"; then
                add_peer_to_config "$PEER_ID"
                share_default_folder_with_peer "$PEER_ID"
                NEEDS_RESTART=true
            fi
        done
    else
        log "No other peers found on the mesh."
    fi

    # If we added new peers, restart Syncthing to apply the changes
    if [ "$NEEDS_RESTART" = true ]; then
        log "New peers were added. Restarting Syncthing to apply changes."
        systemctl restart "syncthing@${SYNCTHING_USER}.service"
    fi

    log "Check complete. Waiting for ${LOOP_INTERVAL} seconds."
    sleep "$LOOP_INTERVAL"
done

