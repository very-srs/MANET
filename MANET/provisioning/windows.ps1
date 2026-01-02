#Requires -RunAsAdministrator
<#
.SYNOPSIS
    A script to image new mesh radio nodes on Windows
.DESCRIPTION
    This PowerShell script provides the same functionality as the Linux bash version
    for flashing Raspberry Pi and Radxa Rock 3A devices with mesh network configurations.
#>

# --- Configuration ---
$TEMPLATE_FILE = "firstrun.sh.template"
$ARMBIAN_IMAGE = "Armbian-r3a-trixia-manet.img"
$CONFIG_DIR = ".mesh-configs"
$OS_IMAGE_URL = "https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2025-10-02/2025-10-01-raspios-trixie-arm64-lite.img.xz"

# Global variables
$Script:HARDWARE_MODEL = ""
$Script:TARGET_DEVICE = ""
$Script:EUD_CONNECTION = ""
$Script:LAN_AP_SSID = ""
$Script:LAN_AP_KEY = ""
$Script:MAX_EUDS_PER_NODE = 0
$Script:INSTALL_MEDIAMTX = ""
$Script:INSTALL_MUMBLE = ""
$Script:MESH_SSID = ""
$Script:MESH_SAE_KEY = ""
$Script:LAN_CIDR_BLOCK = ""
$Script:AUTO_CHANNEL = ""
$Script:RADIO_PW = ""
$Script:DD_PATH = $null
$Script:RPI_IMAGER_PATH = $null

# --- Helper Functions ---

function Calculate-Capacity {
    param(
        [string]$cidr,
        [int]$maxEuds
    )
    
    # Parse CIDR
    if ($cidr -notmatch '^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})/(\d{1,2})$') {
        return @{Total=0; Services=0; EudPool=0; MaxNodes=0}
    }
    
    $ip = $Matches[1]
    $prefix = [int]$Matches[2]
    
    # Calculate total IPs
    $hostBits = 32 - $prefix
    $totalIps = [math]::Pow(2, $hostBits) - 2  # Subtract network and broadcast
    
    $reservedServices = 5
    
    # Calculate max nodes: nodes * (1 + maxEuds) = available
    # nodes = available / (1 + maxEuds)
    if ($maxEuds -gt 0) {
        $maxNodes = [math]::Floor($totalIps / (1 + $maxEuds))
        $eudPool = $maxNodes * $maxEuds
    } else {
        $maxNodes = $totalIps - $reservedServices
        $eudPool = 0
    }
    
    return @{
        Total = $totalIps
        Services = $reservedServices
        EudPool = $eudPool
        MaxNodes = $maxNodes
    }
}

