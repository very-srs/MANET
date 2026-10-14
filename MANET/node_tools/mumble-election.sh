#!/bin/bash
# Elect a Mumble server by mean mesh throughput and synchronize its database
# through Syncthing to preserve data across leadership changes.

. "${MANET_TOOLS_DIR:-${BASH_SOURCE[0]%/*}}/manet-service-common.sh"
service_election_preflight mumble
case $? in
    0) exit 0 ;;
    125) ;;  # Unsettled, or no request submitted to an available worker.
    *) exit 1 ;;
esac

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

# The shared ranker adds a 10 Mbit/s incumbent bias to reduce service churn.

# State
MY_MAC=$(cat "/sys/class/net/${CONTROL_IFACE}/address" 2>/dev/null || true)
LOCK_FILE="${MUMBLE_ELECTION_LOCK:-/var/run/mumble-election.lock}"

# Send a prefixed diagnostic to the Mumble election journal tag.
log() {
    if [ "${MUMBLE_CAPTURE_LOG:-}" = true ]; then
        printf '%s\n' "MUMBLE-ELECTION: $1" >&2
        return
    fi
    printf '%s\n' "MUMBLE-ELECTION: $1" | systemd-cat -t mumble-election
}

# --- Dependency Checks ---
if [ -z "$MY_MAC" ]; then
    log "Cannot determine local MAC address (${CONTROL_IFACE} not up). Exiting."
    exit 1
fi
if ! command -v sqlite3 &>/dev/null; then
    log "ERROR: 'sqlite3' command not found. Please install it."
    exit 1
fi

if ! command -v bc &>/dev/null; then
    log "ERROR: 'bc' command not found. Please install it."
    exit 1
fi

