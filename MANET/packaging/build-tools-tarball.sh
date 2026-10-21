#!/usr/bin/env bash
set -euo pipefail

# build-tools-tarball.sh - assemble a field update tarball.
#
# Contains node_tools scripts, the version file, the shared assets under
# share/manet that those scripts load at runtime (the dashboard logos), the
# systemd units and their enable symlinks, udev rules, and the
# networkd-dispatcher hooks.
#
# The updater verifies, stages, and installs root-relative files. The archive
# can carry anything a release needs: kernels, DTBs, modules, firmware,
# binaries, services, interface definitions, and configuration migrations.
# Sealed field radios must be maintainable without routine reflashing.
# See docs/node-tools-internals.md, "Field update policy", for activation and
# recovery requirements when adding new payloads.
#
# Current payload choices: no SBC kernel overlay, config.txt, systemd-network
# definitions, or S1G binaries. These are not archive limits.
# Managed MT7916 firmware and the Wi-Fi mesh binaries are already carried.
# Optional matched kernel inputs use MANET_KERNEL_BOARD and SBC_OVERLAY_DIR.
# CM4 derives a local trial profile and defaults on, with an operator opt-out.
# HaLow candidate binaries remain excluded from published payloads.
#
# Usage:
#   build-tools-tarball.sh [output.tar.gz]
#   Defaults to: tools.tar.gz

OUT="${1:-tools.tar.gz}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
STAGE="$(mktemp -d)"
BOOTSTRAP_WORK=""

cleanup() { rm -rf "$STAGE"; [ -z "$BOOTSTRAP_WORK" ] || rm -rf "$BOOTSTRAP_WORK"; }
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
# Tests, their data and the build host's bytecode never reach a node.
rm -rf "$STAGE"/usr/local/bin/test_*.py "$STAGE"/usr/local/bin/{fixtures,testdata,__pycache__}

# node-manager.sh is a symlink made on the node by node-manager-select.sh,
# before every node-manager start, from acs= in mesh.conf. Never ship a file
# by that name: it would replace the symlink with one fixed variant.
rm -f "$STAGE/usr/local/bin/node-manager.sh"

chmod -R a+rX "$STAGE/usr/local/bin"
find "$STAGE/usr/local/bin" -type f \
    \( -name '*.sh' -o -name '*.py' -o -name 'morse_cli' -o -name 'chronyc' \) \
    -exec chmod 0755 {} +

install_file 0644 "$REPO_ROOT/MANET/etc/manet_version.txt" "$STAGE/etc/manet_version.txt"

# Persistent journal. Carried by the tools tarball as well as the install one so
# that a fielded board picks it up without being reflashed - a node that resets
# is exactly the node whose logs you want, and until this lands its journal is
# in /run and every reset erases the evidence. Takes effect at the next boot,
# like the units below.
install_file 0644 \
    "$REPO_ROOT/MANET/etc/systemd/journald.conf.d/99-manet-persistent.conf" \
    "$STAGE/etc/systemd/journald.conf.d/99-manet-persistent.conf"

# Units and udev rules. The updater reloads systemd and restarts the status
# and node-manager services. Other newly enabled units start at the next boot.
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

. "$SCRIPT_DIR/lib-supplicant.sh"
stage_mesh_supplicant "$STAGE" "$REPO_ROOT"

# alfred and batctl match the batman-adv module in the kernel payload. The
# updater restarts alfred.
install_file 0755 "$REPO_ROOT/MANET/binaries_arm64/alfred" "$STAGE/usr/sbin/alfred"
install_file 0755 "$REPO_ROOT/MANET/binaries_arm64/batctl" "$STAGE/usr/sbin/batctl"

. "$SCRIPT_DIR/lib-man.sh"
stage_man_pages "$STAGE" "$REPO_ROOT"
. "$SCRIPT_DIR/lib-notices.sh"
stage_notices "$STAGE" "$REPO_ROOT"

mkdir -p "$STAGE/etc/systemd/system/timers.target.wants"
ln -sf ../manet-auto-update.timer \
    "$STAGE/etc/systemd/system/timers.target.wants/manet-auto-update.timer"

mkdir -p "$STAGE/etc/systemd/system/multi-user.target.wants"
for unit in \
    manet-auto-update.path \
    ethernet-autodetect.service \
    sae-watchdog.service \
    manet-dhcp-isolation.service \
    manet-dfs-watch.service \
    manet-atak.service \
    manet-mesh-power.service \
    manet-cpu-policy.service \
    mesh-voice.service \
    manet-voice-setup.service \
    manet-led-status.service
do
    if [ -f "$STAGE/etc/systemd/system/$unit" ]; then
        ln -sf "../$unit" "$STAGE/etc/systemd/system/multi-user.target.wants/$unit"
    fi
done

# Lyra codec plugin and model weights. Carried here as well as in the install
# tarball so a fielded board picks up the codec on a routine update instead of
# needing a reflash - which is the whole point of this archive.
LYRA_SRC="$REPO_ROOT/MANET/lyra_arm64"
if [ -f "$LYRA_SRC/libgstlyra.so" ] && [ -d "$LYRA_SRC/model_coeffs" ]; then
    install_file 0644 "$LYRA_SRC/libgstlyra.so" \
        "$STAGE/usr/lib/aarch64-linux-gnu/gstreamer-1.0/libgstlyra.so"
    install_tree "$LYRA_SRC/model_coeffs" "$STAGE/usr/local/share/lyra/model_coeffs"
    chmod -R a+rX "$STAGE/usr/local/share/lyra" 2>/dev/null || true
    echo "Lyra codec: plugin + model weights staged"
