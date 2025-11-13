#!/bin/bash
#  A script to finalize the setup of a radio after imaging and a first boot
#
#  This script can be re-run to set new wifi setings from a config server
#



# The default URL to the active configuration file on the Docker host.
CONFIG_URL="https://10.30.1.1:8081/data/active.conf"

# Parse command-line options for alternative URL
while [[ $# -gt 0 ]]; do
    case $1 in
        -u|--url)
            CONFIG_URL="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1" >&2
            echo "Usage: $0 [-u|--url <url>]" >&2
            exit 1
            ;;
    esac
done

echo
echo " # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #"
echo " #                                                                           #"
if systemctl is-enabled radio-setup-run-once.service >/dev/null 2>&1; then
	echo " #   This mesh node is being provisioned for the first time.  Basic setup    #"
	echo " #   is now continuing.  This node will reboot one more time when done       #"
else
	echo " #   Re-configuring mesh node with config from:                              #"
	echo "     $CONFIG_URL "
fi
echo " #                                                                           #"
echo " # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #"
echo
echo
sleep 1


### configure mesh defaults, ** these should not get used **
KEY='qhiUvWC3sgxmvgisF+bBeiSjgBlYuN8DczaCgw=='  # a random but not unique key
MESH_NAME="test-01"
FREQS=("2412" "5180")
REG=US  #wifi regulatory region
#CERT_PATH="/root/server.crt"  #allows ssl to the config server


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
#done < <(curl -s --cacert "$CERT_PATH" "$CONFIG_URL")
done < <(curl -k  -s "$CONFIG_URL")

echo "--- Configuration $config_name loaded successfully ---"
echo
echo "Applying settings..."
sleep 0.5
if [[ -n "$mesh_key" ]]; then
	KEY=$mesh_key
	echo " > Using SAE Key: $KEY"
	sleep 0.5
fi

if [[ -n "$mesh_ssid" ]]; then
	echo " > Setting mesh SSID to: $mesh_ssid"
	MESH_NAME=$mesh_ssid
	sleep 0.5
fi

if [[ -n "$new_root_password" ]]; then
	echo " > Setting root password..."
	echo "root:$new_root_password" | chpasswd
fi

if [[ -n "$new_user_password" ]]; then
	echo " > Setting password for user 'radio'..."
	echo "radio:$new_user_password" | chpasswd
fi

#if [[ -n "$hardware_selection" ]]; then
#	echo " > Hardware selected: $hardware_selection"
	# do selections based on "$hardware_selection"
#fi

#if echo $enable_tak_server | grep true; then
#    echo " > Enabling OpenTAKServer..."
#    #do tak stuff, not yet enabled
#fi
if [[ -n "$ipv4_network" ]]; then
	echo " > Setting IPv4 network settings..."
	# Create the configuration file for the IPv4 manager
	cat <<- EOF > /etc/mesh_ipv4.conf
		IPV4_NETWORK="${ipv4_network}/${ipv4_cidr}"
	EOF
	sleep 0.5
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


# First identify mesh and non mesh wlan interfaces

mesh_ifaces=()
nonmesh_ifaces=()

for phy in $(iw dev | awk '/^phy#/{print $1}'); do
    # Convert 'phy#0' → 'phy0'
    phyname=${phy//#/}

    # Find interface(s) for this PHY
    iface=$(iw dev | awk -v phy="$phy" '
        $1=="Interface" {print $2}
    ')

    # Check if it supports mesh
    if iw phy "$phyname" info | grep -q "mesh point"; then
        mesh_ifaces+=("$iface")
    else
        nonmesh_ifaces+=("$iface")
    fi
done

#keep track of this across reboots
> /var/lib/mesh_if
> /var/lib/no_mesh_if

# Bring everything down before renaming
for iface in "${mesh_ifaces[@]}" "${nonmesh_ifaces[@]}"; do
    ip link set "$iface" down 2>/dev/null
done

# Rename mesh-capable ones first, we want them to be wlan0 and wlan1
i=0
for iface in "${mesh_ifaces[@]}"; do
    newname="wlan$i"
    echo $newname >> /var/lib/mesh_if
    ip link set "$iface" name "$newname"
    ((i++))
done

# Rename non-mesh after mesh-capable
for iface in "${nonmesh_ifaces[@]}"; do
    newname="wlan$i"
    echo $newname >> /var/lib/no_mesh_if
    ip link set "$iface" name "$newname"
    ((i++))
done

# Bring them back up
for iface in $(ip -o link show | awk -F': ' '{print $2}' | grep wlan); do
    ip link set "$iface" up 2>/dev/null
done

for WLAN in `cat /var/lib/mesh_if`; do
	echo " > Setting SAE key/SSID for $WLAN ..."
	#create wpa supplicant configs
	echo "MESH_NAME=\"$MESH_NAME\"" > /etc/default/mesh
	cat <<-EOF > /etc/wpa_supplicant/wpa_supplicant-$WLAN-lobby.conf
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
		    mesh_fwding=0
		}
	EOF

	#create the network interface config
	cat <<-EOF >  /etc/systemd/network/30-$WLAN.network
		[Match]
		MACAddress=`ip a | grep -A1 $WLAN | awk '/ether/ {print $2}'`

		[Network]

		[Link]
		RequiredForOnline=no
		MTUBytes=1560
	EOF

	cat <<-EOF >  /etc/systemd/network/10-$WLAN.link
		[Match]
		MACAddress=`ip a | grep -A1 $WLAN | awk '/ether/ {print $2}'`

		[Link]
		Name=$WLAN
	EOF

    echo " > Enabling $WLAN for mesh use ..."
	#start up wpa_supplicant at boot for this interface
	systemctl enable wpa_supplicant@wlan$CT.service
	((CT++))
done
for WLAN in `cat /var/lib/no_mesh_if | head -n 1`; do
	echo " > Setting up $WLAN as a client AP ..."

	echo "   > creating networkd file ..."
	cat <<- EOF > /etc/systemd/network/30-$WLAN.network
		[Match]
		Name=$WLAN

		[Link]
		Unmanaged=yes
		ActivationPolicy=manual
	EOF

	echo "   > creating systemd tx power service ... "
	#set this wlan interface to have a low (5db) tx power
	cat <<- EOF > /etc/systemd/system/wlan-txpower.service
		[Unit]
		Description=Set low TX power on wlan interface
		Before=hostapd.service
		After=network.target

		[Service]
		Type=oneshot
		ExecStart=/usr/sbin/iw dev $WLAN set txpower fixed 1000
		RemainAfterExit=yes

		[Install]
		WantedBy=multi-user.target
	EOF
	systemctl enable --now wlan-txpower.service

	ip link set wlan0 down
	echo "   > creating systemd hostapd service ... "
	#set up hotsapd for this wlan to be an AP for the EUD
	cat <<- EOF > /etc/hostapd/hostapd.conf
		interface=$WLAN
		driver=nl80211
		ssid=$(hostname)
		hw_mode=a
		channel=36
		ieee80211n=1
		ieee80211ac=1
		wmm_enabled=1
		auth_algs=1
		wpa=2
		wpa_key_mgmt=WPA-PSK
		rsn_pairwise=CCMP
		wpa_passphrase=eudtest1!
		country_code=US	
	EOF
	systemctl unmask hostapd
	systemctl enable --now hostapd
done


#
#	System service setup
#

# Replace wpa_supplicant with default files at boot

cat <<- EOF > /etc/systemd/system/mesh-boot-lobby.service
	[Unit]
	Description=Set mesh interfaces to Lobby channels
	Before=wpa_supplicant@.service

	[Service]
	Type=oneshot
	ExecStart=/bin/sh -c 'for LOBBY_FILE in /etc/wpa_supplicant/wlan*-lobby.conf; do DEST_FILE="${LOBBY_FILE%-lobby.conf}.conf"; cp "$LOBBY_FILE" "$DEST_FILE"; done'
	RemainAfterExit=yes

	[Install]
	WantedBy=multi-user.target
EOF
systemctl enable mesh-boot-lobby.service

#get bat0 a link local address for alfred
cat <<- EOF > /etc/sysctl.d/99-batman.conf
	# Enable IPv6 address generation on batman-adv interfaces
	net.ipv6.conf.bat0.disable_ipv6 = 0
	net.ipv6.conf.bat0.addr_gen_mode = 0
	net.ipv6.conf.br0.disable_ipv6 = 0
	net.ipv6.conf.br0.accept_ra = 1
EOF

# Build dependency strings to make batman-enslave service file
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
	After=network-online.target ${AFTER_DEVICES} ${WANTS_SERVICES}
	Wants=network-online.target ${WANTS_SERVICES}

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
	# Wait for bat0 device to exist and be up
	After=network-online.target
	Wants=network-online.target
	Requires=batman-enslave.service


	[Service]
	Type=simple
	ExecStartPre=/bin/bash -c 'for i in {1..20}; do if ip -6 addr show dev bat0 | grep "inet6 fe80::" | grep -qv "tentative"; then exit 0; fi; sleep 1; done; echo "bat0 link-local IPv6 address not ready" >&2; exit 1'
	# Add -m to run alfred in master mode, allowing it to accept client data
	ExecStart=/usr/sbin/alfred -m -i br0 -f
	UMask=0000
	Restart=always
	RestartSec=10

	[Install]
	WantedBy=multi-user.target
EOF
systemctl enable alfred.service

# This script handles IPv4 addressing and node status gossip via alfred
cat <<- EOF > /etc/systemd/system/node-manager.service
	[Unit]
	Description=Mesh Node Status Manager and IPv4 Coordinator
	# This must run after alfred is available
	After=alfred.service
	Wants=alfred.service

	[Service]
	Type=simple
	ExecStart=/usr/local/bin/node-manager.sh
	Restart=on-failure
	RestartSec=15

	[Install]
	WantedBy=multi-user.target
EOF
systemctl enable node-manager.service

cat <<- EOF > /etc/systemd/system/syncthing-peer-manager.service 
	[Unit]
	Description=Syncthing Peer Manager for B.A.T.M.A.N. Mesh
	After=syncthing@radio.service alfred.service
	Wants=syncthing@radio.service alfred.service

	[Service]
	Type=simple
	ExecStart=/usr/local/bin/syncthing-peer-manager.sh
	Restart=on-failure
	RestartSec=30

	[Install]
	WantedBy=multi-user.target
EOF
systemctl enable syncthing-peer-manager.service

#creates a shared directory in /home/radio
systemctl enable syncthing@radio.service
systemctl enable nftables.service

systemctl daemon-reload

#install scripts for auto gateway management
cp /root/networkd-dispatcher/off /etc/networkd-dispatcher/off.d/50-gateway-disable
cp /root/networkd-dispatcher/off /etc/networkd-dispatcher/no-carrier.d/50-gateway-disable
cp /root/networkd-dispatcher/off /etc/networkd-dispatcher/degraded.d/50-gateway-disable
cp /root/networkd-dispatcher/routable /etc/networkd-dispatcher/routable.d/50-gateway-enable
chmod -R 755 /etc/networkd-dispatcher

#enable automatic gateway selection
cat <<- EOF > /etc/systemd/system/gateway-route-manager.service
	[Unit]
	Description=Mesh Gateway Route Manager
	Documentation=man:batctl(8)
	After=network.target node-manager.service
	Wants=node-manager.service
	ConditionPathExists=/usr/local/bin/gateway-route-manager.sh

	[Service]
	Type=simple
	ExecStart=/usr/local/bin/gateway-route-manager.sh
	Restart=always
	RestartSec=10

	User=root

	# Logging
	StandardOutput=journal
	StandardError=journal
	SyslogIdentifier=gateway-route-manager

	[Install]
	WantedBy=multi-user.target
EOF
systemctl enable gateway-route-manager

cat <<- EOF > /etc/systemd/system/mesh-shutdown.service
	[Unit]
	Description=Mesh Network Graceful Shutdown
	DefaultDependencies=no
	Before=shutdown.target reboot.target halt.target
	Requires=alfred.service

	[Service]
	Type=oneshot
	ExecStart=/usr/local/bin/mesh-shutdown.sh
	TimeoutStartSec=10
	RemainAfterExit=yes

	[Install]
	WantedBy=halt.target reboot.target shutdown.target
EOF
systemctl enable mesh-shutdown.service

# Determine if this script is being run for the first time
# and reboot if so to pick up the changes to the interfaces
if systemctl is-enabled radio-setup-run-once.service >/dev/null 2>&1; then
	echo " >> Swapping from network manager to networkd"
	systemctl enable systemd-networkd
	systemctl enable systemd-resolved
	systemctl disable NetworkManager
	apt purge -y network-manager
	ln -sf /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf
	
	echo " >> Removing radio-setup-run-once.service"
	systemctl disable radio-setup-run-once.service
	rm /etc/systemd/system/radio-setup-run-once.service
	echo " >> Doing initial Syncthing config..."
	sudo -u radio syncthing -generate="/home/radio/.config/syncthing"
	sleep 5
	killall syncthing
	SYNCTHING_CONFIG="/home/radio/.config/syncthing/config.xml"
	echo " >> Hardening Syncthing for local-only operation..."
	#disable global discovery and relaying
	sed -i '/<options>/a <globalAnnounceEnabled>false</globalAnnounceEnabled>\n<relaysEnabled>false</relaysEnabled>' "$SYNCTHING_CONFIG"
	# replace the gui block to set the address
	sed -i 's|<gui enabled="true" tls="false" debugging="false">.*</gui>|<gui enabled="true" tls="false" debugging="false">\n        <address>127.0.0.1:8384</address>\n    </gui>|' "$SYNCTHING_CONFIG"
	#make it clear we're done
	echo " -- CONFIGURED -- " >> /etc/issue
	reboot
fi

echo " > restarting networkd..."
systemctl restart systemd-networkd

echo " > resetting ipv4..."
systemctl restart node-manager


sleep 6 # wait for wpa_supplicant to catch up
echo " > resetting BATMAN-ADV bond..."
systemctl restart batman-enslave.service

echo " > restarting alfred..."
systemctl restart alfred.service

sleep 2
networkctl
iw dev
ip -br a

