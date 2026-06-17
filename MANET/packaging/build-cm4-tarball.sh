#!/usr/bin/env bash
set -euo pipefail

# build-cm4-tarball.sh — assemble cm4-install.tar.gz from git repo content
# plus the CM4 SBC overlay (kernel, modules, firmware).
#
# Usage:
#   build-cm4-tarball.sh [output.tar.gz]
#   SBC_OVERLAY_DIR=/path/to/overlay build-cm4-tarball.sh [output.tar.gz]
#
# If SBC_OVERLAY_DIR is not set, looks for the overlay at the default location:
#   kernel-work/packages/cm4-sbc-overlay/
# produced by kernel-work/real_work/build-cm4-sbc-overlay.sh.
#
# Overlay must contain:
#   boot/firmware/kernel8.img
#   boot/firmware/bcm2711*.dtb
#   boot/firmware/overlays/mm610x-spi.dtbo
#   usr/lib/modules/<kver>/
#   usr/lib/firmware/morse/

OUT="${1:-cm4-install.tar.gz}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Resolve SBC overlay: explicit env var, then default location, then hard fail.
DEFAULT_OVERLAY="$REPO_ROOT/kernel-work/packages/cm4-sbc-overlay"
if [ -z "${SBC_OVERLAY_DIR:-}" ]; then
    if [ -d "$DEFAULT_OVERLAY" ]; then
        SBC_OVERLAY_DIR="$DEFAULT_OVERLAY"
        echo "Using default SBC overlay: $SBC_OVERLAY_DIR"
    else
        echo "ERROR: SBC overlay not found." >&2
        echo "       Run kernel-work/real_work/build-cm4-sbc-overlay.sh first," >&2
        echo "       or set SBC_OVERLAY_DIR to an existing overlay directory." >&2
        exit 1
    fi
fi
STAGE="$(mktemp -d)"

cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

install_tree() {
    local src="$1" dst="$2"
    [ -d "$src" ] || return 0
    mkdir -p "$dst"
    cp -a "$src"/. "$dst"/
}

require_dir() {
    local path="$1" label="$2"
    [ -d "$path" ] || { echo "ERROR: Missing $label: $path" >&2; exit 1; }
}

install_file() {
    local mode="$1" src="$2" dst="$3"
    [ -f "$src" ] && install -D -m "$mode" "$src" "$dst"
}

# ── Universal node software ───────────────────────────────────────────────────

mkdir -p "$STAGE/usr/local/bin" "$STAGE/usr/sbin" "$STAGE/etc/systemd/system"

install_tree "$REPO_ROOT/MANET/node_tools"      "$STAGE/usr/local/bin"
chmod -R a+rX "$STAGE/usr/local/bin"
find "$STAGE/usr/local/bin" -type f \
    \( -name '*.sh' -o -name '*.py' -o -name 'morse_cli' -o -name 'chronyc' \) \
    -exec chmod 0755 {} +

install_file 0755 "$REPO_ROOT/MANET/binaries_arm64/alfred"             "$STAGE/usr/sbin/alfred"
install_file 0755 "$REPO_ROOT/MANET/binaries_arm64/batctl"             "$STAGE/usr/sbin/batctl"
install_file 0755 "$REPO_ROOT/MANET/binaries_arm64/wpa_cli_s1g"        "$STAGE/usr/sbin/wpa_cli_s1g"
install_file 0755 "$REPO_ROOT/MANET/binaries_arm64/wpa_supplicant_s1g" "$STAGE/usr/sbin/wpa_supplicant_s1g"

install_tree "$REPO_ROOT/MANET/systemd"         "$STAGE/etc/systemd/system"
install_tree "$REPO_ROOT/MANET/systemd-network" "$STAGE/etc/systemd/network"
install_tree "$REPO_ROOT/MANET/udev/rules.d"    "$STAGE/etc/udev/rules.d"
install_tree "$REPO_ROOT/MANET/share/manet"     "$STAGE/usr/local/share/manet"
install_tree "$REPO_ROOT/MANET/etc"             "$STAGE/etc"

