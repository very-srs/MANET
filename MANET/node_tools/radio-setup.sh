#!/bin/bash
#  A script to finalize the setup of a radio after imaging and a first boot
#
#  This script can be re-run to set new network setings
#  if the mesh config file is updated
#


# log the output of this script to a file for debugging
exec > >(tee /boot/firmware/radio-setup.log) 2>&1
set -x

# default lobby frequencies for wifi
FREQS=("2412" "5180")

# This loop reads the stored setup variables to set the current config
while IFS= read -r line; do
    # Skip empty lines
    if [[ -z "$line" ]]; then
        continue
    fi

    # Split the line into a key and a value at the first ": "
    key="${line%%=*}"
    value="${line#*=}"

    sanitized_key=$(echo "$key" | sed 's/-/_/g' | tr -cd '[:alnum:]_')

    # Check if the key is not empty after sanitization
    if [[ -n "$sanitized_key" ]]; then
        # Export the sanitized key as an environment variable with its value.
        export "$sanitized_key=$value"
        echo "Checking config: $sanitized_key"
    fi
done < <(cat /etc/mesh.conf)

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

if [[ -n "$ssh_public_key" ]]; then
	echo " > Updating authorized_keys for user 'radio'..."
	mkdir -p /home/radio/.ssh
	echo "$ssh_public_key" >> /home/radio/.ssh/authorized_keys
	awk '!seen[$0]++' /home/radio/.ssh/authorized_keys > /tmp/t
	mv /tmp/t /home/radio/.ssh/authorized_keys
fi

sleep 0.5
echo "testing acs variable"
if [[ -n "$acsn" ]]; then
	echo "acs defined as $acsn"
	sleep 0.5
        if [[ "$acs" == "Y" ]]; then
	        echo " > This mesh will channel hop ..."
		cp /usr/local/bin/node-manager-acs.sh /usr/local/bin/node-manager.sh
	else
		echo " > This mesh will remain on a static channel ..."
		cp /usr/local/bin/node-manager-static.sh /usr/local/bin/node-manager.sh
	fi
fi

sleep 2

#
# Finish setting up network devices (wireless)
#

# A system service to force mesh point mode on the wlan interfaces
cat << EOF > /etc/systemd/system/mesh-interface-setup@.service
[Unit]
Description=Set %I to mesh point mode
Before=wpa_supplicant@%i.service
BindsTo=sys-subsystem-net-devices-%i.device
After=sys-subsystem-net-devices-%i.device

[Service]
Type=oneshot
ExecStartPre=/bin/sleep 1
ExecStart=/usr/sbin/ip link set %I down
ExecStart=/usr/sbin/iw dev %I set type mp
ExecStart=/usr/sbin/ip link set %I up
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

echo REGDOMAIN=US > /etc/default/crda

# First identify mesh and non mesh wlan interfaces
mesh_ifaces=()
halow_ifaces=()
nonmesh_ifaces=()

