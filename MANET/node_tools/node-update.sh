#!/bin/bash
#
#  Update the mesh tools to the latest revision
#

# --- Parse Arguments ---
ROUTINE_MODE=false
if [ "$1" == "--routine" ]; then
    ROUTINE_MODE=true
    # Redirect all output to /dev/null in routine mode
    exec &>/dev/null
fi

get_board_type() {
    if [ -f /proc/device-tree/model ]; then
        model=$(tr -d '\0' < /proc/device-tree/model)

        case "$model" in
            *"ROCK3"*)
                echo "r3a"
                ;;
            *"Raspberry Pi 5"*)
                echo "rpi5"
                ;;
            *"Raspberry Pi 4"*|*"Raspberry Pi Compute Module 4"*)
                echo "rpi4"
                ;;
            *)
                echo "unknown"
                return 1
                ;;
        esac
        return 0
    else
        echo "unknown"
        return 1
    fi
}

# --- Routine Mode: Check if version file is stale ---
if [ "$ROUTINE_MODE" = true ]; then
    VERSION_FILE="/etc/manet_version.txt"
    
    # Check if file exists and is newer than 1 day
    if [ -f "$VERSION_FILE" ]; then
        # Check if file was modified within last 24 hours
        if [ -z "$(find "$VERSION_FILE" -mtime +1 2>/dev/null)" ]; then
            # File is fresh, exit silently
            exit 0
        fi
    fi
    # File is stale or doesn't exist, continue to update check
else
    # --- Normal Mode: Check Internet ---
    echo "Testing internet connection"
    if ! ping -W3 -q -c 2 1.1.1.1 > /dev/null 2>&1; then
        echo "No internet detected, exiting"
        exit 2
    fi
fi

BOARD=$(get_board_type)
LOCAL_VERSION=0
if [[ -f /etc/manet_version.txt ]]; then
	LOCAL_VERSION=$(head -n 1 /etc/manet_version.txt)
	if [ "$ROUTINE_MODE" = false ]; then
		echo -n "Local MANET tools are at version $LOCAL_VERSION, "
	fi
fi

REMOTE_VERSION_URL="https://raw.githubusercontent.com/very-srs/MANET/refs/heads/main/MANET/node_tools/version.txt"
case "$BOARD" in
    rpi5)
        REMOTE_VERSION_URL="https://www.colorado-governor.com/manet/rpi5/manet_version.txt"
        ;;
esac

REMOTE_VERSION=$(curl -H 'Cache-Control: no-cache, no-store' \
    -H 'Pragma: no-cache' \
    -fs "$REMOTE_VERSION_URL" | head -n 1 2>/dev/null)

if [ -z "$REMOTE_VERSION" ] && [ "$REMOTE_VERSION_URL" != "https://raw.githubusercontent.com/very-srs/MANET/refs/heads/main/MANET/node_tools/version.txt" ]; then
    REMOTE_VERSION=$(curl -H 'Cache-Control: no-cache, no-store' \
        -H 'Pragma: no-cache' \
        -fs https://raw.githubusercontent.com/very-srs/MANET/refs/heads/main/MANET/node_tools/version.txt | head -n 1 2>/dev/null)
fi

if [ -z "$REMOTE_VERSION" ]; then
    if [ "$ROUTINE_MODE" = false ]; then
        echo "ERROR: Failed to fetch remote version. Check internet connection."
    fi
    exit 3
fi

if [ "$ROUTINE_MODE" = false ]; then
	echo "github MANET tools are at version $REMOTE_VERSION"
fi

if [[ "$LOCAL_VERSION" == "$REMOTE_VERSION" ]]; then
	if [ "$ROUTINE_MODE" = false ]; then
		echo "Node is already running the latest software release, exiting"
	fi
	# Touch the version file to update its timestamp (prevent repeated checks)
	touch /etc/manet_version.txt 2>/dev/null
	exit 0
else
	case "$BOARD" in
	    r3a)
	        if [ "$ROUTINE_MODE" = false ]; then
	            echo "Running on Rock 3A"
	        fi
	        wget -q  https://www.colorado-governor.com/manet/r3a-tools.tar.gz -O /root/tools.tar.gz 2>/dev/null || {
                if [ "$ROUTINE_MODE" = false ]; then
                    echo "ERROR: Failed to download rock3a tools package.  Not updating"
                fi
                exit 1
        	}
	        ;;
	    rpi5)
	        if [ "$ROUTINE_MODE" = false ]; then
	            echo "Running on Pi 5"
	        fi
	        wget -q https://www.colorado-governor.com/manet/rpi5/rpi5-tools.tar.gz -O /root/tools.tar.gz 2>/dev/null || {
                if [ "$ROUTINE_MODE" = false ]; then
                    echo "ERROR: Failed to download rpi5 tools package.  Not updating"
                fi
                exit 1
        	}
	        ;;
	    rpi4)
	        if [ "$ROUTINE_MODE" = false ]; then
	            echo "Running on Pi 4B/CM4"
	        fi
