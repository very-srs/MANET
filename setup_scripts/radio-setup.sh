#!/bin/bash
#  A script to finalize the setup of a radio after imaging and a first boot
#
#  This script can be re-run to set new wifi setings from a config server
#


# The URL to the active configuration file on your Docker host.
CONFIG_URL="https://10.30.1.1:8081/data/active.conf"

### configure mesh defaults, these should not get used
KEY=`head -c 28 /dev/urandom | base64`
MESH_NAME="test-01"
FREQS=("2412" "5180")
REG=US  #wifi regulatory region
CERT_PATH="/root/server.crt"  #allows ssl to the config server


# This loop reads the output from curl to set config variables
echo "--- Fetching configuration from $CONFIG_URL ---"
while IFS= read -r line; do
    # Skip empty lines
    if [[ -z "$line" ]]; then
        continue
    fi

    # Split the line into a key and a value at the first ": "
    key="${line%%: *}"
    value="${line#*: }"

    sanitized_key=$(echo "$key" | sed 's/-/_/g' | tr -cd '[:alnum:]_')

    # Check if the key is not empty after sanitization
    if [[ -n "$sanitized_key" ]]; then
        # Export the sanitized key as an environment variable with its value.
        export "$sanitized_key=$value"
        echo "Checking config: $sanitized_key"
    fi
done < <(curl -s --cacert "$CERT_PATH" "$CONFIG_URL")

echo "--- Configuration $config_name loaded successfully ---"
echo
echo "Applying settings..."

if [[ -n "$mesh_key" ]]; then
	KEY=$mesh_key
	echo " > Using SAE Key: $KEY"
fi

if [[ -n "$mesh_ssid" ]]; then
	echo " > Setting mesh SSID to: $mesh_ssid"
	MESH_NAME=$mesh_ssid
fi

if [[ -n "$new_root_password" ]]; then
	echo " > Setting root password..."
	echo "root:$new_root_password" | chpasswd
fi

if [[ -n "$new_user_password" ]]; then
	echo " > Setting password for user 'radio'..."
	echo "radio:$new_user_password" | chpasswd
fi

if [[ -n "$hardware_selection" ]]; then
	echo " > Hardware selected: $hardware_selection"
	# do selections based on "$hardware_selection"
fi

#if echo $enable_tak_server | grep true; then
#    echo " > Enabling OpenTAKServer..."
#    #do tak stuff, not yet enabled
#fi

if [[ -n "$ipv4_network" ]]; then
	echo " > Setting IPv4 network settings..."
	# Create the configuration file for the IPv4 manager
	cat <<- EOF > /etc/mesh_ipv4.conf
		IPV4_SUBNET="$(echo "$ipv4_network" | cut -d'.' -f1-3)"
		IPV4_MASK="/${ipv4_cidr}"
	EOF
fi

if [[ -n "$ssh_public_key" ]]; then
	echo " > Updating authorized_keys for user 'radio'..."
	mkdir -p /home/radio/.ssh
	echo "$ssh_public_key" >> /home/radio/.ssh/authorized_keys
	awk '!seen[$0]++' /home/radio/.ssh/authorized_keys > /tmp/t
	mv /tmp/t /home/radio/.ssh/authorized_keys
fi



#
# Finish setting up network devices (wireless)
#

CT=0
for WLAN in `networkctl | awk '/wlan/ {print $2}'`; do
	echo " > Setting SAE key/SSID for wlan$CT ..."
	#create wpa supplicant configs
	cat <<-EOF > /etc/wpa_supplicant/wpa_supplicant-wlan$CT.conf
		ctrl_interface=/var/run/wpa_supplicant
		update_config=1
		sae_pwe=1
		ap_scan=2
		network={
		    ssid="$MESH_NAME"
		    mode=5
		    frequency=${FREQS[$CT]}
		    key_mgmt=SAE
		    psk="$KEY"
		    ieee80211w=2
		}
	EOF

	#create the network interface config
	cat <<-EOF >  /etc/systemd/network/30-wlan$CT.network
		[Match]
		MACAddress=`ip a | grep -A1 $WLAN | awk '/ether/ {print $2}'`

		[Network]

		[Link]
		RequiredForOnline=no
		MTUBytes=1560
	EOF

	cat <<-EOF >  /etc/systemd/network/10-wlan$CT.link
		[Match]
		MACAddress=`ip a | grep -A1 $WLAN | awk '/ether/ {print $2}'`

		[Link]
		Name=wlan$CT
		Type=mesh
	EOF

    echo " > Enabling wlan$CT..."
	#start up wpa_supplicant at boot for this interface
	systemctl enable wpa_supplicant@wlan$CT.service
	((CT++))
done


#
#	System service setup
#

# Enslave interfaces to Batman, create second Batman interface for Alfred to use
# Create the batman interface setup script
cat <<- 'EOF' > /usr/local/bin/batman-if-setup.sh
	#!/bin/bash
	set -e

	WLAN_INTERFACES=$(networkctl | awk '/wlan/ {print $2}' | tr '\n' ' ')

	start() {
	    echo "Starting BATMAN interfaces..."
	    # Only create interface if it does not exist
	    ip link show bat0 &>/dev/null || ip link add name bat0 type batadv

	    for WLAN in $WLAN_INTERFACES; do
	        ip link set "$WLAN" up
	        batctl bat0 if add "$WLAN"
	    done

	    ip link set bat0 up
	}

	stop() {
	    echo "Stopping BATMAN interfaces..."
	    for WLAN in $WLAN_INTERFACES; do
	        # Only try to remove interface if it is actually attached
	        if batctl bat0 if | grep -q "$WLAN"; then
	             batctl bat0 if del "$WLAN"
	        fi
	    done

	    ip link show bat0 &>/dev/null && ip link del bat0
	}

	case "$1" in
	    start)
	        start
	        ;;
	    stop)
	        stop
	        ;;
	    *)
	        echo "Usage: $0 {start|stop}"
	        exit 1
	        ;;
	esac
