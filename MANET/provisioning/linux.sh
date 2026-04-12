#!/bin/bash
#
#  A script to image new mesh radio nodes
set -e

# --- Configuration ---
TEMPLATE_FILE="firstrun.sh.template"
ROCK3A_TEMPLATE="rock3a-provision.sh.template"
TEMP_SCRIPT_FILE=$(mktemp)
# Full mirror, fast connection
ARMBIAN_IMAGE_URL="https://fi.mirror.armbian.de/dl/rock-3a/archive/Armbian_25.11.1_Rock-3a_trixie_vendor_6.1.115_minimal.img.xz"
ARMBIAN_IMAGE_FILENAME="Armbian_25.11.1_Rock-3a_trixie_vendor_6.1.115_minimal.img"
ARMBIAN_IMAGE=""  # Will be set by acquire_armbian_image function
CONFIG_DIR=".mesh-configs"
# Hardcode the OS image URL. rpi-imager will download and cache this.
PI_OS_IMAGE_URL="https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2025-10-02/2025-10-01-raspios-trixie-arm64-lite.img.xz"

# --- Helper Functions ---
# Function to validate regulatory domain
validate_regulatory_domain() {
    local domain=$1

    # List of valid regulatory domains (common ones)
    local valid_domains=(
        "US" "CA" "GB" "DE" "FR" "IT" "ES" "NL" "BE" "AT" "CH" "SE" "NO" "DK" "FI"
        "PL" "CZ" "HU" "GR" "PT" "IE" "RO" "BG" "HR" "SI" "SK" "LT" "LV" "EE" "CY"
        "MT" "LU" "AU" "NZ" "JP" "KR" "TW" "SG" "MY" "TH" "PH" "ID" "VN" "IN" "CN"
        "BR" "AR" "MX" "CL" "CO" "PE" "ZA" "IL" "AE" "SA" "RU" "UA" "TR" "EG"
    )

    # Convert to uppercase for comparison
    domain=$(echo "$domain" | tr '[:lower:]' '[:upper:]')

    for valid in "${valid_domains[@]}"; do
        if [ "$domain" == "$valid" ]; then
            echo "$domain"
            return 0
        fi
    done

    return 1
}

uses_eu_halow_region() {
    local domain
    domain=$(echo "$1" | tr '[:lower:]' '[:upper:]')

    local eu_halow_domains=(
        "AT" "BE" "BG" "HR" "CY" "CZ" "DK" "EE" "FI" "FR" "DE" "GR" "HU" "IE"
        "IT" "LV" "LT" "LU" "MT" "NL" "PL" "PT" "RO" "SK" "SI" "ES" "SE"
        "GB" "CH" "NO"
    )

    for eu_halow_domain in "${eu_halow_domains[@]}"; do
        if [ "$domain" == "$eu_halow_domain" ]; then
            return 0
        fi
    done

    return 1
}

halow_regulatory_domain_for_wifi_domain() {
    local domain
    domain=$(echo "$1" | tr '[:lower:]' '[:upper:]')

    if uses_eu_halow_region "$domain"; then
        echo "EU"
    else
        echo "$domain"
    fi
}