function Ask-LanCidr {
    param([int]$maxEuds = 0)
    
    $DEFAULT_CIDR = "10.30.2.0/24"
    
    while ($true) {
        $confirm = Read-Host "Use default LAN network $DEFAULT_CIDR? (Y/n)"
        if ([string]::IsNullOrWhiteSpace($confirm) -or $confirm -match "^[Yy]") {
            $Script:LAN_CIDR_BLOCK = $DEFAULT_CIDR
        } else {
            # Custom CIDR Loop
            while ($true) {
                $custom_cidr = Read-Host "Enter custom LAN CIDR block (e.g., 10.10.0.0/16)"
                
                # 1. Validate general format (IP/Prefix)
                if ($custom_cidr -notmatch '^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})/(\d{1,2})$') {
                    Write-Host "ERROR: Invalid format. Must be x.x.x.x/yy" -ForegroundColor Red
                    continue
                }

                $ip_part = $Matches[1]
                $prefix_part = [int]$Matches[2]

                # 2. Validate Prefix (16-26 is a reasonable range for a LAN)
                if ($prefix_part -lt 16 -or $prefix_part -gt 26) {
                    Write-Host "ERROR: Prefix /$prefix_part is invalid. Must be between /16 and /26." -ForegroundColor Red
                    continue
                }

                # 3. Validate IP as a private range
                $octets = $ip_part -split '\.'
                $o1 = [int]$octets[0]
                $o2 = [int]$octets[1]

                $is_private = $false
                if ($o1 -eq 10) {
                    $is_private = $true
                } elseif ($o1 -eq 172 -and $o2 -ge 16 -and $o2 -le 31) {
                    $is_private = $true
                } elseif ($o1 -eq 192 -and $o2 -eq 168) {
                    $is_private = $true
                }

                if (-not $is_private) {
                    Write-Host "ERROR: IP $ip_part is not in a private range." -ForegroundColor Red
                    Write-Host "Must be in 10.0.0.0/8, 172.16.0.0/12, or 192.168.0.0/16."
                    continue
                }

                # 4. Check if it's a valid network address
                if ($prefix_part -eq 24 -and [int]$octets[3] -ne 0) {
                    Write-Host "WARNING: For a /24 network, the IP should end in .0 (e.g., 192.168.1.0/24)." -ForegroundColor Yellow
                    Write-Host "Your entry $custom_cidr may cause routing issues."
                    $use_anyway = Read-Host "Use it anyway? (y/N)"
                    if ($use_anyway -notmatch "^[Yy]") {
                        continue
                    }
                }

                # All checks passed
                $Script:LAN_CIDR_BLOCK = $custom_cidr
                break
            }
        }
        
        # Show capacity calculation if EUDs are configured
        if ($maxEuds -gt 0) {
            Write-Host ""
            Write-Host "=== Network Capacity Analysis ==="
            $capacity = Calculate-Capacity -cidr $Script:LAN_CIDR_BLOCK -maxEuds $maxEuds
            
            Write-Host "Network: $($Script:LAN_CIDR_BLOCK)"
            Write-Host "  Total usable IPs: $($capacity.Total)"
            Write-Host "  Reserved for services: $($capacity.Services)"
            Write-Host "  Reserved for EUD pool: $($capacity.EudPool) ($maxEuds EUDs × $($capacity.MaxNodes) nodes)"
            Write-Host "  Available for mesh nodes: $($capacity.MaxNodes)"
            Write-Host "=================================="
            Write-Host ""
            
            if ($capacity.MaxNodes -lt 3) {
                Write-Host "WARNING: This configuration only supports $($capacity.MaxNodes) mesh nodes." -ForegroundColor Yellow
                Write-Host "Consider using a larger network or reducing max EUDs per node."
            }
            
            $accept = Read-Host "Accept this configuration? (Y/n)"
            if ([string]::IsNullOrWhiteSpace($accept) -or $accept -match "^[Yy]") {
                break
            }
            Write-Host "Let's reconfigure..."
        } else {
            Write-Host "Using network: $($Script:LAN_CIDR_BLOCK)"
            break
        }
    }
}

