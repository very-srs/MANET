#!/bin/bash
#
#    This script runs at first boot on a recently imaged rpi
#	 It aims to set up the base level system with things like
#    software packages, system services, and firmware.  After
#    it reboots, a second script ( radio-setup.sh ) runs to
#    complete the setup
#
#    oct 2025
#

#wifi region
REG=US

# used for determining a unique hostname
HOST_MAC=$(ip a | grep -A1 `networkctl | grep -v bat | awk '/ether/ {print $2}'`\
 | awk '/ether/ {print $2}' | cut -d':' -f 5-6 | sed 's/://g')

# Path to this script, for looping
SCRIPT_PATH="$(realpath "$0")"

# function to loop during setup until there is a network
check_network() {
	#check for network
	GW=`ip route | awk '/^default/ { print $3 }'`
	echo -n "Checking for network connectivity for package installation..."
	if ! ping -c 2 $GW > /dev/null 2>&1; then
	    echo "Failure to reach default gateway"
	    return 1
	fi
	echo -n "."
	if ! ping -c 2 1.1.1.1 > /dev/null 2>&1; then
	    echo "Failure to reach the internet by IP"
	    return 1
	fi
	echo -n "."
	if ! ping -c 2 google.com > /dev/null 2>&1; then
	    echo "Failure to reach the internet by domain name"
	    return 1
	fi
	echo "Done"
	return 0
}

