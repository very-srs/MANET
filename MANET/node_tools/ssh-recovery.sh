#!/bin/bash
# Keep headless SSH access available on provisioned MANET nodes.

set -u
. "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet-common.sh" || exit 1
manet_load_ssh_account || exit 1

# Print the SSH recovery message to stderr.
log() {
    printf '%s\n' "SSH-RECOVERY: $1" >&2
}

RADIO_PASSWORD=""
if [ -f /etc/mesh.conf ]; then
    # Preserve any equals signs within the stored password value.
    RADIO_PASSWORD=$(awk -F= '$1 == "radio_password" {print substr($0, index($0, "=") + 1); exit}' /etc/mesh.conf)
fi

if ! id -u "$MANET_SSH_USER" >/dev/null 2>&1; then
    log "Creating missing SSH user '$MANET_SSH_USER'"
    useradd -m -U -s /bin/bash -G sudo,adm,dialout,cdrom,audio,video,plugdev,games,users,input,netdev,gpio,i2c,spi "$MANET_SSH_USER" || exit 1
else
    usermod -aG sudo,adm,dialout,cdrom,audio,video,plugdev,games,users,input,netdev,gpio,i2c,spi "$MANET_SSH_USER" 2>/dev/null || true
fi

if [ -n "$RADIO_PASSWORD" ]; then
    printf '%s\n' "$MANET_SSH_USER:$RADIO_PASSWORD" | chpasswd
fi
passwd -u "$MANET_SSH_USER" 2>/dev/null || true

mkdir -p "$MANET_SSH_HOME/.ssh" /etc/ssh/sshd_config.d
chmod 700 "$MANET_SSH_HOME/.ssh"
chown -R "$MANET_SSH_USER:$MANET_SSH_GROUP" "$MANET_SSH_HOME/.ssh"
manet_write_ssh_sudoers || exit 1

# Enable password/public-key authentication and prohibit root passwords.
cat << EOF > /etc/ssh/sshd_config.d/10-manet.conf
PasswordAuthentication yes
KbdInteractiveAuthentication no
PubkeyAuthentication yes
MaxAuthTries 10
UsePAM yes
PermitRootLogin prohibit-password
EOF

# Generate missing host keys before enabling and restarting the daemon.
ssh-keygen -A 2>/dev/null || true
systemctl unmask ssh.service 2>/dev/null || true
systemctl enable ssh.service 2>/dev/null || systemctl enable ssh 2>/dev/null || true
systemctl restart ssh.service 2>/dev/null || systemctl restart ssh 2>/dev/null || true

# ss column four is the local endpoint, ending in :22 for either family.
if ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq '(^|:)22$'; then
    log "SSH is listening on port 22"
else
    log "WARNING: SSH is not listening on port 22 after recovery"
fi

exit 0
