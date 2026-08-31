#!/usr/bin/env bash
set -euo pipefail

# build-tools-tarball.sh — assemble a tools-only update tarball.
#
# Contains node_tools scripts, the version file, the shared assets under
# share/manet that those scripts load at runtime (the dashboard logos), the
# systemd units and their enable symlinks, udev rules, and the
# networkd-dispatcher hooks.
#
# The archive extracts to / on the node, so it can carry any file the node
# needs — units, rules, modules, or a one-shot service that runs a script and
# deletes itself. That is how a fielded board picks up something new (voice, a
# migration, a fix) without being reflashed. An earlier version of this comment
# claimed units and udev rules were out of scope; that described what it
# happened to pack, not a limit on what it can.
#
# Still excluded, deliberately:
#   * the SBC overlay (kernel, modules, firmware) — that is what -install is for
#   * pre-built binaries (alfred, batctl, wpa_supplicant_s1g) — size, and they
#     change far less often than the scripts. The Lyra plugin is the exception
#     and is carried: voice_codec now defaults to lyra, so a fielded node
#     without it is deaf and mute on a lyra mesh, which is a functional
#     regression rather than a missing nicety.
#   * systemd-network configs — rewriting a live node's interface definitions
#     from under it is a different risk class to dropping in a new unit, and
#     belongs in a considered migration rather than every routine update
#
# networkd-dispatcher hooks ARE carried (they were not, until 2026-08-31). They
# are scripts under /etc, not board-specific artifacts, and the rule for this
# archive is that anything which should go out can go in it — the exceptions
# are board-specific files such as config.txt, and the interface definitions
# noted above.
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

# Persistent journal. Carried by the tools tarball as well as the install one so
# that a fielded board picks it up without being reflashed — a node that resets
# is exactly the node whose logs you want, and until this lands its journal is
# in /run and every reset erases the evidence. Takes effect at the next boot,
# like the units below.
install_file 0644 \
    "$REPO_ROOT/MANET/etc/systemd/journald.conf.d/99-manet-persistent.conf" \
    "$STAGE/etc/systemd/journald.conf.d/99-manet-persistent.conf"

# Units and udev rules. node-update.sh only extracts -- it does not run
# daemon-reload or udevadm -- so a new unit becomes active at the next boot via
# the enable symlink below, and anything needing to happen sooner does it from
# a one-shot (see manet-voice-setup.service).
install_tree "$REPO_ROOT/MANET/systemd" "$STAGE/etc/systemd/system"
install_tree "$REPO_ROOT/MANET/udev/rules.d" "$STAGE/etc/udev/rules.d"

# networkd-dispatcher hooks, the same set the install builders stage. A hook is
# an ordinary script -- there is no reason a routine update should not carry a
# fix to one, and until it did, a bad hook could only be corrected by a
# reflash. That mattered: the auto_update gate spent its whole life testing for
# a value nothing ever wrote, and the only mechanism that could have shipped
# the fix was the OTA update that same bug disabled.
. "$SCRIPT_DIR/lib-dispatcher.sh"
stage_dispatcher_hooks "$STAGE" "$REPO_ROOT"

mkdir -p "$STAGE/etc/systemd/system/multi-user.target.wants"
for unit in \
    ethernet-autodetect.service \
    manet-txpower.service \
    mesh-default-route-fix.service \
    sae-watchdog.service \
    ebtables-restore.service \
    mesh-voice.service \
    manet-voice-setup.service
do
    if [ -f "$STAGE/etc/systemd/system/$unit" ]; then
        ln -sf "../$unit" "$STAGE/etc/systemd/system/multi-user.target.wants/$unit"
    fi
done

# Lyra codec plugin and model weights. Carried here as well as in the install
# tarball so a fielded board picks up the codec on a routine update instead of
# needing a reflash — which is the whole point of this archive.
LYRA_SRC="$REPO_ROOT/MANET/lyra_arm64"
if [ -f "$LYRA_SRC/libgstlyra.so" ] && [ -d "$LYRA_SRC/model_coeffs" ]; then
    install_file 0644 "$LYRA_SRC/libgstlyra.so" \
        "$STAGE/usr/lib/aarch64-linux-gnu/gstreamer-1.0/libgstlyra.so"
    install_tree "$LYRA_SRC/model_coeffs" "$STAGE/usr/local/share/lyra/model_coeffs"
    chmod -R a+rX "$STAGE/usr/local/share/lyra" 2>/dev/null || true
    echo "Lyra codec: plugin + model weights staged"
else
    echo "WARNING: MANET/lyra_arm64/{libgstlyra.so,model_coeffs/} missing — this" >&2
    echo "         update will not carry the Lyra codec; nodes fall back to opus." >&2
fi

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
