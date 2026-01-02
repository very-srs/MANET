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

# Read AP interface if configured
AP_INTERFACE=""
if [ -f /var/lib/ap_interface ]; then
    AP_INTERFACE=$(cat /var/lib/ap_interface)
    echo "AP interface detected: $AP_INTERFACE (will be excluded from batman mesh)"
fi

start() {
    echo "Starting BATMAN-ADV setup..."
    #change to batman V algo
	batctl ra BATMAN_V

    # Create bat0 interface if it doesn't exist
    ip link show bat0 &>/dev/null || ip link add name bat0 type batadv


    # Set gateway mode based on state
    if [ -f /var/run/mesh-gateway.state ]; then
        batctl gw_mode server
        echo "Set to gateway server mode"
    else
        batctl gw_mode client
        echo "Set to gateway client mode"
    fi

    for WLAN in $WLAN_INTERFACES; do
        # Skip AP interface - it must not be added to batman mesh
        if [ -n "$AP_INTERFACE" ] && [ "$WLAN" == "$AP_INTERFACE" ]; then
            echo "--> Skipping $WLAN (configured as AP interface)"
            continue
        fi

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
        # Skip AP interface
        if [ -n "$AP_INTERFACE" ] && [ "$WLAN" == "$AP_INTERFACE" ]; then
            continue
        fi
        
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
