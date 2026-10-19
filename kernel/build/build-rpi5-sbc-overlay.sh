#!/bin/bash
set -euo pipefail

# build-rpi5-sbc-overlay.sh - assemble the RPi5 SBC overlay directory from
# build-rpi5/ outputs.  Must be run after build-rpi5.sh.
#
# HaLow bus assumption: RPi5 boards are currently treated as using the USB
# variant of the Morse HaLow card (same as Rock 3A) - no DTS overlay, BCF
# selected by OTP board type. Revisit if/when RPi5 SPI wiring is confirmed.
#
# Output: kernel-work/packages/rpi5-sbc-overlay/
#   boot/firmware/kernel_2712.img   (gzip-compressed kernel)
#   boot/firmware/bcm2712*.dtb
#   usr/lib/modules/<kver>/         (kernel modules + Morse extra, xz-compressed)
#   usr/lib/firmware/morse/         (Morse firmware blobs)
#
# Then build the full tarball with:
#   SBC_OVERLAY_DIR=kernel-work/packages/rpi5-sbc-overlay \
#     bash MANET/packaging/build-rpi5-tarball.sh kernel-work/packages/rpi5-install.tar.gz

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
KWORK="${MANET_KERNEL_WORK:-$REPO_ROOT/kernel-work}"
WORK="$KWORK/real_work"
BUILD="${BUILD:-$WORK/build-rpi5}"
STAGING="$BUILD/staging"
PKG="$KWORK/packages"
OVERLAY="${OVERLAY:-$PKG/rpi5-sbc-overlay}"
MORSE_FW="$KWORK/morse-firmware"

require() {
    local path="$1" label="$2"
    [ -e "$path" ] || { echo "ERROR: Missing $label: $path" >&2; exit 1; }
}

require "$BUILD/arch/arm64/boot/Image.gz"  "kernel Image.gz (run build-rpi5.sh first)"
require "$STAGING/lib/modules"             "staging modules (run build-rpi5.sh first)"

KVER=$(ls "$STAGING/lib/modules/")
require "$STAGING/lib/modules/$KVER" "modules for $KVER"

echo "=== Building RPi5 SBC overlay for $KVER ==="
mkdir -p "$PKG"
rm -rf "$OVERLAY"

# ── Boot content ──────────────────────────────────────────────────────────────

mkdir -p "$OVERLAY/boot/firmware"

cp "$BUILD/arch/arm64/boot/Image.gz" \
   "$OVERLAY/boot/firmware/kernel_2712.img"

cp "$BUILD/arch/arm64/boot/dts/broadcom/"bcm2712*.dtb \
   "$OVERLAY/boot/firmware/"

# ── Kernel modules ────────────────────────────────────────────────────────────
# build-rpi5.sh installs real kernel modules via modules_install into
# staging/lib/modules/, and stages the Morse extras separately under
# staging/usr/lib/modules/.../extra/ - copy both into the overlay's
# usr/lib/modules/ (Trixie merged-usr layout).

mkdir -p "$OVERLAY/usr/lib/modules/$KVER"
cp -r "$STAGING/lib/modules/$KVER/." "$OVERLAY/usr/lib/modules/$KVER/"
rm -f "$OVERLAY/usr/lib/modules/$KVER/build"
rm -f "$OVERLAY/usr/lib/modules/$KVER/source"

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
echo "=== RPi5 SBC overlay complete ==="
echo "Kernel:   $OVERLAY/boot/firmware/kernel_2712.img"
echo "DTBs:     $OVERLAY/boot/firmware/bcm2712*.dtb"
echo "Modules:  $OVERLAY/usr/lib/modules/$KVER/"
echo "Firmware: $OVERLAY/usr/lib/firmware/morse/"
echo ""
echo "Build full install tarball with:"
echo "  SBC_OVERLAY_DIR=$OVERLAY \\"
echo "    bash $REPO_ROOT/MANET/packaging/build-rpi5-tarball.sh \\"
echo "    $PKG/rpi5-install.tar.gz"
