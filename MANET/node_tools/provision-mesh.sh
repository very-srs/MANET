#!/bin/bash
#log the output of the provisioning script
exec > >(tee -a /boot/firmware/provision.log) 2>&1
set -x
echo "=== provision-mesh-node.sh starting at $(date) ==="

# This is where the wifi regulatory country is set
REG="HR"

# Calculate unique hostname from MAC address
HOST_MAC=$(ip a | grep -A1 $(networkctl | grep -v bat | awk '/ether/ {print $2}' | head -1) \
   | awk '/ether/ {print $2}' | cut -d':' -f 5-6 | sed 's/://g')

echo "Waiting for internet connectivity..."
# Blink POWER LED rapidly while waiting (RPi5: /sys/class/leds/PWR)
PWR_LED=/sys/class/leds/PWR
if [ -d "$PWR_LED" ]; then
    echo timer        > "$PWR_LED/trigger"   2>/dev/null || true
    echo 100          > "$PWR_LED/delay_on"  2>/dev/null || true
    echo 100          > "$PWR_LED/delay_off" 2>/dev/null || true
fi

while true; do
    if ping -c 1 -W 2 8.8.8.8 > /dev/null 2>&1; then
        echo "Internet connectivity confirmed!"
        break
    fi
    echo "Waiting for internet..."
    sleep 5
done

# Restore LED to solid-on
if [ -d "$PWR_LED" ]; then
    echo default-on > "$PWR_LED/trigger" 2>/dev/null || true
fi

# A basic method to set the system time
date -s "$(curl -sI google.com | grep -i ^Date: | cut -d' ' -f2-)"

cd /root

# Get rid of the exra nonsense printed on an SSH connection
> /etc/motd

# Pull down a .tar file that consists of all the various additionaly tools and scripts that will be used
# These are mesh tools in /usr/local/bin, the boot config.txt, batctl and alfred, and some systemd service
# files.  Here we can also differentiate between hardware types if he need different setup options.
# This tar file also contains the manet kernel, modified to include the drivers for the various radios used

#  The tar file will be extracted after apt install happens
if [ "rpi5" = "rpi5" ]; then
		echo "Applying RPi 5 specific settings..."
		# Match both bare 'rpi5-install.tar.gz' and versioned variants like
		# 'rpi5-install-v0.25-foo.tar.gz' so a release uploaded under a
		# versioned name does not silently break first-boot provisioning.
		RPI5_URL=$(curl -s https://api.github.com/repos/mrleongalaxyum/manet-dev/releases/latest \
				| grep -oE '"browser_download_url": *"[^"]*rpi5-install[^"/]*\.tar\.gz"' \
				| grep -oE 'https://[^"]*' \
				| head -1)
			if [ -z "$RPI5_URL" ]; then
				echo "ERROR: Could not resolve rpi5-install.tar.gz from latest release"
				exit 1
			fi
			wget -q "$RPI5_URL" -O /root/morse-pi-install.tar.gz || {
				echo "ERROR: Failed to download rpi5 package"
				exit 1
		}
		# The rpi5 is using an SD card and may need to be resized
		# Check if filesystem is nearly full (indicating small partition)
		USAGE_PERCENT=$(df / | tail -1 | awk '{print $5}' | tr -d '%')
		echo "Root filesystem is ${USAGE_PERCENT}% full"

		# If filesystem is >70% full, partition is too small - expand
		if [ $USAGE_PERCENT -gt 70 ]; then
			echo "Filesystem nearly full, expanding partition..."

			# Expand the partition
			raspi-config --expand-rootfs

			# Resize the filesystem to fill the new partition
			resize2fs /dev/mmcblk0p2

			echo "Expansion complete"
		else
		echo "Filesystem has room (${USAGE_PERCENT}%), skipping expansion."
		fi
		touch /var/lib/expand-root-done
elif [ "rpi5" = "rpi4" ]; then
		echo "Applying RPi 4 specific settings..."
		wget -q  https://www.colorado-governor.com/manet/cm4-install.tar.gz -O /root/morse-pi-install.tar.gz || {
				echo "ERROR: Failed to download rpi4 package"
				exit 1
		}

		USAGE_PERCENT=$(df / | tail -1 | awk '{print $5}' | tr -d '%')
		echo "Root filesystem is ${USAGE_PERCENT}% full"

		# If filesystem is >70% full, partition is too small - expand
		if [ $USAGE_PERCENT -gt 70 ]; then
			echo "Filesystem nearly full, expanding partition..."

			# Expand the partition
			raspi-config --expand-rootfs

			# Resize the filesystem to fill the new partition
			resize2fs /dev/mmcblk0p2

			echo "Expansion complete"
		else
		echo "Filesystem has room (${USAGE_PERCENT}%), skipping expansion."
		fi
		touch /var/lib/expand-root-done
