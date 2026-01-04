#!/bin/bash
#
#  A script to image new mesh radio nodes
set -e

# --- Configuration ---
TEMPLATE_FILE="firstrun.sh.template"
TEMP_SCRIPT_FILE=$(mktemp)
ARMBIAN_IMAGE="Armbian-r3a-trixia-manet.img"
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

	# CIDR selection with capacity planning
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
# Pi Imager Config: $config_name
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
	echo "----------------------------"
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


# --- Main Script ---

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
if [ "$HARDWARE_MODEL" = "r3a" ]; then
	echo "Using image: $ARMBIAN_IMAGE"
else
	echo "Using image: $PI_OS_IMAGE_URL"
	echo "rpi-imager will download/cache this image if needed."
fi

# This function will set HARDWARE_MODEL and TARGET_DEVICE
select_hardware_and_target_device

echo ""
read -p "WARNING: This will ERASE ALL DATA on $TARGET_DEVICE. Are you sure? (yes/no): " CONFIRM
if [ "$CONFIRM" != "yes" ]; then
	echo "Aborting."
	rm "$TEMP_SCRIPT_FILE"
	exit 0
fi


# If we are imaging a raspberry pi, create the fristrun script by combining the
# user variables with the template file.  Rpi-imager will inject this into the boot
# partition and it will be run at first boot
if [ "$HARDWARE_MODEL" != "r3a" ]; then
    echo "Generating temporary firstrun script..."
    
    tempScriptFile=$(mktemp)
    
    # Escape special characters in variables for sed
    escape_sed() {
        printf '%s\n' "$1" | sed -e 's/[\/&]/\\&/g' -e 's/|/\\|/g'
    }
    
    HARDWARE_MODEL_ESC=$(escape_sed "$HARDWARE_MODEL")
    EUD_CONNECTION_ESC=$(escape_sed "$EUD_CONNECTION")
    LAN_AP_SSID_ESC=$(escape_sed "$LAN_AP_SSID")
    LAN_AP_KEY_ESC=$(escape_sed "$LAN_AP_KEY")
    MAX_EUDS_PER_NODE_ESC=$(escape_sed "$MAX_EUDS_PER_NODE")
    INSTALL_MEDIAMTX_ESC=$(escape_sed "$INSTALL_MEDIAMTX")
    INSTALL_MUMBLE_ESC=$(escape_sed "$INSTALL_MUMBLE")
    REGULATORY_DOMAIN_ESC=$(escape_sed "$REGULATORY_DOMAIN")
    MESH_SSID_ESC=$(escape_sed "$MESH_SSID")
    MESH_SAE_KEY_ESC=$(escape_sed "$MESH_SAE_KEY")
    LAN_CIDR_BLOCK_ESC=$(escape_sed "$LAN_CIDR_BLOCK")
    AUTO_CHANNEL_ESC=$(escape_sed "$AUTO_CHANNEL")
    RADIO_PW_ESC=$(escape_sed "$RADIO_PW")
    
    sed -e "s|__HARDWARE_MODEL__|${HARDWARE_MODEL_ESC}|g" \
        -e "s|__EUD_CONNECTION__|${EUD_CONNECTION_ESC}|g" \
        -e "s|__LAN_AP_SSID__|${LAN_AP_SSID_ESC}|g" \
        -e "s|__LAN_AP_KEY__|${LAN_AP_KEY_ESC}|g" \
        -e "s|__MAX_EUDS_PER_NODE__|${MAX_EUDS_PER_NODE_ESC}|g" \
        -e "s|__INSTALL_MEDIAMTX__|${INSTALL_MEDIAMTX_ESC}|g" \
        -e "s|__INSTALL_MUMBLE__|${INSTALL_MUMBLE_ESC}|g" \
        -e "s|__REGULATORY_DOMAIN__|${REGULATORY_DOMAIN_ESC}|g" \
        -e "s|__MESH_SSID__|${MESH_SSID_ESC}|g" \
        -e "s|__MESH_SAE_KEY__|${MESH_SAE_KEY_ESC}|g" \
        -e "s|__LAN_CIDR_BLOCK__|${LAN_CIDR_BLOCK_ESC}|g" \
        -e "s|__AUTO_CHANNEL__|${AUTO_CHANNEL_ESC}|g" \
        -e "s|__RADIO_PW__|${RADIO_PW_ESC}|g" \
        "$TEMPLATE_FILE" > "$tempScriptFile"

    chmod +x "$tempScriptFile"
    TEMP_SCRIPT_FILE="$tempScriptFile"
fi

echo "Starting hardware imaging. This may require your password to write to the device."
# For an arbian image, we must inject the setup config into the disk imge
if [ "$HARDWARE_MODEL" = "r3a" ]; then
	# Loop mount the armbian disk image 
	LOOP_NUM=`ls /dev/loop* | grep loop[0-9][0-9] | sort | cut -d'p' -f2 | tail -n 1`
	(( LOOP_NUM++));
	echo "losetup -P /dev/loop${LOOP_NUM} $ARMBIAN_IMAGE"
	sudo losetup -P /dev/loop${LOOP_NUM} $ARMBIAN_IMAGE
	BOOT_MOUNT="/tmp/armbian-boot"
	sudo mkdir -p "$BOOT_MOUNT"
	echo "mount /dev/loop${LOOP_NUM}p1 \"$BOOT_MOUNT\" "
	sudo mount /dev/loop${LOOP_NUM}p1 "$BOOT_MOUNT"

	# Write configuration file
	sudo tee "$BOOT_MOUNT/mesh-config" > /dev/null << EOF
HARDWARE_MODEL=${HARDWARE_MODEL}
EUD_CONNECTION=${EUD_CONNECTION}
LAN_AP_SSID=${LAN_AP_SSID}
LAN_AP_KEY=${LAN_AP_KEY}
MAX_EUDS_PER_NODE=${MAX_EUDS_PER_NODE}
INSTALL_MEDIAMTX=${INSTALL_MEDIAMTX}
INSTALL_MUMBLE=${INSTALL_MUMBLE}
MESH_SSID=${MESH_SSID}
MESH_SAE_KEY=${MESH_SAE_KEY}
LAN_CIDR_BLOCK=${LAN_CIDR_BLOCK}
AUTO_CHANNEL=${AUTO_CHANNEL}
RADIO_PW=${RADIO_PW}
REGULATORY_DOMAIN=${REGULATORY_DOMAIN}
EOF
	# Unmount
	sudo sync
	sudo umount "$BOOT_MOUNT"
	sudo rmdir "$BOOT_MOUNT"
	sudo dd if=\"$ARMBIAN_IMAGE\" of=\"$TARGET_DEVICE\" bs=4M status=progress conv=fsync

else
sudo rpi-imager --cli "$PI_OS_IMAGE_URL" "$TARGET_DEVICE" --first-run-script "$TEMP_SCRIPT_FILE"
fi

rm "$TEMP_SCRIPT_FILE" 2>/dev/null

echo "Done! Flashing complete. The Pi will configure itself on first boot."
