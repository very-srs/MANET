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

tar -zxf /root/tools.tar.gz -C / 2>/dev/null

if [ "$ROUTINE_MODE" = false ]; then
	echo "Node tools updated to version $REMOTE_VERSION - $(tail -n 1 /etc/manet_version.txt)"
fi
