#!/bin/bash
set -euo pipefail

# build-r3a-sbc-overlay.sh - assemble the Rock 3A SBC overlay directory from
# build-r3a/ outputs.  Must be run after build-r3a.sh.
#
# Output: kernel-work/packages/r3a-sbc-overlay/
#   boot/Image                      (uncompressed kernel)
#   boot/dtb/rockchip/rk3568*.dtb   (RK3568 device tree blobs)
#   usr/lib/modules/<kver>/         (kernel modules + Morse extra, xz-compressed)
#   usr/lib/firmware/morse/         (Morse firmware blobs)
#
# Then build the full tarball with:
#   SBC_OVERLAY_DIR=kernel-work/packages/r3a-sbc-overlay \
#     bash MANET/packaging/build-r3a-tarball.sh kernel-work/packages/r3a-install.tar.gz

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
KWORK="${MANET_KERNEL_WORK:-$REPO_ROOT/kernel-work}"
WORK="$KWORK/real_work"
BUILD="${BUILD:-$WORK/build-r3a}"
STAGING="$BUILD/staging"
PKG="$KWORK/packages"
OVERLAY="${OVERLAY:-$PKG/r3a-sbc-overlay}"
MORSE_FW="$KWORK/morse-firmware"

require() {
    local path="$1" label="$2"
    [ -e "$path" ] || { echo "ERROR: Missing $label: $path" >&2; exit 1; }
}

require "$BUILD/arch/arm64/boot/Image"  "kernel Image (run build-r3a.sh first)"
require "$STAGING/lib/modules"          "staging modules (run build-r3a.sh first)"

KVER=$(ls "$STAGING/lib/modules/")
require "$STAGING/lib/modules/$KVER" "modules for $KVER"

echo "=== Building Rock 3A SBC overlay for $KVER ==="
mkdir -p "$PKG"
rm -rf "$OVERLAY"

# ── Boot content ──────────────────────────────────────────────────────────────

mkdir -p "$OVERLAY/boot/dtb/rockchip"

cp "$BUILD/arch/arm64/boot/Image" \
   "$OVERLAY/boot/Image"

# RK3568 DTBs only - Armbian fdtfile points to one of these
cp "$BUILD/arch/arm64/boot/dts/rockchip/"rk3568*.dtb \
   "$OVERLAY/boot/dtb/rockchip/"

# ── Kernel modules ────────────────────────────────────────────────────────────
# build-r3a.sh stages everything (kernel modules + Morse extra) into
# staging/lib/modules/<kver>/ - copy the whole tree including extra/morse/.

mkdir -p "$OVERLAY/usr/lib/modules/$KVER"
cp -r "$STAGING/lib/modules/$KVER/." "$OVERLAY/usr/lib/modules/$KVER/"
rm -f "$OVERLAY/usr/lib/modules/$KVER/build"
rm -f "$OVERLAY/usr/lib/modules/$KVER/source"

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

# ── Patch BCF TX power limits ─────────────────────────────────────────────────
# Every BCF is patched, not just the one this board's module is known to use:
# a USB MM8108 selects its BCF by OTP board type (bcf_boardtype_XXXX.bin) and
# SPI modules name theirs in a module option, so any of them can be loaded.
# Raise every region to 30 dBm, so the BCF tables set no ceiling below that.

echo "Patching TX power for every region in every BCF to 30 dBm..."
for BCF in "$OVERLAY/usr/lib/firmware/morse/"bcf_*.bin; do
    [ -f "$BCF" ] || continue
    # Exit 3 means the file has nothing to patch; any other failure (missing
    # pyelftools, a malformed ELF, an unexpected TLV) must stop the build
    # rather than ship a stock BCF under the 30 dBm policy.
    rc=0
    python3 "$SCRIPT_DIR/morse-bcf-patch.py" "$BCF" "$BCF.patched" \
        --country ALL --power-dbm 30 >/dev/null || rc=$?
    case "$rc" in
        0) mv "$BCF.patched" "$BCF"; echo "  patched $(basename "$BCF")" ;;
        3) rm -f "$BCF.patched"; echo "  skipped $(basename "$BCF") (no TX power entries)" ;;
        *) rm -f "$BCF.patched"; echo "ERROR: cannot patch $(basename "$BCF") (exit $rc)" >&2; exit 1 ;;
    esac
done

echo ""
echo "=== Rock 3A SBC overlay complete ==="
echo "Kernel:   $OVERLAY/boot/Image"
echo "DTBs:     $OVERLAY/boot/dtb/rockchip/rk3568*.dtb"
echo "Modules:  $OVERLAY/usr/lib/modules/$KVER/"
echo "Firmware: $OVERLAY/usr/lib/firmware/morse/"
echo ""
echo "Build full install tarball with:"
echo "  SBC_OVERLAY_DIR=$OVERLAY \\"
echo "    bash $REPO_ROOT/MANET/packaging/build-r3a-tarball.sh \\"
echo "    $PKG/r3a-install.tar.gz"
