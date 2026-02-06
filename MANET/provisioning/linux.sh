#!/bin/bash
#
#  A script to image new mesh radio nodes
set -e

# --- Configuration ---
TEMPLATE_FILE="firstrun.sh.template"
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
        "BR" "AR" "MX" "CL" "CO" "PE" "ZA" "IL" "AE" "SA" "RU" "UA" "TR" "EG" "MA"
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
                read -p "Use default LAN network $DEFAULT_CIDR? (Y/n): " confirm_default
                confirm_default=${confirm_default:-y}

                if [ "$confirm_default" = "y" ] || [ "$confirm_default" = "Y" ]; then
                        LAN_CIDR_BLOCK="$DEFAULT_CIDR"
                else
                        # --- Custom CIDR Loop ---
                        while true; do
                               read -p "Enter custom LAN CIDR block (e.g., 10.10.0.0/16): " custom_cidr

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
            break
        else
            echo "ERROR: Invalid regulatory domain code: $REGULATORY_DOMAIN"
            echo "Please enter a valid 2-letter ISO country code (e.g., US, GB, DE, FR, JP)"
            echo "Common codes: US (United States), GB (UK), DE (Germany), FR (France), JP (Japan)"
            echo "              CA (Canada), AU (Australia), NZ (New Zealand), CN (China)"
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

                               echo "Please connect your CM4 to this computer in USB-boot mode."
                               read -p "Press Enter to run 'sudo rpiboot' and mount the eMMC..."
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


# Now that we know the hardware, acquire the appropriate image
acquire_armbian_image

if [ "$HARDWARE_MODEL" = "r3a" ]; then
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
admin_password=${ADMIN_PW}
auto_update=${AUTO_UPDATE}
EOF

        # Create Armbian firstrun preset file
        echo "Writing /root/.not_logged_in_yet..."
        sudo tee "$ROOT_MOUNT/root/.not_logged_in_yet" > /dev/null << EOF
# Network Settings
PRESET_NET_CHANGE_DEFAULTS="1"
## Ethernet
PRESET_NET_ETHERNET_ENABLED="1"
## WiFi
PRESET_NET_WIFI_ENABLED="0"
PRESET_NET_USE_STATIC="0"
# System
SET_LANG_BASED_ON_LOCATION="y"
PRESET_LOCALE="en_US.UTF-8"
PRESET_TIMEZONE="Etc/UTC"
# Root
PRESET_ROOT_PASSWORD="root"
PRESET_ROOT_KEY=""
# User
PRESET_USER_NAME="radio"
PRESET_USER_PASSWORD="$RADIO_PW"
PRESET_USER_KEY=""
PRESET_DEFAULT_REALNAME="radio"
PRESET_USER_SHELL="bash"
EOF

        # Write the provisioning script (sourced by armbian-firstlogin)
        echo "Writing /root/provisioning.sh..."
        sudo tee "$ROOT_MOUNT/root/provisioning.sh" > /dev/null << 'PROVISIONEOF'
#!/bin/bash
#
# Armbian Rock 3A Mesh Node Provisioning Script
# This script is sourced by armbian-firstlogin after user creation
#

# Don't use set -x when sourced - it will spam the console
# Log to file instead
PROVISION_LOG="/var/log/mesh-provision.log"

