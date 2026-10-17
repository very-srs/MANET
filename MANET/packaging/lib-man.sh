# Shared section 8 manual-page staging for install and tools archives.
# Usage: stage_man_pages <STAGE> <REPO_ROOT>

stage_man_pages() {
    local stage="$1" repo_root="$2"
    local source_dir="$repo_root/MANET/man/man8"
    local destination="$stage/usr/local/share/man/man8"

    install -d -m 0755 "$destination"
    install -m 0644 "$source_dir"/*.8 "$destination/"
}