# Function to calculate network capacity
calculate_capacity() {
        local cidr=$1
        local max_euds=$2

        # Calculate total usable IPs
        local CALC_OUTPUT=$(ipcalc "$cidr" 2>/dev/null)
        if [ -z "$CALC_OUTPUT" ]; then
                echo "0"
                return 1
        fi

        local HOST_MIN=$(echo "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')
        local HOST_MAX=$(echo "$CALC_OUTPUT" | awk '/HostMax/ {print $2}')

        if [ -z "$HOST_MIN" ] || [ -z "$HOST_MAX" ]; then
                echo "0"
                return 1
        fi

        # Convert to integers for calculation
        local MIN_INT=$(echo $HOST_MIN | awk -F. '{print ($1 * 256^3) + ($2 * 256^2) + ($3 * 256) + $4}')
        local MAX_INT=$(echo $HOST_MAX | awk -F. '{print ($1 * 256^3) + ($2 * 256^2) + ($3 * 256) + $4}')

        local TOTAL_USABLE=$((MAX_INT - MIN_INT + 1))

        # Reserved IPs: 5 for services
        local RESERVED_SERVICES=5

        # Calculate based on max EUDs
        # We need to reserve enough for reasonable number of nodes
        # Start with assumption and iterate
        local AVAILABLE_FOR_NODES=$((TOTAL_USABLE - RESERVED_SERVICES))

        if [ "$max_euds" -gt 0 ]; then
                # Solve: nodes + (nodes * max_euds) = available
                # nodes * (1 + max_euds) = available
                # nodes = available / (1 + max_euds)
                local MAX_NODES=$((AVAILABLE_FOR_NODES / (1 + max_euds)))
                local EUD_POOL=$((MAX_NODES * max_euds))
                AVAILABLE_FOR_NODES=$((TOTAL_USABLE - RESERVED_SERVICES - EUD_POOL))
        else
                local MAX_NODES=$((AVAILABLE_FOR_NODES))
                local EUD_POOL=0
        fi

        echo "$TOTAL_USABLE $RESERVED_SERVICES $EUD_POOL $MAX_NODES"
}

# Function to ask for and validate the LAN CIDR block
ask_lan_cidr() {
        local max_euds=${1:-0}
        local DEFAULT_CIDR="10.30.2.0/24"
        local custom_cidr
        local confirm_default
        local ip_part
        local prefix_part

        while true; do
                read -p "Use default mesh network range ( $DEFAULT_CIDR )? (Y/n): " confirm_default
                confirm_default=${confirm_default:-y}

                if [ "$confirm_default" = "y" ] || [ "$confirm_default" = "Y" ]; then
                        LAN_CIDR_BLOCK="$DEFAULT_CIDR"
                else
                        # --- Custom CIDR Loop ---
                        while true; do
                               read -p "Enter custom CIDR block for the mesh (e.g., 10.10.0.0/16): " custom_cidr

                               # 1. Validate general format (IP/Prefix)
                               if ! [[ "$custom_cidr" =~ ^([0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3})\/([0-9]{1,2})$ ]]; then
                                echo "ERROR: Invalid format. Must be x.x.x.x/yy"
                                continue
                               fi

                               ip_part="${BASH_REMATCH[1]}"
                               prefix_part="${BASH_REMATCH[2]}"

                               # 2. Validate Prefix (16-28 is a reasonable range for a LAN)
                               if (( prefix_part < 16 || prefix_part > 26 )); then
                                echo "ERROR: Prefix /${prefix_part} is invalid. Must be between /16 and /26."
                                continue
                               fi

                               # 3. Validate IP as a private range
                               OIFS="$IFS"; IFS='.'; ip_octets=($ip_part); IFS="$OIFS"
                               local o1=${ip_octets[0]}
                               local o2=${ip_octets[1]}

                               local is_private=0
                               if [ "$o1" -eq 10 ]; then
                                is_private=1
                               elif [ "$o1" -eq 172 ] && [ "$o2" -ge 16 ] && [ "$o2" -le 31 ]; then
                                is_private=1
                               elif [ "$o1" -eq 192 ] && [ "$o2" -eq 168 ]; then
                                is_private=1
                               fi

                               if [ "$is_private" -eq 0 ]; then
                                echo "ERROR: IP $ip_part is not in a private range."
                                echo "Must be in 10.0.0.0/8, 172.16.0.0/12, or 192.168.0.0/16."
                                continue
                               fi

                               # 4. Check if it's a valid network address (e.g. not 192.168.1.1/24)
                               if [ "$prefix_part" -eq 24 ] && [ "${ip_octets[3]}" -ne 0 ]; then
                                echo "WARNING: For a /24 network, the IP should end in .0 (e.g., 192.168.1.0/24)."
                                echo "Your entry $custom_cidr may cause routing issues."
                                read -p "Use it anyway? (y/N): " use_anyway
                                use_anyway=${use_anyway:-n}
                                if [ "$use_anyway" != "y" ]; then
                                      continue
                                fi
                               fi

                               # All checks passed
                               LAN_CIDR_BLOCK="$custom_cidr"
                               break
                        done
                fi

                # Show capacity calculation if EUDs are configured
                if [ "$max_euds" -gt 0 ]; then
                        echo ""
                        echo "=== Network Capacity Analysis ==="
                        read TOTAL SERVICES EUD_POOL NODES <<< $(calculate_capacity "$LAN_CIDR_BLOCK" "$max_euds")

                        echo "Network: $LAN_CIDR_BLOCK"
                        echo "  Total usable IPs: $TOTAL"
                        echo "  Reserved for services: $SERVICES"
                        echo "  Reserved for EUD pool: $EUD_POOL (${max_euds} EUDs × ${NODES} nodes)"
                        echo "  Available for mesh nodes: $NODES"
                        echo "=================================="
                        echo ""

                        if [ "$NODES" -lt 3 ]; then
                               echo "WARNING: This configuration only supports $NODES mesh nodes."
                               echo "Consider using a larger network or reducing max EUDs per node."
                        fi

                        read -p "Accept this configuration? (Y/n): " accept
                        accept=${accept:-y}
                        if [ "$accept" = "y" ] || [ "$accept" = "Y" ]; then
                               break
                        fi
                        echo "Let's reconfigure..."
                else
                        echo "Using network: $LAN_CIDR_BLOCK"
                        break
                fi
        done
}


# This finds the top-level disk (e.g., nvme0n1) that hosts the / filesystem of the
# flashing computer
find_boot_disk() {
        local root_dev
        local physical_disk

        # Find the device hosting the root filesystem
        root_dev=$(findmnt -n -o SOURCE /)
        if [ -z "$root_dev" ]; then
                echo "ERROR: Could not find root filesystem." >&2
                return 1
        fi

        # Use lsblk with -s (inverse) to show all ancestor devices
        # Then filter for TYPE="disk" to get the physical disk
        physical_disk=$(lsblk -n -s -o NAME,TYPE "$root_dev" | awk '$2 == "disk" {print $1; exit}' | \
                sed 's/^[├└│─ ]*//')

        if [ -z "$physical_disk" ]; then
                echo "ERROR: Could not trace root device to physical disk." >&2
                return 1
        fi

        echo "$physical_disk"
}

# Function to generate a random alphanumeric password
generate_password() {
        local length=${1:-10}
        # Generate password with alphanumeric characters only (easier to type)
        openssl rand -base64 48 | tr -dc 'a-zA-Z0-9' | head -c "$length"
}

# Function to ask all setup questions
ask_questions() {
        echo "--- Starting New Configuration ---"

        echo "Select EUD (client) connection type:"
        select eud_choice in "Wired" "Wireless" "Auto"; do
                case $eud_choice in
                        "Wired" ) EUD_CONNECTION="wired"; break;;
                        "Wireless" ) EUD_CONNECTION="wireless"; break;;
                        "Auto" ) EUD_CONNECTION="auto"; break;;
                esac
        done

        # If Wireless or Auto, ask for LAN AP configuration
        if [ "$EUD_CONNECTION" = "wireless" ] || [ "$EUD_CONNECTION" = "auto" ]; then
                read -p "Enter LAN AP SSID Name: " LAN_AP_SSID

                while true; do
                        read -p "Enter LAN AP WPA2 Key (8-63 chars) [or press Enter to generate]: " LAN_AP_KEY
                        echo
                        if [ -z "$LAN_AP_KEY" ]; then
                               LAN_AP_KEY=$(openssl rand -base64 45  | tr -d '\n')
                               echo "Generated LAN AP Key: $LAN_AP_KEY"
                               break
                        fi

                        key_len=${#LAN_AP_KEY}
                        if (( key_len < 8 || key_len > 63 )); then
                               echo "ERROR: Key must be between 8 and 63 characters. You entered $key_len characters."
                        else
                               break # Valid key
                        fi
                done
        else
                LAN_AP_SSID=""
                LAN_AP_KEY=""
                MAX_EUDS_PER_NODE=0
        fi

        # Optional Software
        read -p "Install MediaMTX Server? (Y/n): " INSTALL_MEDIAMTX
        INSTALL_MEDIAMTX=${INSTALL_MEDIAMTX:-y}
        if [ "$INSTALL_MEDIAMTX" = "y" ] || [ "$INSTALL_MEDIAMTX" = "Y" ]; then INSTALL_MEDIAMTX="y"; else INSTALL_MEDIAMTX="n"; fi

        read -p "Install Mumble Server (murmur)? (Y/n): " INSTALL_MUMBLE
        INSTALL_MUMBLE=${INSTALL_MUMBLE:-y}
        if [ "$INSTALL_MUMBLE" = "y" ] || [ "$INSTALL_MUMBLE" = "Y" ]; then INSTALL_MUMBLE="y"; else INSTALL_MUMBLE="n"; fi

        # Mesh Configuration
        read -p "Enter MESH SSID Name: " MESH_SSID

        while true; do
                read -p "Enter MESH SAE Key (WPA3 password, 8-63 chars) [or press Enter to generate]: " MESH_SAE_KEY
                echo
                if [ -z "$MESH_SAE_KEY" ]; then
                        MESH_SAE_KEY=$(openssl rand -base64 45  | tr -d '\n')
                        echo "Generated SAE Key: $MESH_SAE_KEY"
                        break
                fi

                key_len=${#MESH_SAE_KEY}
                if (( key_len < 8 || key_len > 63 )); then
                        echo "ERROR: Key must be between 8 and 63 characters. You entered $key_len characters."
                else
                        break # Valid key
                fi
        done

        # WiFi Regulatory Domain
    while true; do
        read -p "Enter WiFi regulatory domain (2-letter country code, default: US): " REGULATORY_DOMAIN
        REGULATORY_DOMAIN=${REGULATORY_DOMAIN:-US}

        if validated_domain=$(validate_regulatory_domain "$REGULATORY_DOMAIN"); then
            REGULATORY_DOMAIN="$validated_domain"
            echo "Using regulatory domain: $REGULATORY_DOMAIN"
            HALOW_REGULATORY_DOMAIN=$(halow_regulatory_domain_for_wifi_domain "$REGULATORY_DOMAIN")
            if [ "$HALOW_REGULATORY_DOMAIN" != "$REGULATORY_DOMAIN" ]; then
                echo "Using HaLow regulatory region: $HALOW_REGULATORY_DOMAIN"
            fi
            break
        else
            echo "ERROR: Invalid regulatory domain code: $REGULATORY_DOMAIN"
            echo "Please enter a valid 2-letter ISO country code (e.g., US, GB, DE, FR, JP)"
            echo "Common codes: US (United States), GB (UK), DE (Germany), FR (France), JP (Japan)"
            echo "              CA (Canada), AU (Australia), NZ (New Zealand), CN (China)"
			echo "NOTE: EU is not a country code, use your actual country"
        fi
    done

        echo "The device will have a user called radio, for ssh access."
        read -p "Enter a password for the radio user [or press Enter to default to 'radio']: " RADIO_PW
        echo

        if [ -z "$RADIO_PW" ]; then
                RADIO_PW="radio"
                echo "Setting default password"
        fi
        echo "Setting radio password to be $RADIO_PW"

        # Network administrator password
        echo ""
        echo "The network administrator password is used to access the mesh admin interface."
        read -p "Enter admin password [or press Enter to generate 10-char random]: " ADMIN_PW
        echo
        if [ -z "$ADMIN_PW" ]; then
                ADMIN_PW=$(generate_password 10)
                echo "Generated admin password: $ADMIN_PW"
        else
                echo "Admin password set."
        fi

        # Automatic updates for MANET tools
        echo ""
        read -p "Enable automatic updates for MANET tools? (Y/n): " AUTO_UPDATE
        AUTO_UPDATE=${AUTO_UPDATE:-y}
        if [ "$AUTO_UPDATE" = "y" ] || [ "$AUTO_UPDATE" = "Y" ]; then
                AUTO_UPDATE="y"
                echo "Automatic updates enabled."
        else
                AUTO_UPDATE="n"
                echo "Automatic updates disabled."
        fi

        # Ask for max EUDs before CIDR selection
        if [ "$EUD_CONNECTION" = "wireless" ] || [ "$EUD_CONNECTION" = "auto" ]; then
                while true; do
                        read -p "Maximum EUDs per node's AP (1-20): " MAX_EUDS_PER_NODE
                        if [[ "$MAX_EUDS_PER_NODE" =~ ^[0-9]+$ ]] && [ "$MAX_EUDS_PER_NODE" -ge 1 ] && [ "$MAX_EUDS_PER_NODE" -le 20 ]; then
                               break
                        else
                               echo "ERROR: Please enter a number between 1 and 20."
                        fi
                done
        fi

        # CIDR selection
        ask_lan_cidr "$MAX_EUDS_PER_NODE"

        # Auto Channel Selection (skip if wireless or auto)
        if [ "$EUD_CONNECTION" = "wireless" ] || [ "$EUD_CONNECTION" = "auto" ]; then
                AUTO_CHANNEL="n"
                echo "Automatic WiFi Channel Selection disabled (not compatible with Wireless/Auto EUD mode)"
        else
                read -p "Use Automatic WiFi Channel Selection? (Y/n): " AUTO_CHANNEL
                AUTO_CHANNEL=${AUTO_CHANNEL:-y}
                if [ "$AUTO_CHANNEL" = "y" ] || [ "$AUTO_CHANNEL" = "Y" ]; then AUTO_CHANNEL="y"; else AUTO_CHANNEL="n"; fi
        fi

        echo "----------------------------------"
}

# Function to save the current variables to a config file
save_config() {
        echo ""
        read -p "Save this configuration? (Y/n): " save_choice
        save_choice=${save_choice:-y}
        if [ "$save_choice" = "y" ] || [ "$save_choice" = "Y" ]; then
                read -p "Enter a name for this config: " config_name
                if [ -z "$config_name" ]; then
                        echo "Invalid name, skipping save."
                        return
                fi

                local CONFIG_FILE="$CONFIG_DIR/$config_name.conf"

                cat << EOF > "$CONFIG_FILE"
# Mesh Config: $config_name
EUD_CONNECTION="$EUD_CONNECTION"
LAN_AP_SSID="$LAN_AP_SSID"
LAN_AP_KEY="$LAN_AP_KEY"
MAX_EUDS_PER_NODE="$MAX_EUDS_PER_NODE"
INSTALL_MEDIAMTX="$INSTALL_MEDIAMTX"
INSTALL_MUMBLE="$INSTALL_MUMBLE"
REGULATORY_DOMAIN="$REGULATORY_DOMAIN"
HALOW_REGULATORY_DOMAIN="$HALOW_REGULATORY_DOMAIN"
MESH_SSID="$MESH_SSID"
MESH_SAE_KEY="$MESH_SAE_KEY"
LAN_CIDR_BLOCK="$LAN_CIDR_BLOCK"
AUTO_CHANNEL="$AUTO_CHANNEL"
RADIO_PW="$RADIO_PW"
ADMIN_PW="$ADMIN_PW"
AUTO_UPDATE="$AUTO_UPDATE"
EOF

                echo "Configuration saved to $CONFIG_FILE"
        fi
}

# Function to load variables from a config file
load_config() {
        local CONFIG_FILE="$1"
        echo "Loading config from $CONFIG_FILE..."
        # Source the file to load the variables into this script
        source "$CONFIG_FILE"
        HALOW_REGULATORY_DOMAIN=${HALOW_REGULATORY_DOMAIN:-$(halow_regulatory_domain_for_wifi_domain "$REGULATORY_DOMAIN")}

        # Display the loaded settings
        echo "--- Loaded Configuration ---"
        head -n 1 "$CONFIG_FILE" | sed 's/\#//'
        echo "  EUD Connection: $EUD_CONNECTION"
        if [ "$EUD_CONNECTION" = "wireless" ] || [ "$EUD_CONNECTION" = "auto" ]; then
                echo "  LAN AP SSID: $LAN_AP_SSID"
                echo "  LAN AP Key: $LAN_AP_KEY"
                echo "  Max EUDs per node: $MAX_EUDS_PER_NODE"
        fi
        echo "  Install MediaMTX: $INSTALL_MEDIAMTX"
        echo "  Install Mumble: $INSTALL_MUMBLE"
        echo "  Regulatory Domain: $REGULATORY_DOMAIN"
        echo "  HaLow Regulatory Region: $HALOW_REGULATORY_DOMAIN"
        echo "  Mesh SSID: $MESH_SSID"
        echo "  Mesh SAE Key: $MESH_SAE_KEY"
        echo "  LAN CIDR Block: $LAN_CIDR_BLOCK"
        echo "  Auto Channel: $AUTO_CHANNEL"
        echo "  User password: $RADIO_PW"
        echo "  Admin password: ${ADMIN_PW:-(not set)}"
        echo "  Auto Update: ${AUTO_UPDATE:-n}"
        echo "----------------------------"
}

# Function to acquire Armbian image for Rock 3A
# Sets ARMBIAN_IMAGE to the path of a usable .img file
acquire_armbian_image() {
        echo ""
        echo "--- Armbian Image Setup for Rock 3A ---"

        # Check if default image exists locally (uncompressed)
        if [ -f "$ARMBIAN_IMAGE_FILENAME" ]; then
                echo "Found local Armbian image: $ARMBIAN_IMAGE_FILENAME"
                ARMBIAN_IMAGE="$ARMBIAN_IMAGE_FILENAME"
                return 0
        fi

        # Check for compressed version
        if [ -f "${ARMBIAN_IMAGE_FILENAME}.xz" ]; then
                echo "Found compressed Armbian image: ${ARMBIAN_IMAGE_FILENAME}.xz"
                echo "Decompressing (this may take a moment)..."
                xz -dk "${ARMBIAN_IMAGE_FILENAME}.xz"
                if [ $? -eq 0 ]; then
                        ARMBIAN_IMAGE="$ARMBIAN_IMAGE_FILENAME"
                        echo "Decompression complete."
                        return 0
                else
                        echo "ERROR: Decompression failed."
                        return 1
                fi
        fi

        echo "Armbian image not found locally."
        echo ""
        echo "Options:"
        echo "  1. Download from Armbian mirror (recommended)"
        echo "     URL: $ARMBIAN_IMAGE_URL"
        echo "  2. Provide path to an existing Armbian Trixie image"
        echo ""

        while true; do
                read -p "Select option (1 or 2): " img_choice
                case $img_choice in
                        1)
                               download_armbian_image
                               return $?
                               ;;
                        2)
                               select_custom_armbian_image
                               return $?
                               ;;
                        *)
                               echo "Invalid selection. Please enter 1 or 2."
                               ;;
                esac
        done
}

