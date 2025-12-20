#Requires -Version 5.1
#Requires -RunAsAdministrator

<#
.SYNOPSIS
    Raspberry Pi mesh node provisioning script for Windows
.DESCRIPTION
    This is a wrapper around rpi-imager that configures mesh nodes.
    Configurations can be saved and reused for imaging multiple nodes.
#>

[CmdletBinding()]
param()

# --- Configuration ---
$TEMPLATE_FILE = "firstrun.sh.template"
$CONFIG_DIR = ".pi-configs"
$OS_IMAGE_URL = "https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2025-10-02/2025-10-01-raspios-trixie-arm64-lite.img.xz"

# --- Global Variables ---
$script:Config = @{
    EUD_CONNECTION = ""
    INSTALL_MEDIAMTX = ""
    INSTALL_MUMBLE = ""
    LAN_SSID = ""
    LAN_SAE_KEY = ""
    LAN_CIDR_BLOCK = ""
    AUTO_CHANNEL = ""
    RADIO_PW = ""
}
$script:HARDWARE_MODEL = ""
$script:TARGET_DEVICE = ""

# --- Helper Functions ---

function Write-ColorOutput {
    param(
        [string]$Message,
        [string]$Color = "White"
    )
    Write-Host $Message -ForegroundColor $Color
}

