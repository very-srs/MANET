#!/bin/bash
# Web UI / iperf firewall
# Kernel-enforced answer to "who can reach the pages on this node".
#
#   port 80    localhost, and addresses in this node's DHCP pool.
#   port 5201  the mesh subnet (peers run iperf3 clients against this node's
#              daemon). Not the uplink.
#
# Why source address and not interface: br0 bridges bat0, so a packet from a
# remote node arrives on br0 exactly like one from a locally attached EUD.
# Pool-based access is intentional for this trusted-team network.
#
# Lives in its own table at a priority ahead of the main filter chain, so it
# is independent of manet-uplink-dispatch.sh's gateway rules: a drop here is
# final, and everything else falls through untouched.
#
# Re-run whenever the DHCP range moves (mesh-ip-manager calls it after
# rewriting the dnsmasq config). Idempotent, and a no-op when nothing changed.

TABLE="manet_ui"
DNSMASQ_CONF="${MANET_DNSMASQ_CONF:-/etc/dnsmasq.d/mesh-eud.conf}"
MESH_CONF="${MANET_MESH_CONF:-/etc/mesh.conf}"
STATE_FILE="${MANET_UI_FW_STATE:-/var/run/manet-ui-firewall.state}"
NFT="${NFT:-nft}"
LOCK_FILE="${MANET_UI_FW_LOCK:-${STATE_FILE}.lock}"

log() {
    printf '%s\n' "UI-FIREWALL: $1" >&2
}

# The DHCP pool dnsmasq is currently handing out.
read_dhcp_range() {
    [ -f "$DNSMASQ_CONF" ] || return 1
    awk -F'[=,]' '/^dhcp-range=/ {print $2, $3; exit}' "$DNSMASQ_CONF"
}

read_mesh_network() {
    awk -F= '$1 == "ipv4_network" {print $2; exit}' "$MESH_CONF" 2>/dev/null
}

apply_rules() {
    local start="$1" end="$2" mesh_net="$3"

    # nft applies the whole input as one transaction. An invalid rule leaves
    # the previous table intact. Adding first also covers the initial boot.
    {
        printf '%s\n' "add table inet $TABLE"
        printf '%s\n' "delete table inet $TABLE"
        printf '%s\n' "add table inet $TABLE"
        printf '%s%s\n' \
            "add chain inet $TABLE input { type filter hook input priority " \
            "-10; policy accept; }"
        printf '%s\n' "add rule inet $TABLE input iifname lo accept"
        printf '%s%s\n' \
            "add rule inet $TABLE input tcp dport 80 ip saddr ${start}" \
            "-${end} accept"
        printf '%s\n' "add rule inet $TABLE input tcp dport 80 drop"
        if [ -n "$mesh_net" ]; then
            printf '%s%s\n' \
                "add rule inet $TABLE input tcp dport 5201 ip saddr " \
                "$mesh_net accept"
        fi
        printf '%s\n' "add rule inet $TABLE input tcp dport 5201 drop"
    } | "$NFT" -f -
}

# Serialize pool changes and their success marker.
exec 9>"$LOCK_FILE" || exit 1
flock -w 10 9 || { log "ERROR: firewall update already running"; exit 1; }

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
   "$NFT" list table inet "$TABLE" >/dev/null 2>&1; then
    exit 0
fi

if apply_rules "$DHCP_START" "$DHCP_END" "$MESH_NET"; then
    if ! printf '%s\n' "$DESIRED" > "${STATE_FILE}.tmp" ||
       ! mv -f -- "${STATE_FILE}.tmp" "$STATE_FILE"; then
        log "ERROR: rules installed but firewall state could not be saved"
        exit 1
    fi
    log "port 80 limited to ${DHCP_START}-${DHCP_END} + localhost; iperf3 to ${MESH_NET:-mesh only}"
else
    log "ERROR: failed to install nftables rules"
    exit 1
fi

exit 0