# Function to download Armbian image from mirror
download_armbian_image() {
        local compressed_file="${ARMBIAN_IMAGE_FILENAME}.xz"

        echo ""
        echo "Downloading Armbian image..."
        echo "Source: $ARMBIAN_IMAGE_URL"
        echo ""

        # Check for wget or curl
        if command -v wget &> /dev/null; then
                wget --progress=bar:force -O "$compressed_file" "$ARMBIAN_IMAGE_URL"
        elif command -v curl &> /dev/null; then
                curl -L --progress-bar -o "$compressed_file" "$ARMBIAN_IMAGE_URL"
        else
                echo "ERROR: Neither wget nor curl found. Please install one to download."
                return 1
        fi

        if [ $? -ne 0 ]; then
                echo "ERROR: Download failed."
                rm -f "$compressed_file" 2>/dev/null
                return 1
        fi

        echo ""
        echo "Download complete. Decompressing..."
        xz -dk "$compressed_file"

        if [ $? -ne 0 ]; then
                echo "ERROR: Decompression failed."
                return 1
        fi

        ARMBIAN_IMAGE="$ARMBIAN_IMAGE_FILENAME"
        echo "Image ready: $ARMBIAN_IMAGE"
        return 0
}

# Function to select a custom Armbian image path
select_custom_armbian_image() {
        echo ""
        echo "=============================================="
        echo "  IMPORTANT: Armbian Image Selection"
        echo "=============================================="
        echo "Please ensure you are selecting an Armbian image"
        echo "that is compatible with the Radxa Rock 3A board."
        echo ""
        echo "       The expected environment is:"
        echo "    minimal/IoT Armbian Trixie ( Debian 13)"
        echo ""
        echo "The image should be an uncompressed .img file."
        echo "If you have a .img.xz file, it will be decompressed."
        echo "=============================================="
        echo ""

        while true; do
                read -p "Enter path to Armbian image: " custom_path

                # Expand ~ if present
                custom_path="${custom_path/#\~/$HOME}"

                if [ -z "$custom_path" ]; then
                        echo "No path entered. Please try again or press Ctrl+C to cancel."
                        continue
                fi

                # Check if it's a compressed file
                if [ -f "$custom_path" ] && [[ "$custom_path" == *.xz ]]; then
                        echo "Compressed image detected. Decompressing..."
                        local decompressed_path="${custom_path%.xz}"
                        xz -dk "$custom_path"
                        if [ $? -eq 0 ]; then
                               ARMBIAN_IMAGE="$decompressed_path"
                               echo "Image ready: $ARMBIAN_IMAGE"
                               return 0
                        else
                               echo "ERROR: Decompression failed."
                               return 1
                        fi
                elif [ -f "$custom_path" ] && [[ "$custom_path" == *.img ]]; then
                        ARMBIAN_IMAGE="$custom_path"
                        echo "Using image: $ARMBIAN_IMAGE"
                        return 0
                elif [ -f "$custom_path" ]; then
                        echo "WARNING: File exists but doesn't have .img or .img.xz extension."
                        read -p "Use this file anyway? (y/N): " use_anyway
                        if [ "$use_anyway" = "y" ] || [ "$use_anyway" = "Y" ]; then
                               ARMBIAN_IMAGE="$custom_path"
                               echo "Using image: $ARMBIAN_IMAGE"
                               return 0
                        fi
                else
                        echo "ERROR: File not found: $custom_path"
                        echo "Please check the path and try again."
                fi
        done
}

