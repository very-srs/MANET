#!/usr/bin/env bash
set -euo pipefail

# Build into a separate output directory. Never install over a live supplicant.
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT="${1:?Usage: build.sh OUTPUT_DIRECTORY}"
mkdir -p "$OUTPUT"
OUTPUT="$(cd -- "$OUTPUT" && pwd)"
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT

pkg-config --exists libnl-3.0 libnl-genl-3.0 openssl

TARBALL="$BUILD_DIR/morse-hostap-1.16.4.tar"
if [ -n "${MORSE_TARBALL:-}" ]; then
    cp -- "$MORSE_TARBALL" "$TARBALL"
else
    git clone --quiet --depth 1 --branch 1.16.4 \
        https://github.com/MorseMicro/hostap.git "$BUILD_DIR/hostap"
    test "$(git -C "$BUILD_DIR/hostap" rev-parse HEAD)" = \
        a1bc239be2caef2dfd0a45757d030ab0c589387d
    git -C "$BUILD_DIR/hostap" archive --format=tar \
        --prefix=hostap-1.16.4/ HEAD > "$TARBALL"
fi
(cd "$BUILD_DIR" && sha256sum --check "$SOURCE_DIR/source.sha256")
tar -xf "$TARBALL" -C "$BUILD_DIR"
TREE="$BUILD_DIR/hostap-1.16.4"

while IFS= read -r patch_name; do
    patch --batch --fuzz=0 -d "$TREE" -p1 < "$SOURCE_DIR/patches/$patch_name"
done < "$SOURCE_DIR/patches/series"
cp "$SOURCE_DIR/.config" "$TREE/wpa_supplicant/.config"
make -C "$TREE/wpa_supplicant" -j "${JOBS:-2}" MORSE_VERSION=1.16.4 \
    wpa_supplicant_s1g wpa_cli_s1g
install -m 0755 "$TREE/wpa_supplicant/wpa_supplicant_s1g" \
    "$OUTPUT/wpa_supplicant_s1g_candidate"
install -m 0755 "$TREE/wpa_supplicant/wpa_cli_s1g" \
    "$OUTPUT/wpa_cli_s1g_candidate"
"$OUTPUT/wpa_supplicant_s1g_candidate" -v
"$OUTPUT/wpa_supplicant_s1g_candidate" -vmesh
"$OUTPUT/wpa_cli_s1g_candidate" -v
