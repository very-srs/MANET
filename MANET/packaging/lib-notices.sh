# Shared license and third-party notice staging for install and tools archives.
# Usage: stage_notices <STAGE> <REPO_ROOT>

stage_notices() {
    local stage="$1" repo_root="$2"
    local destination="$stage/usr/local/share/doc/manet"

    install -d -m 0755 "$destination"
    install -m 0644 "$repo_root/LICENSE" "$repo_root/AUTHORS" \
        "$repo_root/THIRD_PARTY_NOTICES" "$destination/"
}
