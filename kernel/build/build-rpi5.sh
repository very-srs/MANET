#!/bin/bash
set -e

export KBUILD_BUILD_USER=build
export KBUILD_BUILD_HOST=manet
export KBUILD_BUILD_TIMESTAMP=""
export LOCALVERSION="${LOCALVERSION:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
KWORK="${MANET_KERNEL_WORK:-$REPO_ROOT/kernel-work}"
WORK="$KWORK/real_work"
PATCHES="${PATCHES:-$REPO_ROOT/kernel/patches}"
KDIR="$WORK/linux-rpi-6.18"
BUILD="${BUILD:-$WORK/build-rpi5}"
FRAGS="$SCRIPT_DIR/config-fragments"
MORSE="$WORK/morse-driver-1.16"
OVERLAYS="$SCRIPT_DIR/dts-overlays"

ARCH=arm64
CROSS="${CROSS_COMPILE:-aarch64-linux-gnu-}"
JOBS="${JOBS:-$(nproc)}"

# Verify pinned source inputs before changing or building a kernel tree.
python3 "$SCRIPT_DIR/prepare-sources.py" --check --board rpi5

mkdir -p "$BUILD"
cd "$KDIR"

# RPi5 shares the pinned Pi source but may be built before CM4.
TXPOWER_PATCH="$PATCHES/mt76-txpower/0001-mt7915-channel-power.patch"
git apply --reverse --check "$TXPOWER_PATCH" 2>/dev/null || \
    git apply "$TXPOWER_PATCH"
BGCAC="$PATCHES/cfg80211-bgcac"
if ! git apply --reverse --check "$BGCAC/0002-wifi-cfg80211-events-report-background-radar.patch" 2>/dev/null; then
    git apply "$BGCAC/0001-wifi-cfg80211-fix-background-CAC.patch"
    git apply "$BGCAC/0002-wifi-cfg80211-events-report-background-radar.patch"
fi

make O="$BUILD" ARCH=$ARCH CROSS_COMPILE=$CROSS bcm2712_defconfig

./scripts/kconfig/merge_config.sh -m -O "$BUILD" \
    "$BUILD/.config" \
    "$FRAGS/manet-common.config" \
    "$FRAGS/manet-rpi.config" \
    ${EXTRA_CONFIG:+"$EXTRA_CONFIG"}

make O="$BUILD" ARCH=$ARCH CROSS_COMPILE=$CROSS olddefconfig
make O="$BUILD" ARCH=$ARCH CROSS_COMPILE=$CROSS -j$JOBS Image.gz modules dtbs

STAGING="$BUILD/staging"
rm -rf "$STAGING"
make O="$BUILD" ARCH=$ARCH CROSS_COMPILE=$CROSS \
    INSTALL_MOD_PATH="$STAGING" modules_install

KVER=$(cat "$BUILD/include/config/kernel.release")

cd "$MORSE"
# The morse out-of-tree build does NOT track the kernel's generated autoconf.h
# as a dependency, so a stale mac.o silently keeps the old config (e.g. mesh
# disabled) even after a kernel .config change. Always clean first.
make KERNEL_SRC="$BUILD" ARCH=$ARCH CROSS_COMPILE=$CROSS clean
make KERNEL_SRC="$BUILD" \
     ARCH=$ARCH \
     CROSS_COMPILE=$CROSS \
     CONFIG_WLAN_VENDOR_MORSE=m \
     CONFIG_MORSE_USB=y \
     CONFIG_MORSE_VENDOR_COMMAND=y \
     CONFIG_MORSE_USER_ACCESS=y \
     MORSE_TRACE_PATH="$(pwd)" \
     -j$JOBS

MORSE_STAGING="$STAGING/usr/lib/modules/$KVER/extra/morse"
mkdir -p "$MORSE_STAGING"
cp morse.ko "$MORSE_STAGING/"
cp dot11ah/dot11ah.ko "$MORSE_STAGING/"
make KERNEL_SRC="$BUILD" clean
cd -

echo "Patching max_eirp in dot11ah.ko to 30 dBm (all regions)..."
python3 "$SCRIPT_DIR/morse-dot11ah-power-patch.py" \
    "$MORSE_STAGING/dot11ah.ko" \
    "$MORSE_STAGING/dot11ah.ko.patched" \
    --power-dbm 30
mv "$MORSE_STAGING/dot11ah.ko.patched" "$MORSE_STAGING/dot11ah.ko"

find "$STAGING/usr/lib/modules/$KVER" -name "*.ko" -exec xz -f {} \;

echo ""
echo "=== RPi5 build complete ==="
echo "Kernel:  $BUILD/arch/arm64/boot/Image.gz"
echo "DTBs:    $BUILD/arch/arm64/boot/dts/broadcom/"
echo "Modules: $STAGING/usr/lib/modules/$KVER/"
echo "Version: $KVER"