function Ask-Questions {
    Write-Host "--- Starting New Configuration ---"

    # 1. EUD Connection Type
    Write-Host "`nSelect EUD (client) connection type:"
    Write-Host "1. Wired"
    Write-Host "2. Wireless"
    Write-Host "3. Auto"
    
    do {
        $choice = Read-Host "Enter choice (1-3)"
        switch ($choice) {
            "1" { $Script:EUD_CONNECTION = "wired"; break }
            "2" { $Script:EUD_CONNECTION = "wireless"; break }
            "3" { $Script:EUD_CONNECTION = "auto"; break }
        }
    } while ($choice -notmatch "^[123]$")

    # --- If Wireless or Auto, ask for LAN AP configuration ---
    if ($Script:EUD_CONNECTION -eq "wireless" -or $Script:EUD_CONNECTION -eq "auto") {
        $Script:LAN_AP_SSID = Read-Host "Enter LAN AP SSID Name"
        
        while ($true) {
            $key = Read-Host "Enter LAN AP WPA2 Key (8-63 chars) [or press Enter to generate]"
            Write-Host ""
            
            if ([string]::IsNullOrWhiteSpace($key)) {
                # Generate random key
                $bytes = New-Object byte[] 33
                [Security.Cryptography.RNGCryptoServiceProvider]::Create().GetBytes($bytes)
                $Script:LAN_AP_KEY = [Convert]::ToBase64String($bytes)
                Write-Host "Generated LAN AP Key: $($Script:LAN_AP_KEY)"
                break
            }

            if ($key.Length -lt 8 -or $key.Length -gt 63) {
                Write-Host "ERROR: Key must be between 8 and 63 characters. You entered $($key.Length) characters." -ForegroundColor Red
            } else {
                $Script:LAN_AP_KEY = $key
                break
            }
        }
    } else {
        $Script:LAN_AP_SSID = ""
        $Script:LAN_AP_KEY = ""
        $Script:MAX_EUDS_PER_NODE = 0
    }

    # 2. Optional Software
    $response = Read-Host "Install MediaMTX Server? (Y/n)"
    $Script:INSTALL_MEDIAMTX = if ([string]::IsNullOrWhiteSpace($response) -or $response -match "^[Yy]") { "y" } else { "n" }

    $response = Read-Host "Install Mumble Server (murmur)? (Y/n)"
    $Script:INSTALL_MUMBLE = if ([string]::IsNullOrWhiteSpace($response) -or $response -match "^[Yy]") { "y" } else { "n" }

    # 3. LAN Configuration
    $Script:MESH_SSID = Read-Host "Enter LAN SSID Name"

    while ($true) {
        $key = Read-Host "Enter LAN SAE Key (WPA3 password, 8-63 chars) [or press Enter to generate]"
        Write-Host ""
        
        if ([string]::IsNullOrWhiteSpace($key)) {
            # Generate random key
            $bytes = New-Object byte[] 33
            [Security.Cryptography.RNGCryptoServiceProvider]::Create().GetBytes($bytes)
            $Script:MESH_SAE_KEY = [Convert]::ToBase64String($bytes)
            Write-Host "Generated SAE Key: $($Script:MESH_SAE_KEY)"
            break
        }

        if ($key.Length -lt 8 -or $key.Length -gt 63) {
            Write-Host "ERROR: Key must be between 8 and 63 characters. You entered $($key.Length) characters." -ForegroundColor Red
        } else {
            $Script:MESH_SAE_KEY = $key
            break
        }
    }

    Write-Host "The device will have a user called radio, for ssh access."
    $pw = Read-Host "Enter a password for the radio user [or press Enter to default to 'radio']"
    Write-Host ""
    
    if ([string]::IsNullOrWhiteSpace($pw)) {
        $Script:RADIO_PW = "radio"
        Write-Host "Setting default password"
    } else {
        $Script:RADIO_PW = $pw
    }
    Write-Host "Setting radio password to be $($Script:RADIO_PW)"

    # --- Ask for max EUDs before CIDR selection ---
    if ($Script:EUD_CONNECTION -eq "wireless" -or $Script:EUD_CONNECTION -eq "auto") {
        while ($true) {
            $input = Read-Host "Maximum EUDs per node's AP (1-20)"
            if ($input -match '^\d+$' -and [int]$input -ge 1 -and [int]$input -le 20) {
                $Script:MAX_EUDS_PER_NODE = [int]$input
                break
            } else {
                Write-Host "ERROR: Please enter a number between 1 and 20." -ForegroundColor Red
            }
        }
    }

    # --- CIDR selection with capacity planning ---
    Ask-LanCidr -maxEuds $Script:MAX_EUDS_PER_NODE

    # --- Auto Channel Selection (skip if wireless or auto) ---
    if ($Script:EUD_CONNECTION -eq "wireless" -or $Script:EUD_CONNECTION -eq "auto") {
        $Script:AUTO_CHANNEL = "n"
        Write-Host "Automatic WiFi Channel Selection disabled (not compatible with Wireless/Auto EUD mode)"
    } else {
        $response = Read-Host "Use Automatic WiFi Channel Selection? (Y/n)"
        $Script:AUTO_CHANNEL = if ([string]::IsNullOrWhiteSpace($response) -or $response -match "^[Yy]") { "y" } else { "n" }
    }

    Write-Host "----------------------------------"
}