install_file 0644 "$REPO_ROOT/MANET/root/regulatory.db" "$STAGE/root/regulatory.db"

# networkd-dispatcher directory tree and hooks
mkdir -p \
    "$STAGE/etc/networkd-dispatcher/carrier.d" \
    "$STAGE/etc/networkd-dispatcher/routable.d" \
    "$STAGE/etc/networkd-dispatcher/off.d" \
    "$STAGE/etc/networkd-dispatcher/no-carrier.d" \
    "$STAGE/etc/networkd-dispatcher/degraded.d"

mkdir -p "$STAGE/root/networkd-dispatcher"
install_file 0755 "$REPO_ROOT/MANET/networkd-dispatcher/carrier" \
    "$STAGE/root/networkd-dispatcher/carrier"
install_file 0755 "$REPO_ROOT/MANET/networkd-dispatcher/off" \
    "$STAGE/root/networkd-dispatcher/off"

cat > "$STAGE/etc/networkd-dispatcher/carrier.d/50-ethernet-detect" <<'EOF'
#!/bin/bash
set -euo pipefail
/usr/local/bin/manet-uplink-dispatch.sh carrier "${IFACE:-}"
if grep -qi '^auto_update=1' /etc/mesh.conf 2>/dev/null && \
   ping -c 1 -W 2 -I "$IFACE" 8.8.8.8 >/dev/null 2>&1; then
    /usr/local/bin/node-update.sh --routine
fi
EOF

cat > "$STAGE/etc/networkd-dispatcher/routable.d/50-manet-uplink" <<'EOF'
#!/bin/bash
set -euo pipefail
/usr/local/bin/manet-uplink-dispatch.sh routable "${IFACE:-}"
EOF

for state in off no-carrier degraded; do
    install -m 0755 "$REPO_ROOT/MANET/networkd-dispatcher/off" \
        "$STAGE/etc/networkd-dispatcher/${state}.d/50-gateway-disable"
done
chmod 0755 \
    "$STAGE/etc/networkd-dispatcher/carrier.d/50-ethernet-detect" \
    "$STAGE/etc/networkd-dispatcher/routable.d/50-manet-uplink"

# ── systemd unit enables ──────────────────────────────────────────────────────

mkdir -p "$STAGE/etc/systemd/system/multi-user.target.wants"
for unit in \
    ethernet-autodetect.service \
    manet-txpower.service \
    mesh-default-route-fix.service \
    perf-dashboard.service \
    sae-watchdog.service \
    ebtables-restore.service \
    batman-enslave-watch.service
do
    if [ -f "$STAGE/etc/systemd/system/$unit" ]; then
        ln -sf "../$unit" "$STAGE/etc/systemd/system/multi-user.target.wants/$unit"
    fi
done

# ── SBC overlay (kernel, DTBs, overlay dtbo, modules, firmware) ───────────────

require_dir "$SBC_OVERLAY_DIR" "SBC overlay"
install_tree "$SBC_OVERLAY_DIR" "$STAGE"

# ── Trixie merged-usr symlink ─────────────────────────────────────────────────

if [ ! -e "$STAGE/lib" ]; then
    ln -s usr/lib "$STAGE/lib"
fi

# ── Permission fixes ──────────────────────────────────────────────────────────

if [ -f "$STAGE/etc/sudoers.d/perf" ]; then
    chmod 0440 "$STAGE/etc/sudoers.d/perf"
fi

# ── Assemble tarball ──────────────────────────────────────────────────────────

mkdir -p "$(dirname "$OUT")"
tar --owner=0 --group=0 --numeric-owner -czf "$OUT" -C "$STAGE" .
echo "Built: $OUT  ($(du -sh "$OUT" | cut -f1))"
