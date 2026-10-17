# Required in every install and tools archive, including all board variants.
stage_kernel_protection() {
    local stage="$1" repo_root="$2"
    install -D -m 0644 \
        "$repo_root/MANET/etc/apt/preferences.d/00-manet-kernel" \
        "$stage/etc/apt/preferences.d/00-manet-kernel"
    install -D -m 0755 \
        "$repo_root/MANET/node_tools/manet-kernel-protect.py" \
        "$stage/usr/local/bin/manet-kernel-protect.py"
}