function Save-Config {
    Write-Host ""
    $save_choice = Read-Host "Save this configuration? (Y/n)"
    
    if ([string]::IsNullOrWhiteSpace($save_choice) -or $save_choice -match "^[Yy]") {
        $config_name = Read-Host "Enter a name for this config"
        
        if ([string]::IsNullOrWhiteSpace($config_name)) {
            Write-Host "Invalid name, skipping save."
            return
        }

        $CONFIG_FILE = Join-Path $CONFIG_DIR "$config_name.conf"
        
        $config_content = @"
# Pi Imager Config: $config_name
EUD_CONNECTION="$($Script:EUD_CONNECTION)"
LAN_AP_SSID="$($Script:LAN_AP_SSID)"
LAN_AP_KEY="$($Script:LAN_AP_KEY)"
MAX_EUDS_PER_NODE="$($Script:MAX_EUDS_PER_NODE)"
INSTALL_MEDIAMTX="$($Script:INSTALL_MEDIAMTX)"
INSTALL_MUMBLE="$($Script:INSTALL_MUMBLE)"
MESH_SSID="$($Script:MESH_SSID)"
MESH_SAE_KEY="$($Script:MESH_SAE_KEY)"
LAN_CIDR_BLOCK="$($Script:LAN_CIDR_BLOCK)"
AUTO_CHANNEL="$($Script:AUTO_CHANNEL)"
RADIO_PW="$($Script:RADIO_PW)"
"@
        
        $config_content | Out-File -FilePath $CONFIG_FILE -Encoding ASCII
        Write-Host "Configuration saved to $CONFIG_FILE"
    }
}

function Load-Config {
    param([string]$ConfigFile)
    
    Write-Host "Loading config from $ConfigFile..."
    
    Get-Content $ConfigFile | ForEach-Object {
        if ($_ -match '^([^=]+)="([^"]*)"') {
            $varName = $Matches[1]
            $varValue = $Matches[2]
            
            switch ($varName) {
                "EUD_CONNECTION" { $Script:EUD_CONNECTION = $varValue }
                "LAN_AP_SSID" { $Script:LAN_AP_SSID = $varValue }
                "LAN_AP_KEY" { $Script:LAN_AP_KEY = $varValue }
                "MAX_EUDS_PER_NODE" { $Script:MAX_EUDS_PER_NODE = [int]$varValue }
                "INSTALL_MEDIAMTX" { $Script:INSTALL_MEDIAMTX = $varValue }
                "INSTALL_MUMBLE" { $Script:INSTALL_MUMBLE = $varValue }
                "MESH_SSID" { $Script:MESH_SSID = $varValue }
                "MESH_SAE_KEY" { $Script:MESH_SAE_KEY = $varValue }
                "LAN_CIDR_BLOCK" { $Script:LAN_CIDR_BLOCK = $varValue }
                "AUTO_CHANNEL" { $Script:AUTO_CHANNEL = $varValue }
                "RADIO_PW" { $Script:RADIO_PW = $varValue }
            }
        }
    }

    Write-Host "--- Loaded Configuration ---"
    Write-Host "  EUD Connection: $($Script:EUD_CONNECTION)"
    if ($Script:EUD_CONNECTION -eq "wireless" -or $Script:EUD_CONNECTION -eq "auto") {
        Write-Host "  LAN AP SSID: $($Script:LAN_AP_SSID)"
        Write-Host "  LAN AP Key: $($Script:LAN_AP_KEY)"
        Write-Host "  Max EUDs per node: $($Script:MAX_EUDS_PER_NODE)"
    }
    Write-Host "  Install MediaMTX: $($Script:INSTALL_MEDIAMTX)"
    Write-Host "  Install Mumble: $($Script:INSTALL_MUMBLE)"
    Write-Host "  LAN SSID: $($Script:MESH_SSID)"
    Write-Host "  LAN SAE Key: $($Script:MESH_SAE_KEY)"
    Write-Host "  LAN CIDR Block: $($Script:LAN_CIDR_BLOCK)"
    Write-Host "  Auto Channel: $($Script:AUTO_CHANNEL)"
    Write-Host "  User password: $($Script:RADIO_PW)"
    Write-Host "----------------------------"
}

