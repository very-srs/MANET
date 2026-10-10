#!/bin/bash
# Attach mesh interfaces to bat0.
# Standard Wi-Fi needs mesh point mode confirmed by iw before attachment.
# wpa_supplicant_s1g owns HaLow mode. Changing its type interrupts the driver.
# Print the required start or stop argument to stderr.
usage() {
    printf '%s\n' 'usage: batman-if-setup.sh {start|stop}' >&2
}

[ $# -eq 1 ] || { usage; exit 1; }
case "$1" in start|stop) ;; *) usage; exit 1 ;; esac

. "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet-common.sh" || exit 1

set -e

# Source mesh configuration to get the MESH_NAME variable
if [ -f /etc/default/mesh ]; then
    source /etc/default/mesh
else
    echo "Error: Mesh configuration /etc/default/mesh not found!" >&2
    exit 1
fi


# Check whether the interface driver belongs to the Morse family.
is_halow() {
    [[ "$(iface_driver "$1")" == morse* ]]
}


# radio_iface_enabled: manet-common.sh

# Set STANDARD_MESH_INTERFACES, HALOW_INTERFACES and NONMESH_INTERFACES.
# Prefer saved roles, falling back to live driver and PHY capabilities.
refresh_interfaces() {
    STANDARD_MESH_INTERFACES=""
    HALOW_INTERFACES=""
    NONMESH_INTERFACES=""

    # Exclude the AP from mesh discovery to avoid a competing supplicant.
    # A mesh join can fail with -95 and deinitialize the AP while hostapd stays
    # active. Before radio-setup writes role files, /var/lib/ap_interface is
    # the only AP ownership record.
    local AP_IFACE
    AP_IFACE="$(cat /var/lib/ap_interface 2>/dev/null || true)"

    # Prefer radio-setup role files. A disabled Morse USB device can still
    # create a netdev that times out on link-up.
    if [ -f /var/lib/mesh_if ]; then
        # Wait for role interfaces in sysfs because drivers can be slow at boot.
        local all_ifaces
        all_ifaces="$(cat /var/lib/mesh_if /var/lib/halow_if /var/lib/no_mesh_if 2>/dev/null | tr '\n' ' ')"
        for WLAN in $all_ifaces; do
            for i in $(seq 1 10); do
                [ -d "/sys/class/net/$WLAN" ] && break
                sleep 1
                [ "$i" -eq 10 ] &&
                    printf '%s%s\n' \
                        "Warning: $WLAN did not appear in sysfs after 10s, " \
                        "continuing" >&2
            done
        done

        for WLAN in $(cat /var/lib/mesh_if 2>/dev/null); do
            [ -d "/sys/class/net/$WLAN" ] && STANDARD_MESH_INTERFACES+="$WLAN "
        done
        for WLAN in $(cat /var/lib/halow_if 2>/dev/null); do
            [ -d "/sys/class/net/$WLAN" ] && HALOW_INTERFACES+="$WLAN "
        done
        for WLAN in $(cat /var/lib/no_mesh_if 2>/dev/null); do
            [ -d "/sys/class/net/$WLAN" ] && NONMESH_INTERFACES+="$WLAN "
        done

        echo "Role-file wireless classification:"
        printf '%s\n' "  Standard mesh: $STANDARD_MESH_INTERFACES"
        printf '%s\n' "  HaLow: $HALOW_INTERFACES"
        printf '%s\n' "  Non-mesh/AP: $NONMESH_INTERFACES"
        return
    fi

    # iw groups interfaces by PHY. Keep only Interface records.
    WLAN_INTERFACES="$(iw dev 2>/dev/null | awk '$1 == "Interface" {print $2}' | tr '\n' ' ')"

    for WLAN in $WLAN_INTERFACES; do
        if [ -n "$AP_IFACE" ] && [ "$WLAN" = "$AP_IFACE" ]; then
            NONMESH_INTERFACES+="$WLAN "
        elif is_halow "$WLAN"; then
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
    printf '%s\n' "  Standard mesh: $STANDARD_MESH_INTERFACES"
    printf '%s\n' "  HaLow: $HALOW_INTERFACES"
    printf '%s\n' "  Non-mesh/AP: $NONMESH_INTERFACES"
}

