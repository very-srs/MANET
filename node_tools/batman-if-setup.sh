#!/bin/bash
set -e

# Source mesh configuration to get the MESH_NAME variable
if [ -f /etc/default/mesh ]; then
    source /etc/default/mesh
else
    echo "Error: Mesh configuration /etc/default/mesh not found!" >&2
    exit 1
fi

WLAN_INTERFACES=$(networkctl | awk '/wlan/ {print $2}' | tr '\n' ' ')

start() {
    echo "Starting BATMAN-ADV setup..."
    #change to batman V algo
	#batctl ra BATMAN_V

    # Create bat0 interface if it doesn't exist
    ip link show bat0 &>/dev/null || ip link add name bat0 type batadv
	batctl gw_mode client

    for WLAN in $WLAN_INTERFACES; do
        echo "--> Configuring interface: $WLAN"

        # Set the interface type to mesh
        ip link set "$WLAN" type mesh
        ip link set "$WLAN" up

        # Wait for interface to be operationally up in mesh mode
        echo "Waiting for $WLAN to be ready..."
        for i in {1..15}; do
            if ip link show "$WLAN" | grep -q "state UP" && \
               iw dev "$WLAN" info | grep -q "type mesh point"; then
                echo "$WLAN is up in mesh mode."
                break
            fi
            if [ $i -eq 15 ]; then
                echo "!! Timed out waiting for $WLAN to be ready. Skipping." >&2
                continue 2
            fi
            sleep 1
        done

        # Now add to bat0
        echo "Adding $WLAN to bat0..."
        batctl bat0 if add "$WLAN"
    done


    ip link set bat0 up
    echo "bat0 interface is up and configured."
}

stop() {
    echo "Stopping BATMAN-ADV..."
    for WLAN in $WLAN_INTERFACES; do
        if batctl bat0 if | grep -q "$WLAN"; then
            batctl bat0 if del "$WLAN"
        fi
    done
    ip link show bat0 &>/dev/null && ip link del bat0
}

case "$1" in
    start|stop)
        "$1"
        ;;
    *)
        echo "Usage: $0 {start|stop}"
        exit 1
        ;;
esac