function Select-HardwareAndTargetDevice {
    Write-Host ""
    Write-Host "--- 1. Select Hardware ---"
    
    $SKIP_DEV_SELECT = $false

    Write-Host "Select Raspberry Pi Model:"
    Write-Host "1. Radxa Rock 3A"
    Write-Host "2. Raspberry Pi 5"
    Write-Host "3. Raspberry Pi 4B"
    Write-Host "4. Compute Module 4 (CM4)"
    
    do {
        $hw_choice = Read-Host "Enter choice (1-4)"
        
        switch ($hw_choice) {
            "1" {
                $Script:HARDWARE_MODEL = "r3a"
                break
            }
            "2" {
                $Script:HARDWARE_MODEL = "rpi5"
                break
            }
            "3" {
                $Script:HARDWARE_MODEL = "rpi4"
                break
            }
            "4" {
                Write-Host "Compute Module 4 selected."
                Write-Host "IMPORTANT: CM4 flashing on Windows requires:" -ForegroundColor Yellow
                Write-Host "  1. Boot switch set to USB-boot mode"
                Write-Host "  2. rpiboot.exe installed and run (CM4 should appear as USB mass storage)"
                Write-Host "  3. Device should show up as a removable disk in Windows"
                Write-Host ""
                Read-Host "Press Enter once CM4 is connected and appears as a disk in Windows"
                
                $Script:HARDWARE_MODEL = "rpi4"
                $SKIP_DEV_SELECT = $false  # Still need to select which disk is the CM4
                break
            }
        }
    } while ($hw_choice -notmatch "^[1-4]$")

    Write-Host ""
    Write-Host "--- 2. Select Target Device ---"
    
    # Get all removable disks
    $disks = Get-Disk | Where-Object { 
        $_.BusType -in @('USB', 'SD') -or 
        ($_.Model -like '*Compute Module*') -or
        ($_.FriendlyName -like '*RPi*')
    }
    
    if ($disks.Count -eq 0) {
        Write-Host "ERROR: No suitable target devices found." -ForegroundColor Red
        Write-Host "Please ensure your SD card reader, USB drive, or CM4 is connected."
        exit 1
    }

    Write-Host "Available devices:"
    $i = 1
    $diskMap = @{}
    foreach ($disk in $disks) {
        $sizeGB = [math]::Round($disk.Size / 1GB, 2)
        Write-Host "$i. Disk $($disk.Number): $($disk.FriendlyName) - ${sizeGB}GB"
        $diskMap[$i] = $disk
        $i++
    }
    
    do {
        $choice = Read-Host "Enter device number (1-$($disks.Count))"
        $choiceNum = 0
        if ([int]::TryParse($choice, [ref]$choiceNum) -and $diskMap.ContainsKey($choiceNum)) {
            $selectedDisk = $diskMap[$choiceNum]
            $Script:TARGET_DEVICE = $selectedDisk.Number
            Write-Host "Selected: Disk $($Script:TARGET_DEVICE) - $($selectedDisk.FriendlyName)"
            break
        } else {
            Write-Host "Invalid selection." -ForegroundColor Red
        }
    } while ($true)
}

# --- Main Script ---

# Check for Administrator privileges
$currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "ERROR: This script must be run as Administrator!" -ForegroundColor Red
    Write-Host "Right-click PowerShell and select 'Run as Administrator'"
    exit 1
}

# --- 1. Basic Checks ---
if (-not (Test-Path $TEMPLATE_FILE)) {
    Write-Host "ERROR: Template file '$TEMPLATE_FILE' not found." -ForegroundColor Red
    exit 1
}

# Ensure config directory exists
if (-not (Test-Path $CONFIG_DIR)) {
    New-Item -ItemType Directory -Path $CONFIG_DIR | Out-Null
}

# --- 2. Load or Create Config ---
$configFiles = Get-ChildItem -Path $CONFIG_DIR -Filter "*.conf" -ErrorAction SilentlyContinue

