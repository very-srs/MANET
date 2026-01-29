#!/bin/bash
#  A script to finalize the setup of a radio after imaging and a first boot
#
#  This script can be re-run to set new network settings
#  if the mesh config file is updated
#

# log the output of this script to a file for debugging
exec > >(tee /var/log/radio-setup.log) 2>&1
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


echo "Installing morse driver"
mkdir -p /lib/modules/$(uname -r)/extra/morse

# Copy modules
cp /root/morse_driver/morse.ko /lib/modules/$(uname -r)/extra/morse/
cp /root/morse_driver/dot11ah/dot11ah.ko /lib/modules/$(uname -r)/extra/morse/

# Update module dependencies
depmod -a

cp /root/morse_cli/morse_cli /usr/local/bin/
chmod +x /usr/local/bin/*

# Activating drivers
modprobe dot11ah
modprobe morse


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
if [[ -n "$acs" ]]; then
	echo "acs defined as $acs"
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

REGULATORY_DOMAIN=$(grep "^regulatory_domain=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)
REGULATORY_DOMAIN=${REGULATORY_DOMAIN:-US}  # Default to US if not found

echo REGDOMAIN=$REGULATORY_DOMAIN > /etc/default/crda


# Wait for wireless drivers to load
echo "Waiting for wireless drivers to load..."
DRIVER_WAIT_COUNT=0
MAX_DRIVER_WAIT=30  # 60 seconds total

while [ $DRIVER_WAIT_COUNT -lt $MAX_DRIVER_WAIT ]; do
    PHY_COUNT=$(iw dev 2>/dev/null | grep -c "^phy#" || echo "0")

    if [ "$PHY_COUNT" -gt 0 ]; then
        echo "✓ Found $PHY_COUNT wireless PHY(s)"
        break
    fi

    if [ $DRIVER_WAIT_COUNT -eq 0 ]; then
        echo "No wireless interfaces detected yet, waiting for drivers..."
    elif [ $((DRIVER_WAIT_COUNT % 5)) -eq 0 ]; then
        echo "Still waiting... (${DRIVER_WAIT_COUNT}/${MAX_DRIVER_WAIT})"
    fi

    sleep 2
    ((DRIVER_WAIT_COUNT++))
done

if [ "$PHY_COUNT" -eq 0 ]; then
    echo "⚠ WARNING: No wireless interfaces found after $((MAX_DRIVER_WAIT * 2)) seconds"
    echo "  This is normal for wired-only configurations"
    echo "  If you expect wireless: check 'dmesg | grep -i firmware'"
fi

# Detect interfaces, save to files
mesh_ifaces=()
halow_ifaces=()
nonmesh_ifaces=()

for phy in $(iw dev | awk '/^phy#/{print $1}'); do    # Convert 'phy#0' → 'phy0'
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
# Create directory and files even if arrays are empty (supports wired-only configs)
mkdir -p /var/lib
> /var/lib/mesh_if
> /var/lib/halow_if
> /var/lib/no_mesh_if

# Keep track across reboots
for iface in "${mesh_ifaces[@]}"; do
    echo "$iface" >> /var/lib/mesh_if
done

for iface in "${halow_ifaces[@]}"; do
    echo "$iface" >> /var/lib/halow_if
done

for iface in "${nonmesh_ifaces[@]}"; do
    echo "$iface" >> /var/lib/no_mesh_if
done

# Log what we found
echo "Interface detection complete:"
echo "  Mesh-capable: ${#mesh_ifaces[@]} ($(echo ${mesh_ifaces[@]}))"
echo "  HaLow: ${#halow_ifaces[@]} ($(echo ${halow_ifaces[@]}))"
echo "  Non-mesh: ${#nonmesh_ifaces[@]} ($(echo ${nonmesh_ifaces[@]}))"

## Bring everything down before renaming
#for iface in "${mesh_ifaces[@]}" "${halow_ifaces[@]}" "${nonmesh_ifaces[@]}"; do
#    ip link set "$iface" down 2>/dev/null
#done

## Rename regular mesh-capable ones using TEMP names first to avoid conflicts
#i=0
#for iface in "${mesh_ifaces[@]}"; do
#    temp_name="tmp_mesh_$i"
#    ip link set "$iface" name "$temp_name" || echo "Warning: Failed to rename $iface to $temp_name"
#    ((i++))
#done

## Rename HaLow interfaces to temp names
#i=0
#for iface in "${halow_ifaces[@]}"; do
#    temp_name="tmp_halow_$i"
 #   ip link set "$iface" name "$temp_name" || echo "Warning: Failed to rename $iface to $temp_name"
  #  ((i++))
#done

# Rename non-mesh interfaces to temp names
#i=0
#for iface in "${nonmesh_ifaces[@]}"; do
#    temp_name="tmp_nomesh_$i"
#    ip link set "$iface" name "$temp_name" || echo "Warning: Failed to rename $iface to $temp_name"
#    ((i++))
#done

## Now rename from temp to final names
#i=0
#for temp_iface in /sys/class/net/tmp_mesh_*; do
#    [ -e "$temp_iface" ] || continue
#    iface=$(basename "$temp_iface")
#    newname="wlan$i"
#    echo "$newname" >> /var/lib/mesh_if
#    ip link set "$iface" name "$newname" || echo "Warning: Failed to rename $iface to $newname"
#    ((i++))
#done

# Rename HaLow interfaces (wlan2, wlan3, etc.)
#for temp_iface in /sys/class/net/tmp_halow_*; do
#    [ -e "$temp_iface" ] || continue
#    iface=$(basename "$temp_iface")
#    newname="wlan$i"
#    echo "$newname" >> /var/lib/halow_if
#    ip link set "$iface" name "$newname" || echo "Warning: Failed to rename $iface to $newname"
#    ((i++))
#done

## Rename non-mesh after all mesh-capable
#for temp_iface in /sys/class/net/tmp_nomesh_*; do
#    [ -e "$temp_iface" ] || continue
#    iface=$(basename "$temp_iface")
#    newname="wlan$i"
#    echo "$newname" >> /var/lib/no_mesh_if
#    ip link set "$iface" name "$newname" || echo "Warning: Failed to rename $iface to $newname"
#    ((i++))
#done


# Bring them back up
#for iface in $(ip -o link show | awk -F': ' '{print $2}' | grep wlan); do
#    ip link set "$iface" up 2>/dev/null
#done

# ============================================================================
# === AP INTERFACE SELECTION (for wireless/auto EUD modes) ===
# ============================================================================

AP_INTERFACE=""

if [[ "$eud" == "wireless" ]] || [[ "$eud" == "auto" ]]; then
    echo "EUD mode is $eud - selecting AP interface..."
    
    # Priority 1: Use non-mesh interface if available (RPi 5 onboard)
    if [ -s /var/lib/no_mesh_if ]; then
        AP_INTERFACE=$(head -1 /var/lib/no_mesh_if)
        echo " > Using non-mesh interface for AP: $AP_INTERFACE"
    
    # Priority 2: Find 5GHz-capable interface from mesh interfaces
    elif [ -s /var/lib/mesh_if ]; then
        echo " > Searching for 5GHz-capable mesh interface..."
        for iface in $(cat /var/lib/mesh_if); do
            # Get PHY for this interface
            PHY=$(iw dev "$iface" info | grep wiphy | awk '{print "phy" $2}')
            
            # Check if this PHY supports 5GHz (frequencies >= 5000 MHz)
			if iw phy "$PHY" info 2>/dev/null | grep " 5[0-9][0-9][0-9]" >/dev/null; then
                AP_INTERFACE="$iface"
                echo " > Found 5GHz-capable interface: $AP_INTERFACE"
                break
            fi
        done
        
        if [ -z "$AP_INTERFACE" ]; then
            echo "WARNING: No 5GHz-capable interface found. Using first mesh interface."
            AP_INTERFACE=$(head -1 /var/lib/mesh_if)
        fi
    else
        echo "ERROR: No suitable interface found for AP!"
        AP_INTERFACE=""
    fi
    
    # Save AP interface selection
    if [ -n "$AP_INTERFACE" ]; then
        echo "$AP_INTERFACE" > /var/lib/ap_interface
        echo "AP interface selected: $AP_INTERFACE"
    fi
fi

# ============================================================================
# === CONFIGURE MESH INTERFACES (excluding AP if needed) ===
# ============================================================================

CT=0
for WLAN in `cat /var/lib/mesh_if`; do
    # Skip this interface if it's the AP interface
    if [[ -n "$AP_INTERFACE" ]] && [[ "$WLAN" == "$AP_INTERFACE" ]]; then
        echo " > Skipping $WLAN (will be used as AP)"
		((CT++))
        continue
    fi

	echo " > Setting SAE key/SSID for $WLAN ..."
	#create wpa supplicant configs
	echo "MESH_NAME=\"$MESH_NAME\"" > /etc/default/mesh
cat <<-EOF > /etc/wpa_supplicant/wpa_supplicant-$WLAN-lobby.conf
ctrl_interface=/var/run/wpa_supplicant
country=$REGULATORY_DOMAIN
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
MTUBytes=1532
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

# ============================================================================
# === CONFIGURE AP INTERFACE (if wireless/auto mode) ===
# ============================================================================

HOST_MAC=$(ip a | grep -A1 $(networkctl | grep -v bat | awk '/ether/ {print $2}' | head -1) \
   | awk '/ether/ {print $2}' | cut -d':' -f 5-6 | sed 's/://g')


if [[ -n "$AP_INTERFACE" ]]; then
    echo "Configuring $AP_INTERFACE as access point..."

    # Create a service to set interface to managed mode and bring it up
    cat <<-EOF > /etc/systemd/system/ap-interface-setup.service
[Unit]
Description=Set $AP_INTERFACE to managed mode for hostapd
Before=hostapd.service
BindsTo=sys-subsystem-net-devices-${AP_INTERFACE}.device
After=sys-subsystem-net-devices-${AP_INTERFACE}.device

[Service]
Type=oneshot
ExecStart=/usr/sbin/ip link set $AP_INTERFACE down
ExecStart=/usr/sbin/iw dev $AP_INTERFACE set type managed
ExecStart=/usr/sbin/ip link set $AP_INTERFACE up
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF


    # Create networkd config for AP interface (unmanaged, hostapd will control it)
    cat <<-EOF > /etc/systemd/network/30-${AP_INTERFACE}.network
[Match]
Name=$AP_INTERFACE

[Link]
Unmanaged=yes
ActivationPolicy=manual
EOF


    # Get configuration from mesh.conf
	while IFS= read -r line; do
	    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue

	    # Split on first = only
	    key="${line%%=*}"
	    value="${line#*=}"
        case "$key" in
            lan_ap_ssid) LAN_AP_SSID="$value" ;;
            lan_ap_key) LAN_AP_KEY="$value" ;;
            max_euds_per_node) MAX_EUDS="$value" ;;
            ipv4_network) IPV4_NETWORK="$value" ;;
        esac
    done < /etc/mesh.conf

    # Calculate DHCP pool based on max EUDs
    # Pool starts at IP 6 (IPs 1-5 are reserved for services)
    CALC_OUTPUT=$(ipcalc "$IPV4_NETWORK" 2>/dev/null)
    FIRST_IP=$(echo "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')

    # Start pool at IP 6
    DHCP_START="${FIRST_IP%.*}.$((${FIRST_IP##*.} + 5))"

    # Calculate max nodes and pool size
    # Solve: nodes * (1 + max_euds) <= total_available
    PREFIX=$(echo "$IPV4_NETWORK" | cut -d'/' -f2)
    HOST_BITS=$((32 - PREFIX))
    TOTAL_IPS=$((2**HOST_BITS - 2))
    MAX_NODES=$((TOTAL_IPS / (1 + MAX_EUDS)))
    POOL_SIZE=$((MAX_NODES * MAX_EUDS))

    # End pool at start + pool_size - 1
    POOL_END_OFFSET=$((5 + POOL_SIZE - 1))
    DHCP_END="${FIRST_IP%.*}.$((${FIRST_IP##*.} + POOL_END_OFFSET))"

    echo " > DHCP pool: $DHCP_START - $DHCP_END (${POOL_SIZE} IPs for ${MAX_EUDS} EUDs × ${MAX_NODES} nodes)"

    # Create hostapd configuration
    cat <<-EOF > /etc/hostapd/hostapd.conf
interface=$AP_INTERFACE
driver=nl80211
ssid=${LAN_AP_SSID}-${HOST_MAC}

# 5GHz 802.11ax configuration
hw_mode=a
channel=acs_survey
ieee80211n=1
ieee80211ac=1
ieee80211ax=1
wmm_enabled=1

# WPA2 security
auth_algs=1
wpa=2
wpa_key_mgmt=WPA-PSK
wpa_pairwise=CCMP
rsn_pairwise=CCMP
wpa_passphrase=$LAN_AP_KEY

# Regulatory
country_code=$REGULATORY_DOMAIN

# Performance
ht_capab=[HT40+][SHORT-GI-20][SHORT-GI-40]
vht_capab=[RXLDPC][SHORT-GI-80][TX-STBC-2BY1][RX-STBC-1]
EOF

# DHCP script hook for adding host routes
dhcp-script=/usr/local/bin/dhcp-eud-route.sh
EOF

    # Create DHCP script for adding host routes (proxy ARP alternative)
    cat <<-EOF > /usr/local/bin/dhcp-eud-route.sh
#!/bin/bash
# Called by dnsmasq when DHCP events occur
# Args: add|old <mac> <ip> <hostname>

ACTION=\$1
MAC=\$2
IP=\$3
HOSTNAME=\$4
AP_IF="$AP_INTERFACE"

case "\$ACTION" in
    add|old)
        # Add host route for this EUD client
        ip route add \$IP dev \$AP_IF 2>/dev/null
        logger -t dhcp-eud "Added route for \$IP via \$AP_IF"
        ;;
    del)
        # Remove host route
        ip route del \$IP dev \$AP_IF 2>/dev/null
        logger -t dhcp-eud "Removed route for \$IP"
        ;;
esac
EOF
    chmod +x /usr/local/bin/dhcp-eud-route.sh

    # Create TX power limiting service
    cat <<-EOF > /etc/systemd/system/ap-txpower.service
[Unit]
Description=Set low TX power on AP interface
After=sys-subsystem-net-devices-${AP_INTERFACE}.device
BindsTo=sys-subsystem-net-devices-${AP_INTERFACE}.device

[Service]
Type=oneshot
ExecStart=/usr/sbin/iw dev $AP_INTERFACE set txpower fixed 500
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

    # Enable proxy ARP on br0 for EUD routing
    cat <<-EOF >> /etc/sysctl.d/99-mesh.conf

# Proxy ARP for EUD clients
net.ipv4.conf.br0.proxy_arp=1
EOF
    sysctl -p /etc/sysctl.d/99-mesh.conf

    # Enable services based on mode
    systemctl enable ap-txpower.service
    systemctl enable dnsmasq.service

    if [[ "$eud" == "wireless" ]]; then
        # Wireless mode: always-on AP
        echo " > Wireless mode: Enabling and starting AP services"
		systemctl unmask hostapd.service
        systemctl enable ap-interface-setup.service
        systemctl enable hostapd.service
        systemctl start hostapd.service
        systemctl start dnsmasq.service
        systemctl start ap-txpower.service
    else
        # Auto mode: stage services but don't enable/start
        # ethernet-autodetect.sh will manage them
		systemctl unmask hostapd.service
        echo " > Auto mode: AP services staged (ethernet-autodetect will manage)"
        systemctl disable hostapd.service
    fi

    echo "AP configuration complete for $AP_INTERFACE"
fi

# ============================================================================
# === CONFIGURE CLIENT AP (if exists and not used for mesh AP) ===
# ============================================================================

for WLAN in `cat /var/lib/no_mesh_if | head -n 1`; do
    # Skip if this is already the AP interface
    if [[ -n "$AP_INTERFACE" ]] && [[ "$WLAN" == "$AP_INTERFACE" ]]; then
        continue
    fi

	echo " > Setting up $WLAN as a client AP ..."

	echo "   > creating networkd file ..."
cat <<- EOF > /etc/systemd/network/30-$WLAN.network
[Match]
Name=$WLAN

[Link]
Unmanaged=yes
ActivationPolicy=manual
EOF

systemctl enable mesh-interface-setup@$WLAN
done

# ============================================================================
# === HALOW CONFIGURATION ===
# ============================================================================

for WLAN in `cat /var/lib/halow_if | head -n 1`; do
	echo " > Setting up $WLAN for HaLow use ..."
#create the network interface config
cat <<-EOF >  /etc/systemd/network/30-$WLAN.network
[Match]
MACAddress=`ip a | grep -A1 $WLAN | awk '/ether/ {print $2}'`

[Network]

[Link]
RequiredForOnline=no
MTUBytes=1532
EOF

cat <<-EOF >  /etc/systemd/network/10-$WLAN.link
[Match]
MACAddress=`ip a | grep -A1 $WLAN | awk '/ether/ {print $2}'`

[Link]
Name=$WLAN
EOF

rm /etc/wpa_supplicant/*${WLAN}* 2>/dev/null

cat << EOF > /etc/wpa_supplicant/wpa_supplicant-$WLAN-s1g.conf
country="US"
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
echo "options cfg80211 ieee80211_regdom=$REGULATORY_DOMAIN" > /etc/modprobe.d/cfg80211.conf
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
    # Skip AP interface
    if [[ -n "$AP_INTERFACE" ]] && [[ "$WLAN" == "$AP_INTERFACE" ]]; then
		((INT_CT++))
        continue
    fi
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
Description=Syncthing Peer Manager
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

systemctl daemon-reload

systemctl enable --now nftables.service


#install scripts for auto gateway management
cp /root/networkd-dispatcher/off /etc/networkd-dispatcher/off.d/50-gateway-disable
cp /root/networkd-dispatcher/off /etc/networkd-dispatcher/no-carrier.d/50-gateway-disable
cp /root/networkd-dispatcher/off /etc/networkd-dispatcher/degraded.d/50-gateway-disable
cp /root/networkd-dispatcher/carrier /etc/networkd-dispatcher/carrier.d/50-ethernet-detect
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
	mkdir -p /home/radio/Sync/mumble/backups
	chown -R /home/radio/Sync
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
