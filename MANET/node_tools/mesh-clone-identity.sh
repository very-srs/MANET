#!/bin/bash
# Detects a provisioned SD-card clone booted on different hardware and resets
# local-only identity/state that must not be shared between nodes.

set -u

STATE_FILE="/var/lib/mesh_identity"

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - CLONE-ID: $1"
}

primary_mac() {
    for iface in end0 eth0 wlan0 wlan1 wlan2; do
        if [ -r "/sys/class/net/$iface/address" ]; then
            cat "/sys/class/net/$iface/address"
            return 0
        fi
    done

    ip -o link show | awk -F'link/ether ' '/link\/ether/ {print $2; exit}' | awk '{print $1}'
}

mac_suffix() {
    echo "$1" | awk -F: '{print $(NF-1) $NF}' | tr '[:upper:]' '[:lower:]'
}

CURRENT_MAC="$(primary_mac)"
if [ -z "$CURRENT_MAC" ]; then
    log "No MAC address found; leaving identity unchanged"
    exit 0
fi

if [ -f "$STATE_FILE" ]; then
    # shellcheck disable=SC1090
    . "$STATE_FILE" 2>/dev/null || true
fi

if [ "${PRIMARY_MAC:-}" = "$CURRENT_MAC" ]; then
    exit 0
fi

if [ -n "${PRIMARY_MAC:-}" ]; then
    log "Hardware MAC changed from $PRIMARY_MAC to $CURRENT_MAC; resetting cloned identity"

    rm -f /etc/machine-id /var/lib/dbus/machine-id
    systemd-machine-id-setup 2>/dev/null || true
    [ -f /etc/machine-id ] && ln -sf /etc/machine-id /var/lib/dbus/machine-id 2>/dev/null || true

    rm -f /etc/ssh/ssh_host_*
    ssh-keygen -A 2>/dev/null || true

    rm -f /etc/mesh_ipv4_state /var/run/my_ipv4_chunk /tmp/claimed_chunks.txt

    HOST_SUFFIX="$(mac_suffix "$CURRENT_MAC")"
    if [ -n "$HOST_SUFFIX" ]; then
        hostnamectl set-hostname "mesh-${HOST_SUFFIX}" 2>/dev/null || true
    fi
fi

mkdir -p "$(dirname "$STATE_FILE")"
cat > "$STATE_FILE" <<EOF
PRIMARY_MAC="$CURRENT_MAC"
EOF

systemctl enable ssh 2>/dev/null || true
systemctl restart ssh 2>/dev/null || true

exit 0