EOF
chmod +x /usr/local/bin/batman-if-setup.sh

#get bat0 a link local address for alfred
cat <<- EOF > /etc/sysctl.d/99-batman.conf
	# Enable IPv6 address generation on batman-adv interfaces
	net.ipv6.conf.bat0.disable_ipv6 = 0
	net.ipv6.conf.bat0.addr_gen_mode = 0
EOF

# Build dependency strings
WLAN_INTERFACES=$(networkctl | awk '/wlan/ {print $2}' | tr '\n' ' ')
AFTER_DEVICES=""
WANTS_SERVICES=""
INT_CT=0
for WLAN in $WLAN_INTERFACES; do
    AFTER_DEVICES+="sys-subsystem-net-devices-wlan$INT_CT.device "
    WANTS_SERVICES+="wpa_supplicant@wlan$INT_CT.service "
	((INT_CT++))
done

# Create the service file
cat <<- EOF > /etc/systemd/system/batman-enslave.service
	[Unit]
	Description=BATMAN Advanced Interface Manager
	After=${AFTER_DEVICES} ${WANTS_SERVICES}
	Wants=${WANTS_SERVICES}

	[Service]
	Type=oneshot
	RemainAfterExit=yes
	ExecStart=/usr/local/bin/batman-if-setup.sh start
	ExecStop=/usr/local/bin/batman-if-setup.sh stop

	[Install]
	WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable batman-enslave.service

# Start an alfred master listener at boot for mesh data messages
cat <<- EOF > /etc/systemd/system/alfred.service
	[Unit]
	Description=B.A.T.M.A.N. Advanced Layer 2 Forwarding Daemon
	After=network-online.target batman-enslave.service
	Wants=network-online.target batman-enslave.service
	Requires=batman-enslave.service


	[Service]
	Type=simple
	ExecStartPre=/bin/bash -c 'for i in {1..10}; do ip -6 addr show dev bat0 | grep -q "fe80::" && exit 0; sleep 1; done; echo "bat0 missing link-local IPv6" >&2; exit 1'
	# Add -m to run alfred in master mode, allowing it to accept client data
	ExecStart=/usr/sbin/alfred -m -i bat0
	UMask=0000
	Restart=always
	RestartSec=10

	[Install]
	WantedBy=multi-user.target
EOF
systemctl enable alfred.service
systemctl restart alfred.service

#start the script for getting ipv4 established
cp /root/ipv4-manager.sh /usr/local/bin/
cat <<- EOF > /etc/systemd/system/ipv4-manager.service
	[Unit]
	Description=Decentralized IPv4 Address Manager
	After=network-online.target
	Wants=network-online.target

	[Service]
	Type=simple
	ExecStart=/usr/local/bin/ipv4-manager.sh
	Restart=on-failure
	RestartSec=10

	[Install]
	WantedBy=multi-user.target
EOF
systemctl enable ipv4-manager.service


cat <<- EOF > /etc/systemd/system/syncthing-peer-manager.service 
	[Unit]
	Description=Syncthing Peer Manager for B.A.T.M.A.N. Mesh
	After=syncthing@radio.service alfred.service
	Wants=syncthing@radio.service alfred.service

	[Service]
	Type=simple
	ExecStart=/usr/local/bin/syncthing-peer-manager.sh
	Restart=on-failure
q	RestartSec=30

	[Install]
	WantedBy=multi-user.target
EOF
systemctl enable syncthing-peer-manager.service

#creates a shared directory in /home/radio
systemctl enable syncthing@radio.service
systemctl enable nftables.service

systemctl daemon-reload
systemctl restart avahi-daemon

#install scripts for auto gateway management
cp /root/networkd-dispatcher/off /etc/networkd-dispatcher/off.d/50-gateway-disable
cp /root/networkd-dispatcher/degraded /etc/networkd-dispatcher/degraded.d/50-gateway-disable
cp /root/networkd-dispatcher/routable /etc/networkd-dispatcher/routable.d/50-gateway-enable
chmod -R 755 /etc/networkd-dispatcher

# Determine if this script is being run for the first time
# and reboot if so to pick up the changes to the interfaces
for WLAN in `networkctl | awk '/wlan/ {print $2}'`; do
	if ! echo $WLAN | grep wlan[0-9]; then
		echo " > First run detected"
		echo " >> Removing radio-setup-run-once.service"
		systemct disable radio-setup-run-once.service
		rm /etc/systemd/system/radio-setup-run-once.service
		echo " >> Doing initial Syncthing config..."
		sudo -u radio syncthing -generate="/home/radio/.config/syncthing"
		sleep 5
		reboot
	fi
done

echo " > restarting networkd..."
systemctl restart systemd-networkd

echo " > restarting avahi..."
systemctl restart avahi-daemon

echo " > resetting ipv4..."
systemctl restart ipv4-manager

sleep 6 # wait for wpa_supplicant to catch up
systemctl restart batman-enslave.service
echo " > resetting BATMAN-ADV bond..."

sleep 2
networkctl
iw dev
ip -br a