else
    echo "WARNING: MANET/lyra_arm64/{libgstlyra.so,model_coeffs/} missing - this" >&2
    echo "         update will not carry the Lyra codec; nodes fall back to opus." >&2
fi

# mesh-status.py serves these from /usr/local/share/manet
# at runtime, so an OTA update that ships the scripts without them leaves the
# dashboards with broken assets.
install_tree "$REPO_ROOT/MANET/share/manet" "$STAGE/usr/local/share/manet"
chmod -R a+rX "$STAGE/usr/local/share/manet" 2>/dev/null || true

# Matched stock MT7916 set and early-probe/config-change integration.
python3 "$SCRIPT_DIR/stage-mt7916-firmware.py" "$STAGE"
mkdir -p "$STAGE/etc/systemd/system/sysinit.target.wants"
ln -sf ../manet-mt7916-firmware.service \
    "$STAGE/etc/systemd/system/sysinit.target.wants/manet-mt7916-firmware.service"
ln -sf ../manet-morse-bcf.service \
    "$STAGE/etc/systemd/system/sysinit.target.wants/manet-morse-bcf.service"
ln -sf ../manet-mt7916-firmware.path \
    "$STAGE/etc/systemd/system/multi-user.target.wants/manet-mt7916-firmware.path"
ln -sf ../manet-mt7916-firmware-survived.service \
    "$STAGE/etc/systemd/system/multi-user.target.wants/manet-mt7916-firmware-survived.service"

. "$SCRIPT_DIR/lib-kernel-protection.sh"
stage_kernel_protection "$STAGE" "$REPO_ROOT"

. "$SCRIPT_DIR/lib-release-payload.sh"
PACKAGE_BOARD="${MANET_PACKAGE_BOARD:-${MANET_KERNEL_BOARD:-}}"
if [ -z "$PACKAGE_BOARD" ]; then
    case "$(basename "$OUT")" in
        cm4-*) PACKAGE_BOARD=cm4 ;;
        r3a-*) PACKAGE_BOARD=r3a ;;
        rpi5-*) PACKAGE_BOARD=rpi5 ;;
    esac
fi
stage_release_payload "$STAGE" "$REPO_ROOT" "$PACKAGE_BOARD"

# Kernel-less packages remain the default on every board.
if [ -n "${MANET_KERNEL_BOARD:-}" ]; then
    KERNEL_OVERLAY="${SBC_OVERLAY_DIR:-$REPO_ROOT/kernel-work/packages/$MANET_KERNEL_BOARD-sbc-overlay}"
    python3 "$SCRIPT_DIR/stage-kernel-payload.py" --board "$MANET_KERNEL_BOARD" \
        --overlay "$KERNEL_OVERLAY" --stage "$STAGE"
fi

mkdir -p "$(dirname "$OUT")"
OUT_ABS="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"

# Directory modes are recorded in the archive and applied to the target by an
# extractor running as root, so they have to be right here. Two rules:
#
#  1. Never emit an entry for the stage root. Tar stores its mode against "./"
#     and restores it onto the extraction directory - which is "/" on a node.
#     mktemp -d gives 0700, so shipping "./" once set / to 0700 and locked
#     every non-root process out of the filesystem (no traversal, no shared
#     libraries, no ssh logins). Archive the contents instead.
#  2. Strip group/other write. These dirs inherit the build machine's umask,
#     and a umask of 002 ships /usr, /etc and /usr/local as 0775 root:root.
#     Files too: logrotate refuses a group-writable config.
# Bytecode compiled on the build machine has no business on a node.
find "$STAGE" -type d -name __pycache__ -prune -exec rm -rf {} +

find "$STAGE" ! -type l -exec chmod go-w {} +
if [ "${MANET_LEGACY_BOOTSTRAP:-0}" = 1 ]; then
    [ "${MANET_KERNEL_BOARD:-}" = cm4 ] || {
        echo "The v0.569 bootstrap requires MANET_KERNEL_BOARD=cm4" >&2
        exit 1
    }
    BOOTSTRAP_WORK="$(mktemp -d)"
    INNER="$BOOTSTRAP_WORK/cm4-tools.tar.gz"
    ( cd "$STAGE" && find . -mindepth 1 -maxdepth 1 -printf './%P\0' \
        | tar --owner=0 --group=0 --numeric-owner --null -T - -czf "$INNER" )
    python3 "$SCRIPT_DIR/stage-kernel-payload.py" --check-archive "$INNER"
    python3 "$SCRIPT_DIR/stage-release-bootstrap.py" --archive "$INNER" --stage "$STAGE"
    find "$STAGE" ! -type l -exec chmod go-w {} +
fi
( cd "$STAGE" && find . -mindepth 1 -maxdepth 1 -printf './%P\0' \
    | tar --owner=0 --group=0 --numeric-owner --null -T - -czf "$OUT_ABS" )
python3 "$SCRIPT_DIR/stage-kernel-payload.py" --check-archive "$OUT_ABS"
if [ "${MANET_LEGACY_BOOTSTRAP:-0}" = 1 ]; then
    python3 "$SCRIPT_DIR/stage-kernel-payload.py" --check-archive "$OUT_ABS" --legacy
fi
# Publish the checksum only after the archive has been written successfully.
# Use the final basename so sha256sum -c works in the download directory.
( cd "$(dirname "$OUT_ABS")" && sha256sum -- "$(basename "$OUT_ABS")" ) > "$OUT_ABS.sha256.tmp"
mv -f -- "$OUT_ABS.sha256.tmp" "$OUT_ABS.sha256"
echo "Checksum: $OUT.sha256"
echo "Built: $OUT  ($(du -sh "$OUT" | cut -f1))"
