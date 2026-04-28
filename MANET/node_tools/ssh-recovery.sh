#!/bin/bash
# Keep headless SSH access available on provisioned MANET nodes.

set -u

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - SSH-RECOVERY: $1"
}

RADIO_PASSWORD=""
if [ -f /etc/mesh.conf ]; then
    RADIO_PASSWORD=$(awk -F= '$1 == "radio_password" {print substr($0, index($0, "=") + 1); exit}' /etc/mesh.conf)
fi

if ! id -u radio >/dev/null 2>&1; then
    log "Creating missing radio user"
    useradd -m -s /bin/bash -G sudo,adm,dialout,cdrom,audio,video,plugdev,games,users,input,netdev,gpio,i2c,spi radio
else
    usermod -aG sudo,adm,dialout,cdrom,audio,video,plugdev,games,users,input,netdev,gpio,i2c,spi radio 2>/dev/null || true
fi

if [ -n "$RADIO_PASSWORD" ]; then
    echo "radio:$RADIO_PASSWORD" | chpasswd
fi
passwd -u radio 2>/dev/null || true

mkdir -p /home/radio/.ssh /etc/ssh/sshd_config.d
chmod 700 /home/radio/.ssh
chown -R radio:radio /home/radio/.ssh

cat << EOF > /etc/ssh/sshd_config.d/10-manet.conf
PasswordAuthentication yes
KbdInteractiveAuthentication no
PubkeyAuthentication yes
UsePAM yes
PermitRootLogin prohibit-password
EOF

ssh-keygen -A 2>/dev/null || true
systemctl unmask ssh.service 2>/dev/null || true
systemctl enable ssh.service 2>/dev/null || systemctl enable ssh 2>/dev/null || true
systemctl restart ssh.service 2>/dev/null || systemctl restart ssh 2>/dev/null || true

if ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq '(^|:)22$'; then
    log "SSH is listening on port 22"
else
    log "WARNING: SSH is not listening on port 22 after recovery"
fi

exit 0
