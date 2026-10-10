#!/bin/bash
# Point node-manager.sh at the ACS or static variant selected by mesh.conf.
# node-manager.service.d/select.conf runs this before every start. Swap the
# relative symlink atomically so callers never see a missing target.
# --service-start records the running variant in $RUN_DIR/node-manager.running
# for config apply to compare with the selected variant.

BIN_DIR="${MANET_BIN_DIR:-/usr/local/bin}"
MESH_CONF="${MANET_MESH_CONF:-/etc/mesh.conf}"
RUN_DIR="${MANET_RUN_DIR:-/run}"
# Print accepted arguments to stderr.
usage() {
    printf '%s\n' \
        'usage: node-manager-select.sh [--service-start]' >&2
}

[ $# -le 1 ] || { usage; exit 1; }

service_start=false
case "${1:-}" in
    --service-start) service_start=true ;;
    "") ;;
    *) usage; exit 1 ;;
esac

# Atomically save target under RUN_DIR only when service_start is true.
record_running() {
    [ "$service_start" = true ] || return 0
    printf '%s\n' "$target" > "$RUN_DIR/node-manager.running.new" &&
        mv -f "$RUN_DIR/node-manager.running.new" "$RUN_DIR/node-manager.running"
}

# Use the last unindented acs assignment if the config repeats the key.
acs=$(sed -n 's/^acs=//p' "$MESH_CONF" 2>/dev/null | tail -n 1)
# Writers use y/n. Also accept common hand-edited boolean spellings.
if [[ "$acs" =~ ^([Yy]|[Yy][Ee][Ss]|1|[Tt][Rr][Uu][Ee])[[:space:]]*$ ]]; then
    target=node-manager-acs.sh
else
    target=node-manager-static.sh
fi

if [ ! -x "$BIN_DIR/$target" ]; then
    printf '%s\n' "Cannot select $target: $BIN_DIR/$target is missing" >&2
    exit 1
fi

if [ "$(readlink "$BIN_DIR/node-manager.sh" 2>/dev/null)" = "$target" ]; then
    record_running
    exit
fi

# A private scratch directory per call: concurrent selections (setup, config
# apply and a service start can overlap) must not share a temporary name.
scratch=$(mktemp -d "$BIN_DIR/.node-manager-select.XXXXXX") || {
    printf '%s\n' "Cannot create a scratch directory in $BIN_DIR" >&2
    exit 1
}
trap 'rm -rf "$scratch"' EXIT
if ! ln -s "$target" "$scratch/node-manager.sh" ||
        ! mv -Tf "$scratch/node-manager.sh" "$BIN_DIR/node-manager.sh"; then
    printf '%s\n' "Cannot point node-manager.sh at $target" >&2
    exit 1
fi
printf '%s\n' "node-manager.sh -> $target"
record_running
