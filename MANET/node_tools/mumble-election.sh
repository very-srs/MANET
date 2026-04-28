#!/bin/bash
# ==============================================================================
# Mumble Election Script with Database Synchronization
# ==============================================================================
# Elects Mumble server based on mesh centrality (TQ)
# Manages database sync via Syncthing to prevent data loss
# ==============================================================================

# --- Configuration ---
REGISTRY_STATE_FILE="/var/run/mesh_node_registry"
MUMBLE_SERVICE_NAME="mumble-server.service"
MUMBLE_CONFIG="/etc/mumble/mumble-server.ini"
MUMBLE_USER="mumble-server"
CONTROL_IFACE="br0"

# Database paths
WORKING_DB="/var/lib/mumble-server/mumble-server.sqlite"
SHARED_DB_DIR="/home/radio/Sync/mumble"
SHARED_DB="${SHARED_DB_DIR}/mumble-server.sqlite"
BACKUP_DIR="${SHARED_DB_DIR}/backups"

# VIP calculation
MUMBLE_IPV4_VIP=""
MUMBLE_IPV6_VIP=""

# Incumbent bias: current leader gets this many TQ points added to their score.
# Prevents service migration due to normal TQ fluctuation.
INCUMBENT_BIAS=10

# State
MY_MAC=$(cat "/sys/class/net/${CONTROL_IFACE}/address")
LOCK_FILE="/var/run/mumble-election.lock"

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - MUMBLE-ELECTION: $1" | systemd-cat -t mumble-election
}

# --- Dependency Checks ---
if ! command -v sqlite3 &>/dev/null; then
    log "ERROR: 'sqlite3' command not found. Please install it."
    exit 1
fi

if ! command -v bc &>/dev/null; then
    log "ERROR: 'bc' command not found. Please install it."
    exit 1
fi