function Test-Administrator {
    $currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    return $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-RandomBase64String {
    param([int]$Length = 28)
    
    # Try OpenSSL first (from Git or standalone)
    $opensslPaths = @(
        "openssl.exe",
        "C:\Program Files\Git\usr\bin\openssl.exe",
        "C:\Program Files (x86)\Git\usr\bin\openssl.exe"
    )
    
    foreach ($path in $opensslPaths) {
        if (Get-Command $path -ErrorAction SilentlyContinue) {
            $result = & $path rand -base64 $Length 2>$null
            if ($LASTEXITCODE -eq 0) {
                return $result.Trim()
            }
        }
    }
    
    # Fallback to PowerShell crypto
    Write-ColorOutput "Using PowerShell crypto (OpenSSL not found)" "Yellow"
    $bytes = New-Object byte[] $Length
    $rng = [System.Security.Cryptography.RNGCryptoServiceProvider]::Create()
    $rng.GetBytes($bytes)
    return [Convert]::ToBase64String($bytes)
}

function Test-PrivateIPRange {
    param([string]$IP)
    
    $octets = $IP -split '\.'
    if ($octets.Count -ne 4) { return $false }
    
    $o1 = [int]$octets[0]
    $o2 = [int]$octets[1]
    
    # 10.0.0.0/8
    if ($o1 -eq 10) { return $true }
    
    # 172.16.0.0/12
    if ($o1 -eq 172 -and $o2 -ge 16 -and $o2 -le 31) { return $true }
    
    # 192.168.0.0/16
    if ($o1 -eq 192 -and $o2 -eq 168) { return $true }
    
    return $false
}

function Get-LANCIDRBlock {
    $DEFAULT_CIDR = "10.30.2.0/24"
    
    $confirm = Read-Host "Use default LAN network $DEFAULT_CIDR? (Y/n)"
    if ([string]::IsNullOrWhiteSpace($confirm) -or $confirm -eq 'y' -or $confirm -eq 'Y') {
        $script:Config.LAN_CIDR_BLOCK = $DEFAULT_CIDR
        Write-ColorOutput "Using default network: $DEFAULT_CIDR" "Green"
        return
    }
    
    while ($true) {
        $customCidr = Read-Host "Enter custom LAN CIDR block (e.g., 10.10.0.0/16)"
        
        # Validate format
        if ($customCidr -notmatch '^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})/(\d{1,2})$') {
            Write-ColorOutput "ERROR: Invalid format. Must be x.x.x.x/yy" "Red"
            continue
        }
        
        $ipPart = $Matches[1]
        $prefixPart = [int]$Matches[2]
        
        # Validate prefix
        if ($prefixPart -lt 16 -or $prefixPart -gt 26) {
            Write-ColorOutput "ERROR: Prefix /$prefixPart is invalid. Must be between /16 and /26." "Red"
            continue
        }
        
        # Validate private range
        if (-not (Test-PrivateIPRange $ipPart)) {
            Write-ColorOutput "ERROR: IP $ipPart is not in a private range." "Red"
            Write-ColorOutput "Must be in 10.0.0.0/8, 172.16.0.0/12, or 192.168.0.0/16." "Red"
            continue
        }
        
        # Check network address alignment
        $octets = $ipPart -split '\.'
        if ($prefixPart -eq 24 -and [int]$octets[3] -ne 0) {
            Write-ColorOutput "WARNING: For a /24 network, the IP should end in .0 (e.g., 192.168.1.0/24)." "Yellow"
            Write-ColorOutput "Your entry $customCidr may cause routing issues." "Yellow"
            $useAnyway = Read-Host "Use it anyway? (y/N)"
            if ($useAnyway -ne 'y' -and $useAnyway -ne 'Y') {
                continue
            }
        }
        
        $script:Config.LAN_CIDR_BLOCK = $customCidr
        Write-ColorOutput "Using custom network: $customCidr" "Green"
        break
    }
}

function Get-BootDisk {
    $systemDrive = $env:SystemDrive
    $systemPartition = Get-Partition | Where-Object { $_.DriveLetter -eq $systemDrive[0] }
    if ($systemPartition) {
        return $systemPartition.DiskNumber
    }
    return $null
}

function Get-UserQuestions {
    Write-ColorOutput "`n--- Starting New Configuration ---" "Cyan"
    
    # EUD Connection Type
    Write-Host "`nSelect EUD (client) connection type:"
    Write-Host "1. Wired"
    Write-Host "2. Wireless"
    do {
        $choice = Read-Host "Enter choice (1-2)"
    } while ($choice -notmatch '^[12]$')
    $script:Config.EUD_CONNECTION = @("", "wired", "wireless")[[int]$choice]
    
    # Optional Software
    $response = Read-Host "Install MediaMTX Server? (Y/n)"
    $script:Config.INSTALL_MEDIAMTX = if ([string]::IsNullOrWhiteSpace($response) -or $response -match '^[Yy]') { "y" } else { "n" }
    
    $response = Read-Host "Install Mumble Server (murmur)? (Y/n)"
    $script:Config.INSTALL_MUMBLE = if ([string]::IsNullOrWhiteSpace($response) -or $response -match '^[Yy]') { "y" } else { "n" }
    
    # LAN Configuration
    $script:Config.LAN_SSID = Read-Host "Enter LAN SSID Name"
    
    # LAN SAE Key
    while ($true) {
        $saeKey = Read-Host "Enter LAN SAE Key (WPA3 password, 8-63 chars) [or press Enter to generate]" -AsSecureString
        $saeKeyPlain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($saeKey))
        
        if ([string]::IsNullOrWhiteSpace($saeKeyPlain)) {
            $saeKeyPlain = Get-RandomBase64String
            Write-ColorOutput "Generated SAE Key: $saeKeyPlain" "Green"
            $script:Config.LAN_SAE_KEY = $saeKeyPlain
            break
        }
        
        $keyLen = $saeKeyPlain.Length
        if ($keyLen -lt 8 -or $keyLen -gt 63) {
            Write-ColorOutput "ERROR: Key must be between 8 and 63 characters. You entered $keyLen characters." "Red"
        } else {
            $script:Config.LAN_SAE_KEY = $saeKeyPlain
            break
        }
    }
    
    # Radio user password
    Write-Host "`nThe device will have a user called radio, for SSH access."
    $radioPw = Read-Host "Enter a password for the radio user [or press Enter to default to 'radio']" -AsSecureString
    $radioPwPlain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($radioPw))
    
    if ([string]::IsNullOrWhiteSpace($radioPwPlain)) {
        $script:Config.RADIO_PW = "radio"
        Write-ColorOutput "Setting default password" "Yellow"
    } else {
        $script:Config.RADIO_PW = $radioPwPlain
    }
    Write-ColorOutput "Setting radio password to be $($script:Config.RADIO_PW)" "Green"
    
    # LAN CIDR
    Get-LANCIDRBlock
    
    # Auto Channel Selection
    $response = Read-Host "Use Automatic WiFi Channel Selection? (Y/n)"
    $script:Config.AUTO_CHANNEL = if ([string]::IsNullOrWhiteSpace($response) -or $response -match '^[Yy]') { "y" } else { "n" }
    
    Write-ColorOutput "----------------------------------" "Cyan"
}

