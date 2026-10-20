#!/bin/bash
set -euo pipefail

# A disposable source snapshot and a prepared build for the pinned bench Image
# are required. This builds modules only and never installs them on a node.
if [ "$#" != 3 ]; then
    echo "usage: $0 SOURCE PREPARED_BUILD OUTPUT" >&2
    exit 2
fi
SERIES=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
TREE=$(realpath "$1")
BUILD=$(realpath "$2")
OUTPUT=$(realpath -m "$3")
CROSS=${CROSS_COMPILE:-aarch64-linux-gnu-}
export KBUILD_BUILD_USER=build KBUILD_BUILD_HOST=manet KBUILD_BUILD_TIMESTAMP='' LOCALVERSION=''
test "$(git -C "$TREE" rev-parse HEAD)" = 2ff600d29ee478d0a8d46ef8a58afb5e038fe692
test "$(cat "$BUILD/include/config/kernel.release")" = 6.18.0-manet
python3 "$SERIES/apply.py" "$TREE"
make -C "$BUILD" M="$TREE/net/wireless" ARCH=arm64 CROSS_COMPILE="$CROSS" -j"${JOBS:-2}" modules
mkdir -p "$OUTPUT"
# Preserve the cfg80211 provider path from the prepared symbol table.
awk '$2 ~ /^cfg80211_hold_.*_v[23]$/ {printf "%s\t%s\tnet/wireless/cfg80211\t%s\t%s\n", $1, $2, $4, $5}' \
    "$TREE/net/wireless/Module.symvers" > "$OUTPUT/hold.symvers"
make -C "$BUILD" M="$TREE/drivers/net/wireless/mediatek/mt76/mt7915" \
    KBUILD_EXTRA_SYMBOLS="$OUTPUT/hold.symvers" ARCH=arm64 CROSS_COMPILE="$CROSS" -j"${JOBS:-2}" modules
make -C "$BUILD" M="$TREE/net/mac80211" ARCH=arm64 CROSS_COMPILE="$CROSS" -j"${JOBS:-2}" modules
cp "$TREE/net/mac80211/mac80211.ko" "$OUTPUT/mac80211.ko"
cp "$TREE/net/wireless/cfg80211.ko" "$OUTPUT/cfg80211.ko"
cp "$TREE/drivers/net/wireless/mediatek/mt76/mt7915/mt7915e.ko" "$OUTPUT/mt7915e.ko"
python3 "$SERIES/check-pair.py" --tree "$TREE" --build "$BUILD" \
    --cfg80211 "$OUTPUT/cfg80211.ko" --driver "$OUTPUT/mt7915e.ko" \
    --extra-symvers "$OUTPUT/hold.symvers" --mac80211 "$OUTPUT/mac80211.ko" --output "$OUTPUT/verification.json"
(cd "$OUTPUT" && sha256sum cfg80211.ko mt7915e.ko mac80211.ko > SHA256SUMS)