elif [ "rpi5" = "r3a" ]; then
		echo "Applying Rock 3A specific settings..."
		wget -q https://www.colorado-governor.com/manet/r3a-install.tar.gz -O /root/morse-pi-install.tar.gz || {
				echo "ERROR: Failed to download Rock 3A package"
				exit 1
		}
fi

#
#  Setup base system
#


# get the sources up to date and install packages
echo -n "Updating system packages..."
apt update > /dev/null 2>&1

# Remove the question about the iperf daemon during apt install
echo "iperf3 iperf3/start_daemon boolean true" | debconf-set-selections

# This isn't needed for the manet kernel and causes errors
chmod -x /etc/kernel/postinst.d/initramfs-tools

# Install packages for this system
apt install -y ipcalc nmap lshw tcpdump net-tools nftables wireless-tools iperf3\
		radvd bridge-utils firmware-mediatek libnss-mdns syncthing networkd-dispatcher\
		libgps-dev libcap-dev screen arping bc jq git libssl-dev hostapd dnsmasq pulseaudio\
		python3-protobuf unzip chrony systemd-resolved dhcping mpg123\
		libnl-3-dev libnl-genl-3-dev libnl-route-3-dev ebtables libdbus-1-dev gpsd pulseaudio-utils


# Unpack the tar file that was pulled down earlier.  This contains the kernel and 
# mesh tools
tar -zxf /root/morse-pi-install.tar.gz -C /

# Reload udev rules after extracting (picks up 80-usb-ethernet.rules etc.)
udevadm control --reload-rules 2>/dev/null || true
udevadm trigger 2>/dev/null || true

# Fix networkd-dispatcher ownership (tar extracts as current user; dispatcher
# refuses to run scripts not owned by root)
chown root:root /etc/networkd-dispatcher \
  /etc/networkd-dispatcher/carrier.d \
  /etc/networkd-dispatcher/off.d \
  /etc/networkd-dispatcher/no-carrier.d \
  /etc/networkd-dispatcher/degraded.d \
  /etc/networkd-dispatcher/routable.d 2>/dev/null || true
chmod 755 /etc/networkd-dispatcher \
  /etc/networkd-dispatcher/carrier.d \
  /etc/networkd-dispatcher/off.d \
  /etc/networkd-dispatcher/no-carrier.d \
  /etc/networkd-dispatcher/degraded.d \
  /etc/networkd-dispatcher/routable.d 2>/dev/null || true
