#!/bin/bash
#
#    This script runs at first boot on a recently imaged rpi
#        It aims to set up the base level system with things like
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
        GW=`ip route | awk '/^default/ { print $3 }' | head -n 1`
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
        echo
        echo " # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #"
        echo " #                                                                           #"
        echo " #   This mesh node is being provisioned for the first time.  Basic setup    #"
        echo " #   will now happen and the node will reboot for further configuration      #"
        echo " #                                                                           #"
        echo " # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #"
        echo
        echo
        sleep 1

        #
        #  Setup base system
        #

        # get the sources up to date and install packages
        apt update  > /dev/null 2>&1
	systemctl enable --now systemd-networkd  2>&1
	rfkill unblock all
	if df -h | grep -s 'mmcblk0p2  1'; then
		apt install cloud-guest-utils  > /dev/null 2>&1
	        set -euo pipefail
	        ROOT_DEV=$(findmnt / -o SOURCE -n)
	        ROOT_PART_NUM=$(echo "$ROOT_DEV" | grep -o '[0-9]*$')
	        ROOT_DISK=$(echo "$ROOT_DEV" | sed "s/p[0-9]*$//")

	        if [[ ! $ROOT_PART_NUM =~ ^[0-9]+$ ]] || [[ -z $ROOT_DISK ]]; then
	            echo "ERROR: Could not detect root partition/disk." >&2
	            break
	        fi

	        echo "Growing partition $ROOT_PART_NUM on $ROOT_DISK ..."
	        growpart "$ROOT_DISK" "$ROOT_PART_NUM" -N   # -N = dry-run first
	        growpart "$ROOT_DISK" "$ROOT_PART_NUM"

	        echo "Resizing filesystem on $ROOT_DEV ..."
	        resize2fs "$ROOT_DEV"

	        echo "Root filesystem expanded successfully!"
	fi
        echo -n "Updating system packages..."


        apt upgrade -y > /dev/null 2>&1
        echo -n "."

        # Remove the question about the iperf daemon during apt install
        echo "iperf3 iperf3/start_daemon boolean true" | debconf-set-selections

        # Install packages for this system
        apt install -y ipcalc nmap lshw tcpdump net-tools nftables wireless-tools iperf3\
          \radvd bridge-utils firmware-mediatek libnss-mdns syncthing networkd-dispatcher hostapd\
          libgps-dev libcap-dev mumble-server screen arping bc jq git linux-headers-rpi-v8 \
          python3-protobuf unzip chrony build-essential nmap-common systemd-resolved > /dev/null 2>&1
        echo "Done"

        echo "Disabling APT timers for automatic updates..."
        systemctl disable apt-daily.timer
        systemctl disable apt-daily-upgrade.timer

        # Build the Morse Micro halow driver
#        cd /root/morse_driver
#        make -j$(nproc) \
#          CONFIG_WLAN_VENDOR_MORSE=m \
#          CONFIG_MORSE_SDIO=y \
#          -C /lib/modules/$(uname -r)/build \
#          M=$(pwd) \
#          modules

        # Install
#        make -C /lib/modules/$(uname -r)/build M=$(pwd) modules_install
#        depmod -a

        # Get firmware
        cd /root/morse-firmware
        make install
        echo "Morse Micro 802.11ah system insstalled"

        # disable the default wpa_supplicant service
        systemctl disable wpa_supplicant.service

        #set hostname, make unique by ethernet mac addr (last 4)
        hostnamectl hostname radio-$HOST_MAC
        echo "Hostname set"


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
        # Enable system services
        #

        systemctl enable chrony.service
        systemctl disable systemd-networkd-wait-online.service


	cat <<- EOF > /etc/systemd/resolved.conf
		[Resolve]
		LLMNR=no
		MulticastDNS=no
		DNSStubListener=yes
		Cache=yes
	EOF

        #make mumble server ini changes
        sed -i '/ice="tcp -h 127.0.0.1 -p 6502"/s/^#//g' /etc/mumble-server.ini
        sed -i 's/icesecretwrite/;icesecretwrite/g' /etc/mumble-server.ini
        service mumble-server restart
        grep -m 1 SuperUser /var/log/mumble-server/mumble-server.log > /root/mumble_pw

        # install mediaMTX server
        groupadd --system mediamtx
        useradd --system -g mediamtx -d /opt/mediamtx -s /sbin/nologin mediamtx
        mkdir /etc/mediamtx && chown mediamtx:mediamtx /etc/mediamtx
        mkdir -p /opt/mediamtx
        cd /root
        cp mediamtx /opt/mediamtx/
        chmod +x /opt/mediamtx/mediamtx
        cp mediamtx.yml /etc/mediamtx/

        systemctl enable mediamtx

	# unblock wifi
	raspi-config nonint do_wifi_country US

        echo "setting radio-setup.sh to run at next reboot"
        #set up the second provisioning script to run at boot
	cat <<- EOF > /etc/systemd/system/radio-setup-run-once.service
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

}


main "$@"

#install the modified kernel
dpkg -i /root/linux-image-*.deb /root/linux-headers-*.deb

#move kernel from where it was placed by dpkg
mv /boot/*6.6.78-manet* /boot/firmware/

#edit config.txt to make new kernel live
sed 's/^#r#//' /boot/firmware/config.txt

reboot
