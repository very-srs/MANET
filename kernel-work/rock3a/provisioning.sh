#!/bin/bash
#  provisioning for the radxa rock 3a board


exec > >(tee -a /var/log/provisioning.log) 2>&1
set -x

# Check for config file
if [ -f /boot/mesh-config ]; then
    echo "Found mesh configuration, applying..."
    source /boot/mesh-config

    # Set hostname
    hostnamectl hostname radio-$NODE_ID

    # Clean up config file for security
    rm /boot/mesh-config
else
    echo "No configuration found, using defaults"
fi

# set up persistent logging, this is not working
chown root:systemd-journal /var/log/journal
chmod 2755 /var/log/journal
systemctl restart systemd-journald

# Set up SSH directory
mkdir -p /home/radio/.ssh
chmod 700 /home/radio/.ssh
chown -R radio:radio /home/radio/.ssh
echo "User 'radio' created successfully"

systemctl enable --now ssh

cat > /usr/local/bin/provision-mesh.sh << 'PROVISIONEOF'
#!/bin/bash
#log the output of the provisioning script
exec > >(tee -a /var/log/2nd-provision.log) 2>&1
set -x
echo "=== provision-mesh-node.sh starting at $(date) ==="

# This is where the wifi regulatory country is set
REG=US

# Calculate unique hostname from MAC address
HOST_MAC=$(ip a | grep -A1 $(networkctl | grep -v bat | awk '/ether/ {print $2}' | head -1) \
   | awk '/ether/ {print $2}' | cut -d':' -f 5-6 | sed 's/://g')

echo "Waiting for internet connectivity..."
TIMEOUT=300
ELAPSED=0
while [ $ELAPSED -lt $TIMEOUT ]; do
	if ping -c 1 -W 2 8.8.8.8 > /dev/null 2>&1; then
		echo "Internet connectivity confirmed!"
		break
	fi
	echo "Waiting for internet... (${ELAPSED}s)"
	sleep 5
	ELAPSED=$((ELAPSED + 5))
done

if [ $ELAPSED -ge $TIMEOUT ]; then
	echo "ERROR: No internet after ${TIMEOUT}s"
	exit 1
fi

# A basic method to set the system time
date -s "$(curl -sI google.com | grep -i ^Date: | cut -d' ' -f2-)"

cd /root

# Pull down a .tar file that consists of all the various additionaly tools and scripts that will be used
# These are mesh tools in /usr/local/bin, the boot config.txt, batctl and alfred, and some systemd service
# files.  Here we can also differentiate between hardware types if he need different setup options.
# This tar file also contains the manet kernel, modified to include the drivers for the various radios used

#this will be a github release file for a public URL, currently set to a local ip for testing

#wget -q  https://github.com/very-srs/MANET/blob/main/kernel-work/cm4-install.tar.gz -O /root/morse-pi-install.tar.gz || {
#wget -q  http://192.168.69.1:8081/rock-install.tar.gz -O /root/morse-pi-install.tar.gz || {
#	echo "ERROR: Failed to download tar package"
#				exit 1
#		}
#tar -zxf /root/morse-pi-install.tar.gz -C /

#
#  Setup base system
#


# get the sources up to date and install packages
echo -n "Updating system packages..."
apt update > /dev/null 2>&1
apt upgrade -y > /dev/null 2>&1

# Remove the question about the iperf daemon during apt install
echo "iperf3 iperf3/start_daemon boolean true" | debconf-set-selections

# This isn't needed for the manet kernel and causes errors
chmod -x /etc/kernel/postinst.d/initramfs-tools

# Install packages for this system
apt install -y ipcalc nmap lshw tcpdump net-tools nftables wireless-tools iperf3\
		radvd bridge-utils firmware-mediatek libnss-mdns syncthing networkd-dispatcher\
		libgps-dev libcap-dev screen arping bc jq git libssl-dev\
		python3-protobuf unzip chrony build-essential systemd-resolved dhcping \
		libnl-3-dev libnl-genl-3-dev libnl-route-3-dev libdbus-1-dev gpsd
echo "Done"

# probably not installed, but the debian package is old
apt remove -y avahi yq > /dev/null 2>&1

# Download and install Go yq, this has better features
wget -q https://github.com/mikefarah/yq/releases/latest/download/yq_linux_arm64 -O /usr/bin/yq
chmod +x /usr/bin/yq

# Load modules at boot
cat << EOF > /etc/modules-load.d/morse.conf
mac80211
cfg80211
crc7
morse
dot11ah
EOF

