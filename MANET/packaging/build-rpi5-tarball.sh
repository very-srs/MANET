#!/usr/bin/env bash
set -euo pipefail

OUT="${1:-rpi5-install.tar.gz}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
STAGE="$(mktemp -d)"

cleanup() {
    rm -rf "$STAGE"
}
trap cleanup EXIT

install_tree() {
    local src="$1"
    local dst="$2"
    if [ -d "$src" ]; then
        mkdir -p "$dst"
        cp -a "$src"/. "$dst"/
    fi
}

require_dir() {
    local path="$1"
    local label="$2"
    if [ ! -d "$path" ]; then
        echo "Missing $label directory: $path" >&2
        exit 1
    fi
}

install_file() {
    local mode="$1"
    local src="$2"
    local dst="$3"
    if [ -f "$src" ]; then
        install -D -m "$mode" "$src" "$dst"
    fi
}

mkdir -p "$STAGE/usr/local/bin" "$STAGE/usr/sbin" "$STAGE/etc/systemd/system"

install_tree "$REPO_ROOT/MANET/node_tools" "$STAGE/usr/local/bin"
chmod -R a+rX "$STAGE/usr/local/bin"
find "$STAGE/usr/local/bin" -type f \( -name '*.sh' -o -name '*.py' -o -name 'morse_cli' -o -name 'chronyc' \) -exec chmod 0755 {} +

install_file 0755 "$REPO_ROOT/MANET/binaries_arm64/alfred" "$STAGE/usr/sbin/alfred"
install_file 0755 "$REPO_ROOT/MANET/binaries_arm64/batctl" "$STAGE/usr/sbin/batctl"
install_file 0755 "$REPO_ROOT/MANET/binaries_arm64/wpa_cli_s1g" "$STAGE/usr/sbin/wpa_cli_s1g"
install_file 0755 "$REPO_ROOT/MANET/binaries_arm64/wpa_supplicant_s1g" "$STAGE/usr/sbin/wpa_supplicant_s1g"

install_tree "$REPO_ROOT/MANET/systemd" "$STAGE/etc/systemd/system"
install_tree "$REPO_ROOT/MANET/systemd-network" "$STAGE/etc/systemd/network"
install_tree "$REPO_ROOT/MANET/udev/rules.d" "$STAGE/etc/udev/rules.d"
install_tree "$REPO_ROOT/MANET/share/manet" "$STAGE/usr/local/share/manet"
install_tree "$REPO_ROOT/MANET/etc" "$STAGE/etc"

install_file 0644 "$REPO_ROOT/MANET/root/regulatory.db" "$STAGE/root/regulatory.db"

mkdir -p "$STAGE/root/networkd-dispatcher"
install_file 0755 "$REPO_ROOT/MANET/networkd-dispatcher/carrier" "$STAGE/root/networkd-dispatcher/carrier"
install_file 0755 "$REPO_ROOT/MANET/networkd-dispatcher/off" "$STAGE/root/networkd-dispatcher/off"

mkdir -p \
    "$STAGE/etc/networkd-dispatcher/carrier.d" \
    "$STAGE/etc/networkd-dispatcher/routable.d" \
    "$STAGE/etc/networkd-dispatcher/off.d" \
    "$STAGE/etc/networkd-dispatcher/no-carrier.d" \
    "$STAGE/etc/networkd-dispatcher/degraded.d"

cat > "$STAGE/etc/networkd-dispatcher/carrier.d/50-ethernet-detect" <<'EOF'
#!/bin/bash
set -euo pipefail

/usr/local/bin/manet-uplink-dispatch.sh carrier "${IFACE:-}"

if grep -qi '^auto_update=1' /etc/mesh.conf 2>/dev/null && ping -c 1 -W 2 -I "$IFACE" 8.8.8.8 >/dev/null 2>&1; then
    /usr/local/bin/node-update.sh --routine
fi
EOF

cat > "$STAGE/etc/networkd-dispatcher/routable.d/50-manet-uplink" <<'EOF'
#!/bin/bash
set -euo pipefail

/usr/local/bin/manet-uplink-dispatch.sh routable "${IFACE:-}"
EOF

install -m 0755 "$REPO_ROOT/MANET/networkd-dispatcher/off" "$STAGE/etc/networkd-dispatcher/off.d/50-gateway-disable"
install -m 0755 "$REPO_ROOT/MANET/networkd-dispatcher/off" "$STAGE/etc/networkd-dispatcher/no-carrier.d/50-gateway-disable"
install -m 0755 "$REPO_ROOT/MANET/networkd-dispatcher/off" "$STAGE/etc/networkd-dispatcher/degraded.d/50-gateway-disable"
chmod 0755 "$STAGE/etc/networkd-dispatcher/carrier.d/50-ethernet-detect" "$STAGE/etc/networkd-dispatcher/routable.d/50-manet-uplink"

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
        ln -s "../$unit" "$STAGE/etc/systemd/system/multi-user.target.wants/$unit"
    fi
done

if [ -n "${SBC_OVERLAY_DIR:-}" ]; then
    require_dir "$SBC_OVERLAY_DIR" "SBC overlay"
    install_tree "$SBC_OVERLAY_DIR" "$STAGE"
fi

if [ ! -e "$STAGE/lib" ]; then
    ln -s usr/lib "$STAGE/lib"
fi

if [ -f "$STAGE/etc/sudoers.d/perf" ]; then
    chmod 0440 "$STAGE/etc/sudoers.d/perf"
fi

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
