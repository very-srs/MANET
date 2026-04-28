#!/bin/bash
# ==============================================================================
# USB Ethernet Watch
# ==============================================================================
# Called by udev when a USB ethernet interface (tethering, LTE dongle) appears
# or disappears. Triggers ethernet-autodetect.sh with the correct interface.
# ==============================================================================

exec >> /var/log/usb-ethernet-watch.log 2>&1
set -x

ACTION="${1:-$ACTION}"
IFACE="${2:-$INTERFACE}"

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - USB-ETH: $1" | systemd-cat -t usb-ethernet-watch
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - USB-ETH: $1"
}

if [ -z "$IFACE" ]; then
    log "No interface specified"
    exit 1
fi

# Skip mesh/batman/bridge interfaces
case "$IFACE" in
    bat*|br*|wlan*|lo) exit 0 ;;
esac

# Confirm it's USB-backed
BUS=$(readlink /sys/class/net/$IFACE/device/subsystem 2>/dev/null | grep -o 'usb' || true)
if [ "$BUS" != "usb" ]; then
    log "$IFACE is not USB-backed, skipping"
    exit 0
fi

log "$ACTION on $IFACE"

case "$ACTION" in
    add|online)
        log "USB ethernet appeared: $IFACE — ensuring networkd DHCP config..."
        # Always create a networkd DHCP config so the interface gets an IP,
        # even if end0 is currently the active gateway. Without this, the
        # interface has no IP and cannot take over if end0 later disconnects.
        if [ ! -f "/etc/systemd/network/20-${IFACE}.network" ]; then
            cat > "/etc/systemd/network/20-${IFACE}.network" << EOF
[Match]
Name=${IFACE}

[Link]
RequiredForOnline=yes

[Network]
DHCP=ipv4
IPv6AcceptRA=yes
Bridge=

[DHCP]
ClientIdentifier=mac
UseDNS=yes
UseNTP=yes
UseRoutes=yes
Timeout=10

[DHCPv4]
UseRoutes=yes
UseGateway=yes
EOF
            networkctl reload 2>/dev/null || true
            networkctl reconfigure "$IFACE" 2>/dev/null || true
            sleep 3
        fi

        # If end0 is already the active gateway, don't take over — just ensure IP is assigned
        if [ -f /var/run/upstream_iface ] && [ "$(cat /var/run/upstream_iface)" = "end0" ] && \
           [ -f /var/run/mesh-gateway.state ] && \
           ip route show dev end0 2>/dev/null | grep -q '^default'; then
            log "end0 is already active gateway, $IFACE standby with IP only"
            exit 0
        fi

        log "USB ethernet $IFACE taking over as gateway..."
        /usr/local/bin/manet-uplink-dispatch.sh add "$IFACE"
        ;;
    remove|offline)
        log "USB ethernet removed: $IFACE — running cleanup"
        # Clear upstream_iface if it was this interface
        if [ -f /var/run/upstream_iface ] && [ "$(cat /var/run/upstream_iface)" = "$IFACE" ]; then
            rm -f /var/run/upstream_iface
        fi
        /usr/local/bin/manet-uplink-dispatch.sh remove "$IFACE"
        ;;
    *)
        log "Unknown action: $ACTION"
        exit 1
        ;;
esac

exit 0
