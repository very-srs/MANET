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

iface_driver() {
    local iface="$1"
    local driver

    driver="$(basename "$(readlink -f /sys/class/net/$iface/device/driver 2>/dev/null)")"
    if [[ -z "$driver" || "$driver" == "." ]]; then
        driver="$(ethtool -i "$iface" 2>/dev/null | awk -F': ' '$1 == "driver" {print $2; exit}')"
    fi

    echo "$driver"
}

iface_phy() {
    local iface="$1"
    iw dev "$iface" info 2>/dev/null | awk '/wiphy/ {print "phy"$2; exit}'
}

iface_supports_mesh() {
    local iface="$1"
    local phyname

    phyname="$(iface_phy "$iface")"
    [[ -n "$phyname" ]] && iw phy "$phyname" info 2>/dev/null | grep -q "mesh point"
}

is_halow() {
    [[ "$(iface_driver "$1")" == morse* ]]
}

is_nonmesh_wifi() {
    [[ "$(iface_driver "$1")" == brcmfmac ]]
}

refresh_interfaces() {
    WLAN_INTERFACES="$(iw dev 2>/dev/null | awk '$1 == "Interface" {print $2}' | tr '\n' ' ')"
    STANDARD_MESH_INTERFACES=""
    HALOW_INTERFACES=""
    NONMESH_INTERFACES=""

    for WLAN in $WLAN_INTERFACES; do
        if is_halow "$WLAN"; then
            HALOW_INTERFACES+="$WLAN "
        elif is_nonmesh_wifi "$WLAN"; then
            NONMESH_INTERFACES+="$WLAN "
        elif iface_supports_mesh "$WLAN"; then
            STANDARD_MESH_INTERFACES+="$WLAN "
        else
            NONMESH_INTERFACES+="$WLAN "
        fi
    done

    echo "Runtime wireless classification:"
    echo "  Standard mesh: $STANDARD_MESH_INTERFACES"
    echo "  HaLow: $HALOW_INTERFACES"
    echo "  Non-mesh/AP: $NONMESH_INTERFACES"
}

start() {
    echo "Starting BATMAN-ADV setup..."
    refresh_interfaces

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
    for WLAN in $STANDARD_MESH_INTERFACES; do
        echo "--> Configuring standard mesh interface: $WLAN"

        # wpa_supplicant owns standard mesh interfaces. Do not force the type
        # here or we can race the supplicant and knock the interface back to
        # managed mode. Just wait for the configured supplicant to join mesh.
        echo "Waiting for $WLAN to be ready (managed by wpa_supplicant)..."
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
    # wpa_supplicant_s1g owns these devices. Do NOT call 'ip link set type mesh'
    # on an S1G interface. Some Morse driver builds do not report operstate UP
    # even when batman-adv can enslave the interface, so wait for the netdev to
    # exist and let batctl's retry loop decide whether the interface is usable.
    # -------------------------------------------------------------------------
    for WLAN in $HALOW_INTERFACES; do
        echo "--> Configuring HaLow interface: $WLAN"

        echo "Waiting for $WLAN netdev (managed by wpa_supplicant_s1g)..."
        for i in {1..30}; do
            if ip link show "$WLAN" >/dev/null 2>&1; then
                ip link set "$WLAN" up 2>/dev/null || true
                echo "$WLAN exists."
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
    refresh_interfaces
    for WLAN in $STANDARD_MESH_INTERFACES $HALOW_INTERFACES; do
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