#	        wget -q https://github.com/very-srs/MANET/raw/refs/heads/main/MANET/install_packages/cm4-tools.tar.gz -O /root/tools.tar.gz 2>/dev/null || {
	        wget -q  https://www.colorado-governor.com/manet/cm4-tools.tar.gz -O /root/tools.tar.gz 2>/dev/null || {
                if [ "$ROUTINE_MODE" = false ]; then
                    echo "ERROR: Failed to download rpi4 tools package.  Not updating"
                fi
                exit 1
        	}
	        ;;
	    *)
	        if [ "$ROUTINE_MODE" = false ]; then
	            echo "Unknown board type, cannot update"
	        fi
	        exit 1
	        ;;
	esac
fi

# --no-overwrite-dir: never let an archive restyle a directory that already
# exists on the node. A tarball carrying an entry for its own root would
# otherwise apply that mode to /, and a stray 0700 there locks every
# non-root process out of the filesystem.
tar -zxf /root/tools.tar.gz --no-overwrite-dir -C / 2>/dev/null

# Files land, but nothing that reads them is told. A new unit or a drop-in
# stays inert until systemd is reloaded, and a motd hook only prints once it
# is linked into /etc/update-motd.d - neither of which happens on a node that
# is updated over the air rather than reflashed, since radio-setup does not
# run again. Only reached on an actual update, so this costs nothing in
# steady state.
systemctl daemon-reload 2>/dev/null || true
mkdir -p /etc/update-motd.d
[ -x /usr/local/bin/manet-provision-status.sh ] && \
    ln -sf /usr/local/bin/manet-provision-status.sh /etc/update-motd.d/50-manet-provision
[ -x /usr/local/bin/manet-power-status.sh ] && \
    ln -sf /usr/local/bin/manet-power-status.sh /etc/update-motd.d/55-manet-power

# node-manager.sh is generated, so the archive does not carry it -- shipping the
# committed copy would put every ACS node back on the static orchestrator. Both
# variants did just arrive, though, so re-publish whichever one this node has
# selected, or it keeps running the previous release's orchestrator until
# radio-setup happens to run again.
#
# acs= is matched the same way radio-setup.sh and the auto_update gate match it:
# every writer produces y/n, but mesh.conf is operator-editable.
if grep -qiE '^acs=(y|yes|1|true)[[:space:]]*$' /etc/mesh.conf 2>/dev/null; then
    NODE_MANAGER_VARIANT=/usr/local/bin/node-manager-acs.sh
else
    NODE_MANAGER_VARIANT=/usr/local/bin/node-manager-static.sh
fi
if [ -f "$NODE_MANAGER_VARIANT" ] && \
   ! cmp -s "$NODE_MANAGER_VARIANT" /usr/local/bin/node-manager.sh; then
    cp "$NODE_MANAGER_VARIANT" /usr/local/bin/node-manager.sh
    chmod 0755 /usr/local/bin/node-manager.sh
    systemctl restart node-manager.service 2>/dev/null || true
    if [ "$ROUTINE_MODE" = false ]; then
        echo "node-manager.sh re-published from $(basename "$NODE_MANAGER_VARIANT")"
    fi
fi

if [ "$ROUTINE_MODE" = false ]; then
	echo "Node tools updated to version $REMOTE_VERSION - $(tail -n 1 /etc/manet_version.txt)"
fi
