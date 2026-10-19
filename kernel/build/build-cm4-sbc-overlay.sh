#!/bin/bash
set -euo pipefail

# build-cm4-sbc-overlay.sh - assemble the CM4 SBC overlay directory from
# build-cm4/ outputs.  Must be run after build-cm4.sh.
#
# Output: kernel-work/packages/cm4-sbc-overlay/
#   boot/firmware/kernel8.img       (gzip-compressed kernel)
#   boot/firmware/bcm2711*.dtb
#   boot/firmware/overlays/mm610x-spi.dtbo
#   usr/lib/modules/<kver>/         (kernel modules + Morse extra, xz-compressed)
#   usr/lib/firmware/morse/         (Morse firmware blobs)
#
# Then build the full tarball with:
#   SBC_OVERLAY_DIR=kernel-work/packages/cm4-sbc-overlay \
#     bash MANET/packaging/build-cm4-tarball.sh kernel-work/packages/cm4-install.tar.gz

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
KWORK="${MANET_KERNEL_WORK:-$REPO_ROOT/kernel-work}"
WORK="$KWORK/real_work"
BUILD="${BUILD:-$WORK/build-cm4}"
STAGING="$BUILD/staging"
PKG="$KWORK/packages"
OVERLAY="${OVERLAY:-$PKG/cm4-sbc-overlay}"
MORSE_FW="$KWORK/morse-firmware"

require() {
    local path="$1" label="$2"
    [ -e "$path" ] || { echo "ERROR: Missing $label: $path" >&2; exit 1; }
}

require "$BUILD/arch/arm64/boot/Image.gz"          "kernel Image.gz (run build-cm4.sh first)"
require "$STAGING/lib/modules"                      "staging modules (run build-cm4.sh first)"
require "$STAGING/boot/overlays/mm610x-spi.dtbo"   "Morse SPI overlay (run build-cm4.sh first)"

KVER=$(ls "$STAGING/lib/modules/")
require "$STAGING/lib/modules/$KVER" "modules for $KVER"

echo "=== Building CM4 SBC overlay for $KVER ==="
mkdir -p "$PKG"
rm -rf "$OVERLAY"

# ── Boot content ──────────────────────────────────────────────────────────────

mkdir -p "$OVERLAY/boot/firmware/overlays"

cp "$BUILD/arch/arm64/boot/Image.gz" \
   "$OVERLAY/boot/firmware/kernel8.img"

cp "$BUILD/arch/arm64/boot/dts/broadcom/"bcm2711*.dtb \
   "$OVERLAY/boot/firmware/"

cp "$STAGING/boot/overlays/mm610x-spi.dtbo" \
   "$OVERLAY/boot/firmware/overlays/"

# A kernel field update boots its own slot (os_prefix), so it must carry every
# overlay config.txt names, plus overlay_map.dtb for the Pi 4 remaps
# (vc4-kms-v3d -> vc4-kms-v3d-pi4). ALL_OVERLAYS=1 ships the full matched set.
if [ "${ALL_OVERLAYS:-0}" = 1 ]; then
    cp "$BUILD/arch/arm64/boot/dts/overlays/"*.dtbo \
       "$BUILD/arch/arm64/boot/dts/overlays/overlay_map.dtb" \
       "$OVERLAY/boot/firmware/overlays/"
fi

# ── Kernel modules ────────────────────────────────────────────────────────────

mkdir -p "$OVERLAY/usr/lib/modules/$KVER"
cp -r "$STAGING/lib/modules/$KVER/." "$OVERLAY/usr/lib/modules/$KVER/"
rm -f "$OVERLAY/usr/lib/modules/$KVER/build"
rm -f "$OVERLAY/usr/lib/modules/$KVER/source"

# ── Morse extra modules ───────────────────────────────────────────────────────

MORSE_SRC="$STAGING/usr/lib/modules/$KVER/extra"
if [ -d "$MORSE_SRC" ]; then
    cp -r "$MORSE_SRC" "$OVERLAY/usr/lib/modules/$KVER/"
else
    echo "WARNING: Morse extra modules not found at $MORSE_SRC" >&2
fi

# ── Trixie merged-usr symlink (required for depmod -b to find modules) ────────

if [ ! -e "$OVERLAY/lib" ]; then
    ln -s usr/lib "$OVERLAY/lib"
fi

# ── Regenerate module dependency database ─────────────────────────────────────
# modules_install only ran depmod over the kernel modules, not the Morse extras.
# Re-run now that both sets are in place so modprobe can resolve everything.

if command -v depmod &>/dev/null; then
    echo "Running depmod for $KVER..."
    depmod -b "$OVERLAY" "$KVER"
else
    echo "WARNING: depmod not found - run 'sudo depmod -b $OVERLAY $KVER' manually" >&2
fi

# ── Morse firmware blobs ──────────────────────────────────────────────────────
# Pull from kernel-work/morse-firmware/ (git repo). Copy all .bin files from
# firmware/ and every bcf/<vendor>/ subdirectory flat into usr/lib/firmware/morse/,
# matching the layout firstrun.sh installs on target.

if [ ! -d "$MORSE_FW" ]; then
    echo "ERROR: Morse firmware repo not found at $MORSE_FW" >&2
    exit 1
fi

mkdir -p "$OVERLAY/usr/lib/firmware/morse"
find "$MORSE_FW/firmware" "$MORSE_FW/bcf" -name "*.bin" \
    -exec cp {} "$OVERLAY/usr/lib/firmware/morse/" \;

# The BCFs stay exactly as Morse Micro published them: their license permits
# distribution only complete and unmodified. The node applies the TX power
# policy at boot with manet-morse-bcf.py.
# The same license requires a copy of itself to travel with the files.
install -m 0644 "$MORSE_FW/firmware/LICENSE" "$OVERLAY/usr/lib/firmware/morse/LICENSE"

echo ""
echo "=== CM4 SBC overlay complete ==="
echo "Kernel:   $OVERLAY/boot/firmware/kernel8.img"
echo "DTBs:     $OVERLAY/boot/firmware/bcm2711*.dtb"
echo "Overlay:  $OVERLAY/boot/firmware/overlays/mm610x-spi.dtbo"
echo "Modules:  $OVERLAY/usr/lib/modules/$KVER/"
echo "Firmware: $OVERLAY/usr/lib/firmware/morse/"
echo ""
echo "Build full install tarball with:"
echo "  SBC_OVERLAY_DIR=$OVERLAY \\"
echo "    bash $REPO_ROOT/MANET/packaging/build-cm4-tarball.sh \\"
echo "    $PKG/cm4-install.tar.gz"
