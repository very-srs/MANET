#!/bin/bash
#
#  This script enslaves various interfaces to the bat0 batman bridge
#
#  Standard 802.11 mesh interfaces (wpa_supplicant):
#    - Set to mesh point mode via ip link, wait for iw confirmation, then add to bat0
#
#  HaLow / 802.11ah interfaces (wpa_supplicant_s1g):
#    - Already in S1G mesh mode managed by wpa_supplicant_s1g
#    - DO NOT set type mesh (will fail / interrupt the S1G driver)
#    - Just wait for interface UP, then add to bat0
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

# Read HaLow interfaces - these require a different enslave path
HALOW_INTERFACES=""
if [ -f /var/lib/halow_if ]; then
    HALOW_INTERFACES=$(cat /var/lib/halow_if | tr '\n' ' ')
fi

# Read AP interface if configured, it will not be in the bat0 bridge
AP_INTERFACE=""
if [ -f /var/lib/ap_interface ]; then
    AP_INTERFACE=$(cat /var/lib/ap_interface)
    echo "AP interface detected: $AP_INTERFACE (will be excluded from batman mesh)"
fi

# Helper: returns 0 (true) if the given interface is a HaLow interface
is_halow() {
    echo "$HALOW_INTERFACES" | grep -qw "$1"
}

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

    # -------------------------------------------------------------------------
    # Standard 802.11 mesh interfaces
    # -------------------------------------------------------------------------
    for WLAN in $WLAN_INTERFACES; do
        # Skip AP interface - it must not be added to batman mesh
        if [ -n "$AP_INTERFACE" ] && [ "$WLAN" == "$AP_INTERFACE" ]; then
            echo "--> Skipping $WLAN (configured as AP interface)"
            continue
        fi

        # HaLow interfaces are handled in the dedicated section below;
        # attempting 'ip link set type mesh' on an S1G interface will fail and
        # (with set -e) kill this entire script.
        if is_halow "$WLAN"; then
            echo "--> Skipping $WLAN in standard mesh loop (HaLow - handled below)"
            continue
        fi

        echo "--> Configuring standard mesh interface: $WLAN"

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
            if batctl bat0 if add "$WLAN" 2>&1; then
                sleep 0.5
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

    # -------------------------------------------------------------------------
    # HaLow / 802.11ah interfaces
    #
    # wpa_supplicant_s1g already owns these in S1G mesh mode (mode=5 in the
    # conf). Do NOT call 'ip link set type mesh' - that will fail and/or break
    # the S1G driver session. Just wait for the interface to come UP (which
    # happens once wpa_supplicant_s1g has finished initialising), then add to
    # bat0 exactly like any other mesh interface.
    # -------------------------------------------------------------------------
    for WLAN in $HALOW_INTERFACES; do
        echo "--> Configuring HaLow interface: $WLAN"

        echo "Waiting for $WLAN to be ready (managed by wpa_supplicant_s1g)..."
        for i in {1..30}; do
            if ip link show "$WLAN" 2>/dev/null | grep -q "state UP"; then
                echo "$WLAN is up."
                break
            fi
            if [ $i -eq 30 ]; then
                echo "!! Timed out waiting for $WLAN. Skipping." >&2
                continue 2
            fi
            sleep 1
        done

        # Add to bat0 with verification and retry (same as standard path)
        echo "Adding HaLow $WLAN to bat0..."

        ADDED=false
        for attempt in {1..5}; do
            if batctl bat0 if add "$WLAN" 2>&1; then
                sleep 0.5
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
            echo "!! ERROR: Failed to add HaLow $WLAN to bat0 after 5 attempts" >&2
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