# --- VIP Calculation Functions ---
get_mumble_ipv4_vip() {
    local CIDR="$1"
    local CALC_OUTPUT=$(ipcalc "$CIDR" 2>/dev/null)
    if [ -z "$CALC_OUTPUT" ]; then
        return 1
    fi
    
    # Get first usable IP (HostMin)
    local FIRST_IP=$(echo "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')
    
    # Service IP allocation:
    # HostMin + 0 (.1): Reserved/available
    # HostMin + 1 (.2): MediaMTX
    # HostMin + 2 (.3): Mumble
    # HostMin + 3-4 (.4-.5): Reserved for future services
    # HostMin + 5+ (.6+): Chunk-based allocation starts
    
    echo "${FIRST_IP%.*}.$((${FIRST_IP##*.} + 2))"
}

# --- Database Sync Functions ---
ensure_shared_directory() {
    if [ ! -d "$SHARED_DB_DIR" ]; then
        log "Creating shared Mumble directory in Syncthing"
        sudo -u radio mkdir -p "$SHARED_DB_DIR"
        sudo -u radio mkdir -p "$BACKUP_DIR"
    fi
}

check_database_integrity() {
    local db_path=$1
    local db_name=$2
    
    if [ ! -f "$db_path" ]; then
        log "Database check: $db_name does not exist"
        return 1
    fi
    
    log "Checking integrity of $db_name..."
    local integrity_check=$(sqlite3 "$db_path" "PRAGMA integrity_check;" 2>&1)
    
    if [ "$integrity_check" != "ok" ]; then
        log "ERROR: Database integrity check failed for $db_name: $integrity_check"
        return 1
    fi
    
    log "Database integrity check passed for $db_name"
    return 0
}

checkpoint_database() {
    local db_path=$1
    
    if [ ! -f "$db_path" ]; then
        return 0
    fi
    
    log "Checkpointing WAL for database: $db_path"
    
    # Force WAL checkpoint to merge everything into main database file
    sqlite3 "$db_path" "PRAGMA wal_checkpoint(TRUNCATE);" 2>&1 | logger -t mumble-election
    
    # Verify WAL files are gone or empty
    if [ -f "${db_path}-wal" ]; then
        local wal_size=$(stat -c%s "${db_path}-wal" 2>/dev/null || stat -f%z "${db_path}-wal" 2>/dev/null)
        if [ "$wal_size" -gt 0 ]; then
            log "WARNING: WAL file still has data after checkpoint (${wal_size} bytes)"
        fi
    fi
}

safe_copy_database() {
    local source=$1
    local dest=$2
    local temp_dest="${dest}.tmp"
    
    if [ ! -f "$source" ]; then
        log "ERROR: Source database not found: $source"
        return 1
    fi
    
    log "Safe copy: $source -> $dest"
    
    # Method 1: Use SQLite's own backup mechanism (preferred)
    if sqlite3 "$source" ".backup '$temp_dest'" 2>&1 | logger -t mumble-election; then
        log "SQLite backup completed successfully"
        
        # Verify the backup
        if check_database_integrity "$temp_dest" "backup"; then
            # Atomically move into place
            mv "$temp_dest" "$dest"
            log "Database copied successfully using SQLite backup"
            return 0
        else
            log "ERROR: Backup integrity check failed"
            rm -f "$temp_dest"
            return 1
        fi
    fi
    
    # Method 2: Fallback to checkpoint + copy if backup fails
    log "SQLite backup failed, falling back to checkpoint method"
    
    # Checkpoint the source database
    checkpoint_database "$source"
    
    # Copy main database file
    cp "$source" "$temp_dest"
    
    # Check if WAL/SHM files exist and are non-empty
    if [ -f "${source}-wal" ]; then
        local wal_size=$(stat -c%s "${source}-wal" 2>/dev/null || stat -f%z "${source}-wal" 2>/dev/null)
        if [ "$wal_size" -gt 0 ]; then
            log "WARNING: Copying non-empty WAL file"
            cp "${source}-wal" "${temp_dest}-wal"
        fi
    fi
    
    if [ -f "${source}-shm" ]; then
        cp "${source}-shm" "${temp_dest}-shm"
    fi
    
    # Verify integrity
    if check_database_integrity "$temp_dest" "copy"; then
        mv "$temp_dest" "$dest"
        # Clean up WAL/SHM if they exist
        [ -f "${temp_dest}-wal" ] && mv "${temp_dest}-wal" "${dest}-wal"
        [ -f "${temp_dest}-shm" ] && mv "${temp_dest}-shm" "${dest}-shm"
        log "Database copied successfully using checkpoint method"
        return 0
    else
        log "ERROR: Copy integrity check failed"
        rm -f "$temp_dest" "${temp_dest}-wal" "${temp_dest}-shm"
        return 1
    fi
}

create_initial_database() {
    log "No shared database found. Initializing from current installation."
    
    # If we have a working database, copy it to shared location
    if [ -f "$WORKING_DB" ]; then
        log "Copying existing working database to Syncthing"
        
        if safe_copy_database "$WORKING_DB" "$SHARED_DB"; then
            sudo chown radio:radio "$SHARED_DB"
            sudo chmod 644 "$SHARED_DB"
            log "Initial database created in Syncthing"
        else
            log "ERROR: Failed to create initial database"
            return 1
        fi
    else
        log "No existing database. Mumble will create default on first start."
        # Create empty placeholder
        sudo -u radio touch "$SHARED_DB"
    fi
}

sync_database_from_shared() {
    log "Syncing database FROM Syncthing to working location"
    
    # Wait for Syncthing to settle
    sleep 2
    
    if [ ! -f "$SHARED_DB" ]; then
        log "ERROR: Shared database not found at $SHARED_DB"
        return 1
    fi
    
    # Verify shared database integrity before using it
    if ! check_database_integrity "$SHARED_DB" "shared"; then
        log "ERROR: Shared database failed integrity check, will not sync"
        
        # Check if we have a recent backup
        local latest_backup=$(ls -t "$BACKUP_DIR"/*.sqlite 2>/dev/null | head -1)
        if [ -n "$latest_backup" ]; then
            log "Attempting to use latest backup: $(basename $latest_backup)"
            if check_database_integrity "$latest_backup" "backup"; then
                if safe_copy_database "$latest_backup" "$SHARED_DB"; then
                    sudo chown radio:radio "$SHARED_DB"
                    log "Restored shared database from backup"
                else
                    return 1
                fi
            else
                log "ERROR: Backup is also corrupted"
                return 1
            fi
        else
            log "ERROR: No usable backup found"
            return 1
        fi
    fi
    
    # Stop service if running
    if systemctl is-active --quiet "$MUMBLE_SERVICE_NAME"; then
        systemctl stop "$MUMBLE_SERVICE_NAME"
        sleep 2
    fi
    
    # Backup current working DB if it exists and differs
    if [ -f "$WORKING_DB" ]; then
        if ! cmp -s "$WORKING_DB" "$SHARED_DB"; then
            local BACKUP_NAME="working-backup-$(date +%Y%m%d-%H%M%S).sqlite"
            log "Creating backup of working DB: $BACKUP_NAME"
            
            if safe_copy_database "$WORKING_DB" "${BACKUP_DIR}/${BACKUP_NAME}"; then
                sudo chown radio:radio "${BACKUP_DIR}/${BACKUP_NAME}"
                
                # Keep only last 10 backups
                cd "$BACKUP_DIR" && ls -t *.sqlite 2>/dev/null | tail -n +11 | xargs -r rm --
            fi
        fi
    fi
    
    # Ensure working directory exists
    mkdir -p "$(dirname "$WORKING_DB")"
    
    # Copy from Syncthing to working location using safe method
    if safe_copy_database "$SHARED_DB" "$WORKING_DB"; then
        chown ${MUMBLE_USER}:${MUMBLE_USER} "$WORKING_DB"
        chmod 600 "$WORKING_DB"
        
        # Clean up any WAL/SHM files
        [ -f "${WORKING_DB}-wal" ] && chown ${MUMBLE_USER}:${MUMBLE_USER} "${WORKING_DB}-wal"
        [ -f "${WORKING_DB}-shm" ] && chown ${MUMBLE_USER}:${MUMBLE_USER} "${WORKING_DB}-shm"
        
        local db_size=$(stat -c%s "$WORKING_DB" 2>/dev/null || stat -f%z "$WORKING_DB")
        log "Database synced from Syncthing (${db_size} bytes)"
        return 0
    else
        log "ERROR: Failed to sync database from Syncthing"
        return 1
    fi
}

sync_database_to_shared() {
    log "Syncing database TO Syncthing from working location"
    
    if [ ! -f "$WORKING_DB" ]; then
        log "WARNING: No working database to sync"
        return 0
    fi
    
    # Verify working database integrity before syncing
    if ! check_database_integrity "$WORKING_DB" "working"; then
        log "ERROR: Working database failed integrity check, will not sync to Syncthing"
        return 1
    fi
    
    # Create timestamped backup before overwriting
    if [ -f "$SHARED_DB" ]; then
        local BACKUP_NAME="pre-sync-$(date +%Y%m%d-%H%M%S).sqlite"
        log "Creating pre-sync backup: $BACKUP_NAME"
        
        if safe_copy_database "$SHARED_DB" "${BACKUP_DIR}/${BACKUP_NAME}"; then
            sudo chown radio:radio "${BACKUP_DIR}/${BACKUP_NAME}"
        else
            log "WARNING: Failed to create pre-sync backup"
        fi
        
        # Keep only last 10 backups
        cd "$BACKUP_DIR" && ls -t *.sqlite 2>/dev/null | tail -n +11 | xargs -r rm --
    fi
    
    # Checkpoint working database before copying
    checkpoint_database "$WORKING_DB"
    
    # Copy to Syncthing directory using safe method
    local TEMP_SHARED="${SHARED_DB}.uploading"
    
    if safe_copy_database "$WORKING_DB" "$TEMP_SHARED"; then
        sudo chown radio:radio "$TEMP_SHARED"
        sudo chmod 644 "$TEMP_SHARED"
        
        # Atomic move into place
        sudo -u radio mv "$TEMP_SHARED" "$SHARED_DB"
        
        local db_size=$(stat -c%s "$SHARED_DB" 2>/dev/null || stat -f%z "$SHARED_DB")
        log "Database synced to Syncthing (${db_size} bytes)"
        
        # Give Syncthing a moment to notice and index the change
        sleep 2
        return 0
    else
        log "ERROR: Failed to sync database to Syncthing"
        rm -f "$TEMP_SHARED"
        return 1
    fi
}

# --- Main Election Logic ---

# Use flock to prevent concurrent election runs
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    log "Election already in progress, exiting"
    exit 0
fi

# Check if Mumble is enabled
MUMBLE_ENABLED=$(grep "^mumble=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)
if [ "$MUMBLE_ENABLED" != "y" ]; then
    log "Mumble not enabled in mesh.conf, exiting"
    exit 0
fi

# Check dependencies
if [ ! -f "$REGISTRY_STATE_FILE" ]; then
    log "Registry file not found, exiting"
    exit 1
fi

# Ensure Syncthing shared directory exists
ensure_shared_directory

# Calculate VIPs
IPV4_NETWORK=$(grep "^ipv4_network=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)
if [ -z "$IPV4_NETWORK" ]; then
    log "ERROR: ipv4_network not found in mesh.conf"
    exit 1
fi

MUMBLE_IPV4_VIP=$(get_mumble_ipv4_vip "$IPV4_NETWORK")
MUMBLE_IPV6_VIP_WITH_MASK=$(/usr/local/bin/mumble-ip.sh)
MUMBLE_IPV6_VIP=${MUMBLE_IPV6_VIP_WITH_MASK%/*}

# Normalize IPv6 to compressed form (remove :0000: before ::)
MUMBLE_IPV6_VIP=$(echo "$MUMBLE_IPV6_VIP" | sed 's/:0000::/::/g')
MUMBLE_IPV6_VIP_WITH_MASK="${MUMBLE_IPV6_VIP}/128"

if [ -z "$MUMBLE_IPV4_VIP" ] || [ -z "$MUMBLE_IPV6_VIP" ]; then
    log "ERROR: Failed to calculate VIPs"
    exit 1
fi

IPV4_VIP_WITH_MASK="${MUMBLE_IPV4_VIP}/${IPV4_NETWORK#*/}"
log "Mumble VIPs: IPv4=$MUMBLE_IPV4_VIP, IPv6=$MUMBLE_IPV6_VIP"

# --- Detect Current Incumbent ---
# Use the Alfred node registry as the authoritative source for incumbency.
# node-manager publishes IS_MUMBLE_SERVER=true only when the local node holds
# both mumble-server.service AND the Mumble VIP — so the registry reflects the
# actual winner, not just who has the IP configured.  All nodes read the same
# Alfred-propagated registry, so there is no per-node ARP/local-IP ambiguity.
CURRENT_LEADER_MAC=""
if [ -f "$REGISTRY_STATE_FILE" ]; then
    INCUMBENT_NODE_ID=$(grep "IS_MUMBLE_SERVER='true'" "$REGISTRY_STATE_FILE" \
        | head -1 | sed "s/NODE_\([^_]*\)_.*/\1/")
    if [ -n "$INCUMBENT_NODE_ID" ]; then
        INCUMBENT_MAC=$(grep "^NODE_${INCUMBENT_NODE_ID}_MAC_ADDRESS=" "$REGISTRY_STATE_FILE" \
            | cut -d"'" -f2)
        [ -n "$INCUMBENT_MAC" ] && CURRENT_LEADER_MAC="$INCUMBENT_MAC"
    fi
fi
if [ -n "$CURRENT_LEADER_MAC" ]; then
    log "Current incumbent: $CURRENT_LEADER_MAC (bias: +${INCUMBENT_BIAS} TQ)"
fi

# --- Run Election ---
log "Running Mumble election..."

NOW=$(date +%s)
STALE_THRESHOLD=600
BEST_CANDIDATE_MAC=""
HIGHEST_TQ="-1"

# Find best candidate by TQ
while read tq_line; do
    tq_varname=$(echo "$tq_line" | cut -d'=' -f1)
    CURRENT_TQ=$(echo "$tq_line" | cut -d'=' -f2 | tr -d "'")
    MAC_SANITIZED=$(echo "$tq_varname" | sed -n 's/NODE_\([0-9a-fA-F]\+\)_TQ_AVERAGE/\1/p')

    if [ -n "$MAC_SANITIZED" ]; then
        # Check timestamp
        TIMESTAMP_VAR="NODE_${MAC_SANITIZED}_LAST_SEEN_TIMESTAMP"
        TIMESTAMP_VAL=$(grep "^${TIMESTAMP_VAR}=" "$REGISTRY_STATE_FILE" | cut -d'=' -f2 | tr -d "'")
        
        if [ -z "$TIMESTAMP_VAL" ] || [ $((NOW - TIMESTAMP_VAL)) -gt $STALE_THRESHOLD ]; then
            continue
        fi

        MAC_VAR="NODE_${MAC_SANITIZED}_MAC_ADDRESS"
        MAC_LINE=$(grep "^${MAC_VAR}=" "$REGISTRY_STATE_FILE")

        if [ -n "$MAC_LINE" ]; then
            CURRENT_MAC=$(echo "$MAC_LINE" | cut -d'=' -f2 | tr -d "'")

            # Apply incumbent bias: current leader gets a TQ bonus to prevent
            # unnecessary service migration from normal TQ fluctuation
            EFFECTIVE_TQ="$CURRENT_TQ"
            if [ -n "$CURRENT_LEADER_MAC" ] && [ "$CURRENT_MAC" == "$CURRENT_LEADER_MAC" ]; then
                EFFECTIVE_TQ=$(echo "$CURRENT_TQ + $INCUMBENT_BIAS" | bc -l)
            fi

            if (( $(echo "$EFFECTIVE_TQ > $HIGHEST_TQ" | bc -l) )); then
                HIGHEST_TQ=$EFFECTIVE_TQ
                BEST_CANDIDATE_MAC=$CURRENT_MAC
            elif (( $(echo "$EFFECTIVE_TQ == $HIGHEST_TQ" | bc -l) )) && [[ "$CURRENT_MAC" < "$BEST_CANDIDATE_MAC" ]]; then
                BEST_CANDIDATE_MAC=$CURRENT_MAC
            fi
        fi
    fi
done < <(grep 'NODE_.*_TQ_AVERAGE=' "$REGISTRY_STATE_FILE")

# --- Decision and Action ---
if [ -z "$BEST_CANDIDATE_MAC" ]; then
    # === NO WINNER ===
    log "No suitable candidates found"
    
    # Remove VIPs if we have them
    if ip addr show dev "$CONTROL_IFACE" | grep -q "inet $MUMBLE_IPV4_VIP/"; then
        ip addr del "$IPV4_VIP_WITH_MASK" dev "$CONTROL_IFACE" 2>/dev/null
    fi
    if ip -6 addr show dev "$CONTROL_IFACE" | grep -q "$MUMBLE_IPV6_VIP/"; then
        ip addr del "$MUMBLE_IPV6_VIP_WITH_MASK" dev "$CONTROL_IFACE" 2>/dev/null
    fi
    
    # Stop service and sync database back
    if systemctl is-active --quiet "$MUMBLE_SERVICE_NAME"; then
        log "Stopping Mumble service"
        systemctl stop "$MUMBLE_SERVICE_NAME"
        sleep 2
        sync_database_to_shared
    fi
    systemctl reset-failed "$MUMBLE_SERVICE_NAME" 2>/dev/null
    
    rm -f /var/run/mesh-mumble.state

elif [ "$MY_MAC" == "$BEST_CANDIDATE_MAC" ]; then
    # === I WON ===
    log "Won election (TQ: $HIGHEST_TQ)"
    
    # Check if we already have VIPs
    HAS_IPV4_VIP=false
    HAS_IPV6_VIP=false
    
    if ip addr show dev "$CONTROL_IFACE" | grep -q "inet $MUMBLE_IPV4_VIP/"; then
        HAS_IPV4_VIP=true
    fi
    if ip addr show dev "$CONTROL_IFACE" | grep -q "inet6 $MUMBLE_IPV6_VIP/"; then
        HAS_IPV6_VIP=true
    fi
    
    # Assign VIPs if needed
    if [ "$HAS_IPV4_VIP" = false ]; then
        log "Assigning IPv4 VIP: $MUMBLE_IPV4_VIP"
        ip addr add "$IPV4_VIP_WITH_MASK" dev "$CONTROL_IFACE"
        
        if command -v arping &> /dev/null; then
            arping -c 1 -A -I "$CONTROL_IFACE" "$MUMBLE_IPV4_VIP" 2>/dev/null
        fi
    fi
    
    if [ "$HAS_IPV6_VIP" = false ]; then
        log "Assigning IPv6 VIP: $MUMBLE_IPV6_VIP"
        ip addr add "$MUMBLE_IPV6_VIP_WITH_MASK" dev "$CONTROL_IFACE" 2>/dev/null
    fi
    
    WAS_ALREADY_LEADER=false
    if [ "$HAS_IPV4_VIP" = true ] && [ "$HAS_IPV6_VIP" = true ]; then
        WAS_ALREADY_LEADER=true
    fi
    
    # Update Mumble config to bind to VIPs
    if [ -f "$MUMBLE_CONFIG" ]; then
        # Update host binding (ini format)
        sed -i "s/^host=.*/host=$MUMBLE_IPV4_VIP/" "$MUMBLE_CONFIG"
        log "Updated Mumble config to bind to $MUMBLE_IPV4_VIP"
    fi
    
    # Sync database and start service if needed
    if [ "$WAS_ALREADY_LEADER" = false ] || ! systemctl is-active --quiet "$MUMBLE_SERVICE_NAME"; then
        log "Taking over Mumble service..."
        
        # Initialize shared DB if it doesn't exist
        if [ ! -f "$SHARED_DB" ] || [ ! -s "$SHARED_DB" ]; then
            create_initial_database
        fi
        
        # Sync database from Syncthing
        sync_database_from_shared
        
        # Start service
        log "Starting Mumble service"
        systemctl start "$MUMBLE_SERVICE_NAME"
        sleep 2
        
        # Verify it started
        if systemctl is-active --quiet "$MUMBLE_SERVICE_NAME"; then
            log "Mumble service started successfully"
        else
            log "ERROR: Mumble service failed to start"
            systemctl status "$MUMBLE_SERVICE_NAME" --no-pager | logger -t mumble-election
        fi
    else
        log "Already leader and service running. No action needed."
    fi
    
    touch /var/run/mesh-mumble.state

else
    # === I LOST ===
    log "Lost election to ${BEST_CANDIDATE_MAC}"
    
    # Remove VIPs if we have them
    if ip addr show dev "$CONTROL_IFACE" | grep -q "inet $MUMBLE_IPV4_VIP/"; then
        log "Removing IPv4 VIP"
        ip addr del "$IPV4_VIP_WITH_MASK" dev "$CONTROL_IFACE" 2>/dev/null
    fi
    if ip -6 addr show dev "$CONTROL_IFACE" | grep -q "inet6 $MUMBLE_IPV6_VIP/"; then
        log "Removing IPv6 VIP"
        ip addr del "$MUMBLE_IPV6_VIP_WITH_MASK" dev "$CONTROL_IFACE" 2>/dev/null
    fi
    
    # Stop service and sync database back
    if systemctl is-active --quiet "$MUMBLE_SERVICE_NAME"; then
        log "Stopping Mumble service"
        systemctl stop "$MUMBLE_SERVICE_NAME"
        sleep 2
        sync_database_to_shared
    fi
    systemctl reset-failed "$MUMBLE_SERVICE_NAME" 2>/dev/null
    
    rm -f /var/run/mesh-mumble.state
fi

log "Election complete"
exit 0
