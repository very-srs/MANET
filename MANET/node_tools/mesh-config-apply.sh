#!/bin/bash
# Apply /var/run/mesh_pending_config.json to mesh.conf and services when
# activate_at arrives, or immediately with --force.
# Immediate settings: admin_password, mtx, mumble, auto_update.
# Deferred radio settings: acs, regulatory_domain.
# Coordinated mesh cutover: mesh_ssid, mesh_key, ipv4_network.
# EUD mode, AP SSID/key and max_euds_per_node stay local.
# Handle every mesh-config-sync.py accepted key in the three blocks below.

usage() {
    printf '%s\n' \
        'usage: mesh-config-apply.sh [--force]' >&2
}

if [ $# -gt 1 ] || { [ $# -eq 1 ] && [ "$1" != --force ]; }; then
    usage
    exit 1
fi

RUN_DIR="${MANET_RUN_DIR:-/var/run}"
WPA_DIR="${MANET_WPA_DIR:-/etc/wpa_supplicant}"
PENDING_CONFIG="$RUN_DIR/mesh_pending_config.json"
MESH_CONF="${MANET_MESH_CONF:-/etc/mesh.conf}"
APPLY_LOG="${MANET_APPLY_LOG:-/var/log/mesh-config-apply.log}"
APPLIED_VERSION_FILE="$RUN_DIR/mesh_applied_config_version"
CONFIG_WRITER="${MANET_CONFIG_WRITER:-/usr/local/bin/mesh-config-write.py}"
SUPPLICANT_HELPER="${MANET_SUPPLICANT_HELPER:-$(dirname "$0")/manet_supplicant.py}"
REGION_HELPER="${MANET_REGION_HELPER:-$(dirname "$0")/manet-region.py}"
NODE_MANAGER_SELECT="${NODE_MANAGER_SELECT:-$(dirname "$0")/node-manager-select.sh}"
NODE_MANAGER_LINK="${MANET_BIN_DIR:-$(dirname "$0")}/node-manager.sh"

log() {
    printf '%s\n' "[$(date +'%Y-%m-%d %H:%M:%S')] - CONFIG-APPLY: $1" \
        >> "$APPLY_LOG"
    printf '%s\n' "CONFIG-APPLY: $1" | systemd-cat -t mesh-config-apply
}

die() {
    log "ERROR: $1"
    exit 1
}

# Read pending config
[ -f "$PENDING_CONFIG" ] || die "No pending config at $PENDING_CONFIG"

VERSION=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('version',''))" "$PENDING_CONFIG" 2>/dev/null)
[ -n "$VERSION" ] || die "Cannot read version from pending config"

log "Applying config version $VERSION"

# Read staged Alfred input by JSON key and pass it as an argv element.
# Never interpolate remote values into Python source. This protects the
# read even after mesh-config-sync.py validates keys, quotes and newlines.
cfg_get() {
    python3 -c "
import json, sys
d = json.load(open(sys.argv[1])).get('config', {})
val = d.get(sys.argv[2], '')
print(val if val is not None else '')
" "$PENDING_CONFIG" "$1" 2>/dev/null
}

# Helper: update a key=value in /etc/mesh.conf (or add if missing)
conf_set() {
    python3 "$CONFIG_WRITER" "$MESH_CONF" "$1" "$2" || die "Cannot update $1"
}

# Apply safe settings (no mesh disruption)
apply_safe_settings() {
    local changed=false

    for key in admin_password mtx mumble auto_update; do
        local val
        val=$(cfg_get "$key")
        [ -z "$val" ] && continue

        local current
        current=$(grep "^${key}=" "$MESH_CONF" 2>/dev/null | cut -d'=' -f2-)
        if [ "$val" != "$current" ]; then
            # Passwords must never be copied into the journal/apply log.
            if [ "$key" = admin_password ]; then
                log "  admin_password: changed"
            else
                log "  $key: '$current' → '$val'"
            fi
            conf_set "$key" "$val"
            changed=true
        fi
    done

    if [ "$changed" = "true" ]; then
        # Handle service toggles
        local mtx_val mumble_val
        mtx_val=$(cfg_get "mtx")
        mumble_val=$(cfg_get "mumble")

        if [ "$mtx_val" = "n" ]; then
            systemctl stop mediamtx 2>/dev/null || true
            log "  MediaMTX stopped"
        fi
        if [ "$mumble_val" = "n" ]; then
            systemctl stop mumble-server 2>/dev/null || true
            log "  Mumble stopped"
        fi
    fi
}

# Whether node-manager is running the variant node-manager.sh now selects.
node_manager_matches_selection() {
    systemctl is-active --quiet node-manager.service &&
        [ "$(cat "$RUN_DIR/node-manager.running" 2>/dev/null)" = \
          "$(readlink "$NODE_MANAGER_LINK" 2>/dev/null)" ]
}

