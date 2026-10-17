# Optional release inputs must be supplied together; never silently omit one.
stage_release_payload() {
    local stage="$1" repo_root="$2" board="${3:-}"
    if [ -n "${MANET_HOSTAPD_DEB:-}${MANET_REGDB:-}" ]; then
        : "${MANET_HOSTAPD_DEB:?Provide the native Debian hostapd package}"
        [ -n "$board" ] || { echo 'Set MANET_PACKAGE_BOARD for release inputs' >&2; return 1; }
        local regdb_args=()
        if [ "$board" = cm4 ]; then
            : "${MANET_REGDB:?Provide the DFS-tags-only regulatory database}"
            regdb_args=(--regdb "$MANET_REGDB")
        fi
        python3 "$repo_root/MANET/packaging/stage-release-payload.py" \
            --stage "$stage" --hostapd "$MANET_HOSTAPD_DEB" --board "$board" "${regdb_args[@]}"
    fi
}