# Load the morse driver with options
cat << EOF > /etc/modprobe.d/morse.conf
options morse country="$REG"
options morse spi_clock_speed=1500000
options morse bcf=bcf_fgh100mhaamd.bin
EOF

echo "batman-adv" > /etc/modules-load.d/batman.conf

# Disable the default wpa_supplicant service
systemctl disable wpa_supplicant.service > /dev/null 2>&1

# Set hostname, make unique by ethernet mac addr (last 4)
hostnamectl hostname radio-$HOST_MAC
sed -i 's/raspberrypi/radio-$HOST_MAC/g' /etc/hosts
echo "Hostname set"

# Set regulatory region  (default will be US)
echo options cfg80211 ieee80211_regdom=$REG > /etc/modprobe.d/wifi-regdom.conf
echo "Set wifi regulatory domain to $REG"

#turn on packet forwarding
cat << EOF > /etc/sysctl.d/99-mesh.conf
# IPv4 forwarding for mesh
net.ipv4.ip_forward=1
net.ipv4.conf.all.forwarding=1
net.ipv4.conf.default.forwarding=1

# IPv4 multicast forwarding
net.ipv4.conf.all.mc_forwarding=1
net.ipv4.conf.default.mc_forwarding=1
net.ipv4.conf.bat0.mc_forwarding=1
net.ipv4.conf.br0.mc_forwarding=1

# IPv6 forwarding for mesh
net.ipv6.conf.all.forwarding=1
net.ipv6.conf.default.forwarding=1

# IPv6 multicast forwarding
net.ipv6.conf.all.mc_forwarding=1
net.ipv6.conf.default.mc_forwarding=1
net.ipv6.conf.bat0.mc_forwarding=1
net.ipv6.conf.br0.mc_forwarding=1

# Increase multicast route cache for large mesh
net.ipv4.route.max_size=16384
net.ipv6.route.max_size=16384

# Optional: Increase ARP cache for many nodes
net.ipv4.neigh.default.gc_thresh1=1024
net.ipv4.neigh.default.gc_thresh2=2048
net.ipv4.neigh.default.gc_thresh3=4096
EOF


#
#  Create the non wifi interfaces
#

# Create the batman-adv interface, enslave it to br0
# Tell it to get the correct LL address for alfred to work
cat << EOF > /etc/systemd/network/10-bat0.network
[Match]
Name=bat0

[Network]
Bridge=br0
LinkLocalAddressing=ipv6
IPv6Token=eui64
IPv6PrivacyExtensions=no
EOF

# The bridge br0 is the main interface for the mesh node
cat << EOF > /etc/systemd/network/10-br0-bridge.netdev
[NetDev]
Name=br0
Kind=bridge

[Bridge]
MulticastSnooping=true
MulticastQuerier=true
EOF

# br0 will get a slaac ipv6 address
# Scripts will give it an ipv4 address later on
cat << EOF > /etc/systemd/network/20-br0-bridge.network
[Match]
Name=br0

[Network]
DHCP=no
LinkLocalAddressing=ipv6
IPv6AcceptRA=yes
MulticastDNS=yes

[Link]
RequiredForOnline=no
EOF

#stop other interfaces from doing multicast dns, trim down network chatter
cat << EOF > /etc/systemd/network/90-default-no-mdns.network
[Match]
Name=!br0

[Network]
LLMNR=no
MulticastDNS=no
EOF

# Set ethernet links for DHCP as a default setup.  Scripts will juggle this
# around later for device detection
for LAN in `networkctl | awk '/ether/ {print $2}'`; do
	M=`ip link show $LAN | awk '/ether/ {print $2}'`
	cat <<- EOF > /etc/systemd/network/10-$LAN.network
		[Match]
		MACAddress=$M

		[Network]
		DHCP=yes
		LinkLocalAddressing=no
		IPv6AcceptRA=no

		[DHCPv4]
		UseDomains=true
	EOF

done
echo "Ethernet config added"


#
# Configure and enable system services
#


#  Set up the node firewall to accept just about everything and to forward traffic out the
#  ethernet interface.  This can just be set and forgotten and will only make a difference
#  when the node is plugged into a network

#  Should consider moving this to radio setup to take advantage of better interface detection
echo "Configuring nftables for IPv4 NAT gateway"
cat << EOF > /etc/nftables.conf
#!/usr/sbin/nft -f