if ($configFiles.Count -gt 0) {
    Write-Host "Found $($configFiles.Count) saved configuration(s)."
    Write-Host "What would you like to do?"
    Write-Host "1. Load a saved configuration"
    Write-Host "2. Create a new configuration"
    
    do {
        $choice = Read-Host "Enter choice (1-2)"
        
        if ($choice -eq "1") {
            Write-Host "`nPlease select a configuration to load:"
            $i = 1
            $configMap = @{}
            foreach ($file in $configFiles) {
                $name = $file.BaseName
                Write-Host "$i. $name"
                $configMap[$i] = $file.FullName
                $i++
            }
            Write-Host "$i. Cancel"
            
            do {
                $configChoice = Read-Host "Enter number (1-$i)"
                $configNum = 0
                if ([int]::TryParse($configChoice, [ref]$configNum)) {
                    if ($configNum -eq $i) {
                        Write-Host "Aborting."
                        exit 0
                    } elseif ($configMap.ContainsKey($configNum)) {
                        Load-Config -ConfigFile $configMap[$configNum]
                        break
                    }
                }
                Write-Host "Invalid selection." -ForegroundColor Red
            } while ($true)
            break
        } elseif ($choice -eq "2") {
            Ask-Questions
            Save-Config
            break
        }
    } while ($choice -notmatch "^[12]$")
} else {
    Write-Host "No saved configs found. Starting new setup."
    Ask-Questions
    Save-Config
}

# --- 3. Get Image & Device ---
Write-Host ""
Write-Host "--- Image & Device ---"

# Select hardware and device
Select-HardwareAndTargetDevice

# --- Now check for hardware-specific dependencies ---
if ($Script:HARDWARE_MODEL -eq "r3a") {
    # Check for Armbian image
    if (-not (Test-Path $ARMBIAN_IMAGE)) {
        Write-Host "ERROR: Armbian image file '$ARMBIAN_IMAGE' not found." -ForegroundColor Red
        exit 1
    }
    Write-Host "Using image: $ARMBIAN_IMAGE"
    
    # Check for dd in script directory
    $scriptDir = $PSScriptRoot
    if (-not $scriptDir) {
        $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
    }
    $ddPath = Join-Path $scriptDir "ddrelease64.exe"
    
    if (-not (Test-Path $ddPath)) {
        Write-Host "WARNING: ddrelease64.exe not found in script directory." -ForegroundColor Yellow
        Write-Host "Will use slower PowerShell fallback method for flashing."
        Write-Host "For faster flashing, download dd for Windows from: http://www.chrysocome.net/dd"
        Write-Host "and place ddrelease64.exe in: $scriptDir"
        Write-Host ""
        $Script:DD_PATH = $null
    } else {
        Write-Host "Found dd at: $ddPath"
        $Script:DD_PATH = $ddPath
    }
} else {
    # Check for rpi-imager
    $rpiImagerPath = "C:\Program Files\Raspberry Pi Ltd\Imager\rpi-imager.exe"
    
    if (-not (Test-Path $rpiImagerPath)) {
        Write-Host "ERROR: rpi-imager.exe not found at expected location:" -ForegroundColor Red
        Write-Host "  $rpiImagerPath"
        Write-Host "Please install Raspberry Pi Imager from: https://www.raspberrypi.com/software/"
        exit 1
    }
    
    Write-Host "Found rpi-imager at: $rpiImagerPath"
    $Script:RPI_IMAGER_PATH = $rpiImagerPath
    Write-Host "Using image: $OS_IMAGE_URL"
    Write-Host "rpi-imager will download/cache this image if needed."
}

Write-Host ""
$confirm = Read-Host "WARNING: This will ERASE ALL DATA on Disk $($Script:TARGET_DEVICE). Are you sure? (yes/no)"
if ($confirm -ne "yes") {
    Write-Host "Aborting."
    exit 0
}

