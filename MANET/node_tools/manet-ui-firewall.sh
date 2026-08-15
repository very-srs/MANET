#!/usr/bin/env bash
# ==============================================================================
# Web UI / iperf firewall
# ==============================================================================
# Kernel-enforced answer to "who can reach the pages on this node".
#
#   port 80    localhost, and clients holding a DHCP lease from THIS node.
#              Not other radios, not their EUDs, not the uplink LAN.
#   port 5201  the mesh subnet (peers run iperf3 clients against this node's
#              daemon). Not the uplink.
#
# Why source address and not interface: br0 bridges bat0, so a packet from a
# remote node arrives on br0 exactly like one from a locally attached EUD.
# The only thing that separates them is which address it came from — this
# node's own DHCP pool versus the rest of ipv4_network.
#
# Lives in its own table at a priority ahead of the main filter chain, so it
# is independent of manet-uplink-dispatch.sh's gateway rules: a drop here is
# final, and everything else falls through untouched.
#
# Re-run whenever the DHCP range moves (mesh-ip-manager calls it after
# rewriting the dnsmasq config). Idempotent, and a no-op when nothing changed.
# ==============================================================================

TABLE="manet_ui"
DNSMASQ_CONF="${MANET_DNSMASQ_CONF:-/etc/dnsmasq.d/mesh-eud.conf}"
MESH_CONF="${MANET_MESH_CONF:-/etc/mesh.conf}"
STATE_FILE="${MANET_UI_FW_STATE:-/var/run/manet-ui-firewall.state}"
NFT="${NFT:-nft}"

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - UI-FIREWALL: $1"
}

# The DHCP pool dnsmasq is currently handing out, i.e. exactly the addresses
# an EUD of this node can hold.
read_dhcp_range() {
    [ -f "$DNSMASQ_CONF" ] || return 1
    awk -F'[=,]' '/^dhcp-range=/ {print $2, $3; exit}' "$DNSMASQ_CONF"
}

read_mesh_network() {
    awk -F= '$1 == "ipv4_network" {print $2; exit}' "$MESH_CONF" 2>/dev/null
}

apply_rules() {
    local start="$1" end="$2" mesh_net="$3"

    $NFT delete table inet "$TABLE" 2>/dev/null || true
    $NFT add table inet "$TABLE" || return 1
    # policy accept: this chain only ever removes access to these two ports,
    # everything else is somebody else's decision.
    $NFT add chain inet "$TABLE" input \
        '{ type filter hook input priority -10; policy accept; }' || return 1

    # Loopback first — an SSH port-forward to the node lands here, which is
    # how the pages get looked at from a dev machine.
    $NFT add rule inet "$TABLE" input iifname "lo" accept

    $NFT add rule inet "$TABLE" input tcp dport 80 ip saddr "${start}-${end}" accept
    $NFT add rule inet "$TABLE" input tcp dport 80 drop

    if [ -n "$mesh_net" ]; then
        $NFT add rule inet "$TABLE" input tcp dport 5201 ip saddr "$mesh_net" accept
    fi
    $NFT add rule inet "$TABLE" input tcp dport 5201 drop
}

RANGE=$(read_dhcp_range)
if [ -z "$RANGE" ]; then
    # No pool yet means no chunk claimed yet. Installing a rule now would lock
    # out the EUDs we cannot yet name, so leave things alone; mesh-ip-manager
    # calls back once it has an allocation.
    log "No dhcp-range in $DNSMASQ_CONF yet; leaving port 80 rules alone"
    exit 0
fi

read -r DHCP_START DHCP_END <<< "$RANGE"
MESH_NET=$(read_mesh_network)
DESIRED="${DHCP_START}-${DHCP_END}|${MESH_NET}"

if [ "$(cat "$STATE_FILE" 2>/dev/null)" = "$DESIRED" ] &&
   $NFT list table inet "$TABLE" >/dev/null 2>&1; then
    exit 0
fi

if apply_rules "$DHCP_START" "$DHCP_END" "$MESH_NET"; then
    echo "$DESIRED" > "$STATE_FILE"
    log "port 80 limited to ${DHCP_START}-${DHCP_END} + localhost; iperf3 to ${MESH_NET:-mesh only}"
else
    log "ERROR: failed to install nftables rules"
    exit 1
fi

exit 0
