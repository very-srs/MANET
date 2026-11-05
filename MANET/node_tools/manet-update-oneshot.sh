#!/bin/bash
# One-off migrations run once per update by node-update.py, after the payload is
# installed and the durable config is migrated, before services restart. The
# updater ships and runs this every release; it is the standing home for
# release-specific fix-ups that cannot be expressed as a plain file replacement.
#
# This is a CUMULATIVE ledger, not a per-release script. Each fix-up is a
# self-contained "check whether it is needed, then apply it" block, tagged with
# the release that introduced it, oldest first. New releases append a block; old
# blocks are pruned once no node an update must still support could be behind
# them (roughly a stable release or two back). How far back the retained blocks
# reach IS the contract for how out of date a node may be and still update
# reliably: a node older than the oldest block has no guaranteed path.
#
# Rules:
#   - Every block must be idempotent and safe on a node that already has the
#     state: it is guarded by its own "is this needed?" check, and the updater
#     reruns the whole script on a retry after a partially applied install.
#   - Act only through the rooted environment the updater exports, so recovery
#     and tests can target an alternate root:
#       MANET_ROOT          install root ("/" on a node)
#       MANET_HOSTAPD_CONF  the access point's hostapd.conf
#       MANET_WPA_DIR       the wpa_supplicant config directory
#   - Exit non-zero only on a real failure; the updater treats that as fatal and
#     leaves the update pending for retry. "Nothing to do" is success.
#   - Keep blocks ordered oldest-first and version-tagged so pruning is obvious.
set -euo pipefail

HOSTAPD_CONF="${MANET_HOSTAPD_CONF:-/etc/hostapd/hostapd.conf}"

# 0.585: enable Protected Management Frames (ieee80211w=1, PMF-optional) on an
# access point whose hostapd.conf was generated before PMF. radio-setup.sh now
# writes the line, so a later regeneration keeps it; this reaches an existing
# hostapd.conf that the update does not regenerate. Only WPA2 PSK configs, and
# only when the line is absent.
if [ -f "$HOSTAPD_CONF" ] \
   && grep -Eq '^[[:space:]]*wpa=2([[:space:]]|$)' "$HOSTAPD_CONF" \
   && ! grep -Eq '^[[:space:]]*ieee80211w=' "$HOSTAPD_CONF"; then
    tmp="$(mktemp "${HOSTAPD_CONF}.manet-XXXXXX")"
    trap 'rm -f "$tmp"' EXIT
    # Insert before wpa_passphrase to mirror the generated template; otherwise
    # append. hostapd treats these keys as order-independent.
    if grep -Eq '^[[:space:]]*wpa_passphrase=' "$HOSTAPD_CONF"; then
        awk '/^[[:space:]]*wpa_passphrase=/ && !seen { print "ieee80211w=1"; seen=1 } { print }' \
            "$HOSTAPD_CONF" > "$tmp"
    else
        # Append on its own line. awk END normalizes a missing final newline, so
        # a config ending without one (e.g. a bare wpa_psk=...) is not corrupted.
        awk '{ print } END { print "ieee80211w=1" }' "$HOSTAPD_CONF" > "$tmp"
    fi
    chmod --reference="$HOSTAPD_CONF" "$tmp" 2>/dev/null || chmod 0600 "$tmp"
    mv -f "$tmp" "$HOSTAPD_CONF"
    trap - EXIT
    sync "$HOSTAPD_CONF" 2>/dev/null || sync
    echo "manet-update-oneshot: enabled PMF (ieee80211w=1) in $HOSTAPD_CONF"
fi

# A saved Wi-Fi uplink restore snapshot needs no migration here: every path that
# writes hostapd.conf now emits the current PMF format (radio-setup.sh, and via
# manet_config_io.ensure_ap_pmf / a current-value carry: manet_wifi_uplink.py
# release()+write_ap_channel(), manet_eud_ap.py, manet-region.py). An access
# point restored after an uplink drops is rewritten with PMF by the next write.

exit 0