# Start enabled mesh radios, attach HaLow first and bring bat0 up.
# Read operator radio state and refresh the interface-list globals.
start() {
    echo "Starting BATMAN-ADV setup..."
    refresh_interfaces

    # Start mesh supplicants even when unit dependencies do not start them.
    for WLAN in $STANDARD_MESH_INTERFACES; do
        if ! radio_iface_enabled "$WLAN"; then
            printf '%s\n' \
                "Skipping wpa_supplicant for $WLAN (radio-state says down)"
            systemctl stop "wpa_supplicant@${WLAN}.service" 2>/dev/null || true
            ip link set "$WLAN" down 2>/dev/null || true
            continue
        fi
        if ! systemctl is-active "wpa_supplicant@${WLAN}.service" >/dev/null 2>&1; then
            printf '%s\n' "Starting wpa_supplicant for $WLAN..."
            systemctl start "wpa_supplicant@${WLAN}.service" 2>/dev/null || \
                printf '%s%s\n' \
                    "Warning: could not start wpa_supplicant for $WLAN " \
                    "(config may be missing)" >&2
        fi
    done
    for WLAN in $HALOW_INTERFACES; do
        local svc="wpa_supplicant-s1g-${WLAN}.service"
        if ! radio_iface_enabled "$WLAN"; then
            printf '%s\n' "Skipping $svc for $WLAN (radio-state says down)"
            systemctl stop "$svc" 2>/dev/null || true
            ip link set "$WLAN" down 2>/dev/null || true
            continue
        fi
        if ! systemctl is-active "$svc" >/dev/null 2>&1; then
            printf '%s\n' "Starting wpa_supplicant_s1g for $WLAN..."
            systemctl start "$svc" 2>/dev/null || \
                printf '%s\n' "Warning: could not start $svc" >&2
        fi
    done

    # Pin discovered interface names with missing udev .link files.
    # Probe order can change across boots and invalidate role assignments.
    local wrote_links=false
    for WLAN in $STANDARD_MESH_INTERFACES $HALOW_INTERFACES $NONMESH_INTERFACES; do
        local link_file="/etc/systemd/network/10-${WLAN}.link"
        if [ ! -f "$link_file" ]; then
            local mac
            mac=$(cat "/sys/class/net/${WLAN}/address" 2>/dev/null || true)
            if [ -n "$mac" ]; then
                # Preserve any rename staged by radio-setup for the next boot.
                # A lexically earlier .link for the current name would take
                # precedence and prevent the intended rename.
                if grep -qi "MACAddress=$mac" /etc/systemd/network/10-wlan*.link 2>/dev/null; then
                    printf '%s%s\n' \
                        "Skipping $link_file: MAC $mac already pinned by " \
                        "radio-setup"
                    continue
                fi
                printf '[Match]\nMACAddress=%s\n\n[Link]\nName=%s\n' "$mac" "$WLAN" > "$link_file"
                printf '%s\n' "Wrote $link_file (MAC=$mac)"
                wrote_links=true
            fi
        fi
    done
    [ "$wrote_links" = true ] && networkctl reload 2>/dev/null || true

    # Change to batman V routing algorithm
    # Select the algorithm before creating a new batadv interface.
    batctl ra BATMAN_V

    # Create bat0 interface if it doesn't exist
    ip link show bat0 &>/dev/null || ip link add name bat0 type batadv

    # Set gateway mode based on state
    if [ -f /var/run/mesh-gateway.state ]; then
        # Measured bandwidth for an Ethernet uplink, otherwise the default.
        /usr/local/bin/manet-uplink-speed.sh announce "$(cat /var/run/upstream_iface 2>/dev/null)" ||
            batctl gw_mode server
        echo "Set to gateway server mode"
    else
        batctl gw_mode client
        echo "Set to gateway client mode"
    fi

    # HaLow joins first to become the batman-adv primary.
    # Its longer range keeps OGMs active when standard mesh links go down.
    # wpa_supplicant_s1g owns these devices. Do not change their interface type.
    # Some Morse builds never report operstate UP even when attachment works.
    # Wait for the netdev, then let batctl retry attachment.
    for WLAN in $HALOW_INTERFACES; do
        if ! radio_iface_enabled "$WLAN"; then
            printf '%s%s\n' \
                "--> Skipping HaLow interface: $WLAN (radio-state says " \
                "down)"
            systemctl stop "wpa_supplicant-s1g-${WLAN}.service" 2>/dev/null || true
            ip link set "$WLAN" down 2>/dev/null || true
            continue
        fi
        printf '%s\n' "--> Configuring HaLow interface: $WLAN (primary)"

        printf '%s\n' \
            "Waiting for $WLAN netdev (managed by wpa_supplicant_s1g)..."
        for i in {1..10}; do
            if ip link show "$WLAN" >/dev/null 2>&1; then
                ip link set "$WLAN" up 2>/dev/null || true
                printf '%s\n' "$WLAN exists."
                break
            fi
            sleep 1
            if [ $i -eq 10 ]; then
                printf '%s\n' "!! Timed out waiting for $WLAN after 10s. Skipping." >&2
                continue 2
            fi
        done

        # Give mesh hard interfaces room above a 1500-byte payload.
        ip link set "$WLAN" mtu 1532 2>/dev/null || true

        printf '%s\n' "Adding HaLow $WLAN to bat0..."

        # Try five attachments, checking membership 0.5 s after each add.
        # An unsuccessful check or add waits 1 s before the next attempt.
        ADDED=false
        for attempt in {1..5}; do
            if batctl bat0 if add "$WLAN" 2>&1; then
                sleep 0.5
                if batctl bat0 if | grep -q "$WLAN"; then
                    printf '%s%s\n' \
                        "$WLAN successfully added to bat0 (primary " \
                        "interface)"
                    ADDED=true
                    break
                else
                    printf '%s%s\n' \
                        "Attempt $attempt: $WLAN not showing in batctl, " \
                        "retrying..."
                fi
            else
                printf '%s\n' "Attempt $attempt: batctl add failed, retrying..."
            fi
            sleep 1
        done

        if [ "$ADDED" = false ]; then
            printf '%s%s\n' \
                "!! ERROR: Failed to add HaLow $WLAN to bat0 after 5 " \
                "attempts" >&2
            echo "!! This interface will not participate in the mesh" >&2
        fi
    done

    # Standard 802.11 mesh interfaces
    for WLAN in $STANDARD_MESH_INTERFACES; do
        if ! radio_iface_enabled "$WLAN"; then
            printf '%s%s\n' \
                "--> Skipping standard mesh interface: $WLAN (radio-state " \
                "says down)"
            systemctl stop "wpa_supplicant@${WLAN}.service" 2>/dev/null || true
            ip link set "$WLAN" down 2>/dev/null || true
            continue
        fi
        printf '%s\n' "--> Configuring standard mesh interface: $WLAN"

        printf '%s%s\n' \
            "Waiting for $WLAN to be in mesh point mode (managed by " \
            "wpa_supplicant)..."
        for i in {1..10}; do
            if iw dev "$WLAN" info 2>/dev/null | grep -q "type mesh point"; then
                printf '%s\n' "$WLAN is in mesh point mode."
                break
            fi
            sleep 1
            if [ $i -eq 10 ]; then
                printf '%s%s\n' \
                    "!! Timed out waiting for $WLAN to enter mesh point " \
                    "mode after 10s. Skipping." >&2
                continue 2
            fi
        done

        # Leave a 2 s settling interval after mesh point mode appears.
        sleep 2

        # Give mesh hard interfaces room above a 1500-byte payload.
        ip link set "$WLAN" mtu 1532 2>/dev/null || true

        printf '%s\n' "Adding $WLAN to bat0..."

        # Try five attachments, checking membership 0.5 s after each add.
        # An unsuccessful check or add waits 1 s before the next attempt.
        ADDED=false
        for attempt in {1..5}; do
            if batctl bat0 if add "$WLAN" 2>&1; then
                sleep 0.5
                if batctl bat0 if | grep -q "$WLAN"; then
                    printf '%s\n' "$WLAN successfully added to bat0"
                    ADDED=true
                    break
                else
                    printf '%s%s\n' \
                        "Attempt $attempt: $WLAN not showing in batctl, " \
                        "retrying..."
                fi
            else
                printf '%s\n' "Attempt $attempt: batctl add failed, retrying..."
            fi
            sleep 1
        done

        if [ "$ADDED" = false ]; then
            printf '%s\n' \
                "!! ERROR: Failed to add $WLAN to bat0 after 5 attempts" >&2
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
        # Role changes and ACS must not race a cached interface list.
        exec 8>"${MANET_ACS_LOCK_FILE:-/run/channel-election.lock}"
        # Fail the requested transition if the lock stays busy for 10 s.
        flock -w 10 8 || exit 1
        "$1"
        ;;
    *)
        usage
        exit 1
        ;;
esac
