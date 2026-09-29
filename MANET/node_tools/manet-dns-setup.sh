#!/bin/bash
# Shared by provisioning and dnsmasq startup, including tools updates.
set -euo pipefail

exec 9>/run/manet-dns-setup.lock
flock -w 10 9
mkdir -p /etc/systemd/resolved.conf.d
config=/etc/systemd/resolved.conf.d/60-manet-dns.conf
scratch=$(mktemp "${config}.XXXXXX")
trap 'rm -f "$scratch"' EXIT
cat > "$scratch" <<'EOF'
[Resolve]
# DHCP/RA servers take precedence. Never race them against public resolvers.
DNS=
FallbackDNS=1.1.1.1 8.8.8.8
EOF
changed=0
if ! cmp -s "$scratch" "$config"; then
    chmod 644 "$scratch"
    mv "$scratch" "$config"
    changed=1
fi

# resolved recognizes its own upstream file and does not import its contents
# as static DNS. dnsmasq reads the same list, without using a localhost stub.
if [[ "$(readlink /etc/resolv.conf || true)" != /run/systemd/resolve/resolv.conf ]]; then
    ln -sfn /run/systemd/resolve/resolv.conf /etc/resolv.conf
    changed=1
fi
systemctl is-enabled --quiet systemd-resolved.service || systemctl enable systemd-resolved.service
if (( changed )); then
    systemctl restart systemd-resolved.service
else
    systemctl start systemd-resolved.service
fi