# Returns the chosen TARGET_DEVICE path in a global variable.
select_hardware_and_target_device() {
        echo ""
        echo "--- 1. Select Hardware ---"

        # This variable will be set to 1 by the CM4 logic to skip the device menu
        local SKIP_DEV_SELECT=0

        echo "Select Raspberry Pi Model:"
        select hw_choice in "Raxda Rock 3A" "Raspberry Pi 5" "Raspberry Pi 4B" "Compute Module 4 (CM4)"; do
                case $hw_choice in
                        "Raxda Rock 3A" )
                               HARDWARE_MODEL="r3a"
                               if ! command -v losetup &> /dev/null; then
                                echo "ERROR: 'losetup' command not found."
                                echo "Cannot customize disk image without losetup"
                                exit 1
                               fi
                               if ! command -v xz &> /dev/null; then
                                echo "ERROR: 'xz' command not found. Needed for decompressing Armbian images."
                                echo "Please install it (e.g., 'sudo apt install xz-utils')."
                                exit 1
                               fi
                               break
                               ;;
                        "Raspberry Pi 5" )
                               HARDWARE_MODEL="rpi5"
                               break
                               ;;
                        "Raspberry Pi 4B" )
                               HARDWARE_MODEL="rpi4"
                               break
                               ;;
                        "Compute Module 4 (CM4)" )
                               echo "Compute Module 4 selected."
                               if ! command -v rpiboot &> /dev/null; then
                                echo "ERROR: 'rpiboot' command not found."
                                echo "Please install it (e.g., 'sudo apt install rpiboot') and re-run."
                                exit 1
                               fi

                               # --- *** Before/After device detection *** ---
                               echo "Detecting disks *before* rpiboot..."
                               local DISKS_BEFORE
                               DISKS_BEFORE=$(lsblk -d -n -o NAME)
                               echo
                               echo "Please connect your CM4 to this computer in USB-boot mode."
                               read -p "Press Enter to run 'sudo rpiboot' and mount the eMMC..."
                               echo
                               sudo rpiboot
                               echo "'rpiboot' finished. Waiting 4s for device to settle..."
                               sleep 4

                               echo "Detecting disks *after* rpiboot..."
                               local DISKS_AFTER
                               DISKS_AFTER=$(lsblk -d -n -o NAME)

                               # Compare the lists to find the new disk
                               local NEW_DISK
                               NEW_DISK=$(comm -13 <(echo "$DISKS_BEFORE" | sort) <(echo "$DISKS_AFTER" | sort))

                               if [ -z "$NEW_DISK" ]; then
                                echo "ERROR: No new disk detected after rpiboot."
                                echo "Please check connections and try again."
                                exit 1
                               fi

                               local NEW_DISK_SIZE
                               NEW_DISK_SIZE=$(lsblk -d -n -o SIZE "/dev/$NEW_DISK")
                               TARGET_DEVICE="/dev/$NEW_DISK" # Set the global variable
                               echo
                               echo "Detected new device: $TARGET_DEVICE ($NEW_DISK_SIZE)"

                               HARDWARE_MODEL="rpi4" # Set to rpi4 for the template
                               # Set flag to skip manual device selection
                               SKIP_DEV_SELECT=1
                               break
                               ;;
                esac
        done

        echo ""
        echo "--- 2. Select Target Device ---"

        if [ "$SKIP_DEV_SELECT" -eq 1 ]; then
                echo "Using auto-detected CM4 device: $TARGET_DEVICE"
        else
                echo "Detecting available devices..."
                local DEVICES=()

                # Get the boot disk to exclude it
                local BOOT_DISK
                BOOT_DISK=$(find_boot_disk)
                echo "(Excluding boot disk: $BOOT_DISK)"

                # Use lsblk in "pairs" mode (-P) and eval the output
                while IFS= read -r line; do
                        # Reset variables for each line
                        local NAME=""
                        local MOUNTPOINT=""
                        local SIZE=""
                        local TYPE=""
                        eval "$line"

                        # Add any top-level disk that is NOT the boot disk
                        if [ "$TYPE" == "disk" ] && [ "$NAME" != "$BOOT_DISK" ]; then
                               DEVICES+=("/dev/$NAME ($SIZE)")
                        fi
                done < <(lsblk -n -P -o NAME,MOUNTPOINT,SIZE,TYPE)
                # --- *** END FIX *** ---

                if [ ${#DEVICES[@]} -eq 0 ]; then
                        echo "ERROR: No suitable target devices found (e.g., no USB/SD drives detected)."
                        echo "Please make sure your SD card reader or USB drive is plugged in."
                        rm "$TEMP_SCRIPT_FILE"
                        exit 1
                fi

                echo "Please select the target device:"
                PS3="Enter number (or 'q' to quit): "
                select device_choice in "${DEVICES[@]}" "Quit"; do
                        if [[ "$REPLY" =~ ^[Qq]$ ]]; then
                               echo "Aborting."
                               rm "$TEMP_SCRIPT_FILE"
                               exit 0
                        fi

                        if [ "$device_choice" == "Quit" ]; then
                               echo "Aborting."
                               rm "$TEMP_SCRIPT_FILE"
                               exit 0
                        fi

                        if [ -n "$device_choice" ]; then
                               # Extract the path (e.g., "/dev/sda") from "/dev/sda (8G)"
                               TARGET_DEVICE=$(echo "$device_choice" | awk '{print $1}')
                               echo "Selected device: $TARGET_DEVICE"
                               break
                        else
                               echo "Invalid selection."
                        fi
                done
        fi
}

# Function to display final confirmation before flashing
confirm_flash() {
        local device="$1"
        local device_size=$(lsblk -d -n -o SIZE "$device" 2>/dev/null || echo "unknown")

        echo ""
        echo "=============================================="
        echo "         ⚠️  FINAL CONFIRMATION  ⚠️"
        echo "=============================================="
        echo ""
        echo "You are about to ERASE and FLASH:"
        echo ""
        echo "  Device: $device"
        echo "  Size:   $device_size"
        echo ""
        echo "  Hardware: $HARDWARE_MODEL"
        echo "  Mesh SSID: $MESH_SSID"
        echo "  Network: $LAN_CIDR_BLOCK"
        echo ""
        echo "⚠️  ALL DATA ON $device WILL BE DESTROYED! ⚠️"
        echo ""
        echo "=============================================="
        echo ""

        read -p "Type 'yes' to proceed, anything else to abort: " confirm
        if [ "$confirm" != "yes" ]; then
                echo ""
                echo "Aborted by user."
                exit 0
        fi

        echo ""
        echo "Proceeding with flash..."
}


# --- Main Script ---

# This function will set HARDWARE_MODEL and TARGET_DEVICE
select_hardware_and_target_device


# --- 1. Check Dependencies ---
if [ "$HARDWARE_MODEL" != "r3a" ]; then
        if ! command -v rpi-imager &> /dev/null; then
                echo "ERROR: 'rpi-imager' command not found. Please install it."
                exit 1
        fi
fi

if [ ! -f "$TEMPLATE_FILE" ]; then
        echo "ERROR: Template file '$TEMPLATE_FILE' not found."
        exit 1
fi
if ! command -v openssl &> /dev/null; then
        echo "ERROR: 'openssl' command not found. Needed for generating SAE key."
        exit 1
fi
if ! command -v bc &> /dev/null; then
        echo "ERROR: 'bc' command not found. Needed for network calculation."
        echo "Please install it (e.g., 'sudo apt install bc')."
        exit 1
fi
if ! command -v lsblk &> /dev/null; then
        echo "ERROR: 'lsblk' command not found. Needed for device detection."
        exit 1
fi
if ! command -v findmnt &> /dev/null; then
        echo "ERROR: 'findmnt' command not found. Needed for boot device detection."
        echo "Please install it (e.g., 'sudo apt install util-linux')."
        exit 1
fi

# Ensure config directory exists
mkdir -p "$CONFIG_DIR"

# --- 2. Load or Create Config ---
# Find config files
config_files=("$CONFIG_DIR"/*.conf)
num_configs=${#config_files[@]}

# Check if the first match is an actual file
if [ ! -f "${config_files[0]}" ]; then
        num_configs=0
fi

if [ "$num_configs" -gt 0 ]; then
        echo "Found $num_configs saved configuration(s)."
        echo "What would you like to do?"
        select choice in "Load a saved configuration" "Create a new configuration"; do
                case $choice in
                        "Load a saved configuration" )
                               echo "Please select a configuration to load:"
                               # Build a list of just the names for the select menu
                               config_names=()
                               for f in "${config_files[@]}"; do
                                config_names+=("$(basename "$f" .conf)")
                               done
                               config_names+=("Cancel")

                               PS3="Select config (or 'Cancel'): "
                               select config_name in "${config_names[@]}"; do
                                if [ "$config_name" == "Cancel" ]; then
                                      echo "Aborting."
                                      exit 0
                                fi
                                if [ -n "$config_name" ]; then
                                      load_config "$CONFIG_DIR/$config_name.conf"
                                      break
                                else
                                      echo "Invalid selection."
                                fi
                               done
                               break
                               ;;
                        "Create a new configuration" )
                               ask_questions
                               save_config
                               break
                               ;;
                esac
        done
else
        echo "No saved configs found. Starting new setup."
        ask_questions
        save_config
fi


# --- 3. Get Image & Device ---
echo ""
echo "--- Image & Device ---"




# Rock3A provisioning section
if [ "$HARDWARE_MODEL" = "r3a" ]; then
		# Now that we know the hardware, acquire the appropriate image
		acquire_armbian_image

        # Create temp copy of image to avoid modifying original
        TEMP_IMAGE=$(mktemp --suffix=.img)
        echo "Creating temporary copy of $ARMBIAN_IMAGE..."
        cp "$ARMBIAN_IMAGE" "$TEMP_IMAGE"

        # Loop mount the temp image
        LOOP_DEV=$(sudo losetup -fP --show "$TEMP_IMAGE")
        echo "Mounted image as: $LOOP_DEV"

        # Mount root partition (partition 2 on Armbian - partition 1 is /boot)
        ROOT_MOUNT="/tmp/armbian-root"
        sudo mkdir -p "$ROOT_MOUNT"
        echo "Mounting ${LOOP_DEV}p2 to $ROOT_MOUNT"
        sudo mount "${LOOP_DEV}p2" "$ROOT_MOUNT"

        # Write mesh configuration to /etc/mesh.conf
        echo "Writing /etc/mesh.conf..."
        sudo tee "$ROOT_MOUNT/etc/mesh.conf" > /dev/null << EOF
# Mesh Network Configuration
# Generated by provisioning script on $(date)
hardware_model=${HARDWARE_MODEL}
eud=${EUD_CONNECTION}
lan_ap_ssid=${LAN_AP_SSID}
lan_ap_key=${LAN_AP_KEY}
max_euds_per_node=${MAX_EUDS_PER_NODE}
mtx=${INSTALL_MEDIAMTX}
mumble=${INSTALL_MUMBLE}
mesh_ssid=${MESH_SSID}
mesh_key=${MESH_SAE_KEY}
ipv4_network=${LAN_CIDR_BLOCK}
acs=${AUTO_CHANNEL}
regulatory_domain=${REGULATORY_DOMAIN}
halow_regulatory_domain=${HALOW_REGULATORY_DOMAIN}
admin_password=${ADMIN_PW}
auto_update=${AUTO_UPDATE}
EOF

        # ============================================================
        # BYPASS ARMBIAN-FIRSTLOGIN - Headless auto-provisioning
        # ============================================================
        
        # Remove .not_logged_in_yet to prevent armbian-firstlogin from running
        echo "Removing .not_logged_in_yet to bypass interactive setup..."
        sudo rm -f "$ROOT_MOUNT/root/.not_logged_in_yet"
        
        # Pre-create the radio user with hashed password
        echo "Creating radio user..."
        RADIO_PW_HASH=$(openssl passwd -6 "$RADIO_PW")
        
        # Add radio user to passwd (UID 1000, GID 1000, home /home/radio, shell /bin/bash)
        echo "radio:x:1000:1000:radio:/home/radio:/bin/bash" | sudo tee -a "$ROOT_MOUNT/etc/passwd" > /dev/null
        
        # Add radio group
        echo "radio:x:1000:" | sudo tee -a "$ROOT_MOUNT/etc/group" > /dev/null
        
        # Add radio to shadow with hashed password
        echo "radio:${RADIO_PW_HASH}:19700:0:99999:7:::" | sudo tee -a "$ROOT_MOUNT/etc/shadow" > /dev/null
        
        # Add radio to sudo group
        sudo sed -i 's/^sudo:x:\([0-9]*\):.*$/sudo:x:\1:radio/' "$ROOT_MOUNT/etc/group"
        
        # Create home directory
        sudo mkdir -p "$ROOT_MOUNT/home/radio"
        sudo chown 1000:1000 "$ROOT_MOUNT/home/radio"
        sudo chmod 755 "$ROOT_MOUNT/home/radio"
        
        # Add radio to sudoers (passwordless sudo)
        echo "radio ALL=(ALL) NOPASSWD: ALL" | sudo tee "$ROOT_MOUNT/etc/sudoers.d/radio" > /dev/null
        sudo chmod 440 "$ROOT_MOUNT/etc/sudoers.d/radio"

        # ============================================================
        # Generate and install the provisioning script
        # ============================================================
        
        echo "Generating provisioning script from Rock3A template..."
        TEMP_PROVISION_SCRIPT=$(mktemp)
        
        # Check if Rock3A template exists
        if [ ! -f "$ROCK3A_TEMPLATE" ]; then
                echo "ERROR: Rock3A template '$ROCK3A_TEMPLATE' not found."
                exit 1
        fi
        
        # Copy the template
        cp "$ROCK3A_TEMPLATE" "$TEMP_PROVISION_SCRIPT"
        
        # Apply all the placeholder substitutions
        sed -i "s|__HARDWARE_MODEL__|${HARDWARE_MODEL}|g" "$TEMP_PROVISION_SCRIPT"
        sed -i "s|__EUD_CONNECTION__|${EUD_CONNECTION}|g" "$TEMP_PROVISION_SCRIPT"
        sed -i "s|__LAN_AP_SSID__|${LAN_AP_SSID}|g" "$TEMP_PROVISION_SCRIPT"
        sed -i "s|__LAN_AP_KEY__|${LAN_AP_KEY}|g" "$TEMP_PROVISION_SCRIPT"
        sed -i "s|__MAX_EUDS_PER_NODE__|${MAX_EUDS_PER_NODE}|g" "$TEMP_PROVISION_SCRIPT"
        sed -i "s|__INSTALL_MEDIAMTX__|${INSTALL_MEDIAMTX}|g" "$TEMP_PROVISION_SCRIPT"
        sed -i "s|__INSTALL_MUMBLE__|${INSTALL_MUMBLE}|g" "$TEMP_PROVISION_SCRIPT"
        sed -i "s|__MESH_SSID__|${MESH_SSID}|g" "$TEMP_PROVISION_SCRIPT"
        sed -i "s|__MESH_SAE_KEY__|${MESH_SAE_KEY}|g" "$TEMP_PROVISION_SCRIPT"
        sed -i "s|__LAN_CIDR_BLOCK__|${LAN_CIDR_BLOCK}|g" "$TEMP_PROVISION_SCRIPT"
        sed -i "s|__AUTO_CHANNEL__|${AUTO_CHANNEL}|g" "$TEMP_PROVISION_SCRIPT"
        sed -i "s|__RADIO_PW__|${RADIO_PW}|g" "$TEMP_PROVISION_SCRIPT"
        sed -i "s|__REGULATORY_DOMAIN__|${REGULATORY_DOMAIN}|g" "$TEMP_PROVISION_SCRIPT"
        sed -i "s|__HALOW_REGULATORY_DOMAIN__|${HALOW_REGULATORY_DOMAIN}|g" "$TEMP_PROVISION_SCRIPT"
        sed -i "s|__ADMIN_PW__|${ADMIN_PW}|g" "$TEMP_PROVISION_SCRIPT"
        sed -i "s|__AUTO_UPDATE__|${AUTO_UPDATE}|g" "$TEMP_PROVISION_SCRIPT"
        
        # Install provisioning script directly to /usr/local/bin
        echo "Installing provisioning script to /usr/local/bin/provision-mesh.sh..."
        sudo cp "$TEMP_PROVISION_SCRIPT" "$ROOT_MOUNT/usr/local/bin/provision-mesh.sh"
        sudo chmod +x "$ROOT_MOUNT/usr/local/bin/provision-mesh.sh"
        
        # Cleanup temp file
        rm -f "$TEMP_PROVISION_SCRIPT"

        # ============================================================
        # Create systemd service for auto-provisioning on first boot
        # ============================================================
        
        echo "Creating mesh-provision systemd service..."
        sudo tee "$ROOT_MOUNT/etc/systemd/system/mesh-provision.service" > /dev/null << 'SERVICE_EOF'
[Unit]
Description=Mesh Network First Boot Provisioning
ConditionPathExists=/root/.mesh-not-provisioned
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/provision-mesh.sh
ExecStartPost=/bin/rm -f /root/.mesh-not-provisioned
RemainAfterExit=yes
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
SERVICE_EOF

        # Create the flag file that triggers provisioning
        echo "Creating provisioning trigger flag..."
        sudo touch "$ROOT_MOUNT/root/.mesh-not-provisioned"
        
        # Enable the service (create symlink manually since systemctl won't work on mounted image)
        echo "Enabling mesh-provision service..."
        sudo mkdir -p "$ROOT_MOUNT/etc/systemd/system/multi-user.target.wants"
        sudo ln -sf /etc/systemd/system/mesh-provision.service \
                "$ROOT_MOUNT/etc/systemd/system/multi-user.target.wants/mesh-provision.service"

        # ============================================================
        # Unmount and flash
        # ============================================================
        
        echo "Unmounting image..."
        sudo sync
        sudo umount "$ROOT_MOUNT"
        sudo rmdir "$ROOT_MOUNT"
        sudo losetup -d "$LOOP_DEV"

        # Final confirmation before flashing
        confirm_flash "$TARGET_DEVICE"

        # Wipe target device to avoid stale partition data
        echo "Wiping target device..."
        sudo wipefs -a "$TARGET_DEVICE"

        # Flash to device
        echo "Flashing image to $TARGET_DEVICE..."
        sudo dd if="$TEMP_IMAGE" of="$TARGET_DEVICE" bs=4M status=progress conv=fsync
        sudo sync

        # Clean up temp image
        rm -f "$TEMP_IMAGE"

        echo ""
        echo "=============================================="
        echo "           ✅ Flash complete!"
        echo "=============================================="
        echo ""
        echo "You can now remove the SD card and boot your"
        echo "Rock 3A. First boot provisioning will run"
        echo "automatically when connected to the internet."
        echo ""
        echo "  - Root password: 1234 (Armbian default)"
        echo "  - Radio user: radio / <your configured password>"
        echo ""
		echo " ONCE BOOTED, THE MESH NODE WILL AUTOMATICALLY START"
		echo " SETTING ITSELF UP AND WILL REBOOT MULTIPLE TIMES"
		echo " Just leave it alone, this process takes about ten"
		echo " minutes"
else
        # Raspberry Pi path - use rpi-imager

        # Generate the firstrun script from template
        echo "Generating firstrun script from template..."

        # Do all the same substitutions as before
        sed -e "s|__HARDWARE_MODEL__|${HARDWARE_MODEL}|g" \
            -e "s|__EUD_CONNECTION__|${EUD_CONNECTION}|g" \
            -e "s|__LAN_AP_SSID__|${LAN_AP_SSID}|g" \
            -e "s|__LAN_AP_KEY__|${LAN_AP_KEY}|g" \
            -e "s|__MAX_EUDS_PER_NODE__|${MAX_EUDS_PER_NODE}|g" \
            -e "s|__INSTALL_MEDIAMTX__|${INSTALL_MEDIAMTX}|g" \
            -e "s|__INSTALL_MUMBLE__|${INSTALL_MUMBLE}|g" \
            -e "s|__MESH_SSID__|${MESH_SSID}|g" \
            -e "s|__MESH_SAE_KEY__|${MESH_SAE_KEY}|g" \
            -e "s|__LAN_CIDR_BLOCK__|${LAN_CIDR_BLOCK}|g" \
            -e "s|__AUTO_CHANNEL__|${AUTO_CHANNEL}|g" \
            -e "s|__RADIO_PW__|${RADIO_PW}|g" \
            -e "s|__REGULATORY_DOMAIN__|${REGULATORY_DOMAIN}|g" \
            -e "s|__HALOW_REGULATORY_DOMAIN__|${HALOW_REGULATORY_DOMAIN}|g" \
            -e "s|__ADMIN_PW__|${ADMIN_PW}|g" \
            -e "s|__AUTO_UPDATE__|${AUTO_UPDATE}|g" \
            "$TEMPLATE_FILE" > "$TEMP_SCRIPT_FILE"
        
        # Final confirmation before flashing
        confirm_flash "$TARGET_DEVICE"

        sudo rpi-imager --cli "$PI_OS_IMAGE_URL" "$TARGET_DEVICE" --first-run-script "$TEMP_SCRIPT_FILE"
        echo ""
        echo " ONCE BOOTED, THE MESH NODE WILL AUTOMATICALLY START"
        echo " SETTING ITSELF UP AND WILL REBOOT MULTIPLE TIMES"
        echo " Just leave it alone, this process takes about ten"
        echo " minutes"

fi
