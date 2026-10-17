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
KDIR="$WORK/linux-6.18"
BUILD="${BUILD:-$WORK/build-r3a}"
FRAGS="$SCRIPT_DIR/config-fragments"
MORSE="$WORK/morse-driver-1.16"
OVERLAYS="$SCRIPT_DIR/dts-overlays"

ARCH=arm64
CROSS="${CROSS_COMPILE:-aarch64-linux-gnu-}"
JOBS="${JOBS:-$(nproc)}"

# Verify pinned source inputs before changing or building a kernel tree.
python3 "$SCRIPT_DIR/prepare-sources.py" --check --board r3a

mkdir -p "$BUILD"

cd "$KDIR"

# Rock 3A DT fix kept in the MANET repo (kernel/patches/r3a-pcie); skip if applied
R3A_PATCH="$PATCHES/r3a-pcie/0001-rock-3a-pcie-startup-delay.patch"
git apply --reverse --check "$R3A_PATCH" 2>/dev/null || git apply "$R3A_PATCH"

# Preserve the existing channel-power policy before applying timing support.
TXPOWER_PATCH="$PATCHES/mt76-txpower/0001-mt7915-channel-power.patch"
git apply --reverse --check "$TXPOWER_PATCH" 2>/dev/null || git apply "$TXPOWER_PATCH"

# Apply/verify timing plus background observation as one transaction.
python3 "$PATCHES/mt76-bgobs/apply.py" "$KDIR"

# Upstream background-CAC fix (92fecd2 + 68b908b), not in 6.18 stable.
# 0002 depends on 0001; apply the pair unless 0002 is already in the tree.
BGCAC="$PATCHES/cfg80211-bgcac"
if ! git apply --reverse --check "$BGCAC/0002-wifi-cfg80211-events-report-background-radar.patch" 2>/dev/null; then
    git apply "$BGCAC/0001-wifi-cfg80211-fix-background-CAC.patch"
    git apply "$BGCAC/0002-wifi-cfg80211-events-report-background-radar.patch"
fi

# Start from arm64 defconfig (mainline 6.18; rockchip_defconfig no longer exists)
make O="$BUILD" ARCH=$ARCH CROSS_COMPILE=$CROSS defconfig

# Merge MANET fragments
./scripts/kconfig/merge_config.sh -m -O "$BUILD" \
    "$BUILD/.config" \
    "$FRAGS/manet-common.config" \
    "$FRAGS/manet-r3a.config" \
    ${EXTRA_CONFIG:+"$EXTRA_CONFIG"}

make O="$BUILD" ARCH=$ARCH CROSS_COMPILE=$CROSS olddefconfig

# Build kernel + modules + DTBs
make O="$BUILD" ARCH=$ARCH CROSS_COMPILE=$CROSS -j$JOBS Image modules dtbs

# Install modules to staging dir
STAGING="$BUILD/staging"
rm -rf "$STAGING"
make O="$BUILD" ARCH=$ARCH CROSS_COMPILE=$CROSS \
    INSTALL_MOD_PATH="$STAGING" modules_install

# Build Morse 1.16 out-of-tree
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

# Copy morse modules into staging
MORSE_STAGING="$STAGING/lib/modules/$KVER/extra/morse"
mkdir -p "$MORSE_STAGING"
find "$MORSE" -name "*.ko" -exec cp {} "$MORSE_STAGING/" \;

echo "Patching max_eirp in dot11ah.ko to 30 dBm (all regions)..."
python3 "$SCRIPT_DIR/morse-dot11ah-power-patch.py" \
    "$MORSE_STAGING/dot11ah.ko" \
    "$MORSE_STAGING/dot11ah.ko.patched" \
    --power-dbm 30
mv "$MORSE_STAGING/dot11ah.ko.patched" "$MORSE_STAGING/dot11ah.ko"

# Compress modules
find "$STAGING/lib/modules/$KVER" -name "*.ko" -exec xz -f {} \;

echo ""
echo "=== Build complete ==="
echo "Kernel:  $BUILD/arch/arm64/boot/Image"
echo "DTBs:    $BUILD/arch/arm64/boot/dts/rockchip/"
echo "Modules: $STAGING/lib/modules/$KVER/"
echo "Version: $KVER"
