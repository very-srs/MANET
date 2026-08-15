#!/usr/bin/env bash
set -euo pipefail

# build-tools-tarball.sh — assemble a tools-only update tarball.
#
# Contains node_tools scripts, the version file, and the shared assets under
# share/manet that those scripts load at runtime (the dashboard logos). Does
# NOT include the SBC overlay (kernel/modules/firmware), systemd units,
# networkd configs, udev rules, or pre-built binaries (alfred, batctl,
# wpa_supplicant_s1g).
#
# Usage:
#   build-tools-tarball.sh [output.tar.gz]
#   Defaults to: tools.tar.gz

OUT="${1:-tools.tar.gz}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
STAGE="$(mktemp -d)"

cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

install_tree() {
    local src="$1" dst="$2"
    [ -d "$src" ] || return 0
    mkdir -p "$dst"
    cp -a "$src"/. "$dst"/
}

install_file() {
    local mode="$1" src="$2" dst="$3"
    [ -f "$src" ] && install -D -m "$mode" "$src" "$dst"
}

mkdir -p "$STAGE/usr/local/bin" "$STAGE/etc"

install_tree "$REPO_ROOT/MANET/node_tools" "$STAGE/usr/local/bin"
chmod -R a+rX "$STAGE/usr/local/bin"
find "$STAGE/usr/local/bin" -type f \
    \( -name '*.sh' -o -name '*.py' -o -name 'morse_cli' -o -name 'chronyc' \) \
    -exec chmod 0755 {} +

install_file 0644 "$REPO_ROOT/MANET/etc/manet_version.txt" "$STAGE/etc/manet_version.txt"

# mesh-status.py serves these from /usr/local/share/manet
# at runtime, so an OTA update that ships the scripts without them leaves the
# dashboards with broken assets.
install_tree "$REPO_ROOT/MANET/share/manet" "$STAGE/usr/local/share/manet"
chmod -R a+rX "$STAGE/usr/local/share/manet" 2>/dev/null || true

mkdir -p "$(dirname "$OUT")"
OUT_ABS="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"

# Directory modes are recorded in the archive and applied to the target by an
# extractor running as root, so they have to be right here. Two rules:
#
#  1. Never emit an entry for the stage root. Tar stores its mode against "./"
#     and restores it onto the extraction directory — which is "/" on a node.
#     mktemp -d gives 0700, so shipping "./" once set / to 0700 and locked
#     every non-root process out of the filesystem (no traversal, no shared
#     libraries, no ssh logins). Archive the contents instead.
#  2. Strip group/other write. These dirs inherit the build machine's umask,
#     and a umask of 002 ships /usr, /etc and /usr/local as 0775 root:root.
# Bytecode compiled on the build machine has no business on a node.
find "$STAGE" -type d -name __pycache__ -prune -exec rm -rf {} +

find "$STAGE" -type d -exec chmod go-w {} +
( cd "$STAGE" && find . -mindepth 1 -maxdepth 1 -printf './%P\0' \
    | tar --owner=0 --group=0 --numeric-owner --null -T - -czf "$OUT_ABS" )
echo "Built: $OUT  ($(du -sh "$OUT" | cut -f1))"
