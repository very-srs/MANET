#!/usr/bin/env bash
# Include the Wi-Fi mesh supplicant in install and tools archives.
stage_mesh_supplicant() {
    local stage="$1" repo="$2" name
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
