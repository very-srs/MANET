#!/bin/sh

# Exit immediately if a command exits with a non-zero status.
set -e

if [ -z "$ETH_IFACE" ]; then
    ETH_IFACE=$(ip -o link show | awk -F': ' '{print $2}' | grep -E '^e|^en' | head -n1)
    echo "Detected Ethernet interface: $ETH_IFACE"
fi

if [ -z "$WIFI_IFACE" ]; then
    WIFI_IFACE=$(ip -o link show | awk -F': ' '{print $2}' | grep -E '^w' | head -n1)
    echo "Detected Wi-Fi interface: $WIFI_IFACE"
fi

echo "--- Starting network configuration ---"

echo "Flushing old IP configuration from $ETH_IFACE..."
ip addr flush dev "$ETH_IFACE"

echo "Bringing interfaces up..."
ip link set "$ETH_IFACE" up || { echo "Failed to bring up $ETH_IFACE"; exit 1; }
ip link set "$WIFI_IFACE" up || echo "Warning: Could not bring up $WIFI_IFACE (ensure WiFi is connected)"

#echo "Enabling IP forwarding..."
#echo 1 > /proc/sys/net/ipv4/ip_forward || { echo "Failed to enable IP forwarding"; exit 1; }

echo "Checking/assigning IP 10.30.1.1 to $ETH_IFACE..."
if ! ip addr show dev "$ETH_IFACE" | grep -q "10.30.1.1"; then
    ip addr flush dev "$ETH_IFACE" || echo "Warning: Could not flush $ETH_IFACE IPs"
    ip addr add 10.30.1.1/24 dev "$ETH_IFACE" || { echo "Failed to assign IP to $ETH_IFACE"; exit 1; }
fi

echo "Setting up iptables NAT rules..."
iptables -F FORWARD
iptables -t nat -F POSTROUTING
iptables -A FORWARD -i "$ETH_IFACE" -o "$WIFI_IFACE" -j ACCEPT
iptables -A FORWARD -i "$WIFI_IFACE" -o "$ETH_IFACE" -m state --state RELATED,ESTABLISHED -j ACCEPT
iptables -t nat -A POSTROUTING -o "$WIFI_IFACE" -j MASQUERADE


# --- SSL Certificate Generation ---
# Check if a certificate already exists in the persistent volume.
# If not, generate a new self-signed one.
if [ ! -f "/data/server.pem" ]; then
    echo "--- Generating self-signed SSL certificate ---"
    # Generate a key and a certificate valid for 10 years.
    openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -nodes \
      -keyout /data/server.key -out /data/server.crt \
      -subj "/CN=manet-config" \
      -addext "subjectAltName=IP:10.30.1.1"

    # Combine the key and certificate into a single .pem file, which lighttpd requires.
    cat /data/server.key /data/server.crt > /data/server.pem

    # Set secure permissions on the private key files.
    chmod 600 /data/server.key /data/server.pem
    echo "--- SSL certificate generated successfully ---"
fi

echo "Ensuring correct permissions for /data directory..."
chown -R lighttpd:lighttpd /data
chmod -R g+w /data

echo "Initializing log files..."
mkdir -p /var/log/lighttpd
touch /var/log/lighttpd/error.log
touch /var/log/lighttpd/access.log
chown -R lighttpd:lighttpd /var/log/lighttpd

echo "Configuring dnsmasq for interface $ETH_IFACE..."
sed -i "s/__ETH_IFACE__/$ETH_IFACE/g" /etc/dnsmasq.conf

echo "Starting dnsmasq in the background..."
dnsmasq

echo "Starting lighttpd in the background..."
lighttpd -f /etc/lighttpd/lighttpd.conf

echo "Starting log processor in the background..."
/log_processor.sh &

echo "--- Setup complete. Tailing logs. ---"
exec tail -F /var/log/lighttpd/error.log /var/log/lighttpd/access.log