# Flush the old ruleset to start clean
flush ruleset
table inet filter {
  # The INPUT chain handles traffic destined for the node itself.
  chain input {
	type filter hook input priority 0; policy drop;
	ct state {established, related} accept
	ct state invalid drop
	iifname "lo" accept
	# Accept ALL traffic coming from the trusted mesh interface.
	iifname "br0" accept
	iifname "bat0" accept
	iifname "eth0" accept #tcp dport 22 accept
  }
  chain forward {
	type filter hook forward priority 0; policy drop;

	# Allow traffic from the trusted mesh to be forwarded
	# out to the internet via the Ethernet port.
	iifname "br0" oifname "eth0" accept

	# Allow the return traffic from the internet back to the mesh.
	iifname "eth0" oifname "br0" ct state established, related accept
  }
  chain output {
	type filter hook output priority 0; policy accept;
  }
}

table ip nat {
  chain postrouting {
	type nat hook postrouting priority 100;
	oifname "eth0" masquerade
  }
}
EOF

echo "Setting up router advertisements"
# Configure router advertisements for slaac on ipv6
# The announced ipv6 prefix with be where all the nodes
# auto configure their addresses to be local to each other
#
# The two files are for when the node is a client
# ( AdvDefaultLifetime 0 ) vs when it advertises itself as
# a gateway ( AdvDefaultLifetime 600 ).  A networkd-dispatcher
# script does the swap
cat << EOF > /etc/radvd-mesh.conf
interface br0
{
  AdvSendAdvert on;
  AdvDefaultLifetime 0;
  prefix fd01:ed20:ecb4:0::/64  {
	AdvOnLink on;
	AdvAutonomous on;
	AdvRouterAddr off;
  };
};
EOF

cat << EOF > /etc/radvd-gateway.conf
interface br0 {
  AdvSendAdvert on;
  AdvDefaultLifetime 600;
  prefix fd01:ed20:ecb4:0::/64  {
	AdvOnLink on;
	AdvAutonomous on;
  };
};
EOF

# Default to mesh config
cp /etc/radvd-mesh.conf /etc/radvd.conf

# make radvd wait for bat0 to be up
mkdir -p /etc/systemd/system/radvd.service.d/
cat << EOF > /etc/systemd/system/radvd.service.d/override.conf
[Unit]
After=batman-enslave.service
Wants=batman-enslave.service

[Service]
ExecStartPre=/bin/sleep 5
EOF

systemctl enable radvd


# Attempt to sync network time at boot
# Uses data from Alfred to look for any NTP servers (a gw that has
# sync'd its time from the internet) on the mesh.  It picks the
# one with the best transmission quality, does a time sync with it,
# and then disables chrony to prevent excess network traffic
cat << EOF > /etc/systemd/system/one-shot-time-sync.service
[Unit]
Description=One-Shot Mesh Time Synchronization
# This must run after the mesh is fully up and the manager has started.
After=node-manager.service
Wants=node-manager.service

[Service]
Type=oneshot
ExecstartPre=/bin/sleep 5
ExecStart=/usr/local/bin/one-shot-time-sync.sh

[Install]
WantedBy=multi-user.target
EOF
# this will be enabled by radio-setup.sh
#systemctl enable one-shot-time-sync.service

# Config for the active gateway acting as a mesh NTP server
cat << EOF > /etc/chrony/chrony-server.conf
# Use public NTP servers from the internet.
pool pool.ntp.org iburst
driftfile /var/lib/chrony/chrony.drift
makestep 1.0 3
# Allow clients from our private mesh prefix.
allow fd01:ed20:ecb4::/64
# Serve time even if internet connection is lost.
local stratum 10
EOF

# Config used ONLY to test external NTP connectivity
cat << EOF > /etc/chrony/chrony-test.conf
# Use public NTP servers from the internet.
pool pool.ntp.org iburst
driftfile /var/lib/chrony/chrony.drift
makestep 1.0 3
# Do NOT allow any clients - this is just a test config.
deny all
EOF

# Set the default configuration to be a client.  Allows chrony to start
echo "Setting default NTP mode to offline"
cat << EOF > /etc/chrony-default.conf
# This configuration file makes chronyd start but remain offline
# until explicitly told to sync via chronyc.
driftfile /var/lib/chrony/chrony.drift
makestep 1.0 3
offline
deny all
EOF
cp /etc/chrony-default.conf /etc/chrony.conf
systemctl enable chrony.service

# Set br0 to be the wait online interface, avoids boot delay
mkdir -p /etc/systemd/system/systemd-networkd-wait-online.service.d/
cat <<- EOF > /etc/systemd/system/systemd-networkd-wait-online.service.d/override.conf
[Service]
ExecStart=
ExecStart=/lib/systemd/systemd-networkd-wait-online --interface=br0
EOF
# But let's not us it
systemctl mask systemd-networkd-wait-online.service

