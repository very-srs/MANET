#!/bin/bash
#  A script to finalize the setup of a radio after imaging and a first boot
# 
#  This script can be re-run to set new network settings
#  if the mesh config file is updated
#

# log the output of this script to a file for debugging
exec > >(tee /var/log/radio-setup.log) 2>&1
set -x

led_error() {
    echo heartbeat > /sys/class/leds/PWR/trigger
}
trap led_error ERR

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

# Look up the current physical interface name for a logical name.
# During provisioning, logical names (wlan0/1/2) may not yet match kernel names.
phys_iface() {
    local logical="$1"
    local phys
    phys=$(grep "^${logical}:" /var/lib/iface_map 2>/dev/null | cut -d: -f2)
    echo "${phys:-$logical}"   # fall back to logical name if no mapping (post-reboot)
}

set_mesh_hostname() {
    local new_hostname="$1"
    [ -n "$new_hostname" ] || return 0

    if hostnamectl set-hostname "$new_hostname" 2>/dev/null; then
        return 0
    fi

    echo "$new_hostname" > /etc/hostname 2>/dev/null || true
    if grep -q '^127\.0\.1\.1' /etc/hosts 2>/dev/null; then
        sed -i "s/^127\\.0\\.1\\.1.*/127.0.1.1\t${new_hostname}/" /etc/hosts
    else
        echo "127.0.1.1	${new_hostname}" >> /etc/hosts
    fi
    hostname "$new_hostname" 2>/dev/null || hostnamectl --transient set-hostname "$new_hostname" 2>/dev/null || true
}