# Create firstrun script for Raspberry Pi devices
if ($Script:HARDWARE_MODEL -ne "r3a") {
    Write-Host "Generating temporary firstrun script..."
    
    $tempScriptFile = [System.IO.Path]::GetTempFileName()
    
    $templateContent = Get-Content $TEMPLATE_FILE -Raw
    $templateContent = $templateContent -replace '__HARDWARE_MODEL__', $Script:HARDWARE_MODEL
    $templateContent = $templateContent -replace '__EUD_CONNECTION__', $Script:EUD_CONNECTION
    $templateContent = $templateContent -replace '__LAN_AP_SSID__', $Script:LAN_AP_SSID
    $templateContent = $templateContent -replace '__LAN_AP_KEY__', $Script:LAN_AP_KEY
    $templateContent = $templateContent -replace '__MAX_EUDS_PER_NODE__', $Script:MAX_EUDS_PER_NODE
    $templateContent = $templateContent -replace '__INSTALL_MEDIAMTX__', $Script:INSTALL_MEDIAMTX
    $templateContent = $templateContent -replace '__INSTALL_MUMBLE__', $Script:INSTALL_MUMBLE
    $templateContent = $templateContent -replace '__MESH_SSID__', $Script:MESH_SSID
    $templateContent = $templateContent -replace '__MESH_SAE_KEY__', $Script:MESH_SAE_KEY
    $templateContent = $templateContent -replace '__LAN_CIDR_BLOCK__', $Script:LAN_CIDR_BLOCK
    $templateContent = $templateContent -replace '__AUTO_CHANNEL__', $Script:AUTO_CHANNEL
    $templateContent = $templateContent -replace '__RADIO_PW__', $Script:RADIO_PW
    
    $templateContent | Out-File -FilePath $tempScriptFile -Encoding ASCII
}

Write-Host "Starting hardware imaging..."

