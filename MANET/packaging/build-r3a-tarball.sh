#!/usr/bin/env bash
set -euo pipefail

# build-r3a-tarball.sh — assemble r3a-install.tar.gz from git repo content
# plus the Rock 3A SBC overlay (kernel, modules, firmware).
#
# Usage:
#   build-r3a-tarball.sh [output.tar.gz]
#   SBC_OVERLAY_DIR=/path/to/overlay build-r3a-tarball.sh [output.tar.gz]
#
# If SBC_OVERLAY_DIR is not set, looks for the overlay at the default location:
#   kernel-work/packages/r3a-sbc-overlay/
# produced by kernel-work/real_work/build-r3a-sbc-overlay.sh.
#
# Overlay must contain:
#   boot/Image
#   boot/dtb/rockchip/rk3568*.dtb
#   usr/lib/modules/<kver>/
#   usr/lib/firmware/morse/

OUT="${1:-r3a-install.tar.gz}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Resolve SBC overlay: explicit env var, then default location, then hard fail.
DEFAULT_OVERLAY="$REPO_ROOT/kernel-work/packages/r3a-sbc-overlay"
if [ -z "${SBC_OVERLAY_DIR:-}" ]; then
    if [ -d "$DEFAULT_OVERLAY" ]; then
        SBC_OVERLAY_DIR="$DEFAULT_OVERLAY"
        echo "Using default SBC overlay: $SBC_OVERLAY_DIR"
    else
        echo "ERROR: SBC overlay not found." >&2
        echo "       Run kernel-work/real_work/build-r3a-sbc-overlay.sh first," >&2
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

# ── Lyra codec plugin ─────────────────────────────────────────────────────────
# voice_codec defaults to lyra, and a node without these two artifacts falls
# back to opus — which on a lyra mesh means it is deaf and mute, not merely
# lower quality. So this warns loudly rather than skipping quietly the way
# install_file does. Build them with:
#   MANET/packaging/build-lyra-aarch64.sh          (cross-builds liblyra + deps)
#   make -C MANET/packaging/gst-lyra               (natively, on an aarch64 node)
# then drop the results in MANET/lyra_arm64/.
LYRA_SRC="$REPO_ROOT/MANET/lyra_arm64"
if [ -f "$LYRA_SRC/libgstlyra.so" ] && [ -d "$LYRA_SRC/model_coeffs" ]; then
    install_file 0644 "$LYRA_SRC/libgstlyra.so" \
        "$STAGE/usr/lib/aarch64-linux-gnu/gstreamer-1.0/libgstlyra.so"
    install_tree "$LYRA_SRC/model_coeffs" "$STAGE/usr/local/share/lyra/model_coeffs"
    echo "Lyra codec: plugin + model weights staged"
else
    echo "WARNING: MANET/lyra_arm64/{libgstlyra.so,model_coeffs/} missing — the" >&2
    echo "         tarball will ship without the Lyra codec, and every node will" >&2
    echo "         fall back to opus. See MANET/packaging/README.md." >&2
fi

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
    sae-watchdog.service \
    ebtables-restore.service \
    batman-enslave-watch.service
do
    if [ -f "$STAGE/etc/systemd/system/$unit" ]; then
        ln -sf "../$unit" "$STAGE/etc/systemd/system/multi-user.target.wants/$unit"
    fi
done

# ── SBC overlay (kernel, DTBs, modules, firmware) ─────────────────────────────

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
OUT_ABS="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"

# Directory modes are recorded in the archive and applied to the target by an
# extractor running as root, so they have to be right here. Two rules:
#
#  1. Never emit an entry for the stage root. Tar stores its mode against "./"
#     and restores it onto the extraction directory — which is "/" on a node.
#     mktemp -d gives 0700, so shipping "./" once set / to 0700 and locked
#     every non-root process out of the filesystem (no traversal, no shared
#     libraries, no ssh logins). Archive the contents instead.
#  2. Strip group/other write. These dirs inherit the build machine's umask,
#     and a umask of 002 ships /usr, /etc and /usr/local as 0775 root:root.
# Bytecode compiled on the build machine has no business on a node.
find "$STAGE" -type d -name __pycache__ -prune -exec rm -rf {} +

find "$STAGE" -type d -exec chmod go-w {} +
( cd "$STAGE" && find . -mindepth 1 -maxdepth 1 -printf './%P\0' \
    | tar --owner=0 --group=0 --numeric-owner --null -T - -czf "$OUT_ABS" )
echo "Built: $OUT  ($(du -sh "$OUT" | cut -f1))"