{
    echo "=== Rock 3A provisioning starting at $(date) ==="

    # Source the mesh configuration
    if [ -f /etc/mesh.conf ]; then
        source /etc/mesh.conf
    else
        echo "ERROR: /etc/mesh.conf not found!"
        # Don't exit - we're sourced, just return
        return 1 2>/dev/null || true
    fi

    # Set regulatory domain
    REG="${regulatory_domain:-US}"

    # Calculate unique hostname from MAC address
    HOST_MAC=$(ip a | grep -A1 "$(ip -o link show | awk -F': ' '/^[0-9]+: e/ {print $2; exit}')" \
       | awk '/ether/ {print $2}' | cut -d':' -f 5-6 | sed 's/://g')
    if [ -n "$HOST_MAC" ]; then
        hostnamectl set-hostname "radio-${HOST_MAC}"
        echo "Hostname set to radio-${HOST_MAC}"
    fi

    echo "Waiting for internet connectivity..."
    TIMEOUT=300
    ELAPSED=0
    while [ $ELAPSED -lt $TIMEOUT ]; do
        if ping -c 1 -W 2 8.8.8.8 > /dev/null 2>&1; then
            echo "Internet connectivity confirmed!"
            break
        fi
        echo "Waiting for internet... (${ELAPSED}s)"
        sleep 5
        ELAPSED=$((ELAPSED + 5))
    done

    if [ $ELAPSED -ge $TIMEOUT ]; then
        echo "ERROR: No internet after ${TIMEOUT}s"
        return 1 2>/dev/null || true
    fi

    # Set system time
    date -s "$(curl -sI google.com | grep -i ^Date: | cut -d' ' -f2-)" 2>/dev/null || true

    cd /root

    # Clear motd
    > /etc/motd

    # Update system packages FIRST (before extracting tarball to avoid kernel overwrites)
    echo "Updating system packages..."
    apt-get update > /dev/null 2>&1
    DEBIAN_FRONTEND=noninteractive apt-get upgrade -y > /dev/null 2>&1

    # Remove the question about the iperf daemon during apt install
    echo "iperf3 iperf3/start_daemon boolean true" | debconf-set-selections

    # Install required packages
    echo "Installing required packages..."
    DEBIAN_FRONTEND=noninteractive apt-get install -y ipcalc nmap lshw tcpdump net-tools nftables wireless-tools iperf3 \
        radvd bridge-utils firmware-mediatek libnss-mdns syncthing networkd-dispatcher \
        libgps-dev libcap-dev screen arping bc jq git libssl-dev hostapd dnsmasq \
        python3-protobuf unzip chrony build-essential systemd-resolved dhcping \
        libnl-3-dev libnl-genl-3-dev libnl-route-3-dev ebtables libdbus-1-dev gpsd

    # Download the install package
    echo -n "Downloading Rock 3A install package..."
    wget -q https://www.colorado-governor.com/manet/r3a-install.tar.gz -O /root/morse-pi-install.tar.gz || {
        echo "ERROR: Failed to download Rock 3A install package"
        return 1 2>/dev/null || true
    }
	echo "done"
    # Unpack the install tarball AFTER apt updates to avoid kernel overwrites
    echo "Extracting install package..."
    tar -zxf /root/morse-pi-install.tar.gz -C /

	# Unpack the r3a kernel
	cd /root
	dpkg -i *.deb

	# Add the morse firmware
	cd /root/morse-firmware
	cp firmware/mm8108*.bin /lib/firmware/morse/
	cp bcf/morsemicro/*.bin /lib/firmware/morse/
	cp bcf/azurewave/*.bin /lib/firmware/morse/
	cp bcf/netprisma/*.bin /lib/firmware/morse/
	cp bcf/quectel/*.bin /lib/firmware/morse/


    # Disable dnsmasq (we'll configure it ourselves)
    systemctl stop dnsmasq
    systemctl disable dnsmasq
    systemctl mask dnsmasq

    # Remove old avahi/yq if present
    apt-get remove -y avahi yq > /dev/null 2>&1 || true

    # Install Go yq
    wget -q https://github.com/mikefarah/yq/releases/latest/download/yq_linux_arm64 -O /usr/bin/yq
    chmod +x /usr/bin/yq

    # Disable automatic update timers
    systemctl disable apt-daily.timer > /dev/null 2>&1 || true
    systemctl disable apt-daily-upgrade.timer > /dev/null 2>&1 || true

	# Load batman-adv module
    echo "batman-adv" > /etc/modules-load.d/batman.conf

	echo "Creating bridge interfaces..."
	# Create the batman-adv interface, enslave it to br0
	cat << EOF > /etc/systemd/network/10-bat0.network
[Match]
Name=bat0

[Network]
Bridge=br0
LinkLocalAddressing=ipv6
IPv6Token=eui64
IPv6PrivacyExtensions=no

[Link]
MTUBytes=1500
EOF

	# The bridge br0 is the main interface for the mesh node
	cat << EOF > /etc/systemd/network/10-br0-bridge.netdev
[NetDev]
Name=br0
Kind=bridge

[Bridge]
MulticastSnooping=true
MulticastQuerier=true
EOF

	# br0 will get a slaac ipv6 address
	cat << EOF > /etc/systemd/network/20-br0-bridge.network
[Match]
Name=br0

[Network]
DHCP=no
LinkLocalAddressing=ipv6
IPv6AcceptRA=yes
MulticastDNS=yes

[Link]
RequiredForOnline=no
MTUBytes=1500
EOF

	echo "Bridge configuration complete"

    # Load modules at boot
    cat << EOF > /etc/modules-load.d/morse.conf
mac80211
cfg80211
crc7
morse
dot11ah
EOF

    # Morse driver options
    cat << EOF > /etc/modprobe.d/morse.conf
options morse country=${REG}
options morse enable_mcast_whitelist=0 enable_mcast_rate_control=1
EOF

    # Set regulatory domain
    iw reg set "$REG" 2>/dev/null || true

    # Make sure tools are executable
    chmod +x /usr/local/bin/* 2>/dev/null || true

    # Use known DNS
    rm -f /etc/resolv.conf
    echo "nameserver 1.1.1.1" > /etc/resolv.conf

    # Clean up
    rm -f /root/morse-pi-install.tar.gz

    # Remove this script so it doesn't run again
    rm -f /root/provisioning.sh

	# Disable predict names
	sed -i '/^extraargs=/ s/$/ net.ifnames=0/' /boot/armbianEnv.txt


	# Create the one-shot radio-setup service to run at next boot
	cat << EOF > /etc/systemd/system/radio-setup-run-once.service
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


	echo "=== Rock 3A provisioning complete at $(date) ==="
	reboot

} >> "$PROVISION_LOG" 2>&1

# Show user that provisioning completed
echo ""
echo "Mesh node provisioning complete. See $PROVISION_LOG for details."
echo "System will reboot in 10 seconds to apply changes..."
echo ""

# Schedule reboot after this script returns (don't reboot while sourced)
( sleep 10 && reboot ) &
PROVISIONEOF

        sudo chmod +x "$ROOT_MOUNT/root/provisioning.sh"

        # Unmount
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

else
        # Raspberry Pi path - use rpi-imager
        # Final confirmation before flashing
        confirm_flash "$TARGET_DEVICE"

        sudo rpi-imager --cli "$PI_OS_IMAGE_URL" "$TARGET_DEVICE" --first-run-script "$TEMP_SCRIPT_FILE"
fi
