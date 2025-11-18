#!/bin/bash
set -e

# --- Configuration ---
TEMPLATE_FILE="firstrun.sh.template"
TEMP_SCRIPT_FILE=$(mktemp)
CONFIG_DIR=".pi-configs"
# Hardcode the OS image URL. rpi-imager will download and cache this.
OS_IMAGE_URL="https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2025-10-02/2025-10-01-raspios-trixie-arm64-lite.img.xz"

# --- Helper Functions ---

# Function to ask for and validate the LAN CIDR block
ask_lan_cidr() {
    local DEFAULT_CIDR="10.30.2.0/24"
    local custom_cidr
    local confirm_default
    local ip_part
    local prefix_part
    
    read -p "Use default LAN network $DEFAULT_CIDR? (Y/n): " confirm_default
    confirm_default=${confirm_default:-y}

    if [ "$confirm_default" = "y" ] || [ "$confirm_default" = "Y" ]; then
        LAN_CIDR_BLOCK="$DEFAULT_CIDR"
        echo "Using default network: $LAN_CIDR_BLOCK"
        return
    fi

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

        # 2. Validate Prefix (16-30 is a reasonable range for a LAN)
        if (( prefix_part < 16 || prefix_part > 30 )); then
            echo "ERROR: Prefix /${prefix_part} is invalid. Must be between /16 and /30."
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
        # This is complex, for now we just check the format.
        # A simple check: the last octet for a /24 should be 0.
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
        echo "Using custom network: $LAN_CIDR_BLOCK"
        break
    done
}


# --- *** NEW FUNCTION: Robustly find the boot disk *** ---
# This finds the top-level disk (e.g., nvme0n1) that hosts the / filesystem
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
    physical_disk=$(lsblk -n -s -o NAME,TYPE "$root_dev" | awk '$2 == "disk" {print $1; exit}')

    if [ -z "$physical_disk" ]; then
        echo "ERROR: Could not trace root device to physical disk." >&2
        return 1
    fi

    echo "$physical_disk"
}

# Function to ask all setup questions
ask_questions() {
    echo "--- Starting New Configuration ---"

    # --- 1. Hardware & Role ---
    # ** HARDWARE SELECTION MOVED OUT OF THIS FUNCTION **

    echo "Select EUD (client) connection type:"
    select eud_choice in "Wired" "Wireless"; do
        case $eud_choice in
            "Wired" ) EUD_CONNECTION="wired"; break;;
            "Wireless" ) EUD_CONNECTION="wireless"; break;;
        esac
    done

    # --- 2. Optional Software ---
    read -p "Install MediaMTX Server? (Y/n): " INSTALL_MEDIAMTX
    INSTALL_MEDIAMTX=${INSTALL_MEDIAMTX:-y}
    if [ "$INSTALL_MEDIAMTX" = "y" ] || [ "$INSTALL_MEDIAMTX" = "Y" ]; then INSTALL_MEDIAMTX="y"; else INSTALL_MEDIAMTX="n"; fi

    read -p "Install Mumble Server (murmur)? (Y/n): " INSTALL_MUMBLE
    INSTALL_MUMBLE=${INSTALL_MUMBLE:-y}
    if [ "$INSTALL_MUMBLE" = "y" ] || [ "$INSTALL_MUMBLE" = "Y" ]; then INSTALL_MUMBLE="y"; else INSTALL_MUMBLE="n"; fi

    # --- 3. LAN Configuration ---
    read -p "Enter LAN SSID Name: " LAN_SSID
    
    while true; do
        read -s -p "Enter LAN SAE Key (WPA3 password, 8-63 chars) [or press Enter to generate]: " LAN_SAE_KEY
        echo
        if [ -z "$LAN_SAE_KEY" ]; then
            LAN_SAE_KEY=$(openssl rand -base64 24)
            echo "Generated SAE Key: $LAN_SAE_KEY"
            break
        fi
        
        key_len=${#LAN_SAE_KEY}
        if (( key_len < 8 || key_len > 63 )); then
            echo "ERROR: Key must be between 8 and 63 characters. You entered $key_len characters."
        else
            break # Valid key
        fi
    done
    
    # Call the new CIDR function
    ask_lan_cidr
    
    read -p "Use Automatic Channel Selection? (Y/n): " AUTO_CHANNEL
    AUTO_CHANNEL=${AUTO_CHANNEL:-y}
    if [ "$AUTO_CHANNEL" = "y" ] || [ "$AUTO_CHANNEL" = "Y" ]; then AUTO_CHANNEL="y"; else AUTO_CHANNEL="n"; fi
    
    echo "----------------------------------"
}

