#!/bin/bash
# ==============================================================================
# mesh-config-apply.sh
# ==============================================================================
# Applies a staged config package from /var/run/mesh_pending_config.json
# to /etc/mesh.conf and activates the appropriate services.
#
# Called by the node-manager when activate_at time is reached.
# Also callable directly for testing: mesh-config-apply.sh --force
#
# Safe settings (applied immediately, no mesh disruption):
#   admin_password, eud, lan_ap_ssid, lan_ap_key, max_euds_per_node,
#   mtx, mumble, auto_update
#
# Dangerous settings (require coordinated cutover, will briefly drop mesh):
#   mesh_ssid, mesh_key, ipv4_network
# ==============================================================================

PENDING_CONFIG="/var/run/mesh_pending_config.json"
MESH_CONF="/etc/mesh.conf"
APPLY_LOG="/var/log/mesh-config-apply.log"
APPLIED_VERSION_FILE="/var/run/mesh_applied_config_version"

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - CONFIG-APPLY: $1" | tee -a "$APPLY_LOG" | systemd-cat -t mesh-config-apply
}

die() {
    log "ERROR: $1"
    exit 1
}

# ==============================================================================
# Read pending config
# ==============================================================================
[ -f "$PENDING_CONFIG" ] || die "No pending config at $PENDING_CONFIG"

VERSION=$(python3 -c "import json,sys; d=json.load(open('$PENDING_CONFIG')); print(d.get('version',''))" 2>/dev/null)
[ -n "$VERSION" ] || die "Cannot read version from pending config"

CONFIG_JSON=$(python3 -c "import json,sys; d=json.load(open('$PENDING_CONFIG')); print(json.dumps(d.get('config',{})))" 2>/dev/null)
[ -n "$CONFIG_JSON" ] || die "Cannot read config block from pending config"

log "Applying config version $VERSION"

# ==============================================================================
# Helper: read a value from the JSON config
# ==============================================================================
cfg_get() {
    python3 -c "
import json, sys
d = json.loads('''$CONFIG_JSON''')
val = d.get('$1', '')
print(val if val is not None else '')
" 2>/dev/null
}

# ==============================================================================
# Helper: update a key=value in /etc/mesh.conf (or add if missing)
# ==============================================================================
conf_set() {
    local key="$1"
    local val="$2"
    if grep -q "^${key}=" "$MESH_CONF" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${val}|" "$MESH_CONF"
    else
        echo "${key}=${val}" >> "$MESH_CONF"
    fi
}

# ==============================================================================
# Apply safe settings (no mesh disruption)
# ==============================================================================
apply_safe_settings() {
    local changed=false

    for key in admin_password eud lan_ap_ssid lan_ap_key max_euds_per_node mtx mumble auto_update; do
        local val
        val=$(cfg_get "$key")
        [ -z "$val" ] && continue

        local current
        current=$(grep "^${key}=" "$MESH_CONF" 2>/dev/null | cut -d'=' -f2-)
        if [ "$val" != "$current" ]; then
            log "  $key: '$current' → '$val'"
            conf_set "$key" "$val"
            changed=true
        fi
    done

    if [ "$changed" = "true" ]; then
        # Restart AP if SSID/key changed
        local ap_changed=false
        for k in lan_ap_ssid lan_ap_key; do
            val=$(cfg_get "$k")
            [ -n "$val" ] && ap_changed=true
        done

        if [ "$ap_changed" = "true" ]; then
            log "  AP credentials changed — restarting hostapd"
            AP_IFACE=$(cat /var/lib/ap_interface 2>/dev/null || echo "")
            if [ -n "$AP_IFACE" ]; then
                NEW_SSID=$(cfg_get "lan_ap_ssid")
                NEW_KEY=$(cfg_get "lan_ap_key")
                if [ -f "/etc/hostapd/hostapd.conf" ]; then
                    sed -i "s|^ssid=.*|ssid=${NEW_SSID}|" /etc/hostapd/hostapd.conf
                    sed -i "s|^wpa_passphrase=.*|wpa_passphrase=${NEW_KEY}|" /etc/hostapd/hostapd.conf
                    systemctl restart hostapd 2>/dev/null || true
                fi
            fi
        fi

        # Reload EUD mode if changed
        local eud_val
        eud_val=$(cfg_get "eud")
        if [ -n "$eud_val" ]; then
            log "  EUD mode changed — will take effect on next node-manager cycle"
        fi

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

# ==============================================================================
# Apply dangerous settings (mesh SSID, key, IP range)
# These require wpa_supplicant restart — the mesh will briefly disconnect
# ==============================================================================
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
        for conf in /etc/wpa_supplicant/wpa_supplicant-wlan*.conf; do
            [ -f "$conf" ] && sed -i "s|ssid=\".*\"|ssid=\"${new_ssid}\"|" "$conf"
        done
    }

    [ -n "$new_key" ] && [ "$new_key" != "$cur_key" ] && {
        log "  mesh_key: changed"
        conf_set "mesh_key" "$new_key"
        for conf in /etc/wpa_supplicant/wpa_supplicant-wlan*.conf; do
            [ -f "$conf" ] && sed -i "s|sae_password=.*|sae_password=${new_key}|" "$conf"
        done
    }

    [ -n "$new_cidr" ] && [ "$new_cidr" != "$cur_cidr" ] && {
        log "  ipv4_network: '$cur_cidr' → '$new_cidr'"
        conf_set "ipv4_network" "$new_cidr"
        # IP manager will recalculate chunk on next cycle
        rm -f /var/run/my_ipv4_chunk /var/run/mesh_ipv4_state 2>/dev/null
    }

    # Restart wpa_supplicant on configured mesh interfaces
    log "  Restarting wpa_supplicant on mesh interfaces..."
    for iface in $(cat /var/lib/mesh_if /var/lib/halow_if 2>/dev/null); do
        [ -z "$iface" ] && continue
        systemctl restart "wpa_supplicant@${iface}.service" 2>/dev/null || \
        systemctl restart "wpa_supplicant-s1g-${iface}.service" 2>/dev/null || true
    done

    log "  Dangerous settings applied. Mesh reconnecting..."
}

# ==============================================================================
# Main
# ==============================================================================
log "=== Config apply starting (version: $VERSION) ==="

apply_safe_settings
apply_dangerous_settings

# Record which version was applied
echo "$VERSION" > "$APPLIED_VERSION_FILE"

# Clear the pending config — it's been applied
rm -f "$PENDING_CONFIG"

# Clear the ACK version state file so node-manager stops broadcasting it
rm -f /var/run/mesh_config_ack_version

log "=== Config apply complete ==="