main() {
    if ! check_network; then
    # there is a lack of a network, print some info and loop
	echo "IP info:"
	ip -br a
	ip route
        echo "Sleeping for ten seconds and trying again..."
	sleep 10
        exec "$SCRIPT_PATH" "$@"   # restart the script
    fi

    echo "Network is available, continuing..."

	#
	#  Setup base system
	#

	# Disable kernel updates, this is a custom kernel with wifi drivers added
	armbian-config --cmd UPD002

	# get the sources up to date and install packages
	echo -n "Updating system packages..."
	apt update  > /dev/null 2>&1
	echo -n "."
	apt upgrade -y > /dev/null 2>&1
	echo -n "."

	# Remove the question about the iperf daemon during apt install
	echo "iperf3 iperf3/start_daemon boolean true" | debconf-set-selections

	# Install packages for this system
	apt install -y ipcalc nmap lshw tcpdump net-tools nftables wireless-tools iperf3\
	  \radvd bridge-utils firmware-mediatek libnss-mdns syncthing networkd-dispatcher\
	  libgps-dev libcap-dev mumble-server screen arping bc yq\
	  python3-protobuf chrony > /dev/null 2>&1
	echo "Done"

	echo "Disabling APT timers for automatic updates..."
	systemctl disable apt-daily.timer
	systemctl disable apt-daily-upgrade.timer

	# The version of alfred in the debian packages is old.  Install one built oct 2025
	cp /root/alfred /usr/sbin/
	cp /root/batctl /usr/sbin/

	# Add the protobuf tools
	cp /root/NodeInfo_pb2.py /usr/local/bin/
	cp /root/encoder.py /usr/local/bin/
	cp /root/decoder.py /usr/local/bin/
    chmod +x /usr/local/bin/encoder.py
    chmod +x /usr/local/bin/decoder.py

	#copy over mtx tools
	cp /root/mtx-ip.sh /usr/local/bin/   # Selects the ipv6 for mediaMTX
	chmod +x /usr/local/bin/mtx-ip.sh
	cp /root/mediamtx-election.sh /usr/local/bin  # Determines who will be the MTX server
	chmod +x /usr/local/bin/mediamtx-election.sh

	# setup rpi config parameters to activate the pcie bus, used by wireless card
	sed -i 's/#dtparam=spi=on/dtparam=spi=on/g' /boot/firmware/config.txt
	if ! grep -q 'dtparam=pciex1' /boot/firmware/config.txt; then
		echo "dtparam=pciex1" >> /boot/firmware/config.txt
	fi
	if ! grep -q 'dtoverlay=pcie-32bit-dma' /boot/firmware/config.txt; then
		echo "dtoverlay=pcie-32bit-dma" >> /boot/firmware/config.txt
	fi
	if ! grep -q 'dtoverlay=pciex1-compat' /boot/firmware/config.txt; then
		echo "dtoverlay=pciex1-compat-pi5,no-mip" >> /boot/firmware/config.txt
	fi
	echo "PCIe subsystem enabled"

	sed -i 's/otg_mode=1//g' /boot/firmware/config.txt
	if ! grep -q 'dr_mode=host' /boot/firmware/config.txt; then
		echo "dtoverlay=dwc2,dr_mode=host" >> /boot/firmware/config.txt
	fi


	# disable the default wpa_supplicant service
	systemctl disable wpa_supplicant.service

	#set hostname, make unique by ethernet mac addr (last 4)
	hostnamectl hostname radio-$HOST_MAC
	echo "Hostname set"

	# enable i2c
	echo i2c_dev > /etc/modules-load.d/i2c_dev.conf
	echo "Enabled i2c"

	# enable SPI
	sed -i 's/#dtparam=spi=on/dtparam=spi=on/g' /boot/firmware/config.txt

	#set regulatory region as US
	echo options cfg80211 ieee80211_regdom=$REG > /etc/modprobe.d/wifi-regdom.conf
	echo "Set wifi regulatory domain to $REG"

	#turn on packet forwarding
	cat <<- EOF > /etc/sysctl.d/99-mesh.conf
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
	cat <<- EOF > /etc/systemd/network/10-bat0.network 
		[Match]
		Name=bat0

		[Network]
		Bridge=br0
		LinkLocalAddressing=ipv6
		IPv6Token=eui64
		IPv6PrivacyExtensions=no
	EOF

	# The bridge br0 is the main interface for the mesh node
	cat <<-EOF > /etc/systemd/network/10-br0-bridge.netdev
		[NetDev]
		Name=br0
		Kind=bridge

		[Bridge]
		MulticastSnooping=false
	EOF

	# br0 will get a slaac ipv6 address
	cat <<-EOF > /etc/systemd/network/20-br0-bridge.network
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

	#stop other interfaces from doing multicast dns
	cat <<- EOF > /etc/systemd/network/90-default-no-mdns.network
		[Match]
		Name=!br0

		[Network]
		LLMNR=no
		MulticastDNS=no
	EOF

	#set ethernet links for DHCP, currently only used for setup
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

	echo "Configuring nftables for IPv4 NAT gateway"
	cat <<- EOF > /etc/nftables.conf
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

		        iifname "end0" tcp dport 22 accept
		    }

		    chain forward {
		        type filter hook forward priority 0; policy drop;

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
	EOF

	echo "Setting up router advertisements"
	# Configure router advertisements for slaac on ipv6
	# The announced ipv6 prefix with be where all the nodes
	# auto configure their addresses to be local to each other
	#
	# The to files are for when the node is a client
	# ( AdvDefaultLifetime 0 ) vs when it advertises itself as
	# a gateway ( AdvDefaultLifetime 600 ).  A networkd-dispatcher
	# script does the swap

	cat <<-EOF > /etc/radvd-mesh.conf
		interface br0
		{
		    AdvSendAdvert on;
		    AdvDefaultLifetime 0;
		    prefix fd01:ed20:ecb4::/48
		    {
		        AdvOnLink on;
		        AdvAutonomous on;
		        AdvRouterAddr off;
		    };
		};
	EOF

	cat <<- EOF > /etc/radvd-gateway.conf
		interface br0 {
		    AdvSendAdvert on;
		    AdvDefaultLifetime 600;
		    prefix fd01:ed20:ecb4::/48  {
		        AdvOnLink on;
		        AdvAutonomous on;
		    };
		};
	EOF

	# Default to mesh config
	cp /etc/radvd-mesh.conf /etc/radvd.conf

	# make radvd wait for bat0 to be up
	mkdir -p /etc/systemd/system/radvd.service.d/
	cat <<- EOF > /etc/systemd/system/radvd.service.d/override.conf
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
	cat <<- EOF > /etc/systemd/system/one-shot-time-sync.service
		[Unit]
		Description=One-Shot Mesh Time Synchronization
		# This must run after the mesh is fully up and the manager has started.
		After=node-manager.service
		Wants=node-manager.service

		[Service]
		Type=oneshot
		ExecStart=/usr/local/bin/one-shot-time-sync.sh

		[Install]
		WantedBy=multi-user.target
	EOF
	cp /root/one-shot-time-sync.sh /usr/local/bin/
	chmod +x /usr/local/bin/one-shot-time-sync.sh
# this is currently not working
#	systemctl enable one-shot-time-sync.service

	# Config for the active gateway acting as a mesh NTP server
	cat <<- EOF > /etc/chrony/chrony-server.conf
		# Use public NTP servers from the internet.
		pool pool.ntp.org iburst
		driftfile /var/lib/chrony/chrony.drift
		makestep 1.0 3
		# Allow clients from our private mesh prefix.
		allow fd5a:1<0xC2><0xB6><0xC2><0xB6>::/64
		# Serve time even if internet connection is lost.
		local stratum 10
	EOF

	# Config used ONLY to test external NTP connectivity
	cat <<- EOF > /etc/chrony/chrony-test.conf
		# Use public NTP servers from the internet.
		pool pool.ntp.org iburst
		driftfile /var/lib/chrony/chrony.drift
		makestep 1.0 3
		# Do NOT allow any clients - this is just a test config.
		deny all
	EOF

	# Set the default configuration to be a client.  Allows chrony to start
	echo "Setting default NTP mode to offline..."
	cat <<- EOF > /etc/chrony-default.conf
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
	# But let's try not using it
	systemctl disable systemd-networkd-wait-online.service

	# Disable netplan, networkd will do the networking
	rm -f /etc/netplan/*
	cat <<- EOF > /etc/netplan/99-disable-netplan.yaml
		# This file tells Netplan to do nothing.
		network:
		  version: 2
		  renderer: networkd
	EOF
	echo "Netplan disabled, will use networkd instead"

	cat <<- EOF > /etc/systemd/resolved.conf
		[Resolve]
		LLMNR=no
		MulticastDNS=no
		DNSStubListener=yes
		Cache=yes
	EOF

	# Started getting panics with the MM kernel, adding this while debugging
	cat <<- EOF > /etc/sysctl.d/90-kernelpanic-reboot.conf
		kernel.panic = 10
		kernel.panic_on_oops = 1
	EOF


	echo "Disabling default chrony networkd-dispatcher script"
	chmod -x /usr/lib/NetworkManager/dispatcher.d/*

	#make mumble server ini changes
	sed -i '/ice="tcp -h 127.0.0.1 -p 6502"/s/^#//g' /etc/mumble-server.ini
	sed -i 's/icesecretwrite/;icesecretwrite/g' /etc/mumble-server.ini
	service mumble-server restart
	grep -m 1 SuperUser /var/log/mumble-server/mumble-server.log > /root/mumble_pw

	#remove un-needed configs
	rm -f /etc/systemd/network/00-arm*

	echo "setting radio-setup.sh to run at next reboot"
	#set up the second provisioning script to run at boot
	cat <<- EOF > /etc/systemd/system/radio-setup-run-once.service
		[Unit]
		Description=Run radio setup script once after reboot
		After=network-online.target multi-user.target
		Wants=network-online.target

		[Service]
		Type=oneshot
		ExecStart=/root/radio-setup.sh
		ExecStartPre=/bin/sleep 10
		RemainAfterExit=no

		[Install]
		WantedBy=multi-user.target
	EOF
	systemctl enable radio-setup-run-once.service

}


main "$@"

reboot

