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

TARBALL="$BUILD_DIR/wpa_supplicant-2.11.tar.gz"
if [ -n "${WPA_TARBALL:-}" ]; then
    cp -- "$WPA_TARBALL" "$TARBALL"
else
    curl --fail --location --retry 3 --proto '=https' --tlsv1.2 \
        https://w1.fi/releases/wpa_supplicant-2.11.tar.gz -o "$TARBALL"
fi
(cd "$BUILD_DIR" && sha256sum --check "$SOURCE_DIR/source.sha256")
tar -xzf "$TARBALL" -C "$BUILD_DIR"
TREE="$BUILD_DIR/wpa_supplicant-2.11"

while IFS= read -r patch_name; do
    patch --batch --fuzz=0 -d "$TREE" -p1 < "$SOURCE_DIR/patches/$patch_name"
done < "$SOURCE_DIR/patches/series"
cp "$SOURCE_DIR/.config" "$TREE/wpa_supplicant/.config"
make -C "$TREE/wpa_supplicant" -j "${JOBS:-2}" wpa_supplicant wpa_cli
install -s -m 0755 "$TREE/wpa_supplicant/wpa_supplicant" \
    "$OUTPUT/wpa_supplicant_manet"
install -s -m 0755 "$TREE/wpa_supplicant/wpa_cli" "$OUTPUT/wpa_cli_manet"
"$OUTPUT/wpa_supplicant_manet" -v
"$OUTPUT/wpa_cli_manet" -v