function Save-Configuration {
    Write-Host ""
    $saveChoice = Read-Host "Save this configuration? (Y/n)"
    if (-not ([string]::IsNullOrWhiteSpace($saveChoice) -or $saveChoice -match '^[Yy]')) {
        return
    }
    
    $configName = Read-Host "Enter a name for this config (e.g., media-server)"
    if ([string]::IsNullOrWhiteSpace($configName)) {
        Write-ColorOutput "Invalid name, skipping save." "Yellow"
        return
    }
    
    $configFile = Join-Path $CONFIG_DIR "$configName.conf"
    
    $configContent = @"
# Pi Imager Config: $configName
EUD_CONNECTION=$($script:Config.EUD_CONNECTION)
INSTALL_MEDIAMTX=$($script:Config.INSTALL_MEDIAMTX)
INSTALL_MUMBLE=$($script:Config.INSTALL_MUMBLE)
LAN_SSID=$($script:Config.LAN_SSID)
LAN_SAE_KEY=$($script:Config.LAN_SAE_KEY)
LAN_CIDR_BLOCK=$($script:Config.LAN_CIDR_BLOCK)
AUTO_CHANNEL=$($script:Config.AUTO_CHANNEL)
RADIO_PW=$($script:Config.RADIO_PW)
"@
    
    Set-Content -Path $configFile -Value $configContent
    Write-ColorOutput "Configuration saved to $configFile" "Green"
}

function Load-Configuration {
    param([string]$ConfigFile)
    
    Write-ColorOutput "Loading config from $ConfigFile..." "Cyan"
    
    $content = Get-Content $ConfigFile
    foreach ($line in $content) {
        if ($line -match '^([^#][^=]+)=(.*)$') {
            $key = $Matches[1].Trim()
            $value = $Matches[2].Trim()
            $script:Config[$key] = $value
        }
    }
    
    Write-ColorOutput "`n--- Loaded Configuration ---" "Cyan"
    Write-Host "  EUD Connection: $($script:Config.EUD_CONNECTION)"
    Write-Host "  Install MediaMTX: $($script:Config.INSTALL_MEDIAMTX)"
    Write-Host "  Install Mumble: $($script:Config.INSTALL_MUMBLE)"
    Write-Host "  LAN SSID: $($script:Config.LAN_SSID)"
    Write-Host "  LAN SAE Key: $($script:Config.LAN_SAE_KEY)"
    Write-Host "  LAN CIDR Block: $($script:Config.LAN_CIDR_BLOCK)"
    Write-Host "  Auto Channel: $($script:Config.AUTO_CHANNEL)"
    Write-Host "  User password: $($script:Config.RADIO_PW)"
    Write-ColorOutput "----------------------------`n" "Cyan"
}

