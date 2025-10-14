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
	  \screen radvd bridge-utils firmware-mediatek libnss-mdns syncthing\
	  avahi-daemon avahi-utils libgps-dev libcap-dev mumble-server > /dev/null 2>&1
	echo "Done"

	# The version of alfred in the debian packages is old.  Install one built oct 2025
	cp /root/alfred /usr/sbin/
	cp /root/batctl /usr/sbin/

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

	# get rid of otg, get rid of host, add peripheral at the end of the file
	# trying to put out ethernet via usb-c
	sed -i 's/otg_mode=1//g' /boot/firmware/config.txt
	if ! grep -q 'dr_mode=host' /boot/firmware/config.txt; then
		echo "dtoverlay=dwc2,dr_mode=host/g" >> /boot/firmware/config.txt
	fi


	# disable the default wpa_supplicant service
	systemctl disable wpa_supplicant.service

	#set hostname, make unique by ethernet mac addr (last 4)
	hostnamectl hostname radio-$HOST_MAC
	echo "Hostname set"

	# enable i2c
	echo i2c_dev > /etc/modules-load.d/i2c_dev.conf
	echo "Enabled i2c"

	#set regulatory region as US
	echo options cfg80211 ieee80211_regdom=$REG > /etc/modprobe.d/wifi-regdom.conf
	echo "Set wifi regulatory domain to $REG"

	#turn on packet forwarding
	cat <<- EOF > /etc/sysctl.d/99-forwarding.conf
		net.ipv4.ip_forward=1
		net.ipv6.conf.all.forwarding=1
	EOF


	#
	#  Create the non wifi interfaces
	#

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

	# Place other interfaces into the bridge
	cat <<-EOF > /etc/systemd/network/30-br0-bind.network
		[Match]
		Name=bat0 usb0

		[Network]
		Bridge=br0
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

	#configure router advertisements for slaac on ipv6
	cat <<-EOF > /etc/radvd-mesh.conf
		interface br0
		{
		    AdvSendAdvert on;
		    AdvDefaultLifetime 0;
		    prefix fd5a:1753:4340:1::/64
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
		    AdvDefaultLifetime 600; #advertise this node as a default router
		    AdvDefaultRoute on;
		    prefix fd5a:1753:4340:1::/64 {
		        AdvOnLink on;
		        AdvAutonomous on;
		    };
		};
	EOF
	# Default to mesh config
	cp /etc/radvd-mesh.conf /etc/radvd.conf
	systemctl enable radvd

	# Set avahi to wait for the network to be up
	mkdir -p /etc/systemd/system/avahi-daemon.service.d/
	cat <<- EOF > /etc/systemd/system/avahi-daemon.service.d/override.conf
		[Unit]
		Wants=network-online.target
		After=network-online.target radvd.service
	EOF
	# Publish host names but not local addresses via avahi, and only the bridge address
	sed -i 's/publish-workstation=no/publish-workstation=yes/g' /etc/avahi/avahi-daemon.conf
	sed -i 's/#allow-interfaces=eth0/allow-interfaces=br0/g' /etc/avahi/avahi-daemon.conf
	# Advertise ssh 
	cat <<- EOF > /etc/avahi/services/ssh.service
		<?xml version="1.0" standalone='no'?>
		<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
		<service-group>
		  <name replace-wildcards="yes">%h</name>
		  <service>
		    <type>_ssh._tcp</type>
		    <port>22</port>
		  </service>
		</service-group>
	EOF

	# Set br0 to be the wait online interface
	mkdir -p /etc/systemd/system/systemd-networkd-wait-online.service.d/
	cat <<- EOF > /etc/systemd/system/systemd-networkd-wait-online.service.d/override.conf#
	    [Service]
	    ExecStart=
	    ExecStart=/lib/systemd/systemd-networkd-wait-online --interface=br0
	EOF
	# But let's try not using it, it was hanging at first reboot
	systemctl disable systemd-networkd-wait-online.service

	# Disable netplan
	rm -f /etc/netplan/*
	cat <<- EOF > /etc/netplan/99-disable-netplan.yaml
		# This file tells Netplan to do nothing.
		network:
		  version: 2
		  renderer: networkd
	EOF
	echo "Netplan disabled, will use networkd instead"

	echo "Disabling mDNS in systemd-resolved"

	# Ensure the MulticastDNS setting is set to 'no'
	sed -i 's/#MulticastDNS=.*/MulticastDNS=no/' /etc/systemd/resolved.conf


	#make mumble server ini changes
	sed -i '/ice="tcp -h 127.0.0.1 -p 6502"/s/^#//g' /etc/mumble-server.ini
	sed -i 's/icesecretwrite/;icesecretwrite/g' /etc/mumble-server.ini
	service mumble-server restart
	grep -m 1 SuperUser /var/log/mumble-server/mumble-server.log > /root/mumble_pw

	#remove un-needed configs
	rm -f /etc/systemd/network/00-arm*

	#continue setup after a reboot
	chmod +x /root/radio-setup.sh
	crontab -l 2>/dev/null
	echo '@reboot sleep 30 && /root/radio_setup.sh' | crontab -
}


main "$@"

reboot