if ($Script:HARDWARE_MODEL -eq "r3a") {
    # Armbian image modification and flashing
    Write-Host "Creating temporary working copy of Armbian image..."
    $tempImage = [System.IO.Path]::GetTempFileName()
    $tempImage = $tempImage -replace '\.tmp$', '.img'
    
    try {
        # Copy image to temp location
        Copy-Item -Path $ARMBIAN_IMAGE -Destination $tempImage -Force
        Write-Host "Temporary image created at: $tempImage"
        
        # Mount the image
        Write-Host "Mounting image for configuration injection..."
        $mountResult = Mount-DiskImage -ImagePath $tempImage -PassThru
        
        if (-not $mountResult) {
            throw "Failed to mount disk image"
        }
        
        # Wait for mount to complete
        Start-Sleep -Seconds 2
        
        # Get the disk number of the mounted image
        $imageDisk = Get-DiskImage -ImagePath $tempImage | Get-Disk
        
        # Get the boot partition (usually the first partition, FAT32)
        $bootPartition = Get-Partition -DiskNumber $imageDisk.Number | Where-Object {
            $_.Type -eq 'Basic' -or $_.FileSystem -eq 'FAT32'
        } | Select-Object -First 1
        
        if (-not $bootPartition) {
            throw "Could not find boot partition in image"
        }
        
        # Ensure partition has a drive letter
        if (-not $bootPartition.DriveLetter) {
            Write-Host "Assigning drive letter to boot partition..."
            $driveLetter = (Get-Volume | Where-Object { $_.DriveLetter -and $_.DriveType -eq 'Fixed' } | 
                           ForEach-Object { $_.DriveLetter } | Sort-Object | Select-Object -Last 1)
            $nextLetter = [char]([int][char]$driveLetter + 1)
            $bootPartition | Set-Partition -NewDriveLetter $nextLetter
            $bootPartition = Get-Partition -DiskNumber $imageDisk.Number | Where-Object { $_.DriveLetter -eq $nextLetter }
        }
        
        $bootDrive = "$($bootPartition.DriveLetter):\"
        Write-Host "Boot partition mounted at: $bootDrive"
        
        # Write configuration file
        Write-Host "Writing mesh configuration to boot partition..."
        $configPath = Join-Path $bootDrive "mesh-config"
        
        $configContent = @"
HARDWARE_MODEL=$($Script:HARDWARE_MODEL)
EUD_CONNECTION=$($Script:EUD_CONNECTION)
LAN_AP_SSID=$($Script:LAN_AP_SSID)
LAN_AP_KEY=$($Script:LAN_AP_KEY)
MAX_EUDS_PER_NODE=$($Script:MAX_EUDS_PER_NODE)
INSTALL_MEDIAMTX=$($Script:INSTALL_MEDIAMTX)
INSTALL_MUMBLE=$($Script:INSTALL_MUMBLE)
MESH_SSID=$($Script:MESH_SSID)
MESH_SAE_KEY=$($Script:MESH_SAE_KEY)
LAN_CIDR_BLOCK=$($Script:LAN_CIDR_BLOCK)
AUTO_CHANNEL=$($Script:AUTO_CHANNEL)
RADIO_PW=$($Script:RADIO_PW)
"@
        
        $configContent | Out-File -FilePath $configPath -Encoding ASCII -NoNewline
        Write-Host "Configuration written successfully."
        
        # Dismount the image
        Write-Host "Finalizing modified image..."
        Start-Sleep -Seconds 1
        Dismount-DiskImage -ImagePath $tempImage
        Start-Sleep -Seconds 2
        
        # Now write the configured image to target device
        Write-Host "Writing configured image to Disk $($Script:TARGET_DEVICE)..."
        Write-Host "This may take several minutes..."
        
        $targetPath = "\\.\PhysicalDrive$($Script:TARGET_DEVICE)"
        
        # Use dd if we found it earlier
        if ($Script:DD_PATH) {
            # Use dd
            $ddArgs = "if=`"$tempImage`"", "of=$targetPath", "bs=4M", "--progress"
            Write-Host "Using dd: $($Script:DD_PATH) $($ddArgs -join ' ')"
            $ddProcess = Start-Process -FilePath $Script:DD_PATH -ArgumentList $ddArgs -Wait -PassThru -NoNewWindow
            
            if ($ddProcess.ExitCode -ne 0) {
                throw "dd failed with exit code $($ddProcess.ExitCode)"
            }
        } else {
            # Fallback to PowerShell method (slower but works)
            Write-Host "Using PowerShell method (this will be slower)..."
            
            # Open source and destination
            $sourceStream = [System.IO.File]::OpenRead($tempImage)
            $destStream = [System.IO.File]::OpenWrite($targetPath)
            
            try {
                $bufferSize = 4 * 1024 * 1024  # 4MB
                $buffer = New-Object byte[] $bufferSize
                $totalBytes = $sourceStream.Length
                $bytesWritten = 0
                
                while ($true) {
                    $bytesRead = $sourceStream.Read($buffer, 0, $buffer.Length)
                    if ($bytesRead -eq 0) { break }
                    
                    $destStream.Write($buffer, 0, $bytesRead)
                    $bytesWritten += $bytesRead
                    
                    $percentComplete = [math]::Round(($bytesWritten / $totalBytes) * 100, 1)
                    Write-Progress -Activity "Writing image to disk" -Status "$percentComplete% Complete" -PercentComplete $percentComplete
                }
                
                $destStream.Flush()
                Write-Progress -Activity "Writing image to disk" -Completed
            } finally {
                $sourceStream.Close()
                $destStream.Close()
            }
        }
        
        Write-Host "Image written successfully." -ForegroundColor Green
        
    } catch {
        Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
        
        # Try to cleanup on error
        try {
            Dismount-DiskImage -ImagePath $tempImage -ErrorAction SilentlyContinue
        } catch {}
        
        if (Test-Path $tempImage) {
            Remove-Item $tempImage -Force -ErrorAction SilentlyContinue
        }
        
        exit 1
        
    } finally {
        # Clean up temp image
        if (Test-Path $tempImage) {
            Write-Host "Cleaning up temporary image..."
            Remove-Item $tempImage -Force -ErrorAction SilentlyContinue
        }
    }
    
} else {
    # Use rpi-imager for Raspberry Pi devices
    # Convert disk number to device path format rpi-imager expects
    $devicePath = "\\.\PhysicalDrive$($Script:TARGET_DEVICE)"
    
    Write-Host "Launching rpi-imager..."
    Write-Host "Device: $devicePath"
    Write-Host "Script: $tempScriptFile"
    
    $process = Start-Process -FilePath $Script:RPI_IMAGER_PATH `
        -ArgumentList "--cli", "`"$OS_IMAGE_URL`"", "`"$devicePath`"", "--first-run-script", "`"$tempScriptFile`"" `
        -Wait -PassThru -NoNewWindow
    
    if ($process.ExitCode -ne 0) {
        Write-Host "ERROR: rpi-imager failed to flash the image." -ForegroundColor Red
        Remove-Item $tempScriptFile -ErrorAction SilentlyContinue
        exit 1
    }
    
    Remove-Item $tempScriptFile -ErrorAction SilentlyContinue
}

Write-Host "`nDone! Flashing complete. The device will configure itself on first boot." -ForegroundColor Green