# --- VIP Calculation Functions ---
# Print the third usable address in the supplied CIDR, reserved for Mumble.
get_mumble_ipv4_vip() {
    local CIDR="$1"
    local CALC_OUTPUT=$(manet-ipcalc.sh "$CIDR" 2>/dev/null)
    if [ -z "$CALC_OUTPUT" ]; then
        return 1
    fi
    
    # Get first usable IP (HostMin)
    local FIRST_IP=$(printf '%s\n' "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')
    
    # Service IP allocation:
    # HostMin + 0 (.1): Reserved/available
    # HostMin + 1 (.2): MediaMTX
    # HostMin + 2 (.3): Mumble
    # HostMin + 3-4 (.4-.5): Reserved for future services
    # HostMin + 5+ (.6+): Chunk-based allocation starts
    
    printf '%s\n' "${FIRST_IP%.*}.$((${FIRST_IP##*.} + 2))"
}

# --- Database Sync Functions ---
# Create SHARED_DB_DIR and BACKUP_DIR as radio whenever either is absent.
ensure_shared_directory() {
    if [ ! -d "$SHARED_DB_DIR" ]; then
        log "Creating shared Mumble directory in Syncthing"
        sudo -u radio mkdir -p "$SHARED_DB_DIR"
    fi
    if [ ! -d "$BACKUP_DIR" ]; then
        sudo -u radio mkdir -p "$BACKUP_DIR"
    fi
}

# Check database path $1 with SQLite integrity_check and log it as name $2.
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

# Request a WAL checkpoint for the supplied database and warn on remaining WAL.
checkpoint_database() {
    local db_path=$1 checkpoint_error
    
    if [ ! -f "$db_path" ]; then
        return 0
    fi
    
    log "Checkpointing WAL for database: $db_path"
    
    # Discard the numeric result row, retaining SQLite's error diagnostics.
    if ! checkpoint_error=$(sqlite3 "$db_path" \
            "PRAGMA wal_checkpoint(TRUNCATE);" 2>&1 >/dev/null); then
        log "WARNING: WAL checkpoint failed: $checkpoint_error"
    fi
    
    # Verify WAL files are gone or empty
    if [ -f "${db_path}-wal" ]; then
        local wal_size=$(stat -c%s "${db_path}-wal" 2>/dev/null || stat -f%z "${db_path}-wal" 2>/dev/null)
        if [ "$wal_size" -gt 0 ]; then
            log "WARNING: WAL file still has data after checkpoint (${wal_size} bytes)"
        fi
    fi
}

# Copy source $1 through a temporary destination $2.tmp and check integrity.
# Prefer SQLite backup, falling back to checkpoint + copy if backup fails.
safe_copy_database() {
    local source=$1
    local dest=$2
    local temp_dest="${dest}.tmp"
    local mode=${3:-stopped}
    local backup_output backup_status
    
    if [ ! -f "$source" ]; then
        log "ERROR: Source database not found: $source"
        return 1
    fi
    
    log "Safe copy: $source -> $dest"
    
    # Method 1: Use SQLite's own backup mechanism (preferred)
    backup_output=$(sqlite3 "$source" ".backup '$temp_dest'" 2>&1)
    backup_status=$?
    [ -z "$backup_output" ] || log "$backup_output"
    if [ "$backup_status" -eq 0 ]; then
        log "SQLite backup completed successfully"
        
        # Verify the backup
        if check_database_integrity "$temp_dest" "backup"; then
            # Atomically move into place
            mv "$temp_dest" "$dest" || return 1
            log "Database copied successfully using SQLite backup"
            return 0
        else
            log "ERROR: Backup integrity check failed"
            rm -f "$temp_dest"
        fi
    fi
    
    # A live WAL cannot safely be copied as separate changing files.
    if [ "$mode" = live ]; then
        log 'Live SQLite backup failed, retrying at the next election'
        rm -f "$temp_dest" "${temp_dest}-wal" "${temp_dest}-shm"
        return 1
    fi

    # Method 2: Fallback to checkpoint + copy if backup fails
    log "SQLite backup failed, falling back to checkpoint method"
    
    # Checkpoint the source database
    checkpoint_database "$source"
    
    # Copy main database file
    cp "$source" "$temp_dest" || return 1
    
    # Check if WAL/SHM files exist and are non-empty
    if [ -f "${source}-wal" ]; then
        local wal_size=$(stat -c%s "${source}-wal" 2>/dev/null || stat -f%z "${source}-wal" 2>/dev/null)
        if [ "$wal_size" -gt 0 ]; then
            log "WARNING: Copying non-empty WAL file"
            cp "${source}-wal" "${temp_dest}-wal" || return 1
        fi
    fi
    
    if [ -f "${source}-shm" ]; then
        cp "${source}-shm" "${temp_dest}-shm" || return 1
    fi
    
    # Verify integrity
    if check_database_integrity "$temp_dest" "copy"; then
        mv "$temp_dest" "$dest" || return 1
        # Clean up WAL/SHM if they exist
        if [ -f "${temp_dest}-wal" ]; then
            mv "${temp_dest}-wal" "${dest}-wal" || return 1
        fi
        if [ -f "${temp_dest}-shm" ]; then
            mv "${temp_dest}-shm" "${dest}-shm" || return 1
        fi
        log "Database copied successfully using checkpoint method"
        return 0
    else
        log "ERROR: Copy integrity check failed"
        rm -f "$temp_dest" "${temp_dest}-wal" "${temp_dest}-shm"
        return 1
    fi
}

# Seed SHARED_DB from WORKING_DB or create an empty placeholder as radio.
create_initial_database() {
    log "No shared database found. Initializing from current installation."
    
    # Copy a valid local database to the shared directory.
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

# Recover SHARED_DB from backup if needed, stop MUMBLE_SERVICE_NAME and
# replace WORKING_DB with a checked copy owned by MUMBLE_USER.
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
        
        # Check for a recent backup.
        # Select only the newest .sqlite by modification time for recovery.
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
                # Sort by modification time before dropping all but ten backups.
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

# Back up SHARED_DB and publish a checked WORKING_DB copy for Syncthing.
sync_database_to_shared() {
    local mode=${1:-stopped} stamp=${2:-} signature result
    local helper="${MANET_TOOLS_DIR:-/usr/local/bin}/manet_service_handover.py"
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
        # Sort by modification time before dropping all but ten backups.
        cd "$BACKUP_DIR" && ls -t *.sqlite 2>/dev/null | tail -n +11 | xargs -r rm --
    fi
    
    # Checkpoint working database before copying
    checkpoint_database "$WORKING_DB"

    # Record the version before copying. Writes during backup or publication
    # stay dirty and are exported again on the next cadence pass.
    if [ -n "$stamp" ]; then
        signature=$(python3 "$helper" db-signature "$WORKING_DB") || return 1
    fi
    
    # Copy to Syncthing directory using safe method
    local TEMP_SHARED="${SHARED_DB}.uploading"
    
    if safe_copy_database "$WORKING_DB" "$TEMP_SHARED" "$mode"; then
        sudo chown radio:radio "$TEMP_SHARED" || return 1
        sudo chmod 644 "$TEMP_SHARED" || return 1

        if [ "$mode" = live ]; then
            # Backup may take time. A retiring owner must not publish over
            # the current winner's canonical snapshot.
            result=$(python3 "$helper" rank mumble \
                "$REGISTRY_STATE_FILE") || return 1
            if [ "${result%% *}" != "$MY_MAC" ]; then
                log 'Winner changed before Mumble database publication'
                rm -f "$TEMP_SHARED"
                return 1
            fi
        fi
        
        # Atomic move into place
        if ! sudo -u radio mv "$TEMP_SHARED" "$SHARED_DB"; then
            log "ERROR: Failed to replace shared database"
            rm -f "$TEMP_SHARED"
            return 1
        fi
        if [ -n "$stamp" ]; then
            printf '%s\n' "$signature" > "$stamp.tmp" || return 1
            mv "$stamp.tmp" "$stamp" || return 1
        fi
        
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

# Prepare DB and wildcard bindings while no VIP has yet been claimed.
prepare_mumble_service() {
    if [ -f "$MUMBLE_CONFIG" ]; then
        if grep -q '^host=' "$MUMBLE_CONFIG"; then
            sed -i 's/^host=.*/host=/' "$MUMBLE_CONFIG" || return 1
        else
            printf '\nhost=\n' >> "$MUMBLE_CONFIG" || return 1
        fi
    else
        log "Mumble config missing"
        return 1
    fi
    ensure_shared_directory || return 1
    # Initialize shared DB if it doesn't exist
    if [ ! -f "$SHARED_DB" ] || [ ! -s "$SHARED_DB" ]; then
        create_initial_database
    fi

    # Sync database from Syncthing
    if ! sync_database_from_shared; then
        log "WARNING: Shared database sync failed; using local state"
        # Sync can fail before stopping the old server. Reload its binding.
        if systemctl is-active --quiet "$MUMBLE_SERVICE_NAME"; then
            systemctl stop "$MUMBLE_SERVICE_NAME"
            sleep 2
        fi
        if [ -f "$WORKING_DB" ] &&
            ! check_database_integrity "$WORKING_DB" "working"; then
            log "ERROR: Local database is corrupt; cannot start Mumble"
            return 1
        fi
    fi

    systemctl start "$MUMBLE_SERVICE_NAME"
}

# A listening Mumble server must also have a valid working database.
verify_mumble_database() {
    check_database_integrity "$WORKING_DB" "working"
}

# Export changed live data only from the locally elected, serving winner.
export_mumble_changes() {
    local signature previous="" details rc db_size line
    local helper="${MANET_TOOLS_DIR:-/usr/local/bin}/manet_service_handover.py"
    signature=$(python3 "$helper" db-signature "$WORKING_DB") || return 1
    if [ -f "$LOCK_FILE.db-export" ]; then
        read -r previous < "$LOCK_FILE.db-export"
    fi
    [ "$signature" != "$previous" ] || return 0
    # Buffer this pass only. Failed exports replay all captured diagnostics.
    details=$(
        MUMBLE_CAPTURE_LOG=true
        {
            ensure_shared_directory &&
                sync_database_to_shared live "$LOCK_FILE.db-export"
        } 2>&1
    )
    rc=$?
    if [ "$rc" -ne 0 ]; then
        log 'Mumble DB export failed, retrying next election'
        while IFS= read -r line; do
            [ -z "$line" ] || log "${line#MUMBLE-ELECTION: }"
        done <<< "$details"
        return "$rc"
    fi
    db_size=$(stat -c%s "$SHARED_DB") || return 1
    log "Exported Mumble DB ($db_size bytes)"
}

# Preserve final data without racing the winner's canonical shared snapshot.
release_mumble_database() {
    local directory="$SHARED_DB_DIR/retired/${MY_MAC//:/}" generation
    local helper="${MANET_TOOLS_DIR:-/usr/local/bin}/manet_service_handover.py"
    read -r generation < /proc/sys/kernel/random/uuid || return 1
    sudo -u radio mkdir -p "$directory" || return 1
    local SHARED_DB="$directory/$generation.sqlite"
    log "Preserving retired owner's final database at $SHARED_DB"
    sync_database_to_shared || return 1
    if ! python3 "$helper" prune-retired "$SHARED_DB"; then
        # Publication succeeded. Do not create more snapshots just to retry
        # cleanup. The next final export retries this owner's pruning.
        log 'WARNING: Failed to prune retired Mumble snapshots'
    fi
}

# --- Main Election Logic ---

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

# Calculate VIPs
IPV4_NETWORK=$(grep "^ipv4_network=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)
if [ -z "$IPV4_NETWORK" ]; then
    log "ERROR: ipv4_network not found in mesh.conf"
    exit 1
fi

MUMBLE_IPV4_VIP=$(get_mumble_ipv4_vip "$IPV4_NETWORK")
MUMBLE_IPV6_VIP_WITH_MASK=$(service_election_ipv6 mumble \
    /usr/local/bin/mumble-ip.sh) || exit 1
MUMBLE_IPV6_VIP=${MUMBLE_IPV6_VIP_WITH_MASK%/*}

if [ -z "$MUMBLE_IPV4_VIP" ] || [ -z "$MUMBLE_IPV6_VIP" ]; then
    log "ERROR: Failed to calculate VIPs"
    exit 1
fi

IPV4_VIP_WITH_MASK="${MUMBLE_IPV4_VIP}/${IPV4_NETWORK#*/}"


SERVICE_KIND=mumble
SERVICE_UNIT="$MUMBLE_SERVICE_NAME"
SERVICE_TCP_PORTS=64738
SERVICE_UDP_PORTS=64738
SERVICE_IPV6_VIP_WITH_MASK="$MUMBLE_IPV6_VIP_WITH_MASK"
SERVICE_PREPARE_HOOK=prepare_mumble_service
SERVICE_VERIFY_HOOK=verify_mumble_database
SERVICE_RELEASE_HOOK=release_mumble_database
SERVICE_SERVING_HOOK=export_mumble_changes
service_election_run