has_usb_morse_device() {
    local dev text

    for dev in /sys/bus/usb/devices/*; do
        [ -d "$dev" ] || continue
        text="$(cat "$dev/product" "$dev/manufacturer" 2>/dev/null | tr '[:upper:]' '[:lower:]')"
        echo "$text" | grep -Eq 'morse|mm81|halow|802\.11ah' && return 0
    done

    return 1
}

has_morse_netdev() {
    local iface driver

    for iface in /sys/class/net/*; do
        [ -e "$iface" ] || continue
        driver="$(basename "$(readlink -f "$iface/device/driver" 2>/dev/null)")"
        [[ "$driver" == morse* ]] && return 0
    done

    return 1
}

echo "Installing morse driver"
mkdir -p /lib/modules/$(uname -r)/extra/morse

# Copy Morse modules when provisioning assets are present. On already-provisioned
# nodes the modules may already be installed and /root/morse_driver may be gone.
if [ -f /root/morse_driver/morse.ko ]; then
    cp /root/morse_driver/morse.ko /lib/modules/$(uname -r)/extra/morse/
else
    echo " > /root/morse_driver/morse.ko not found; keeping existing installed module"
fi

if [ -f /root/morse_driver/dot11ah/dot11ah.ko ]; then
    cp /root/morse_driver/dot11ah/dot11ah.ko /lib/modules/$(uname -r)/extra/morse/
else
    echo " > /root/morse_driver/dot11ah/dot11ah.ko not found; keeping existing installed module"
fi

# Update module dependencies
depmod -a

cp /root/morse_cli/morse_cli /usr/local/bin/ 2>/dev/null || true
chmod +x /usr/local/bin/*

# Write safe Morse options before the first modprobe. Cloned images may carry
# board-specific SPI BCF options that break USB MM81xx probe.
EARLY_REGULATORY_DOMAIN=$(grep "^regulatory_domain=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)
EARLY_REGULATORY_DOMAIN=${EARLY_REGULATORY_DOMAIN:-US}
EARLY_HALOW_REGULATORY_DOMAIN=$(grep "^halow_regulatory_domain=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)
EARLY_HALOW_REGULATORY_DOMAIN=${EARLY_HALOW_REGULATORY_DOMAIN:-$EARLY_REGULATORY_DOMAIN}
case "$EARLY_REGULATORY_DOMAIN" in
    AT|BE|BG|HR|CY|CZ|DK|EE|FI|FR|DE|GR|HU|IE|IT|LV|LT|LU|MT|NL|PL|PT|RO|SK|SI|ES|SE|GB|CH|NO)
        EARLY_HALOW_REGULATORY_DOMAIN="EU"
        ;;
esac

echo "options cfg80211 ieee80211_regdom=$EARLY_REGULATORY_DOMAIN" > /etc/modprobe.d/cfg80211.conf
EARLY_MORSE_BCF=""
EARLY_MORSE_SPI_CLOCK=""
if [ -f /etc/modprobe.d/morse.conf ] && ! has_usb_morse_device; then
    EARLY_MORSE_BCF=$(grep -oP '(?<=bcf=)\S+' /etc/modprobe.d/morse.conf | head -1)
    EARLY_MORSE_SPI_CLOCK=$(grep -oP '(?<=spi_clock_speed=)\S+' /etc/modprobe.d/morse.conf | head -1)
fi
echo "options morse enable_mcast_whitelist=0 enable_mcast_rate_control=1" > /etc/modprobe.d/morse.conf
echo "options morse country=$EARLY_HALOW_REGULATORY_DOMAIN" >> /etc/modprobe.d/morse.conf
[[ -n "$EARLY_MORSE_BCF" ]] && echo "options morse bcf=$EARLY_MORSE_BCF" >> /etc/modprobe.d/morse.conf
[[ -n "$EARLY_MORSE_SPI_CLOCK" ]] && echo "options morse spi_clock_speed=$EARLY_MORSE_SPI_CLOCK" >> /etc/modprobe.d/morse.conf
if [[ "$EARLY_HALOW_REGULATORY_DOMAIN" == "EU" ]]; then
    echo "options morse enable_auto_duty_cycle=0 enable_auto_mpsw=0" >> /etc/modprobe.d/morse.conf
fi

if has_usb_morse_device && ! has_morse_netdev; then
    modprobe -r morse 2>/dev/null || true
    modprobe -r dot11ah 2>/dev/null || true
fi

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

echo " > Ensuring SSH password access for user 'radio'..."
id -u radio >/dev/null 2>&1 || useradd -m -s /bin/bash -G sudo,adm,dialout,cdrom,audio,video,plugdev,games,users,input,netdev,gpio,i2c,spi radio
usermod -aG sudo,adm,dialout,cdrom,audio,video,plugdev,games,users,input,netdev,gpio,i2c,spi radio 2>/dev/null || true
if [[ -n "$new_user_password" ]]; then
    echo "radio:$new_user_password" | chpasswd
elif [[ -n "$radio_password" ]]; then
    echo "radio:$radio_password" | chpasswd
fi
passwd -u radio 2>/dev/null || true
mkdir -p /home/radio/.ssh /etc/ssh/sshd_config.d
chmod 700 /home/radio/.ssh
chown -R radio:radio /home/radio/.ssh
cat << EOF > /etc/ssh/sshd_config.d/10-manet.conf
PasswordAuthentication yes
KbdInteractiveAuthentication no
PubkeyAuthentication yes
UsePAM yes
PermitRootLogin prohibit-password
EOF
systemctl enable ssh 2>/dev/null || true
systemctl restart ssh 2>/dev/null || true

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

cat << 'EOF' > /usr/local/bin/unblock-wifi-rfkill.sh
#!/bin/sh
for rfkill in /sys/class/rfkill/rfkill*; do
    [ -e "$rfkill/type" ] || continue
    [ "$(cat "$rfkill/type" 2>/dev/null)" = "wlan" ] || continue
    [ "$(cat "$rfkill/hard" 2>/dev/null)" = "1" ] && continue
    echo 0 > "$rfkill/soft" 2>/dev/null || true
done
EOF
chmod +x /usr/local/bin/unblock-wifi-rfkill.sh
/usr/local/bin/unblock-wifi-rfkill.sh

cat << 'EOF' > /usr/local/bin/prepare-standard-mesh-iface.sh
#!/bin/sh
set -u

IFACE="${1:-}"
[ -n "$IFACE" ] || exit 0
[ -e "/sys/class/net/$IFACE" ] || exit 0

/usr/local/bin/unblock-wifi-rfkill.sh 2>/dev/null || true

driver="$(basename "$(readlink -f "/sys/class/net/$IFACE/device/driver" 2>/dev/null)")"
if [ -z "$driver" ] || [ "$driver" = "." ]; then
    driver="$(ethtool -i "$IFACE" 2>/dev/null | awk -F': ' '$1 == "driver" {print $2; exit}')"
fi

case "$driver" in
    brcmfmac|morse*)
        exit 0
        ;;
esac

iw dev "$IFACE" info 2>/dev/null | grep -q 'type mesh point' && exit 0

ip link set "$IFACE" down 2>/dev/null || true
sleep 1
iw dev "$IFACE" set type mp 2>/dev/null || true
sleep 1

if ! iw dev "$IFACE" info 2>/dev/null | grep -q 'type mesh point'; then
    echo "Warning: $IFACE did not enter mesh point mode before wpa_supplicant" >&2
fi
EOF
chmod +x /usr/local/bin/prepare-standard-mesh-iface.sh

cat << EOF > /etc/systemd/system/wifi-rfkill-unblock.service
[Unit]
Description=Unblock Wi-Fi rfkill switches
Before=hostapd.service batman-enslave.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/unblock-wifi-rfkill.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

# A system service to force mesh point mode on the wlan interfaces
cat << EOF > /etc/systemd/system/mesh-interface-setup@.service
[Unit]
Description=Set %I to mesh point mode
Before=wpa_supplicant@%i.service
BindsTo=sys-subsystem-net-devices-%i.device
After=sys-subsystem-net-devices-%i.device

[Service]
Type=oneshot
ExecStartPre=/usr/local/bin/unblock-wifi-rfkill.sh
ExecStartPre=/bin/sleep 1
ExecStart=/usr/local/bin/prepare-standard-mesh-iface.sh %I
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

mkdir -p /etc/systemd/system/wpa_supplicant@.service.d
cat << EOF > /etc/systemd/system/wpa_supplicant@.service.d/10-mesh-prep.conf
[Unit]
After=wifi-rfkill-unblock.service
Wants=wifi-rfkill-unblock.service

[Service]
ExecStartPre=/usr/local/bin/unblock-wifi-rfkill.sh
ExecStartPre=/usr/local/bin/prepare-standard-mesh-iface.sh %i
EOF

REGULATORY_DOMAIN=$(grep "^regulatory_domain=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)
REGULATORY_DOMAIN=${REGULATORY_DOMAIN:-US}  # Default to US if not found
HALOW_REGULATORY_DOMAIN=$(grep "^halow_regulatory_domain=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)
HALOW_REGULATORY_DOMAIN=${HALOW_REGULATORY_DOMAIN:-$REGULATORY_DOMAIN}
REG=$REGULATORY_DOMAIN

echo REGDOMAIN=$REGULATORY_DOMAIN > /etc/default/crda

uses_eu_halow_region() {
    local domain="$1"

    case "$domain" in
        AT|BE|BG|HR|CY|CZ|DK|EE|FI|FR|DE|GR|HU|IE|IT|LV|LT|LU|MT|NL|PL|PT|RO|SK|SI|ES|SE|GB|CH|NO)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

if [ -z "$HALOW_REGULATORY_DOMAIN" ] || uses_eu_halow_region "$REGULATORY_DOMAIN"; then
    HALOW_REGULATORY_DOMAIN="EU"
fi

# cfg80211 regdomain to use — EU when HaLow is EU so S1G channels are
# resolvable by wpa_supplicant_s1g. Also used in wpa_supplicant configs
# for 2.4/5 GHz radios to keep cfg80211 from reverting to WORLD.
if [[ "$HALOW_REGULATORY_DOMAIN" == "EU" ]]; then
    CFG80211_REGDOM="EU"
else
    CFG80211_REGDOM="$REGULATORY_DOMAIN"
fi


# Wait for wireless drivers to load
echo "Waiting for wireless drivers to load..."
DRIVER_WAIT_COUNT=0
MAX_DRIVER_WAIT=30  # 60 seconds total

while [ $DRIVER_WAIT_COUNT -lt $MAX_DRIVER_WAIT ]; do
    PHY_COUNT=$(iw dev 2>/dev/null | grep -c "^phy#")

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

# ============================================================================
# === INTERFACE DETECTION ===
# ============================================================================

# Detect interfaces, classify by type
mesh_ifaces=()
halow_ifaces=()
nonmesh_ifaces=()

iface_driver() {
    local iface="$1"
    local driver

    driver="$(basename "$(readlink -f /sys/class/net/$iface/device/driver 2>/dev/null)")"
    if [[ -z "$driver" || "$driver" == "." ]]; then
        driver="$(ethtool -i "$iface" 2>/dev/null | awk -F': ' '$1 == "driver" {print $2; exit}')"
    fi

    echo "$driver"
}

is_halow_iface() {
    local iface="$1"
    local driver phyname

    driver="$(iface_driver "$iface")"
    [[ "$driver" == morse* ]] && return 0

    # Fallback: USB HaLow adapters may report a USB driver name instead of
    # morse via sysfs.  A wireless phy with no standard 2.4/5 GHz frequencies
    # is a sub-GHz (HaLow) device.
    phyname="$(iface_phy "$iface")"
    [[ -z "$phyname" ]] && return 1

    if ! iw phy "$phyname" info 2>/dev/null | grep -q "2412\.0 MHz" && \
       ! iw phy "$phyname" info 2>/dev/null | grep -q "5180\.0 MHz"; then
        return 0
    fi

    return 1
}

is_nonmesh_wifi() {
    local iface="$1"
    local driver

    driver="$(iface_driver "$iface")"

    # Raspberry Pi onboard Wi-Fi advertises mesh point support in some kernels,
    # but on this platform it is reserved for the EUD AP/hotspot. Keep it out
    # of mesh_if so rerunning setup cannot enslave the AP radio to bat0.
    [[ "$driver" == brcmfmac ]]
}

iface_phy() {
    local iface="$1"
    iw dev "$iface" info 2>/dev/null | awk '/wiphy/ {print "phy"$2; exit}'
}

iface_supports_mesh() {
    local iface="$1"
    local phyname

    phyname="$(iface_phy "$iface")"
    [[ -n "$phyname" ]] && iw phy "$phyname" info 2>/dev/null | grep -q "mesh point"
}

iface_supports_freq() {
    local iface="$1"
    local freq="$2"
    local phyname
    phyname="$(iface_phy "$iface")"
    [[ -n "$phyname" ]] && iw phy "$phyname" info 2>/dev/null | grep -q "${freq}\\.0 MHz"
}

iface_mesh_freq() {
    local iface="$1"
    local phyname
    phyname="$(iface_phy "$iface")"
    [[ -z "$phyname" ]] && echo "" && return

    if iw phy "$phyname" info 2>/dev/null | grep -q "2412\\.0 MHz"; then
        echo "2412"
    elif iw phy "$phyname" info 2>/dev/null | grep -q "5180\\.0 MHz"; then
        echo "5180"
    else
        echo ""
    fi
}

for iface in $(iw dev | awk '$1 == "Interface" {print $2}'); do
    if is_halow_iface "$iface"; then
        halow_ifaces+=("$iface")
    elif is_nonmesh_wifi "$iface"; then
        nonmesh_ifaces+=("$iface")
    elif iface_supports_mesh "$iface"; then
        mesh_ifaces+=("$iface")
    else
        nonmesh_ifaces+=("$iface")
    fi
done

# Order standard mesh interfaces by role, not by volatile wlanX name. The first
# interface receives the 2.4 GHz lobby config and the second receives the 5 GHz
# lobby config. Prefer single-band matches when possible, then fall back to any
# device supporting the required lobby frequency.
if [ "${#mesh_ifaces[@]}" -gt 1 ]; then
    mesh_24=""
    mesh_5=""

    for iface in "${mesh_ifaces[@]}"; do
        if iface_supports_freq "$iface" 2412 && ! iface_supports_freq "$iface" 5180; then
            mesh_24="$iface"
            break
        fi
    done
    for iface in "${mesh_ifaces[@]}"; do
        if iface_supports_freq "$iface" 5180 && ! iface_supports_freq "$iface" 2412; then
            mesh_5="$iface"
            break
        fi
    done
    [ -z "$mesh_24" ] && for iface in "${mesh_ifaces[@]}"; do
        iface_supports_freq "$iface" 2412 && mesh_24="$iface" && break
    done
    [ -z "$mesh_5" ] && for iface in "${mesh_ifaces[@]}"; do
        [ "$iface" = "$mesh_24" ] && continue
        iface_supports_freq "$iface" 5180 && mesh_5="$iface" && break
    done

    reordered=()
    [ -n "$mesh_24" ] && reordered+=("$mesh_24")
    [ -n "$mesh_5" ] && reordered+=("$mesh_5")
    for iface in "${mesh_ifaces[@]}"; do
        [ "$iface" = "$mesh_24" ] && continue
        [ "$iface" = "$mesh_5" ] && continue
        reordered+=("$iface")
    done
    mesh_ifaces=("${reordered[@]}")
elif [ "${#mesh_ifaces[@]}" -eq 1 ]; then
    mapfile -t mesh_ifaces < <(printf '%s\n' "${mesh_ifaces[@]}" | sort -V)
fi

# Create directory and files (supports wired-only configs if arrays are empty)
mkdir -p /var/lib
> /var/lib/mesh_if
> /var/lib/mesh_24_if
> /var/lib/mesh_5_if
> /var/lib/halow_if
> /var/lib/no_mesh_if
> /var/lib/iface_map   # runtime_name:physical_name (for MAC lookups during provisioning)

# Persist the runtime interface names. The rest of the setup and boot scripts
# consume these files as live netdev names, so writing desired logical names
# before udev has renamed anything can make HaLow and non-mesh devices appear
# swapped. Keep iface_map as an identity map for phys_iface() callers.
for iface in "${mesh_ifaces[@]}"; do
    echo "$iface" >> /var/lib/mesh_if
    echo "$iface:$iface" >> /var/lib/iface_map
    echo " > Mapped $iface (mesh)"
done
[ "${#mesh_ifaces[@]}" -gt 0 ] && echo "${mesh_ifaces[0]}" > /var/lib/mesh_24_if
[ "${#mesh_ifaces[@]}" -gt 1 ] && echo "${mesh_ifaces[1]}" > /var/lib/mesh_5_if
for iface in "${halow_ifaces[@]}"; do
    echo "$iface" >> /var/lib/halow_if
    echo "$iface:$iface" >> /var/lib/iface_map
    echo " > Mapped $iface (HaLow)"
done
for iface in "${nonmesh_ifaces[@]}"; do
    echo "$iface" >> /var/lib/no_mesh_if
    echo "$iface:$iface" >> /var/lib/iface_map
    echo " > Mapped $iface (non-mesh)"
done

# Log what we found
echo "Interface detection complete:"
echo "  Mesh-capable: ${#mesh_ifaces[@]} (${mesh_ifaces[*]})"
echo "  Mesh 2.4 role: $(cat /var/lib/mesh_24_if 2>/dev/null || true)"
echo "  Mesh 5.0 role: $(cat /var/lib/mesh_5_if 2>/dev/null || true)"
echo "  HaLow: ${#halow_ifaces[@]} (${halow_ifaces[*]})"
echo "  Non-mesh: ${#nonmesh_ifaces[@]} (${nonmesh_ifaces[*]})"
echo "  Logical mapping:"
cat /var/lib/iface_map

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
            PHY=$(iw dev "$iface" info | grep wiphy | awk '{print "phy" $2}')
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
# === CLEANUP STALE PER-INTERFACE SERVICES AND CONFIGS ===
# ============================================================================
# Previous runs may have enabled wpa_supplicant or s1g services for interfaces
# that no longer hold those roles (e.g. after a .link rename swapped which
# physical card is wlanX). Disable any per-interface service whose target
# interface isn't currently classified for that role.

current_mesh="$(cat /var/lib/mesh_if 2>/dev/null | tr '\n' ' ')"
current_halow="$(cat /var/lib/halow_if 2>/dev/null | tr '\n' ' ')"

cleanup_iface_service() {
    local svc_pattern="$1"   # e.g. "wpa_supplicant@wlan*.service"
    local valid_list="$2"    # space-separated interface names that should keep this service
    local svc iface link

    # Find enabled units by looking at symlinks in target wants directories.
    # This catches both concrete units and instantiated template units.
    for link in /etc/systemd/system/*.wants/$svc_pattern \
                /etc/systemd/system/*.requires/$svc_pattern; do
        [ -L "$link" ] || continue
        svc="$(basename "$link")"

        iface="$(echo "$svc" | sed -E 's/.*[@-](wlan[0-9]+)\.service$/\1/')"
        [[ "$iface" == "$svc" ]] && continue

        if ! echo " $valid_list " | grep -q " $iface "; then
            echo " > Disabling stale service: $svc (iface $iface no longer in role)"
            systemctl disable --now "$svc" 2>/dev/null || true
            systemctl reset-failed "$svc" 2>/dev/null || true
        fi
    done

    # Also catch units that are loaded/failed but never had an enable symlink
    # (e.g. started manually, or left over after their wants symlink was removed
    # but the runtime instance kept lingering).
    for svc in $(systemctl list-units --all --no-legend --state=failed,loaded "$svc_pattern" 2>/dev/null | awk '{print $1}'); do
        iface="$(echo "$svc" | sed -E 's/.*[@-](wlan[0-9]+)\.service$/\1/')"
        [[ "$iface" == "$svc" ]] && continue

        if ! echo " $valid_list " | grep -q " $iface "; then
            echo " > Cleaning up stale runtime instance: $svc"
            systemctl stop "$svc" 2>/dev/null || true
            systemctl reset-failed "$svc" 2>/dev/null || true
        fi
    done
}

cleanup_iface_service 'wpa_supplicant@wlan*.service' "$current_mesh"
cleanup_iface_service 'wpa_supplicant-s1g-wlan*.service' "$current_halow"

# Remove stale per-interface wpa_supplicant config files for interfaces that
# don't currently hold a wireless role. mesh-boot-lobby.service blindly copies
# every *-lobby.conf at boot, so leftovers will resurrect dead configs.
all_wireless_roles="$current_mesh $current_halow"
for conf in /etc/wpa_supplicant/wpa_supplicant-wlan*.conf; do
    [ -e "$conf" ] || continue
    iface="$(basename "$conf" | sed -E 's/^wpa_supplicant-(wlan[0-9]+).*/\1/')"
    if ! echo " $all_wireless_roles " | grep -q " $iface "; then
        echo " > Removing stale wpa_supplicant config: $conf"
        rm -f "$conf"
    fi
done

# ============================================================================
# === CONFIGURE MESH INTERFACES (excluding AP if needed) ===
# ============================================================================

# Pin wlanX names by MAC so role assignments survive reboot in a predictable
# order: wlan0=2.4GHz mesh, wlan1=5GHz mesh, wlan2=HaLow, wlan3=non-mesh AP.
# The classification above already determined which physical phy plays which
# role; we now bind that role to a stable MAC-keyed name via systemd .link.
# These take effect at next boot — current run uses kernel-assigned names from
# the role files.
rm -f /etc/systemd/network/10-wlan*.link

iface_mac() {
    cat "/sys/class/net/$1/address" 2>/dev/null
}

write_link_file() {
    local target_name="$1"
    local mac="$2"
    [[ -z "$mac" ]] && return

cat <<-EOF > /etc/systemd/network/10-${target_name}.link
[Match]
MACAddress=$mac
Type=wlan

[Link]
Name=$target_name
EOF
    echo " > Pinning $target_name to MAC $mac"
}

# Mesh interfaces are already ordered: [0]=2.4GHz, [1]=5GHz
[ "${#mesh_ifaces[@]}" -gt 0 ] && write_link_file wlan0 "$(iface_mac "${mesh_ifaces[0]}")"
[ "${#mesh_ifaces[@]}" -gt 1 ] && write_link_file wlan1 "$(iface_mac "${mesh_ifaces[1]}")"
[ "${#halow_ifaces[@]}" -gt 0 ] && write_link_file wlan2 "$(iface_mac "${halow_ifaces[0]}")"
[ "${#nonmesh_ifaces[@]}" -gt 0 ] && write_link_file wlan3 "$(iface_mac "${nonmesh_ifaces[0]}")"
echo "MESH_NAME=\"$MESH_NAME\"" > /etc/default/mesh


# Detect if the .link files we just wrote disagree with current runtime names.
# If they do, the next boot will rename interfaces and the role files we wrote
# this run will be stale. Schedule a post-reboot re-run to refresh them.
needs_rerun=0
check_rename() {
    local target="$1"
    local current="$2"
    [[ -z "$current" ]] && return
    [[ "$target" == "$current" ]] && return
    echo " > Rename pending: $current -> $target (next boot)"
    needs_rerun=1
}

[ "${#mesh_ifaces[@]}" -gt 0 ] && check_rename wlan0 "${mesh_ifaces[0]}"
[ "${#mesh_ifaces[@]}" -gt 1 ] && check_rename wlan1 "${mesh_ifaces[1]}"
[ "${#halow_ifaces[@]}" -gt 0 ] && check_rename wlan2 "${halow_ifaces[0]}"
[ "${#nonmesh_ifaces[@]}" -gt 0 ] && check_rename wlan3 "${nonmesh_ifaces[0]}"

if [ "$needs_rerun" -eq 1 ]; then
    echo " > Interface renames staged. Scheduling post-reboot re-run."

cat << 'EOF' > /etc/systemd/system/radio-setup-rerun.service
[Unit]
Description=Re-run radio-setup after interface rename
After=multi-user.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/radio-setup.sh
ExecStartPost=/bin/systemctl disable radio-setup-rerun.service
ExecStartPost=/bin/rm -f /etc/systemd/system/radio-setup-rerun.service
RemainAfterExit=no

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable radio-setup-rerun.service
    touch /var/lib/radio-setup-reboot-pending
else
    echo " > Interface names already match desired layout, no rename needed"
fi

for WLAN in $(cat /var/lib/mesh_if); do
    # Skip this interface if it's the AP interface
    if [[ -n "$AP_INTERFACE" ]] && [[ "$WLAN" == "$AP_INTERFACE" ]]; then
        echo " > Skipping $WLAN (will be used as AP)"
        continue
    fi

    FREQ=$(iface_mesh_freq "$WLAN")
    if [[ -z "$FREQ" ]]; then
        echo " > WARNING: Cannot determine band for $WLAN, skipping"
        continue
    fi

    echo " > Setting SAE key/SSID for $WLAN (${FREQ} MHz) ..."

cat <<-EOF > /etc/wpa_supplicant/wpa_supplicant-$WLAN-lobby.conf
ctrl_interface=/var/run/wpa_supplicant
country=$CFG80211_REGDOM
update_config=1
sae_pwe=1
ap_scan=2
network={
    ssid="$MESH_NAME"
    mode=5
    frequency=${FREQ}
    key_mgmt=SAE
    sae_password="$KEY"
    ieee80211w=2
    mesh_fwding=0
}
EOF

    # Create the network interface config
cat <<-EOF > /etc/systemd/network/30-$WLAN.network
[Match]
MACAddress=$(ip a | grep -A1 "$(phys_iface $WLAN)" | awk '/ether/ {print $2}')

[Network]

[Link]
RequiredForOnline=no
MTUBytes=1532
EOF

    echo " > Enabling $WLAN for mesh use ..."
    cp /etc/wpa_supplicant/wpa_supplicant-$WLAN-lobby.conf /etc/wpa_supplicant/wpa_supplicant-$WLAN.conf
    systemctl enable wpa_supplicant@$WLAN.service
done

# ============================================================================
# === CONFIGURE AP INTERFACE (if wireless/auto mode) ===
# ============================================================================

HOST_MAC=$(ip a | grep -A1 $(networkctl | grep -v bat | awk '/ether/ {print $2}' | head -1) \
   | awk '/ether/ {print $2}' | cut -d':' -f 5-6 | sed 's/://g')

if [[ -n "$AP_INTERFACE" ]]; then
    echo "Configuring $AP_INTERFACE as access point..."

cat <<-EOF > /etc/systemd/system/ap-interface-setup.service
[Unit]
Description=Set $AP_INTERFACE to managed mode for hostapd
Before=hostapd.service
After=wifi-rfkill-unblock.service
Wants=wifi-rfkill-unblock.service

[Service]
Type=oneshot
ExecStartPre=/usr/local/bin/unblock-wifi-rfkill.sh
ExecStartPre=/bin/sleep 2
ExecStart=-/usr/sbin/ip link set $AP_INTERFACE down
ExecStart=-/usr/sbin/iw dev $AP_INTERFACE set type managed
ExecStart=-/usr/sbin/ip link set $AP_INTERFACE up
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
    CALC_OUTPUT=$(ipcalc "$IPV4_NETWORK" 2>/dev/null)
    FIRST_IP=$(echo "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')

    # Start pool at IP 6
    DHCP_START="${FIRST_IP%.*}.$((${FIRST_IP##*.} + 5))"

    PREFIX=$(echo "$IPV4_NETWORK" | cut -d'/' -f2)
    HOST_BITS=$((32 - PREFIX))
    TOTAL_IPS=$((2**HOST_BITS - 2))
    MAX_NODES=$((TOTAL_IPS / (1 + MAX_EUDS)))
    POOL_SIZE=$((MAX_NODES * MAX_EUDS))

    POOL_END_OFFSET=$((5 + POOL_SIZE - 1))
    DHCP_END="${FIRST_IP%.*}.$((${FIRST_IP##*.} + POOL_END_OFFSET))"

    echo " > DHCP pool: $DHCP_START - $DHCP_END (${POOL_SIZE} IPs for ${MAX_EUDS} EUDs × ${MAX_NODES} nodes)"

    AP_CHANNEL="${lan_ap_channel:-11}"

    cat <<-EOF > /etc/hostapd/hostapd.conf
interface=$AP_INTERFACE
bridge=br0
driver=nl80211
ssid=${LAN_AP_SSID}-${HOST_MAC}
country_code=$REGULATORY_DOMAIN
ieee80211d=1

# Raspberry Pi onboard 2.4 GHz AP for EUD clients
hw_mode=g
channel=$AP_CHANNEL
ieee80211n=1
wmm_enabled=1

# WPA2 security
auth_algs=1
wpa=2
wpa_key_mgmt=WPA-PSK
wpa_pairwise=CCMP
rsn_pairwise=CCMP
wpa_passphrase=$LAN_AP_KEY
EOF

cat <<-EOF > /etc/systemd/system/ap-txpower.service
[Unit]
Description=Set low TX power on AP interface
After=ap-interface-setup.service
Wants=ap-interface-setup.service

[Service]
Type=oneshot
ExecStartPre=/bin/sleep 2
ExecStart=-/usr/sbin/iw dev $AP_INTERFACE set txpower fixed 500
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

    systemctl enable ap-txpower.service
    systemctl unmask dnsmasq.service
    systemctl enable dnsmasq.service

    if [[ "$eud" == "wireless" ]]; then
        echo " > Wireless mode: Enabling and starting AP services"
        systemctl unmask hostapd.service
        systemctl enable ap-interface-setup.service
        systemctl enable hostapd.service
        systemctl restart ap-interface-setup.service 2>/dev/null || true
        systemctl restart hostapd.service 2>/dev/null || true
        systemctl restart dnsmasq.service 2>/dev/null || true
        systemctl restart ap-txpower.service 2>/dev/null || true
    else
        systemctl unmask hostapd.service
        echo " > Auto mode: AP services staged (ethernet-autodetect will manage)"
        systemctl disable hostapd.service
    fi

    echo "AP configuration complete for $AP_INTERFACE"
fi

# ============================================================================
# === CONFIGURE CLIENT AP (if exists and not used for mesh AP) ===
# ============================================================================

for WLAN in $(cat /var/lib/no_mesh_if | head -n 1); do
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

for WLAN in $(cat /var/lib/halow_if | head -n 1); do
    echo " > Setting up $WLAN for HaLow use ..."

    # Create the network interface config
cat <<-EOF > /etc/systemd/network/30-$WLAN.network
[Match]
MACAddress=$(ip a | grep -A1 "$(phys_iface $WLAN)" | awk '/ether/ {print $2}')

[Network]

[Link]
RequiredForOnline=no
MTUBytes=1532
EOF

    rm -f /etc/wpa_supplicant/*${WLAN}* 2>/dev/null

    if [[ "$HALOW_REGULATORY_DOMAIN" == "US" ]]; then
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
    else
cat << EOF > /etc/wpa_supplicant/wpa_supplicant-$WLAN-s1g.conf
country="$HALOW_REGULATORY_DOMAIN"
ctrl_interface=/var/run/wpa_supplicant_s1g
sae_pwe=1
max_peer_links=10
mesh_fwding=0
network={
    ssid="$mesh_ssid"
    key_mgmt=SAE
    mode=5
    channel=5
    op_class=66
    country="$HALOW_REGULATORY_DOMAIN"
    s1g_prim_chwidth=0
    s1g_prim_1mhz_chan_index=0
    dtim_period=1
    mesh_rssi_threshold=-85
    dot11MeshHWMPRootMode=0
    dot11MeshGateAnnouncements=0
    mbca_config=0
    mbca_min_beacon_gap_ms=25
    mbca_tbtt_adj_interval_sec=60
    dot11MeshBeaconTimingReportInterval=10
    mbss_start_scan_duration_ms=2048
    mesh_beaconless_mode=0
    mesh_dynamic_peering=0
    sae_password="$mesh_key"
    pairwise=CCMP
    ieee80211w=2
    beacon_int=100
}
EOF
    fi

cat << EOF > /etc/systemd/system/wpa_supplicant-s1g-$WLAN.service
[Unit]
Description=WPA supplicant (S1G/HaLow) for $WLAN
After=sys-subsystem-net-devices-${WLAN}.device
Requires=sys-subsystem-net-devices-${WLAN}.device

[Service]
Type=simple
ExecStartPre=/usr/local/bin/unblock-wifi-rfkill.sh
ExecStartPre=/bin/sleep 3
ExecStart=/usr/sbin/wpa_supplicant_s1g -c /etc/wpa_supplicant/wpa_supplicant-$WLAN-s1g.conf -i $WLAN -D nl80211
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    systemctl disable wpa_supplicant_s1g@$WLAN.service 2>/dev/null || true
    systemctl enable wpa_supplicant-s1g-$WLAN.service

done

# ============================================================================
# === MORSE / HALOW MODULE OPTIONS ===
# ============================================================================
echo "options cfg80211 ieee80211_regdom=$CFG80211_REGDOM" > /etc/modprobe.d/cfg80211.conf

# Preserve hardware-specific SPI modprobe options that were written by firstrun.
# USB MM81xx adapters auto-select BCF by board type; forcing SPI BCF breaks probe.
MORSE_BCF=""
MORSE_SPI_CLOCK=""
if [ -f /etc/modprobe.d/morse.conf ] && ! has_usb_morse_device; then
    MORSE_BCF=$(grep -oP '(?<=bcf=)\S+' /etc/modprobe.d/morse.conf | head -1)
    MORSE_SPI_CLOCK=$(grep -oP '(?<=spi_clock_speed=)\S+' /etc/modprobe.d/morse.conf | head -1)
fi

echo "options morse enable_mcast_whitelist=0 enable_mcast_rate_control=1" > /etc/modprobe.d/morse.conf
echo "options morse country=$HALOW_REGULATORY_DOMAIN" >> /etc/modprobe.d/morse.conf

[[ -n "$MORSE_BCF" ]]       && echo "options morse bcf=$MORSE_BCF" >> /etc/modprobe.d/morse.conf
[[ -n "$MORSE_SPI_CLOCK" ]] && echo "options morse spi_clock_speed=$MORSE_SPI_CLOCK" >> /etc/modprobe.d/morse.conf


if [[ "$HALOW_REGULATORY_DOMAIN" == "EU" ]]; then
    echo "options morse enable_auto_duty_cycle=0 enable_auto_mpsw=0" >> /etc/modprobe.d/morse.conf
fi

# ============================================================================
# === SYSTEM SERVICE SETUP ===
# ============================================================================

# Watch for button presses
cat << EOF > /etc/systemd/system/led-boot.service
[Unit]
Description=LED boot
After=sysinit.target
DefaultDependencies=no

[Service]
Type=oneshot
RemainAfterExit=no
ExecStart=/usr/local/bin/led-boot.sh
TimeoutStartSec=infinity

[Install]
WantedBy=sysinit.target
EOF
systemctl enable led-boot.service
systemctl enable wifi-rfkill-unblock.service

cat << EOF > /etc/systemd/system/button-monitor.service
[Unit]
Description=Button monitor - launches LED info on press
After=multi-user.target

[Service]
Type=simple
ExecStart=/usr/local/bin/button-monitor.sh
Restart=always
RestartSec=1

[Install]
WantedBy=multi-user.target
EOF
systemctl enable button-monitor.service

cat << EOF > /etc/systemd/system/mesh-clone-identity.service
[Unit]
Description=Reset cloned mesh node identity when hardware MAC changes
After=local-fs.target
Before=batman-enslave.service alfred.service node-manager.service gateway-route-manager.service syncthing@radio.service hostapd.service dnsmasq.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/mesh-clone-identity.sh

[Install]
WantedBy=multi-user.target
EOF
systemctl enable mesh-clone-identity.service

cat << EOF > /etc/systemd/system/ssh-recovery.service
[Unit]
Description=Ensure MANET SSH access is available
After=local-fs.target
Before=network-online.target batman-enslave.service alfred.service node-manager.service hostapd.service dnsmasq.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/ssh-recovery.sh

[Install]
WantedBy=multi-user.target
EOF
systemctl enable ssh-recovery.service



# Replace wpa_supplicant with lobby channel files at boot
cat <<- EOF > /etc/systemd/system/mesh-boot-lobby.service
[Unit]
Description=Set mesh interfaces to Lobby channels
Before=wpa_supplicant@.service

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'for LOBBY_FILE in /etc/wpa_supplicant/wpa_supplicant-wlan*-lobby.conf; do [ -e "\$\$LOBBY_FILE" ] || continue; DEST_FILE="\$\${LOBBY_FILE%-lobby.conf}.conf"; cp "\$\$LOBBY_FILE" "\$\$DEST_FILE"; done'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl enable mesh-boot-lobby.service

# Get bat0 a link local address for alfred
cat <<- EOF > /etc/sysctl.d/99-batman.conf
# Enable IPv6 address generation on batman-adv interfaces
net.ipv6.conf.bat0.disable_ipv6 = 0
net.ipv6.conf.bat0.addr_gen_mode = 0
net.ipv6.conf.br0.disable_ipv6 = 0
net.ipv6.conf.br0.accept_ra = 1
EOF

# Build dependency strings to make batman-enslave service file
AFTER_DEVICES=""
WANTS_SERVICES=""
INT_CT=0
for WLAN in $(cat /var/lib/mesh_if); do
    # Skip AP interface
    if [[ -n "$AP_INTERFACE" ]] && [[ "$WLAN" == "$AP_INTERFACE" ]]; then
        ((INT_CT++))
        continue
    fi
    AFTER_DEVICES+="sys-subsystem-net-devices-$WLAN.device "
    WANTS_SERVICES+="wpa_supplicant@$WLAN.service "
    ((INT_CT++))
done
for WLAN in $(cat /var/lib/halow_if | head -n 1); do
    AFTER_DEVICES+="sys-subsystem-net-devices-$WLAN.device "
    WANTS_SERVICES+="wpa_supplicant-s1g-$WLAN.service "
    ((INT_CT++))
done

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

# Alfred master listener for mesh data messages
cat <<- EOF > /etc/systemd/system/alfred.service
[Unit]
Description=B.A.T.M.A.N. Advanced Layer 2 Forwarding Daemon
After=network-online.target
Wants=network-online.target
Requires=batman-enslave.service

[Service]
Type=simple
ExecStartPre=/bin/bash -c 'for i in {1..20}; do if ip -6 addr show dev bat0 | grep "inet6 fe80::" | grep -qv "tentative"; then exit 0; fi; sleep 1; done; echo "bat0 link-local IPv6 address not ready" >&2; exit 1'
ExecStart=/usr/sbin/alfred -m -i br0 -f
UMask=0000
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
systemctl enable alfred.service

# Node manager: IPv4 addressing and status gossip via alfred
cat <<- EOF > /etc/systemd/system/node-manager.service
[Unit]
Description=Mesh Node Status Manager and IPv4 Coordinator
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

systemctl enable syncthing@radio.service

systemctl daemon-reload
systemctl enable --now nftables.service

# Install scripts for auto gateway management
cp /root/networkd-dispatcher/off /etc/networkd-dispatcher/off.d/50-gateway-disable
cp /root/networkd-dispatcher/off /etc/networkd-dispatcher/no-carrier.d/50-gateway-disable
cp /root/networkd-dispatcher/off /etc/networkd-dispatcher/degraded.d/50-gateway-disable
cp /root/networkd-dispatcher/carrier /etc/networkd-dispatcher/carrier.d/50-ethernet-detect
chmod -R 755 /etc/networkd-dispatcher

cat <<- EOF > /etc/systemd/system/ethernet-autodetect.service
[Unit]
Description=MANET Ethernet Hotplug Auto Detection
After=systemd-networkd.service batman-enslave.service
Wants=systemd-networkd.service
ConditionPathExists=/usr/local/bin/ethernet-autodetect.sh

[Service]
Type=oneshot
ExecStart=/usr/local/bin/ethernet-autodetect.sh --hotplug
TimeoutStartSec=45

[Install]
WantedBy=multi-user.target
EOF
systemctl enable ethernet-autodetect.service

cp /root/regulatory.db /lib/firmware/

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


# Power saving
cat << EOF > /etc/systemd/system/cpu-powersave.service
[Unit]
Description=Set CPU to powersave mode
After=multi-user.target

[Service]
Type=oneshot
RemainAfterExit=yes

# Set governor
ExecStart=/bin/bash -c 'echo powersave > /sys/devices/system/cpu/cpufreq/policy0/scaling_governor'
# Cap max frequency to 1.0 GHz
ExecStart=/bin/bash -c 'echo 1008000 > /sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq'
# Disable cores 2 and 3
ExecStart=/bin/bash -c 'echo 0 > /sys/devices/system/cpu/cpu2/online'
ExecStart=/bin/bash -c 'echo 0 > /sys/devices/system/cpu/cpu3/online'

# Restore on stop (or when election scripts call systemctl stop cpu-powersave)
ExecStop=/bin/bash -c 'echo 1 > /sys/devices/system/cpu/cpu2/online'
ExecStop=/bin/bash -c 'echo 1 > /sys/devices/system/cpu/cpu3/online'
ExecStop=/bin/bash -c 'echo ondemand > /sys/devices/system/cpu/cpufreq/policy0/scaling_governor'
ExecStop=/bin/bash -c 'echo 1416000 > /sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq'

[Install]
WantedBy=multi-user.target
EOF
systemctl enable cpu-powersave



cat << EOF > /etc/systemd/system/mesh-hosts-update.service
[Unit]
Description=Update /etc/hosts from mesh registry
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/mesh-hosts-update.sh
EOF

cat << EOF > /etc/systemd/system/mesh-hosts-update.timer
[Unit]
Description=Refresh mesh hosts periodically

[Timer]
OnBootSec=60
OnUnitActiveSec=120

[Install]
WantedBy=timers.target
EOF

systemctl enable mesh-hosts-update.timer


# ============================================================================
# === HOSTNAME ===
# ============================================================================

HOST_MAC=$(ip a | grep -A1 $(networkctl | grep -v bat | awk '/ether/ {print $2}' | head -1) \
   | awk '/ether/ {print $2}' | cut -d':' -f 5-6 | sed 's/://g')

set_mesh_hostname "mesh-${HOST_MAC}"

# ============================================================================
# === Web Status / config ===
# ============================================================================
cat << EOF > /etc/systemd/system/mesh-status.service
[Unit]
Description=MANET Node Status Web Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/bin/mesh-status.py 80
Restart=on-failure
RestartSec=5
User=root
# Allows reading /etc/mesh.conf (contains credentials) and calling batctl
AmbientCapabilities=CAP_NET_RAW CAP_NET_ADMIN

[Install]
WantedBy=multi-user.target
EOF

systemctl enable mesh-status

# ============================================================================
# === mDNS — manet.local ===
# ============================================================================
# Advertise this node as manet.local on the AP/EUD interface only.
# avahi-daemon is kept but restricted to deny mesh interfaces (bat0, wlan0-2).
# Clients connected to the EUD AP can reach the admin panel at http://manet.local

apt install -y avahi-daemon iperf3 traceroute 2>/dev/null || true
install -m 644 /etc/avahi/avahi-daemon.conf /etc/avahi/avahi-daemon.conf.bak 2>/dev/null || true
cp /usr/local/share/manet/avahi-daemon.conf /etc/avahi/avahi-daemon.conf
# Restrict avahi to the AP-only interface so nodes on the mesh don't conflict on 'manet'
AVAHI_AP_IF=$(head -1 /var/lib/no_mesh_if 2>/dev/null)
if [ -n "$AVAHI_AP_IF" ]; then
    sed -i "s/allow-interfaces=.*/allow-interfaces=$AVAHI_AP_IF/" /etc/avahi/avahi-daemon.conf
fi
cp /usr/local/share/manet/manet-http.service /etc/avahi/services/manet-http.service
cp /usr/local/share/manet/perf-http.service /etc/avahi/services/perf-http.service 2>/dev/null || true
systemctl enable avahi-daemon
systemctl restart avahi-daemon || true

# ============================================================================
# === UPS HAT (E) BATTERY MONITOR ===
# ============================================================================

# Enable I2C for battery fuel gauge (Waveshare UPS HAT E — IP2368 MCU at 0x2D)
# Bookworm+: /boot/firmware/config.txt; older images: /boot/config.txt
for _cfg in /boot/firmware/config.txt /boot/config.txt; do
    [ -f "$_cfg" ] || continue
    if ! grep -q '^dtparam=i2c_arm=on$' "$_cfg"; then
        # Remove any existing i2c_arm line then append the correct one
        sed -i '/^dtparam=i2c_arm/d' "$_cfg"
        echo "dtparam=i2c_arm=on" >> "$_cfg"
        echo " > I2C enabled in $_cfg"
    fi
done

# RPi5 uses i2c_designware — i2c-dev module must load at boot for /dev/i2c-1
if ! grep -q '^i2c-dev$' /etc/modules 2>/dev/null; then
    echo 'i2c-dev' >> /etc/modules
    echo " > i2c-dev added to /etc/modules"
fi

# Install smbus and i2c-tools for battery-reader.py and diagnostics
apt update -qq && apt install -y python3-smbus i2c-tools || true

systemctl enable battery-reader.service

# ============================================================================
# === FIRST RUN vs RE-RUN ===
# ============================================================================

# Determine if this script is being run for the first time
# and reboot if so to pick up the changes to the interfaces
if systemctl is-enabled radio-setup-run-once.service >/dev/null 2>&1; then
    apt remove -y network-manager
    systemctl mask rpi-eeprom-update.service
    systemctl set-default multi-user.target

    echo " >> Removing radio-setup-run-once.service"
    systemctl disable radio-setup-run-once.service

    echo " >> Doing initial Syncthing config..."
    sudo -u radio syncthing -generate="/home/radio/.config/syncthing"
    sleep 5
    killall syncthing
    mkdir -p /home/radio/Sync/mumble/backups
    chown -R radio:radio /home/radio/Sync
    chown -R radio:radio /home/radio/.config/syncthing

    SYNCTHING_CONFIG="/home/radio/.config/syncthing/config.xml"
    echo " >> Hardening Syncthing for local-only operation..."
    sed -i '/<options>/a <globalAnnounceEnabled>false</globalAnnounceEnabled>\n<relaysEnabled>false</relaysEnabled>' "$SYNCTHING_CONFIG"
    sed -i 's|<gui enabled="true" tls="false" debugging="false">.*</gui>|<gui enabled="true" tls="false" debugging="false">\n        <address>127.0.0.1:8384</address>\n    </gui>|' "$SYNCTHING_CONFIG"
    echo " -- CONFIGURED -- " >> /etc/issue
    reboot
fi

echo " > restarting networkd..."
systemctl restart systemd-networkd

echo " > restarting mesh supplicants..."
for WLAN in $(cat /var/lib/mesh_if 2>/dev/null); do
    if [[ -n "$AP_INTERFACE" ]] && [[ "$WLAN" == "$AP_INTERFACE" ]]; then
        continue
    fi
    systemctl reset-failed wpa_supplicant@$WLAN.service 2>/dev/null || true
    systemctl restart wpa_supplicant@$WLAN.service 2>/dev/null || true
done

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

echo heartbeat > /sys/class/leds/ACT/trigger

if [ -f /var/lib/radio-setup-reboot-pending ]; then
    rm -f /var/lib/radio-setup-reboot-pending
    echo ""
    echo "=================================================="
    echo " Interface renames pending - rebooting in 5s"
    echo " radio-setup will re-run automatically after boot"
    echo "=================================================="
    sleep 5
    reboot
fi
