#!/bin/bash
#
#  This script enslaves various interfaces to the bat0 batman bridge
#

set -e

# Source mesh configuration to get the MESH_NAME variable
if [ -f /etc/default/mesh ]; then
    source /etc/default/mesh
else
    echo "Error: Mesh configuration /etc/default/mesh not found!" >&2
    exit 1
fi

# Find all the wireless network interfaces
WLAN_INTERFACES=$(networkctl | awk '/wlan/ {print $2}' | tr '\n' ' ')

# Read AP interface if configured, it will not be in the bat0 bridge
AP_INTERFACE=""
if [ -f /var/lib/ap_interface ]; then
    AP_INTERFACE=$(cat /var/lib/ap_interface)
    echo "AP interface detected: $AP_INTERFACE (will be excluded from batman mesh)"
fi

start() {
    echo "Starting BATMAN-ADV setup..."
    # Change to batman V routing algorithm
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

        # Add extra delay for wpa_supplicant to fully initialize
        sleep 2

        # Now add to bat0 with verification and retry
        echo "Adding $WLAN to bat0..."

        ADDED=false
        for attempt in {1..5}; do
            # Attempt to add
            if batctl bat0 if add "$WLAN" 2>&1; then
                # Give it a moment to settle
                sleep 0.5

                # Verify it was actually added
                if batctl bat0 if | grep -q "$WLAN"; then
                    echo "$WLAN successfully added to bat0"
                    ADDED=true
                    break
                else
                    echo "Attempt $attempt: $WLAN not showing in batctl, retrying..."
                fi
            else
                echo "Attempt $attempt: batctl add failed, retrying..."
            fi

            sleep 1
        done

        if [ "$ADDED" = false ]; then
            echo "!! ERROR: Failed to add $WLAN to bat0 after 5 attempts" >&2
            echo "!! This interface will not participate in the mesh" >&2
        fi
    done

    ip link set bat0 up
    echo "bat0 interface is up and configured."

    # Final verification
    echo ""
    echo "=== Final bat0 membership ==="
    batctl bat0 if
    echo "============================="
}

# Clear out bat0
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