# Disable netplan, networkd will do the networking
mkdir -p /etc/netplan
rm -f /etc/netplan/*
cat << EOF > /etc/netplan/99-disable-netplan.yaml
# This file tells Netplan to do nothing.
network:
version: 2
renderer: networkd
EOF
echo "Netplan disabled, will use networkd instead"

# Configure resolved
cat << EOF > /etc/systemd/resolved.conf
[Resolve]
LLMNR=no
MulticastDNS=no
DNSStubListener=yes
Cache=yes
EOF

# Old issue, but useful to avoid any hung mesh node so leaving this in
cat << EOF > /etc/sysctl.d/90-kernelpanic-reboot.conf
kernel.panic = 10
kernel.panic_on_oops = 1
EOF

# Install optional service selections
if [ "INSTALL_MUMBLE" = "y" ]; then
		apt install -y mumble-server
		#make mumble server ini changes
		sed -i '/ice="tcp -h 127.0.0.1 -p 6502"/s/^#//g' /etc/mumble-server.ini
		sed -i 's/icesecretwrite/;icesecretwrite/g' /etc/mumble-server.ini
		service mumble-server restart
		grep -m 1 SuperUser /var/log/mumble-server/mumble-server.log > /root/mumble_pw
fi

# install mediaMTX server
if [ "INSTALL_MEDIAMTX" = "y" ]; then
	echo "Installing MediaMTX"
		cd /tmp
		wget -q https://github.com/bluenviron/mediamtx/releases/download/v1.15.3/mediamtx_v1.15.3_linux_arm64.tar.gz
		gzip -d mediamtx_v1.15.3_linux_arm64.tar.gz
		tar -xf mediamtx_v1.15.3_linux_arm64.tar
		groupadd --system mediamtx
		useradd --system -g mediamtx -d /opt/mediamtx -s /sbin/nologin mediamtx
		mkdir /etc/mediamtx && chown mediamtx:mediamtx /etc/mediamtx
		mkdir -p /opt/mediamtx
		cp mediamtx /opt/mediamtx/
		chmod +x /opt/mediamtx/mediamtx
		cp mediamtx.yml /etc/mediamtx/

cat << EOF > /etc/systemd/system/mediamtx.service
[Unit]
Description=MediaMTX RTSP/RTMP/WebRTC Server
After=network.target

[Service]
User=mediamtx
Group=mediamtx
WorkingDirectory=/opt/mediamtx
ExecStart=/opt/mediamtx/mediamtx /etc/mediamtx/mediamtx.yml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
fi

echo "Setting radio-setup.sh to run at next reboot"
#set up the second provisioning script to run at boot
cat << EOF > /etc/systemd/system/radio-setup-run-once.service
[Unit]
Description=Run radio setup script once after reboot
After=network-online.target multi-user.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/radio-setup.sh
ExecStartPre=/bin/sleep 10
RemainAfterExit=no

[Install]
WantedBy=multi-user.target
EOF
systemctl enable radio-setup-run-once.service

echo "Enabling systemd-networkd and systemd-resolved"
systemctl enable --now systemd-networkd
systemctl enable systemd-resolved

# Force systemd-resolved to be the DNS provider
#ln -sf /run/systemd/resolve/resolv.conf /etc/resolv.conf

rm /etc/resolv.conf
echo "nameserver 1.1.1.1" > /etc/resolv.conf

#write out the config selections for future use
echo "mesh_key=$LAN_SAE_KEY" > /etc/mesh.conf
echo "mesh_ssid=$LAN_SSID" >> /etc/mesh.conf
echo "ipv4_network=$LAN_CIDR_BLOCK"  >> /etc/mesh.conf
echo "mumble=$INSTALL_MUMBLE" >> /etc/mesh.conf
echo "mtx=$INSTALL_MEDIAMTX" >> /etc/mesh.conf
echo "acs=$AUTO_CHANNEL" >> /etc/mesh.conf
echo "eud=$EUD_CONNECTION" >> /etc/mesh.conf

# Disable the current script so it won't run again at next boot
systemctl disable mesh-provision

echo "=== Provisioning complete at $(date) ==="
echo "=== Rebooting to apply changes ==="
reboot

PROVISIONEOF

# Make sure all our tools can run
chmod +x /usr/local/bin/*

# Create systemd service to run the provisioning AFTER network is ready
# This is the above portion of the script that was just written out
cat > /etc/systemd/system/mesh-provision.service << 'EOF'
[Unit]
Description=Provision Mesh Node After Network
After=network-online.target multi-user.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/provision-mesh.sh
RemainAfterExit=yes
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable mesh-provision.service

echo "=== firstrun.sh complete - provisioning will continue after reboot ==="
echo "=== firstrun.sh exiting at $(date) ==="
