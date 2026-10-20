#!/usr/bin/env bash
set -euo pipefail

# Build into a separate output directory. Never install over a running daemon.
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT="${1:?Usage: build.sh OUTPUT_DIRECTORY}"
mkdir -p "$OUTPUT"
OUTPUT="$(cd -- "$OUTPUT" && pwd)"
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT

pkg-config --exists libnl-3.0 libnl-genl-3.0 libcap

while read -r name tag commit archive_sha; do
    case "$name" in ''|'#'*) continue ;; esac
    tree="$BUILD_DIR/$name"
    git init -q "$tree"
    git -C "$tree" fetch -q --depth=1 "${BATMAN_MIRROR:-https://git.open-mesh.org}/$name.git" \
        "refs/tags/$tag:refs/tags/$tag"
    git -C "$tree" checkout -q --detach "$tag"
    test "$(git -C "$tree" rev-parse HEAD)" = "$commit"
    got="$(git -C "$tree" archive --format=tar HEAD | sha256sum | cut -d' ' -f1)"
    test "$got" = "$archive_sha"
    version="${tag#v}"
done < "$SOURCE_DIR/sources.txt"

make -C "$BUILD_DIR/alfred" -j "${JOBS:-2}" \
    CONFIG_ALFRED_GPSD=n CONFIG_ALFRED_VIS=n alfred
make -C "$BUILD_DIR/batctl" -j "${JOBS:-2}" batctl
install -s -m 0755 "$BUILD_DIR/alfred/alfred" "$OUTPUT/alfred"
install -s -m 0755 "$BUILD_DIR/batctl/batctl" "$OUTPUT/batctl"
# alfred prints its usage after the version and exits 1.
("$OUTPUT/alfred" -v || true) | head -n 1 | grep -q " $version\$"
"$OUTPUT/batctl" -v | grep -q "^batctl $version "
