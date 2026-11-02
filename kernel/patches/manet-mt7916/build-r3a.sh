#!/bin/bash
set -e
export KBUILD_BUILD_USER=build
export KBUILD_BUILD_HOST=manet
export KBUILD_BUILD_TIMESTAMP=""
export LOCALVERSION="${LOCALVERSION:--r1}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SERIES="$SCRIPT_DIR"
SCRIPT_DIR="$REPO_ROOT/kernel/build"
KWORK="${MANET_KERNEL_WORK:-$REPO_ROOT/kernel-work}"
WORK="$KWORK/real_work"
KDIR="$WORK/linux-6.18"
BUILD="${BUILD:-$WORK/build-r3a}"
FRAGS="$SCRIPT_DIR/config-fragments"
MORSE="$WORK/morse-driver-1.16"
BATMAN="$WORK/batman-adv-2026.3"
BATMAN_VERSION=2026.3

ARCH=arm64
CROSS="${CROSS_COMPILE:-aarch64-linux-gnu-}"
JOBS="${JOBS:-$(nproc)}"

# Verify pinned source inputs before changing or building a kernel tree.
python3 "$SCRIPT_DIR/prepare-sources.py" --check --board r3a
MMRC_REV=$(git -C "$MORSE" rev-parse HEAD:mmrc-submodule)
if [ ! -f "$MORSE/mmrc-submodule/src/core/mmrc.h" ] || \
   [ "$(git -C "$MORSE/mmrc-submodule" rev-parse HEAD 2>/dev/null)" != "$MMRC_REV" ]; then
    echo "Initialize Morse's pinned mmrc-submodule ($MMRC_REV) before building" >&2
    exit 1
fi

# One application transaction includes both sides of the private hold API.
# The combined series also carries the channel-power, observer and
# background-CAC patches that kernel/build/build-r3a.sh applies separately.
python3 "$SERIES/apply.py" "$KDIR"
mkdir -p "$BUILD"
cd "$KDIR"

# Rock 3A DT fix kept in the MANET repo (kernel/patches/r3a-pcie); skip if applied
R3A_PCIE_PATCH="$REPO_ROOT/kernel/patches/r3a-pcie/0001-rock-3a-pcie-startup-delay.patch"
git apply --reverse --check "$R3A_PCIE_PATCH" 2>/dev/null || git apply "$R3A_PCIE_PATCH"

# Start from arm64 defconfig (mainline 6.18 has no rockchip_defconfig)
make O="$BUILD" ARCH=$ARCH CROSS_COMPILE=$CROSS defconfig

./scripts/kconfig/merge_config.sh -m -O "$BUILD" \
    "$BUILD/.config" \
    "$FRAGS/manet-common.config" \
    "$FRAGS/manet-r3a.config" \
    ${EXTRA_CONFIG:+"$EXTRA_CONFIG"}

make O="$BUILD" ARCH=$ARCH CROSS_COMPILE=$CROSS olddefconfig
make O="$BUILD" ARCH=$ARCH CROSS_COMPILE=$CROSS -j$JOBS Image modules dtbs

STAGING="$BUILD/staging"
rm -rf "$STAGING"
make O="$BUILD" ARCH=$ARCH CROSS_COMPILE=$CROSS \
    INSTALL_MOD_PATH="$STAGING" modules_install

KVER=$(cat "$BUILD/include/config/kernel.release")

cd "$MORSE"

# The morse out-of-tree build does NOT track the kernel's generated autoconf.h
# as a dependency, so a stale mac.o silently keeps the old config (e.g. mesh
# disabled) even after a kernel .config change. Always clean first.
# Rock 3A uses the USB MM8108 only.
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

MORSE_STAGING="$STAGING/lib/modules/$KVER/extra/morse"
mkdir -p "$MORSE_STAGING"
cp morse.ko "$MORSE_STAGING/"
cp dot11ah/dot11ah.ko "$MORSE_STAGING/"
make KERNEL_SRC="$BUILD" clean
cd -

# batman-adv 2026.3 is built out of tree. It replaces the kernel's own module
# so only one batman-adv.ko exists for depmod to find.
make -C "$BATMAN" KERNELPATH="$BUILD" ARCH=$ARCH CROSS_COMPILE=$CROSS clean
make -C "$BATMAN" KERNELPATH="$BUILD" ARCH=$ARCH CROSS_COMPILE=$CROSS \
     REVISION=$BATMAN_VERSION -j$JOBS
BATMAN_KO="$STAGING/lib/modules/$KVER/kernel/net/batman-adv/batman-adv.ko"
rm -f "$BATMAN_KO" "$BATMAN_KO.xz"
cp "$BATMAN/net/batman-adv/batman-adv.ko" "$BATMAN_KO"
make -C "$BATMAN" KERNELPATH="$BUILD" ARCH=$ARCH CROSS_COMPILE=$CROSS clean
test "$(modinfo -F version "$BATMAN_KO")" = "$BATMAN_VERSION"
modinfo -F vermagic "$BATMAN_KO" | grep -q "^$KVER "

echo "Patching max_eirp in dot11ah.ko to 30 dBm (all regions)..."
python3 "$SCRIPT_DIR/morse-dot11ah-power-patch.py" \
    "$MORSE_STAGING/dot11ah.ko" \
    "$MORSE_STAGING/dot11ah.ko.patched" \
    --power-dbm 30
mv "$MORSE_STAGING/dot11ah.ko.patched" "$MORSE_STAGING/dot11ah.ko"

find "$STAGING/lib/modules/$KVER" -name "*.ko" -exec xz -f {} \;

echo ""
echo "=== Rock 3A build complete ==="
echo "Kernel:  $BUILD/arch/arm64/boot/Image"
echo "DTBs:    $BUILD/arch/arm64/boot/dts/rockchip/"
echo "Modules: $STAGING/lib/modules/$KVER/"
echo "Version: $KVER"
