#!/bin/bash
#
#  Update the mesh tools to the latest revision
#


get_board_type() {
    if [ -f /proc/device-tree/model ]; then
        model=$(tr -d '\0' < /proc/device-tree/model)

        case "$model" in
            *"ROCK 3A"*|*"Rock 3A"*)
                echo "rock3a"
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

echo "Testing internet connection"
if ! ping -W3 -q -c 2 1.1.1.1 > /dev/null 2>&1; then
    echo "No internet detected, exiting"
    exit 2
fi

BOARD=$(get_board_type)
LOCAL_VERSION=0
if [[ -f /etc/manet_version.txt ]]; then
	LOCAL_VERSION=$(head -n 1 /etc/manet_version.txt)
	echo -n "Local MANET tools are at version $LOCAL_VERSION, "
fi
REMOTE_VERSION=$(curl -s https://raw.githubusercontent.com/very-srs/MANET/refs/heads/main/MANET/node_tools/version.txt | head -n 1)
echo "github MANET tools are at version $REMOTE_VERSION"

if [[ "$LOCAL_VERSION" == "$REMOTE_VERSION" ]]; then
	echo "Node is already running the latest software release, exiting"
	exit 0;
else
	case "$BOARD" in
	    rock3a)
	        echo "Running on Rock 3A"
	        wget -q https://github.com/very-srs/MANET/raw/refs/heads/main/MANET/install_packages/rock-tools.tar.gz -O /root/tools.tar.gz || {
                echo "ERROR: Failed to download rock3a tools package.  Not updating"
                exit 1
        	}
	        ;;
	    rpi5)
	        echo "Running on Pi 5"
	        wget -q https://github.com/very-srs/MANET/raw/refs/heads/main/MANET/install_packages/rpi5-tools.tar.gz -O /root/tools.tar.gz || {
                echo "ERROR: Failed to download rpi5 tools package.  Not updating"
                exit 1
        	}
	        ;;
	    rpi4)
	        echo "Running on Pi 4B/CM4"
	        wget -q https://github.com/very-srs/MANET/raw/refs/heads/main/MANET/install_packages/cm4-tools.tar.gz -O /root/tools.tar.gz || {
                echo "ERROR: Failed to download rpi4 tools package.  Not updating"
                exit 1
        	}
	        ;;
	    *)
	        echo "Unknown board type, cannot update"
	        exit 1
	        ;;
	esac
fi

tar -zxf /root/tools.tar.gz -C /
echo "Node tools updated to version $REMOTE_VERSION - $(tail -n 1 /etc/manet_version.txt)"

