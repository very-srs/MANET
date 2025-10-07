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

echo -n "--- Configuration $config_name loaded successfully ---\n\n"
echo "Applying settings..."

if [[ -n "$sae_key" ]]; then
	KEY=$sae_key
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

if echo $enable_tak_server | grep true; then
    echo " > Enabling OpenTAKServer..."
    #do tak stuff, not yet enabled
fi


#
# Finish setting up network devices (wireless)
#

CT=0
for WLAN in `networkctl | awk '/wlan/ {print $2}'`; do
	echo " > Setting SAE key/SSID for $WLAN ..."
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
	systemctl restart wpa_supplicant@wlan$CT.service
	((CT++))
done


#
#	System service setup
#

# Enslave interfaces to Batman, create second Batman interface for Alfred to use
# This is needed to be done with a system service due to batadv not being
# added to networkd in bookworm
cat <<- EOF > /etc/systemd/system/batman-enslave.service
	[Unit]
	Description=Enslave wlan interfaces to bat0 for BATMAN Advanced

	# Wait for the network devices AND wpa_supplicant to be ready
	After=sys-subsystem-net-devices-wlan0.device sys-subsystem-net-devices-wlan1.device
	After=wpa_supplicant@wlan0.service wpa_supplicant@wlan1.service
	Wants=wpa_supplicant@wlan0.service wpa_supplicant@wlan1.service

	[Service]
	Type=oneshot
	RemainAfterExit=yes

	ExecStart=/usr/bin/ip link add name bat0 type batadv
	ExecStart=/usr/bin/ip link add name bat1 type batadv

	ExecStart=/usr/bin/ip link set wlan0 up
	ExecStart=/usr/bin/ip link set wlan1 up

	# Enslave the interfaces to bat0
	ExecStart=/usr/sbin/batctl if add wlan0
	ExecStart=/usr/sbin/batctl if add wlan1
	ExecStart=/usr/bin/ip link set bat1 up

	# Clean up when the service is stopped
	ExecStop=/usr/sbin/batctl if del wlan0
	ExecStop=/usr/sbin/batctl if del wlan1

	[Install]
	WantedBy=multi-user.target
EOF
systemctl enable batman-enslave.service

# Start an alfred master listener at boot for mesh data messages
cat <<- EOF > /etc/systemd/system/alfred.service
	[Unit]
	Description=B.A.T.M.A.N. Advanced Layer 2 Forwarding Daemon
	After=network-online.target
	Wants=network-online.target

	[Service]
	Type=simple
	# Add -m to run alfred in master mode, allowing it to accept client data
	ExecStart=/usr/sbin/alfred -m -i bat1
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
systemctl restart ipv4-manager.service

systemctl daemon-reload
systemctl restart avahi-daemon

echo "> Clearing reboot job from cron tab..."
crontab -r

# Determine if this script is being run for the first time
# and reboot if so to pick up the changes to the interfaces
for WLAN in `networkctl | awk '/wlan/ {print $2}'`; do
	if ! echo $WLAN | grep wlan[0-9]; then
		echo " > First run detected, rebooting..."
		sleep 2
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

echo "Radio settings updated"
sleep 2
networkctl
iw dev
ip -br a