chown root:root /etc/networkd-dispatcher/carrier.d/* \
  /etc/networkd-dispatcher/off.d/* \
  /etc/networkd-dispatcher/no-carrier.d/* \
  /etc/networkd-dispatcher/degraded.d/* \
  /etc/networkd-dispatcher/routable.d/* 2>/dev/null || true

# Fix sudoers.d ownership
chown root:root /etc/sudoers.d /etc/sudoers.d/* 2>/dev/null || true
chmod 750 /etc/sudoers.d 2>/dev/null || true
chmod 440 /etc/sudoers.d/* 2>/dev/null || true

# Platform-specific post-extraction steps
if [ "rpi5" = "r3a" ]; then
	# Unpack the r3a kernel
	cd /root
	dpkg -i *.deb

	# Add the morse firmware
	cd /root/morse-firmware
	cp firmware/mm8108*.bin /lib/firmware/morse/
	cp bcf/morsemicro/*.bin /lib/firmware/morse/
	cp bcf/azurewave/*.bin /lib/firmware/morse/
	cp bcf/netprisma/*.bin /lib/firmware/morse/
	cp bcf/quectel/*.bin /lib/firmware/morse/

	# Disable predictable interface names for Armbian
	sed -i '/^extraargs=/ s/$/ net.ifnames=0/' /boot/armbianEnv.txt
	
	cd /root
else
	# RPi-specific firmware
	cd /root/morse-firmware
	sudo cp firmware/mm8108*.bin /lib/firmware/morse/
	sudo cp bcf/morsemicro/*.bin /lib/firmware/morse/
	sudo cp bcf/azurewave/*.bin /lib/firmware/morse/
	sudo cp bcf/netprisma/*.bin /lib/firmware/morse/
	sudo cp bcf/quectel/*.bin /lib/firmware/morse/
fi


# Installed but not being used now
systemctl stop dnsmasq
systemctl disable dnsmasq
systemctl mask dnsmasq
echo "Done"


# probably not installed, but the debian package is old
apt remove -y avahi yq > /dev/null 2>&1

# Download and install Go yq, this has better features
wget -q https://github.com/mikefarah/yq/releases/latest/download/yq_linux_arm64 -O /usr/bin/yq
chmod +x /usr/bin/yq

# These add network traffic, disable them
echo "Disabling APT timers for automatic updates"
systemctl disable apt-daily.timer > /dev/null 2>&1
systemctl disable apt-daily-upgrade.timer > /dev/null 2>&1

# This was hanging on boot, disable it
systemctl mask dev-zram0.device > /dev/null 2>&1



# Load batman-adv module at boot
echo "batman_adv" > /etc/modules-load.d/batman.conf

# Load modules at boot
cat << EOF > /etc/modules-load.d/morse.conf
mac80211
cfg80211
crc7
morse
dot11ah
EOF

# Load the morse driver with options to allow the interface to be picked up
# This will be re-written by radio-setup and become specific to the chosen region
cat << EOF > /etc/modprobe.d/morse.conf
options morse country=US
options morse spi_clock_speed=1500000
options morse bcf=bcf_fgh100mhaamd.bin
options morse enable_mcast_whitelist=0 enable_mcast_rate_control=1
EOF


# More kernel tweaks
cat << EOF > /etc/sysctl.d/99-mesh.conf
# IPv4 forwarding
net.ipv4.conf.all.forwarding=1
net.ipv4.conf.default.forwarding=1

# IPv6 forwarding
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

[Link]
MTUBytes=1500
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
MTUBytes=1500
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
# Set ethernet links for DHCP as a default setup. Scripts will juggle this
# around later for device detection
CT=0
for LAN in `networkctl | awk '/ether/ {print $2}'`; do
    M=$(ip link show $LAN | awk '/ether/ {print $2}')
    cat <<- EOF > /etc/systemd/network/10-end${CT}.network
		[Match]
		MACAddress=$M

		[Network]
		DHCP=yes
		LinkLocalAddressing=no
		IPv6AcceptRA=no

		[DHCPv4]
		UseDomains=true
	EOF
    cat <<- EOF > /etc/systemd/network/11-end${CT}-rename.link
		[Match]
		MACAddress=$M

		[Link]
		Name=end${CT}
	EOF
    (( CT++ ))
    cp  /etc/systemd/network/10-end${CT}.network  /etc/systemd/network/10-end${CT}.network.dhcp-default
done
echo "Ethernet config added"

# Create ethernet templates for manet-uplink-dispatch.sh (OUTSIDE the loop)
echo "Creating ethernet detection templates..."
cat << 'EOF' > /etc/systemd/network/20-end0-gateway.network.off
[Match]
Name=end0

[Network]
DHCP=yes
LinkLocalAddressing=no
IPv6AcceptRA=no

[DHCPv4]
UseDomains=true
EOF

cat << 'EOF' > /etc/systemd/network/20-end0-eud.network.off
[Match]
Name=end0

[Network]
Bridge=br0
LinkLocalAddressing=no
IPv6AcceptRA=no

[Link]
RequiredForOnline=no
EOF
echo "Ethernet templates created"

#
# Configure and enable system services
#


#  Set up the node firewall to accept just about everything and to forward traffic out the
#  ethernet interface.
#  This can just be set and forgotten and will only make a difference
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
	iifname "end0" accept
  }
  chain forward {
	type filter hook forward priority 0; policy drop;

    # Allow forwarding within the mesh bridge (EUD → gateway)
    iifname "br0" oifname "br0" accept

	# Allow traffic from the trusted mesh to be forwarded
	# out to the internet via the Ethernet port.
	iifname "br0" oifname "end0" accept

	# Allow the return traffic from the internet back to the mesh.
	iifname "end0" oifname "br0" ct state established, related accept
  }
  chain output {
	type filter hook output priority 0; policy accept;
  }
}

table ip nat {
  chain postrouting {
	type nat hook postrouting priority 100;
	oifname "end0" masquerade
  }
}

table inet mesh_mangle {
    chain forward {
        type filter hook forward priority mangle; policy accept;
        tcp flags syn tcp option maxseg size set 1400
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
# sync'd its time from the internet) on the mesh.
# It picks the
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

# Get DNS working again after dnsmasq install
systemctl restart systemd-resolved


echo "Disabling default chrony networkd-dispatcher script"
rm -Rf /usr/lib/NetworkManager/dispatcher.d/*

# Install optional service selections
if [ "y" = "y" ]; then
		apt install -y mumble-server
		#make mumble server ini changes
		sed -i '/ice="tcp -h 127.0.0.1 -p 6502"/s/^#//g' /etc/mumble-server.ini
		sed -i 's/icesecretwrite/;icesecretwrite/g' /etc/mumble-server.ini
		service mumble-server restart
		grep -m 1 SuperUser /var/log/mumble-server/mumble-server.log > /root/mumble_pw
fi

# install mediaMTX server
if [ "y" = "y" ]; then
	echo "Installing MediaMTX"
		cd /tmp
		wget -q https://github.com/bluenviron/mediamtx/releases/download/v1.15.3/mediamtx_v1.15.3_linux_arm64.tar.gz || {
	        echo "ERROR: Failed to download MediaMTX"
	        exit 1
	    }
	    tar -xzf mediamtx_v1.15.3_linux_arm64.tar.gz || {
	        echo "ERROR: Failed to extract MediaMTX"
	        exit 1
	    }

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

# Install optional service selections
if [ "n" = "y" ]; then
	cp /usr/local/bin/node-manager-acs.sh /usr/local/bin/node-manager.sh
else
	cp /usr/local/bin/node-manager-static.sh /usr/local/bin/node-manager.sh
fi
chmod +x /usr/local/bin/node-manager.sh
chmod +x /usr/local/bin/*

echo "Setting radio-setup.sh to run at next reboot"
#set up the second provisioning script to run at boot
cat << EOF > /etc/systemd/system/radio-setup-run-once.service
[Unit]
Description=Run radio setup script once after reboot
After=network-online.target
Wants=network-online.target
# Note: do not add 'Before=batman-enslave.service node-manager.service ...'
# here. radio-setup.sh itself writes those .service files (heredoc emit), so
# at the time radio-setup-run-once first runs they either don't exist yet
# (the directive is a no-op) or they were just written by this very script
# (the directive is redundant). multi-user.target ordering with Type=oneshot
# is sufficient to keep radio-setup ahead of mesh runtime services.

[Service]
Type=oneshot
ExecStart=/usr/local/bin/radio-setup.sh
ExecStartPre=/bin/sleep 10
RemainAfterExit=no

[Install]
WantedBy=multi-user.target
EOF
systemctl enable radio-setup-run-once.service


echo "Disabling network manager"
systemctl stop dhcpcd
systemctl disable dhcpcd
systemctl stop NetworkManager
systemctl disable NetworkManager
systemctl mask NetworkManager

echo "Enabling systemd-networkd and systemd-resolved"
systemctl enable --now systemd-networkd
systemctl enable systemd-resolved

# Force systemd-resolved to be the DNS provider
#ln -sf /run/systemd/resolve/resolv.conf /etc/resolv.conf


#write out the config selections for future use
echo "mesh_key=5cAKt6KRrxatlDghiAzeXIZ9jkubLMVUHm0/bchIDgxZooomywHdqabZ3d0D" > /etc/mesh.conf
echo "mesh_ssid=MESH" >> /etc/mesh.conf
echo "regulatory_domain=HR" >> /etc/mesh.conf
echo "halow_regulatory_domain=EU" >> /etc/mesh.conf
echo "lan_ap_ssid=MANET" >> /etc/mesh.conf
echo "lan_ap_key=raspberry" >> /etc/mesh.conf
echo "ipv4_network=10.30.2.0/24"  >> /etc/mesh.conf
echo "mumble=y" >> /etc/mesh.conf
echo "mtx=y" >> /etc/mesh.conf
echo "acs=n" >> /etc/mesh.conf
echo "eud=wireless" >> /etc/mesh.conf
echo "max_euds_per_node=20" >> /etc/mesh.conf


# Disable the current script so it won't run again at next boot
systemctl disable mesh-provision

#use a known dns for next setup
rm /etc/resolv.conf
echo "nameserver 1.1.1.1" > /etc/resolv.conf

echo "=== Provisioning complete at $(date) ==="
echo "=== Rebooting to apply changes ==="
reboot

