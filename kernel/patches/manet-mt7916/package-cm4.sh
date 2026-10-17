#!/bin/bash
set -euo pipefail
if [ "$#" != 3 ]; then
    echo "usage: $0 BUILD OVERLAY OUTPUT" >&2
    exit 2
fi
SERIES=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$SERIES/../../.." && pwd)
export BUILD=$(realpath "$1")
export OVERLAY=$(realpath -m "$2")
OUTPUT=$(realpath -m "$3")
WORK=${MANET_KERNEL_WORK:-$ROOT/kernel-work}
RELEASE=$(cat "$BUILD/include/config/kernel.release")
ALL_OVERLAYS=1 bash "$ROOT/kernel/build/build-cm4-sbc-overlay.sh"
cp "$WORK/real_work/linux-rpi-6.18/arch/arm/boot/dts/overlays/README" \
    "$OVERLAY/boot/firmware/overlays/README"
mkdir -p "$OUTPUT"
depmod -e -E "$BUILD/Module.symvers" -b "$OVERLAY" "$RELEASE" 2> "$OUTPUT/depmod.log"
if [ -s "$OUTPUT/depmod.log" ]; then
    cat "$OUTPUT/depmod.log" >&2
    echo "Kernel module dependency/CRC validation produced diagnostics" >&2
    exit 1
fi
python3 "$ROOT/MANET/packaging/stage-kernel-payload.py" \
    --board cm4 --overlay "$OVERLAY" --stage "$OUTPUT"
ARCHIVE="$OUTPUT/cm4-kernel-$RELEASE.tar.gz"
tar --sort=name --mtime='@0' --owner=0 --group=0 --numeric-owner \
    -C "$OUTPUT" -cf - manet-kernel | gzip -n > "$ARCHIVE"
python3 "$ROOT/MANET/packaging/stage-kernel-payload.py" --check-archive "$ARCHIVE"
sha256sum "$ARCHIVE" "$OUTPUT/manet-kernel/manifest.json"
