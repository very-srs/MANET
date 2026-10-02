#!/bin/bash
# mesh-config-apply.sh
# Applies a staged config package from /var/run/mesh_pending_config.json
# to /etc/mesh.conf and activates the appropriate services.
#
# Called by the node-manager when activate_at time is reached.
# Also callable directly for testing: mesh-config-apply.sh --force
#
# Safe settings (applied immediately, no mesh disruption):
#   admin_password, mtx, mumble, auto_update
#
# Node-local settings (written here, radio effect deferred):
#   acs, regulatory_domain
# EUD mode, AP SSID/key, and max_euds_per_node are per-node and are not
# applied from an Alfred package.
#
# Dangerous settings (require coordinated cutover, will briefly drop mesh):
#   mesh_ssid, mesh_key, ipv4_network
#
# Every key mesh-config-sync.py accepts must be handled by one of the three
# blocks below. acs and regulatory_domain were validated and staged but never
# written, so a change to either ACKed, reported applied, and did nothing.

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
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - CONFIG-APPLY: $1" | tee -a "$APPLY_LOG" | systemd-cat -t mesh-config-apply
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

# Helper: read a value from the staged config
# The file is written from an Alfred broadcast, so the value is remote input.
# It is read straight out of the JSON by key and passed as an argv element,
# never interpolated into the Python source, where a quote in a value would
# otherwise end up as code. mesh-config-sync.py has already whitelisted the
# keys and rejected quotes and newlines; this is the second layer.
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

# Apply deferred settings (acs, regulatory_domain)
# The regulatory domain is written by manet-region.py into the same radio
# files radio-setup.sh uses: /etc/modprobe.d/{cfg80211,morse}.conf, crda,
# hostapd and every supplicant's country (and the HaLow region's default
# channel when the US/EU plan changes). Those take hold when the modules and
# supplicants next start, so the change applies at the next boot. Radios are
# not restarted from here: that is the dangerous class, and the rollback
# snapshot does not cover a regdomain change.
#
# acs is the exception: node-manager-select.sh points node-manager.sh at the
# matching variant before every node-manager start, so it takes effect now
# for no more than a node-manager restart.
apply_deferred_settings() {
    local val current

    for key in regulatory_domain acs; do
        val=$(cfg_get "$key")
        [ -z "$val" ] && continue

        current=$(grep "^${key}=" "$MESH_CONF" 2>/dev/null | cut -d'=' -f2-)
        # Both keys reconcile on every package that names them, even when
        # mesh.conf already holds the value, so a newly staged activation
        # repairs an earlier partial failure. A healthy node is a no-op.
        if [ "$val" != "$current" ]; then
            log "  $key: '$current' -> '$val'"
            conf_set "$key" "$val"
        fi

        case "$key" in
            regulatory_domain)
                # Reconcile the radio files on every package that names a
                # region, even when mesh.conf already holds it, so a new
                # activation repairs an earlier partial failure. A failure
                # stops the apply: the version is not recorded as applied.
                local region_out region_rc
                region_out=$(python3 "$REGION_HELPER" apply 2>&1)
                region_rc=$?
                while IFS= read -r line; do log "  $line"; done <<< "$region_out"
                [ "$region_rc" -eq 0 ] ||
                    die "regulatory domain saved but the radio files were not updated"
                log "  regulatory domain takes effect at the next boot"
                ;;
            acs)
                # The selector is a quiet no-op when node-manager.sh already
                # points at the right variant. The running manager must also
                # be that variant: node-manager-select.sh --service-start
                # records it at every start. Otherwise restart it, below.
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

# An orchestrator change restarts node-manager, synchronously and verified,
# before the version is recorded. mesh-config-sync.py runs this script as its
# own transient unit, so restarting the manager (and the sync process inside
# it) does not cut this script short. A failure leaves the version
# unrecorded; a newly staged activation re-checks and retries.
if [ "${RESTART_NODE_MANAGER:-false}" = true ]; then
    log "  Restarting node-manager for the orchestrator change"
    systemctl restart node-manager.service ||
        die "node-manager did not restart after the orchestrator change"
    node_manager_matches_selection ||
        die "node-manager is not running the selected orchestrator after restart"
    log "  node-manager running $(cat "$RUN_DIR/node-manager.running")"
fi

# Record which version was applied
echo "$VERSION" > "$APPLIED_VERSION_FILE"

# Clear the pending config: it's been applied
rm -f "$PENDING_CONFIG"

# Clear the ACK version state file so node-manager stops broadcasting it
rm -f "$RUN_DIR/mesh_config_ack_version"

log "=== Config apply complete ==="