# Function to save the current variables to a config file
save_config() {
    echo ""
    read -p "Save this configuration? (Y/n): " save_choice
    save_choice=${save_choice:-y}
    if [ "$save_choice" = "y" ] || [ "$save_choice" = "Y" ]; then
        read -p "Enter a name for this config (e.g., media-server): " config_name
        if [ -z "$config_name" ]; then
            echo "Invalid name, skipping save."
            return
        fi
        
        local CONFIG_FILE="$CONFIG_DIR/$config_name.conf"
        
        # Use a heredoc to write all variables to the file
        # ** HARDWARE_MODEL is no longer saved. It's selected at runtime. **
        cat << EOF > "$CONFIG_FILE"
# Pi Imager Config: $config_name
EUD_CONNECTION="$EUD_CONNECTION"
INSTALL_MEDIAMTX="$INSTALL_MEDIAMTX"
INSTALL_MUMBLE="$INSTALL_MUMBLE"
LAN_SSID="$LAN_SSID"
LAN_SAE_KEY="$LAN_SAE_KEY"
LAN_CIDR_BLOCK="$LAN_CIDR_BLOCK"
AUTO_CHANNEL="$AUTO_CHANNEL"
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
    # ** HARDWARE_MODEL is no longer loaded. It's selected at runtime. **
    echo "  EUD Connection: $EUD_CONNECTION"
    echo "  Install MediaMTX: $INSTALL_MEDIAMTX"
    echo "  Install Mumble: $INSTALL_MUMBLE"
    echo "  LAN SSID: $LAN_SSID"
    echo "  LAN SAE Key: $LAN_SAE_KEY"
    echo "  LAN CIDR Block: $LAN_CIDR_BLOCK"
    echo "  Auto Channel: $AUTO_CHANNEL"
    echo "----------------------------"
}

# --- *** NEW FUNCTION: Select Hardware and Target Device *** ---
# This function is now called AFTER loading or creating a config.
# It returns the chosen TARGET_DEVICE path in a global variable.
select_hardware_and_target_device() {
    echo ""
    echo "--- 1. Select Hardware ---"
    
    # This variable will be set to 1 by the CM4 logic to skip the device menu
    local SKIP_DEV_SELECT=0
    
    echo "Select Raspberry Pi Model:"
    select hw_choice in "Raspberry Pi 5" "Raspberry Pi 4B" "Compute Module 4 (CM4)"; do
        case $hw_choice in
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
                echo "'rpiboot' finished. Waiting 5s for device to settle..."
                sleep 5

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

        # --- *** FIX: Removed -l flag from lsblk *** ---
        # Use lsblk in "pairs" mode (-P) and eval the output
        while IFS= read -r line; do
            # Reset variables for each line
            local NAME=""
            local MOUNTPOINT=""
            local SIZE=""
            local TYPE=""
            # Safely evaluate the key-value pairs from lsblk
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
if [ ! -f "$TEMPLATE_FILE" ]; then
    echo "ERROR: Template file '$TEMPLATE_FILE' not found."
    exit 1
fi
if ! command -v rpi-imager &> /dev/null; then
    echo "ERROR: 'rpi-imager' command not found. Please install it."
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
# --- *** NEW: Add findmnt dependency check *** ---
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
echo "Using image: $OS_IMAGE_URL"
echo "rpi-imager will download/cache this image if needed."

# --- *** REFACTOR: Call the new hardware/device function *** ---
# This function will set HARDWARE_MODEL and TARGET_DEVICE
select_hardware_and_target_device

# --- *** END REFACTOR *** ---

echo ""
read -p "WARNING: This will ERASE ALL DATA on $TARGET_DEVICE. Are you sure? (yes/no): " CONFIRM
if [ "$CONFIRM" != "yes" ]; then
    echo "Aborting."
    rm "$TEMP_SCRIPT_FILE"
    exit 0
fi

# --- 4. Process Variables ---
# (No processing needed for this simplified version)

# --- 5. Create Temporary Script ---
echo "Generating temporary firstrun script..."
sed -e "s|__HARDWARE_MODEL__|${HARDWARE_MODEL}|g" \
    -e "s|__EUD_CONNECTION__|${EUD_CONNECTION}|g" \
    -e "s|__INSTALL_MEDIAMTX__|${INSTALL_MEDIAMTX}|g" \
    -e "s|__INSTALL_MUMBLE__|${INSTALL_MUMBLE}|g" \
    -e "s|__LAN_SSID__|${LAN_SSID}|g" \
    -e "s|__LAN_SAE_KEY__|${LAN_SAE_KEY}|g" \
    -e "s|__LAN_CIDR_BLOCK__|${LAN_CIDR_BLOCK}|g" \
    -e "s|__AUTO_CHANNEL__|${AUTO_CHANNEL}|g" \
    "$TEMPLATE_FILE" > "$TEMP_SCRIPT_FILE"

chmod +x "$TEMP_SCRIPT_FILE"

# --- 6. Run rpi-imager ---
echo "Starting rpi-imager. This may require your password to write to the device."
sudo rpi-imager --cli "$OS_IMAGE_URL" "$TARGET_DEVICE" --first-run-script "$TEMP_SCRIPT_FILE"

# --- 7. Cleanup ---
rm "$TEMP_SCRIPT_FILE"
echo "Done! Flashing complete. The Pi will configure itself on first boot."