function Select-HardwareAndDevice {
    Write-ColorOutput "`n--- 1. Select Hardware ---" "Cyan"
    
    $skipDeviceSelect = $false
    
    Write-Host "Select Raspberry Pi Model:"
    Write-Host "1. Raspberry Pi 5"
    Write-Host "2. Raspberry Pi 4B"
    Write-Host "3. Compute Module 4 (CM4)"
    
    do {
        $hwChoice = Read-Host "Enter choice (1-3)"
    } while ($hwChoice -notmatch '^[123]$')
    
    switch ($hwChoice) {
        "1" {
            $script:HARDWARE_MODEL = "rpi5"
        }
        "2" {
            $script:HARDWARE_MODEL = "rpi4"
        }
        "3" {
            Write-ColorOutput "Compute Module 4 selected." "Green"
            
            # Check for rpiboot
            $rpibootPath = $null
            $possiblePaths = @(
                "rpiboot.exe",
                "C:\Program Files\Raspberry Pi\rpiboot.exe",
                "C:\Program Files (x86)\Raspberry Pi\rpiboot.exe"
            )
            
            foreach ($path in $possiblePaths) {
                if (Get-Command $path -ErrorAction SilentlyContinue) {
                    $rpibootPath = $path
                    break
                }
            }
            
            if (-not $rpibootPath) {
                Write-ColorOutput "ERROR: 'rpiboot.exe' not found." "Red"
                Write-ColorOutput "Please install it from https://github.com/raspberrypi/usbboot/releases" "Red"
                exit 1
            }
            
            # Detect disks before rpiboot
            Write-ColorOutput "Detecting disks before rpiboot..." "Yellow"
            $disksBefore = Get-Disk | Select-Object -ExpandProperty Number
            
            Write-Host "`nPlease connect your CM4 to this computer in USB-boot mode."
            Read-Host "Press Enter to run rpiboot and mount the eMMC"
            
            Write-ColorOutput "Running rpiboot..." "Yellow"
            & $rpibootPath
            
            Write-ColorOutput "Waiting 4 seconds for device to settle..." "Yellow"
            Start-Sleep -Seconds 4
            
            # Detect disks after rpiboot
            Write-ColorOutput "Detecting disks after rpiboot..." "Yellow"
            $disksAfter = Get-Disk | Select-Object -ExpandProperty Number
            
            $newDisks = $disksAfter | Where-Object { $_ -notin $disksBefore }
            
            if ($newDisks.Count -eq 0) {
                Write-ColorOutput "ERROR: No new disk detected after rpiboot." "Red"
                Write-ColorOutput "Please check connections and try again." "Red"
                exit 1
            }
            
            $newDiskNum = $newDisks[0]
            $diskInfo = Get-Disk -Number $newDiskNum
            $diskSize = [math]::Round($diskInfo.Size / 1GB, 2)
            
            $script:TARGET_DEVICE = "\\.\PhysicalDrive$newDiskNum"
            Write-ColorOutput "Detected new device: $($script:TARGET_DEVICE) (Disk $newDiskNum, $diskSize GB)" "Green"
            
            $script:HARDWARE_MODEL = "rpi4"
            $skipDeviceSelect = $true
        }
    }
    
    Write-ColorOutput "`n--- 2. Select Target Device ---" "Cyan"
    
    if ($skipDeviceSelect) {
        Write-ColorOutput "Using auto-detected CM4 device: $($script:TARGET_DEVICE)" "Green"
        return
    }
    
    # Get boot disk to exclude
    $bootDisk = Get-BootDisk
    Write-ColorOutput "(Excluding boot disk: Disk $bootDisk)" "Yellow"
    
    # Get removable and non-boot disks
    $availableDisks = Get-Disk | Where-Object {
        $_.Number -ne $bootDisk -and
        ($_.BusType -eq 'USB' -or $_.BusType -eq 'SD' -or ($_.Size -lt 128GB))
    } | Sort-Object Number
    
    if ($availableDisks.Count -eq 0) {
        Write-ColorOutput "ERROR: No suitable target devices found." "Red"
        Write-ColorOutput "Please make sure your SD card reader or USB drive is plugged in." "Red"
        exit 1
    }
    
    Write-Host "`nAvailable devices:"
    $i = 1
    $deviceList = @()
    foreach ($disk in $availableDisks) {
        $sizeGB = [math]::Round($disk.Size / 1GB, 2)
        $volumes = Get-Partition -DiskNumber $disk.Number -ErrorAction SilentlyContinue |
            Get-Volume -ErrorAction SilentlyContinue |
            Where-Object { $_.DriveLetter } |
            Select-Object -ExpandProperty DriveLetter
        
        $driveLetters = if ($volumes) { ($volumes | ForEach-Object { "$_`:" }) -join ", " } else { "No drive letters" }
        
        $displayText = "Disk $($disk.Number): $sizeGB GB [$driveLetters] - $($disk.FriendlyName)"
        Write-Host "$i. $displayText"
        $deviceList += @{
            Number = $disk.Number
            Display = $displayText
        }
        $i++
    }
    Write-Host "$i. Quit"
    
    do {
        $choice = Read-Host "`nEnter number (or $i to quit)"
        $choiceNum = [int]$choice
    } while ($choiceNum -lt 1 -or $choiceNum -gt $i)
    
    if ($choiceNum -eq $i) {
        Write-ColorOutput "Aborting." "Yellow"
        exit 0
    }
    
    $selectedDisk = $deviceList[$choiceNum - 1].Number
    $script:TARGET_DEVICE = "\\.\PhysicalDrive$selectedDisk"
    Write-ColorOutput "Selected device: $($script:TARGET_DEVICE) (Disk $selectedDisk)" "Green"
}

# --- Main Script ---

# Check admin
if (-not (Test-Administrator)) {
    Write-ColorOutput "ERROR: This script requires administrator privileges." "Red"
    Write-ColorOutput "Please run PowerShell as Administrator and try again." "Red"
    exit 1
}

Write-ColorOutput "Raspberry Pi Mesh Node Provisioning Script" "Cyan"
Write-ColorOutput "==========================================" "Cyan"

# Check dependencies
if (-not (Test-Path $TEMPLATE_FILE)) {
    Write-ColorOutput "ERROR: Template file '$TEMPLATE_FILE' not found." "Red"
    exit 1
}