# Apply acs and regulatory_domain. manet-region.py updates cfg80211/morse
# modprobe files, crda, hostapd, supplicant countries and HaLow US/EU defaults.
# Radio changes take effect at next boot. No radios restart here because the
# rollback snapshot excludes regulatory changes.
# acs takes effect immediately by selecting the matching node-manager.sh
# variant and restarting node-manager if needed.
apply_deferred_settings() {
    local val current

    for key in regulatory_domain acs; do
        val=$(cfg_get "$key")
        [ -z "$val" ] && continue

        current=$(grep "^${key}=" "$MESH_CONF" 2>/dev/null | cut -d'=' -f2-)
        # Reconcile named keys even when mesh.conf already matches, so a new
        # activation repairs partial failures. Healthy state is a no-op.
        if [ "$val" != "$current" ]; then
            log "  $key: '$current' -> '$val'"
            conf_set "$key" "$val"
        fi

        case "$key" in
            regulatory_domain)
                # Reconcile radio files even if mesh.conf matches the region.
                # Failure stops apply without recording the version as applied.
                local region_out region_rc
                region_out=$(python3 "$REGION_HELPER" apply 2>&1)
                region_rc=$?
                while IFS= read -r line; do log "  $line"; done <<< "$region_out"
                [ "$region_rc" -eq 0 ] ||
                    die "regulatory domain saved but the radio files were not updated"
                log "  regulatory domain takes effect at the next boot"
                ;;
            acs)
                # Selection is a no-op when the symlink matches. Also check
                # the variant recorded by --service-start. Restart below
                # when the running variant differs.
                local select_out
                select_out=$("$NODE_MANAGER_SELECT" 2>&1) ||
                    die "cannot select the node manager: $select_out"
                [ -n "$select_out" ] && log "  $select_out"
                if ! node_manager_matches_selection; then
                    RESTART_NODE_MANAGER=true
                fi
                ;;
        esac
    done
}

# Apply dangerous settings (mesh SSID, key, IP range)
# These require wpa_supplicant restart: the mesh will briefly disconnect
apply_dangerous_settings() {
    local any_dangerous=false

    local new_ssid new_key new_cidr
    new_ssid=$(cfg_get "mesh_ssid")
    new_key=$(cfg_get "mesh_key")
    new_cidr=$(cfg_get "ipv4_network")

    local cur_ssid cur_key cur_cidr
    cur_ssid=$(grep "^mesh_ssid=" "$MESH_CONF" 2>/dev/null | cut -d'=' -f2-)
    cur_key=$(grep "^mesh_key=" "$MESH_CONF" 2>/dev/null | cut -d'=' -f2-)
    cur_cidr=$(grep "^ipv4_network=" "$MESH_CONF" 2>/dev/null | cut -d'=' -f2-)

    [ -n "$new_ssid" ] && [ "$new_ssid" != "$cur_ssid" ] && any_dangerous=true
    [ -n "$new_key"  ] && [ "$new_key"  != "$cur_key"  ] && any_dangerous=true
    [ -n "$new_cidr" ] && [ "$new_cidr" != "$cur_cidr" ] && any_dangerous=true

    [ "$any_dangerous" = "false" ] && return

    log "Applying dangerous settings (mesh will briefly disconnect)"

    [ -n "$new_ssid" ] && [ "$new_ssid" != "$cur_ssid" ] && {
        log "  mesh_ssid: '$cur_ssid' → '$new_ssid'"
        conf_set "mesh_ssid" "$new_ssid"
        # Update wpa_supplicant configs
        for conf in "$WPA_DIR"/wpa_supplicant-wlan*.conf; do
            [[ "$conf" == *-uplink.conf ]] && continue
            [ -f "$conf" ] || continue
            python3 "$CONFIG_WRITER" "$conf" ssid "$new_ssid" --quoted || die "Cannot update supplicant SSID"
        done
    }

    [ -n "$new_key" ] && [ "$new_key" != "$cur_key" ] && {
        log "  mesh_key: changed"
        conf_set "mesh_key" "$new_key"
        for conf in "$WPA_DIR"/wpa_supplicant-wlan*.conf; do
            [[ "$conf" == *-uplink.conf ]] && continue
            [ -f "$conf" ] || continue
            python3 "$CONFIG_WRITER" "$conf" sae_password "$new_key" --quoted || die "Cannot update supplicant password"
        done
    }

    [ -n "$new_cidr" ] && [ "$new_cidr" != "$cur_cidr" ] && {
        log "  ipv4_network: '$cur_cidr' → '$new_cidr'"
        conf_set "ipv4_network" "$new_cidr"
        # IP manager will recalculate chunk on next cycle
        rm -f "$RUN_DIR/my_ipv4_chunk" "$RUN_DIR/my_ipv4_chunk_size" "$RUN_DIR/mesh_ipv4_state" 2>/dev/null
    }

    # Restart wpa_supplicant on configured mesh interfaces
    log "  Restarting wpa_supplicant on mesh interfaces..."
    python3 "$SUPPLICANT_HELPER" restart || die "Mesh supplicant restart failed"

    log "  Dangerous settings applied. Mesh reconnecting..."
}

# Main
log "=== Config apply starting (version: $VERSION) ==="

apply_safe_settings
apply_deferred_settings
apply_dangerous_settings

# Verify the node-manager restart before recording a changed orchestrator
# version. mesh-config-sync.py invokes this in a separate transient unit so
# restarting the manager and its sync process cannot interrupt apply.
# Failure leaves the version unrecorded for a newly staged retry.
if [ "${RESTART_NODE_MANAGER:-false}" = true ]; then
    log "  Restarting node-manager for the orchestrator change"
    systemctl restart node-manager.service ||
        die "node-manager did not restart after the orchestrator change"
    node_manager_matches_selection ||
        die "node-manager is not running the selected orchestrator after restart"
    log "  node-manager running $(cat "$RUN_DIR/node-manager.running")"
fi

# Record which version was applied
printf '%s\n' "$VERSION" > "$APPLIED_VERSION_FILE"

# Clear the pending config: it's been applied
rm -f "$PENDING_CONFIG"

# Clear the ACK version state file so node-manager stops broadcasting it
rm -f "$RUN_DIR/mesh_config_ack_version"

log "=== Config apply complete ==="
