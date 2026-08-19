#!/bin/bash
#
# one-shot-time-sync.sh
# This script runs once on boot to find the best NTP server on the mesh,
# sync the time, and then disable the chrony service to minimize traffic.
#

REGISTRY_STATE_FILE="/var/run/mesh_node_registry"
STATE_FILE="/var/run/initial_time_synced"

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - ONE-SHOT-SYNC: $1" | systemd-cat -t time-sync
}

# If we've already synced on this boot, do nothing.
if [ -f "$STATE_FILE" ]; then
    log "Initial time sync has already been performed. Exiting."
    exit 0
fi

# Nodes that hold their own time source stay synced rather than doing a
# one-shot, and must keep chrony running so everyone else can sync against
# them: a GPS node disciplines from the local SHM refclock at no network cost,
# and a gateway polls over Ethernet rather than mesh airtime. A gateway whose
# internet sync failed has neither marker, so it correctly falls through and
# takes its time from a mesh peer like any other node.
if [ -f /var/run/mesh-ntp.state ] || [ -f /var/run/mesh-ntp-gps.state ]; then
    log "This node is a mesh time source (GPS or gateway); it stays synced. Exiting."
    exit 0
fi

log "Starting one-shot time sync..."

# Wait for the mesh registry file to be created via alfred
WAIT_COUNT=0
while [ ! -s "$REGISTRY_STATE_FILE" ]; do
    ((WAIT_COUNT++))
    # Log frequently for the first 24 attempts (2 minutes)
    if [ "$WAIT_COUNT" -le 24 ]; then
        log "Waiting for mesh registry to be populated... (${WAIT_COUNT})"
        sleep 5
    # After the initial period, log only periodically
    else
        log "Still waiting for mesh registry... (Attempt ${WAIT_COUNT})"
		sleep 10
    fi
done
log "Mesh registry is available. Proceeding with sync..."

# Find the Best NTP Server, from the POV of this mesh node
source "$REGISTRY_STATE_FILE"
BEST_SERVER_HOSTNAME=""
BEST_SERVER_MAC=""
HIGHEST_LOCAL_TQ="0" # Initialize TQ to 0 (lowest possible)
PREFIX_PATTERN="NODE_([0-9a-fA-F]+)"

# Get local TQ scores to all neighbors
mapfile -t BATCTL_OUTPUT < <(batctl o)

# Loop through the registry to find candidate servers
while IFS= read -r line; do
    # Find nodes that are flagged as NTP servers in the registry
    if [[ $line =~ ${PREFIX_PATTERN}_IS_NTP_SERVER=\'true\' ]]; then
        MAC_SANITIZED="${BASH_REMATCH[1]}"

        # Construct variable names for MAC and Hostname
        MAC_VAR="NODE_${MAC_SANITIZED}_MAC_ADDRESS"
        HOSTNAME_VAR="NODE_${MAC_SANITIZED}_HOSTNAME"

        CANDIDATE_MAC=${!MAC_VAR}
        CANDIDATE_HOSTNAME=${!HOSTNAME_VAR}

        # Find the TQ score from this node to the candidate server's MAC
        CURRENT_LOCAL_TQ="0" # Default to 0 if not found
        for bat_line in "${BATCTL_OUTPUT[@]}"; do
            # The Originator column in `batctl o` is the MAC address
            if [[ "$bat_line" == *"$CANDIDATE_MAC"* ]]; then
                # Extract the TQ score
                TQ_RAW=$(echo "$bat_line" | awk '{print $3}' | tr -d '()')
                # Validate if it's a number
                if [[ "$TQ_RAW" =~ ^[0-9]+$ ]]; then
                    CURRENT_LOCAL_TQ=$TQ_RAW
                fi
                break # Found the MAC, no need to check further lines
            fi
        done

        # Compare LOCAL TQ scores to find the best server *from our perspective*
        if (( CURRENT_LOCAL_TQ > HIGHEST_LOCAL_TQ )); then
            HIGHEST_LOCAL_TQ=$CURRENT_LOCAL_TQ
            BEST_SERVER_HOSTNAME=$CANDIDATE_HOSTNAME
            BEST_SERVER_MAC=$CANDIDATE_MAC # Store MAC for logging/debugging
        fi
    fi
done < "$REGISTRY_STATE_FILE"

# --- Sync Time and Shut Down ---
if [ -n "$BEST_SERVER_HOSTNAME" ]; then
    log "Best NTP server found from this node's perspective: ${BEST_SERVER_HOSTNAME}\
     (MAC: ${BEST_SERVER_MAC}) with local TQ ${HIGHEST_LOCAL_TQ}."

    # Resolve the hostname to an IPv6 address
#	BEST_SERVER_IP=$(resolvectl query --type=AAAA "${BEST_SERVER_HOSTNAME}.local" \
#  | awk '/^.*: .*:/ {print $2; exit}')

    BEST_SERVER_IP=$( /usr/local/bin/mac-to-ip.sh "$BEST_SERVER_MAC" )

    if [ -n "$BEST_SERVER_IP" ]; then
        log "Syncing time with ${BEST_SERVER_IP}..."

        # chronyc needs a live daemon, and ethernet-autodetect may already have
        # stopped it earlier in this boot.
        systemctl is-active --quiet chrony.service || systemctl start chrony.service

        # "burst" only acts on sources chronyd already knows about. This peer is
        # discovered from the registry at runtime and appears in no config file,
        # so it has to be added first — without this the command returned
        # "503 No such source" and the sync silently never happened.
        chronyc -a "add server ${BEST_SERVER_IP} iburst" >/dev/null 2>&1
        chronyc -a "burst 1/1 ${BEST_SERVER_IP}" >/dev/null 2>&1

        # burst returns 200 as soon as the command is accepted, which says
        # nothing about whether the clock was ever set. waitsync is what
        # actually blocks until chronyd has disciplined it.
        if timeout 90 chronyc waitsync 60 0 0 1 >/dev/null 2>&1; then
            log "Time sync successful. Clock disciplined from ${BEST_SERVER_IP}."
            touch "$STATE_FILE"
            # Stop, but leave the unit enabled: a GPS dongle appearing later, or
            # this node becoming a gateway, both need to start chrony again.
            log "Stopping chrony; this node has its time and need not keep polling."
            systemctl stop chrony.service
        else
            log "Time sync did not complete against ${BEST_SERVER_IP}."
        fi
    else
        log "Could not resolve hostname ${BEST_SERVER_HOSTNAME}.local"
    fi
else
    log "No NTP servers found on the mesh."
fi

log "One-shot time sync complete."