for phy in $(iw dev | awk '/^phy#/{print $1}'); do
    # Convert 'phy#0' → 'phy0'
    phyname=${phy//#/}

    # Find interface(s) for this PHY
    iface=$(iw dev | awk -v target="$phy" '
        $1 ~ /^phy#/ { current_phy = $1 }
        current_phy == target && $1 == "Interface" { print $2 }
    ')

    # Check if it's HaLow (802.11ah) - look for Band 7 or morse driver
	if iw phy "$phyname" info | grep -q "0.5 Mbps"; then
        halow_ifaces+=("$iface")
    elif iw phy "$phyname" info | grep -q "mesh point"; then
        mesh_ifaces+=("$iface")
    else
        nonmesh_ifaces+=("$iface")
    fi
done

# Keep track across reboots
> /var/lib/mesh_if
> /var/lib/halow_if
> /var/lib/no_mesh_if

# Bring everything down before renaming
for iface in "${mesh_ifaces[@]}" "${halow_ifaces[@]}" "${nonmesh_ifaces[@]}"; do
    ip link set "$iface" down 2>/dev/null
done

# Rename regular mesh-capable ones first (wlan0, wlan1)
i=0
for iface in "${mesh_ifaces[@]}"; do
    newname="wlan$i"
    echo $newname >> /var/lib/mesh_if
    ip link set "$iface" name "$newname"
    ((i++))
done

# Rename HaLow interfaces (wlan2, wlan3, etc.)
for iface in "${halow_ifaces[@]}"; do
    newname="wlan$i"
    echo $newname >> /var/lib/halow_if
    ip link set "$iface" name "$newname"
    ((i++))
done

# Rename non-mesh after all mesh-capable
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

CT=0
for WLAN in `cat /var/lib/mesh_if`; do
	echo " > Setting SAE key/SSID for $WLAN ..."
	#create wpa supplicant configs
	echo "MESH_NAME=\"$MESH_NAME\"" > /etc/default/mesh
cat <<-EOF > /etc/wpa_supplicant/wpa_supplicant-$WLAN-lobby.conf
ctrl_interface=/var/run/wpa_supplicant
country=US
update_config=1
sae_pwe=1
ap_scan=2
network={
    ssid="$MESH_NAME"
    mode=5
    frequency=${FREQS[$CT]}
    key_mgmt=SAE
    sae_password="$KEY"
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
	cp /etc/wpa_supplicant/wpa_supplicant-$WLAN-lobby.conf /etc/wpa_supplicant/wpa_supplicant-$WLAN.conf
	#start up wpa_supplicant at boot for this interface
	systemctl enable wpa_supplicant@$WLAN.service
	((CT++))
done

# this will be used for rpi5's built in wlan interface
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

#systemctl enable mesh-interface-setup@$WLAN


echo "   > creating systemd tx power service ... "
##set this wlan interface to have a low (5db) tx power
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
hannel=36
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
#	systemctl unmask hostapd
#	systemctl enable --now hostapd
done

for WLAN in `cat /var/lib/halow_if | head -n 1`; do
	echo " > Setting up $WLAN for HaLow use ..."
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

rm /etc/wpa_supplicant/*${WLAN}* 2>/dev/null

cat << EOF > /etc/wpa_supplicant/wpa_supplicant-$WLAN-s1g.conf
country=US
ctrl_interface=/var/run/wpa_supplicant_s1g
sae_pwe=1
max_peer_links=10
mesh_fwding=0
network={
    ssid="$mesh_ssid"
    key_mgmt=SAE
    mode=5
    channel=12
    op_class=71
    country="US"
    s1g_prim_chwidth=1
    s1g_prim_1mhz_chan_index=3
    dtim_period=1
    mesh_rssi_threshold=-85
    dot11MeshHWMPRootMode=0
    dot11MeshGateAnnouncements=0
    mbca_config=1
    mbca_min_beacon_gap_ms=25
    mbca_tbtt_adj_interval_sec=60
    dot11MeshBeaconTimingReportInterval=10
    mbss_start_scan_duration_ms=2048
    mesh_beaconless_mode=0
    mesh_dynamic_peering=0
    sae_password="$mesh_key"
    pairwise=CCMP
    ieee80211w=2
    beacon_int=1000
}
EOF


cat << EOF > /etc/systemd/system/wpa_supplicant-s1g-$WLAN.service 
[Unit]
Description=WPA supplicant (S1G/HaLow) for $WLAN
After=morse-delayed-load.service
Requires=morse-delayed-load.service

[Service]
Type=simple
ExecStartPre=/bin/sleep 3
ExecStart=/usr/sbin/wpa_supplicant_s1g -c /etc/wpa_supplicant/wpa_supplicant-$WLAN-s1g.conf -i $WLAN -D nl80211
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl disable wpa_supplicant_s1g@$WLAN.service
systemctl enable wpa_supplicant-s1g-$WLAN.service

done


#stop this from loading at boot, happens too quickly
echo "blacklist morse" > /etc/modprobe.d/morse-blacklist.conf
echo "options cfg80211 ieee80211_regdom=US" > /etc/modprobe.d/cfg80211.conf

cat << EOF > /etc/systemd/system/morse-delayed-load.service
[Unit]
Description=Load Morse HaLow driver with delay
After=network.target systemd-modules-load.service
Before=wpa_supplicant-s1g-wlan2.service

[Service]
Type=oneshot
# Wait for system to stabilize
ExecStartPre=/bin/sleep 3
# Load dependencies first
ExecStart=/sbin/modprobe dot11ah
# Then load morse driver
ExecStart=/sbin/modprobe morse
# Wait for interface to appear
ExecStartPost=/bin/bash -c 'for i in {1..10}; do ip link show wlan2 && break || sleep 1; done'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl enable morse-delayed-load.service


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
ExecStart=/bin/sh -c 'for LOBBY_FILE in /etc/wpa_supplicant/wpa_supplicant-wlan*-lobby.conf; do DEST_FILE="\${LOBBY_FILE%-lobby.conf}.conf"; cp "\$LOBBY_FILE" "\$DEST_FILE"; done'
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
for WLAN in `cat /var/lib/mesh_if`; do
    AFTER_DEVICES+="sys-subsystem-net-devices-wlan$INT_CT.device "
    WANTS_SERVICES+="wpa_supplicant@wlan$INT_CT.service "
	((INT_CT++))
done
for WLAN in `cat /var/lib/halow_if | head -n 1`; do
    AFTER_DEVICES+="sys-subsystem-net-devices-$WLAN.device "
    WANTS_SERVICES+="wpa_supplicant-s1g-$WLAN.service "
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
systemctl disable nftables.service

systemctl daemon-reload

#install scripts for auto gateway management
cp /root/networkd-dispatcher/off /etc/networkd-dispatcher/off.d/50-gateway-disable
cp /root/networkd-dispatcher/off /etc/networkd-dispatcher/no-carrier.d/50-gateway-disable
cp /root/networkd-dispatcher/off /etc/networkd-dispatcher/degraded.d/50-gateway-disable
cp /root/networkd-dispatcher/routable /etc/networkd-dispatcher/routable.d/50-gateway-enable
chmod -R 755 /etc/networkd-dispatcher

cp /root/regulatory.db /lib/firmware/

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

###  removed from kernel
# Prevent RFkill from disabling interfaces
#rfkill unblock all
#cat <<- EOF > /etc/udev/rules.d/99-rfkill-unblock.conf
# Automatically unblock all rfkill switches
#SUBSYSTEM=="rfkill", ACTION=="add", RUN+="/usr/sbin/rfkill unblock %k"
#EOF

# Determine if this script is being run for the first time
# and reboot if so to pick up the changes to the interfaces
if systemctl is-enabled radio-setup-run-once.service >/dev/null 2>&1; then
	apt remove -y network-manager avahi*
	systemctl mask rpi-eeprom-update.service
	systemctl set-default multi-user.target

	echo " >> Removing radio-setup-run-once.service"
	systemctl disable radio-setup-run-once.service

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
