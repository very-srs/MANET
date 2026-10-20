#!/bin/bash
set -e
export KBUILD_BUILD_USER=build
export KBUILD_BUILD_HOST=manet
export KBUILD_BUILD_TIMESTAMP=""
export LOCALVERSION="${LOCALVERSION:--r3}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SERIES="$SCRIPT_DIR"
SCRIPT_DIR="$REPO_ROOT/kernel/build"
KWORK="${MANET_KERNEL_WORK:-$REPO_ROOT/kernel-work}"
WORK="$KWORK/real_work"
KDIR="$WORK/linux-rpi-6.18"
BUILD="${BUILD:-$WORK/build-cm4}"
FRAGS="$SCRIPT_DIR/config-fragments"
MORSE="$WORK/morse-driver-1.16"
BATMAN="$WORK/batman-adv-2026.3"
OVERLAYS="$SCRIPT_DIR/dts-overlays"

ARCH=arm64
CROSS="${CROSS_COMPILE:-aarch64-linux-gnu-}"
JOBS="${JOBS:-$(nproc)}"

# Verify pinned source inputs before changing or building a kernel tree.
python3 "$SCRIPT_DIR/prepare-sources.py" --check --board cm4
MMRC_REV=$(git -C "$MORSE" rev-parse HEAD:mmrc-submodule)
if [ ! -f "$MORSE/mmrc-submodule/src/core/mmrc.h" ] || \
   [ "$(git -C "$MORSE/mmrc-submodule" rev-parse HEAD 2>/dev/null)" != "$MMRC_REV" ]; then
    echo "Initialize Morse's pinned mmrc-submodule ($MMRC_REV) before building" >&2
    exit 1
fi

# One application transaction includes both sides of the private hold API.
python3 "$SERIES/apply.py" "$KDIR"
mkdir -p "$BUILD"
cd "$KDIR"

make O="$BUILD" ARCH=$ARCH CROSS_COMPILE=$CROSS bcm2711_defconfig

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

# Compile the Morse SPI overlay for CM4
mkdir -p "$STAGING/boot/overlays"
dtc -@ -I dts -O dtb \
    -o "$STAGING/boot/overlays/mm610x-spi.dtbo" \
    "$OVERLAYS/cm4-morse-spi.dts"

cd "$MORSE"

# The morse out-of-tree build does NOT track the kernel's generated autoconf.h
# as a dependency, so a stale mac.o silently keeps the old config (e.g. mesh
# disabled) even after a kernel .config change. Always clean first.
make KERNEL_SRC="$BUILD" ARCH=$ARCH CROSS_COMPILE=$CROSS clean
make KERNEL_SRC="$BUILD" \
     ARCH=$ARCH \
     CROSS_COMPILE=$CROSS \
     CONFIG_WLAN_VENDOR_MORSE=m \
     CONFIG_MORSE_SPI=y \
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

# batman-adv 2026.3 is built out of tree. It replaces the kernel's own module
# so only one batman-adv.ko exists for depmod to find.
make -C "$BATMAN" KERNELPATH="$BUILD" ARCH=$ARCH CROSS_COMPILE=$CROSS clean
make -C "$BATMAN" KERNELPATH="$BUILD" ARCH=$ARCH CROSS_COMPILE=$CROSS -j$JOBS
BATMAN_KO="$STAGING/usr/lib/modules/$KVER/kernel/net/batman-adv/batman-adv.ko"
test -f "$BATMAN_KO"
cp "$BATMAN/net/batman-adv/batman-adv.ko" "$BATMAN_KO"
make -C "$BATMAN" KERNELPATH="$BUILD" ARCH=$ARCH CROSS_COMPILE=$CROSS clean
test "$(modinfo -F version "$BATMAN_KO")" = 2026.3
modinfo -F vermagic "$BATMAN_KO" | grep -q "^$KVER "

echo "Patching max_eirp in dot11ah.ko to 30 dBm (all regions)..."
python3 "$SCRIPT_DIR/morse-dot11ah-power-patch.py" \
    "$MORSE_STAGING/dot11ah.ko" \
    "$MORSE_STAGING/dot11ah.ko.patched" \
    --power-dbm 30
mv "$MORSE_STAGING/dot11ah.ko.patched" "$MORSE_STAGING/dot11ah.ko"

find "$STAGING/usr/lib/modules/$KVER" -name "*.ko" -exec xz -f {} \;

echo ""
echo "=== CM4 build complete ==="
echo "Kernel:  $BUILD/arch/arm64/boot/Image.gz"
echo "DTBs:    $BUILD/arch/arm64/boot/dts/broadcom/"
echo "Overlay: $STAGING/boot/overlays/mm610x-spi.dtbo"
echo "Modules: $STAGING/usr/lib/modules/$KVER/"
echo "Version: $KVER"