if (-not (Get-Command rpi-imager -ErrorAction SilentlyContinue)) {
    Write-ColorOutput "ERROR: 'rpi-imager' command not found." "Red"
    Write-ColorOutput "Please install it from https://www.raspberrypi.com/software/" "Red"
    exit 1
}

# Ensure config directory exists
if (-not (Test-Path $CONFIG_DIR)) {
    New-Item -ItemType Directory -Path $CONFIG_DIR | Out-Null
}

# Load or create config
$configFiles = Get-ChildItem -Path $CONFIG_DIR -Filter "*.conf" -ErrorAction SilentlyContinue

if ($configFiles.Count -gt 0) {
    Write-Host "`nFound $($configFiles.Count) saved configuration(s)."
    Write-Host "What would you like to do?"
    Write-Host "1. Load a saved configuration"
    Write-Host "2. Create a new configuration"
    
    do {
        $choice = Read-Host "Enter choice (1-2)"
    } while ($choice -notmatch '^[12]$')
    
    if ($choice -eq "1") {
        Write-Host "`nPlease select a configuration to load:"
        $i = 1
        foreach ($file in $configFiles) {
            Write-Host "$i. $($file.BaseName)"
            $i++
        }
        Write-Host "$i. Cancel"
        
        do {
            $configChoice = Read-Host "Enter number"
            $configChoiceNum = [int]$configChoice
        } while ($configChoiceNum -lt 1 -or $configChoiceNum -gt $i)
        
        if ($configChoiceNum -eq $i) {
            Write-ColorOutput "Aborting." "Yellow"
            exit 0
        }
        
        Load-Configuration $configFiles[$configChoiceNum - 1].FullName
    } else {
        Get-UserQuestions
        Save-Configuration
    }
} else {
    Write-ColorOutput "No saved configs found. Starting new setup." "Yellow"
    Get-UserQuestions
    Save-Configuration
}

# Image & Device selection
Write-ColorOutput "`n--- Image & Device ---" "Cyan"
Write-Host "Using image: $OS_IMAGE_URL"
Write-Host "rpi-imager will download/cache this image if needed."

Select-HardwareAndDevice

# Confirm
Write-Host ""
Write-ColorOutput "WARNING: This will ERASE ALL DATA on $($script:TARGET_DEVICE)" "Red"
$confirm = Read-Host "Are you sure? (yes/no)"
if ($confirm -ne "yes") {
    Write-ColorOutput "Aborting." "Yellow"
    exit 0
}

# Create temporary firstrun script
Write-ColorOutput "`nGenerating temporary firstrun script..." "Yellow"
$tempScriptFile = [System.IO.Path]::GetTempFileName()

$templateContent = Get-Content $TEMPLATE_FILE -Raw
$templateContent = $templateContent `
    -replace '__HARDWARE_MODEL__', $script:HARDWARE_MODEL `
    -replace '__EUD_CONNECTION__', $script:Config.EUD_CONNECTION `
    -replace '__INSTALL_MEDIAMTX__', $script:Config.INSTALL_MEDIAMTX `
    -replace '__INSTALL_MUMBLE__', $script:Config.INSTALL_MUMBLE `
    -replace '__LAN_SSID__', $script:Config.LAN_SSID `
    -replace '__LAN_SAE_KEY__', $script:Config.LAN_SAE_KEY `
    -replace '__LAN_CIDR_BLOCK__', $script:Config.LAN_CIDR_BLOCK `
    -replace '__AUTO_CHANNEL__', $script:Config.AUTO_CHANNEL `
    -replace '__RADIO_PW__', $script:Config.RADIO_PW

Set-Content -Path $tempScriptFile -Value $templateContent

# Run rpi-imager
Write-ColorOutput "`nStarting rpi-imager..." "Yellow"
Write-ColorOutput "This will require administrator privileges to write to the device." "Yellow"

& rpi-imager --cli $OS_IMAGE_URL $script:TARGET_DEVICE --first-run-script $tempScriptFile

if ($LASTEXITCODE -ne 0) {
    Write-ColorOutput "`nERROR: rpi-imager failed with exit code $LASTEXITCODE" "Red"
    Remove-Item $tempScriptFile -Force
    exit 1
}

# Cleanup
Remove-Item $tempScriptFile -Force

Write-ColorOutput "`nDone! Flashing complete." "Green"
Write-ColorOutput "The Pi will configure itself on first boot." "Green"
