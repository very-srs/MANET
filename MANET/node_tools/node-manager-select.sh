#!/bin/bash
# Point node-manager.sh at the orchestrator mesh.conf selects: the ACS
# (automatic channel) variant when acs is on, the static variant otherwise.
#
# Runs before every node-manager start (node-manager.service.d/select.conf),
# so the choice follows mesh.conf at each boot and each restart. A relative
# symlink, swapped in atomically, so nothing ever sees a missing or
# half-written node-manager.sh.
#
# --service-start (the service's ExecStartPre only) also records which
# variant this start runs in $RUN_DIR/node-manager.running, so a config apply
# can tell whether the running manager matches the selection.

BIN_DIR="${MANET_BIN_DIR:-/usr/local/bin}"
MESH_CONF="${MANET_MESH_CONF:-/etc/mesh.conf}"
RUN_DIR="${MANET_RUN_DIR:-/run}"
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

record_running() {
    [ "$service_start" = true ] || return 0
    printf '%s\n' "$target" > "$RUN_DIR/node-manager.running.new" &&
        mv -f "$RUN_DIR/node-manager.running.new" "$RUN_DIR/node-manager.running"
}

acs=$(sed -n 's/^acs=//p' "$MESH_CONF" 2>/dev/null | tail -n 1)
# Every writer produces y/n; stay liberal about a hand-edited mesh.conf.
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
