#!/usr/bin/env bash
# Include the Wi-Fi mesh supplicant in install and tools archives.
stage_mesh_supplicant() {
    local stage="$1" repo="$2" name
    local dfs_enabled="${MANET_DFS_ENABLED:-1}"
    case "$dfs_enabled" in 0|1) ;; *) echo 'MANET_DFS_ENABLED must be 0 or 1' >&2; return 1;; esac
    if [ "$dfs_enabled" = 1 ]; then
        # Cross packaging cannot execute ARM64 here. Check the exact version
        # and live control ABI embedded by the paired 0007/0008 source build.
        if ! python3 - "$repo/MANET/binaries_arm64/wpa_supplicant_manet" <<'PY'
from pathlib import Path
import sys
try:
    data = Path(sys.argv[1]).read_bytes()
except OSError:
    data = b''
sys.exit(0 if all(s in data for s in (
    b'wpa_supplicant v2.11-manet-dfs3\n', b'MANET_DFS_V3 owner=%d\n\0',
    b'MANET_DFS_OWNER 1\0',
    b'nl80211: MANET external background CAC event %d on %d MHz\0')) else 1)
PY
        then
            echo 'DFS requires the native Debian wpa_supplicant v2.11-manet-dfs3 build (0007/0008).' >&2
            return 1
        fi
    fi
    install -d -m 0755 "$stage/usr/local/share/manet"
    printf '%s\n' "$dfs_enabled" > "$stage/usr/local/share/manet/dfs-enabled"
    for name in wpa_supplicant_manet wpa_cli_manet; do
        if [ -f "$repo/MANET/binaries_arm64/$name" ]; then
            install -D -m 0755 "$repo/MANET/binaries_arm64/$name" \
                "$stage/usr/sbin/$name"
        else
            printf 'WARNING: %s is missing; nodes use the Debian fallback.\n' \
                "$name" >&2
        fi
    done
}
