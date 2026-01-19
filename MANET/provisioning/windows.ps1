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

$ARMBIAN_IMAGE_URL = "https://fi.mirror.armbian.de/dl/rock-3a/archive/Armbian_25.11.1_Rock-3a_trixie_current_6.12.58_minimal.img.xz"
$ARMBIAN_IMAGE_FILENAME = "Armbian_25.11.1_Rock-3a_trixie_current_6.12.58_minimal.img"
$Script:ARMBIAN_IMAGE = ""  # Will be set by Get-ArmbianImage function

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
$Script:REGULATORY_DOMAIN = ""

# --- Helper Functions ---

function Test-RegulatoryDomain {
    param([string]$domain)
    
    $validDomains = @(
        "US", "CA", "GB", "DE", "FR", "IT", "ES", "NL", "BE", "AT", "CH", "SE", "NO", "DK", "FI",
        "PL", "CZ", "HU", "GR", "PT", "IE", "RO", "BG", "HR", "SI", "SK", "LT", "LV", "EE", "CY",
        "MT", "LU", "AU", "NZ", "JP", "KR", "TW", "SG", "MY", "TH", "PH", "ID", "VN", "IN", "CN",
        "BR", "AR", "MX", "CL", "CO", "PE", "ZA", "IL", "AE", "SA", "RU", "UA", "TR", "EG", "MA"
    )
    
    $domain = $domain.ToUpper()
    
    if ($validDomains -contains $domain) {
        return $domain
    }
    
    return $null
}

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

function Test-Ext4Driver {
    <#
    .SYNOPSIS
        Check if an ext4 driver is installed on Windows
    .DESCRIPTION
        Checks for common ext4/ext2 drivers that allow Windows to read/write Linux filesystems.
        Returns $true if a compatible driver is found.
    #>
    
    # Check for Ext2Fsd service (most common free ext4 driver)
    $ext2fsdService = Get-Service -Name "Ext2Fsd" -ErrorAction SilentlyContinue
    if ($ext2fsdService) {
        Write-Host "Found Ext2Fsd driver (Status: $($ext2fsdService.Status))"
        if ($ext2fsdService.Status -ne "Running") {
            Write-Host "Starting Ext2Fsd service..."
            try {
                Start-Service -Name "Ext2Fsd" -ErrorAction Stop
                Start-Sleep -Seconds 2
                return $true
            } catch {
                Write-Host "WARNING: Could not start Ext2Fsd service: $_" -ForegroundColor Yellow
                return $false
            }
        }
        return $true
    }
    
    # Check for Paragon Linux File Systems driver
    $paragonService = Get-Service -Name "فارागون*" -ErrorAction SilentlyContinue
    if (-not $paragonService) {
        $paragonService = Get-Service | Where-Object { $_.DisplayName -like "*Paragon*Linux*" } | Select-Object -First 1
    }
    if ($paragonService) {
        Write-Host "Found Paragon Linux File Systems driver"
        return $true
    }
    
    # Check if ext2fsd.sys exists in drivers folder
    $ext2fsdDriver = Join-Path $env:SystemRoot "System32\drivers\ext2fsd.sys"
    if (Test-Path $ext2fsdDriver) {
        Write-Host "Found ext2fsd.sys driver file, but service not running"
        Write-Host "You may need to run Ext2 Volume Manager to start the driver"
        return $false
    }
    
    return $false
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
            Write-Host "  Reserved for EUD pool: $($capacity.EudPool) ($maxEuds EUDs x $($capacity.MaxNodes) nodes)"
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

    # 3. Mesh Configuration
    $Script:MESH_SSID = Read-Host "Enter MESH SSID Name"

    while ($true) {
        $key = Read-Host "Enter MESH SAE Key (WPA3 password, 8-63 chars) [or press Enter to generate]"
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

    # WiFi Regulatory Domain
    while ($true) {
        $domain = Read-Host "Enter WiFi regulatory domain (2-letter country code, default: US)"
        if ([string]::IsNullOrWhiteSpace($domain)) {
            $domain = "US"
        }
        
        $validated = Test-RegulatoryDomain -domain $domain
        if ($validated) {
            $Script:REGULATORY_DOMAIN = $validated
            Write-Host "Using regulatory domain: $($Script:REGULATORY_DOMAIN)"
            break
        } else {
            Write-Host "ERROR: Invalid regulatory domain code: $domain" -ForegroundColor Red
            Write-Host "Please enter a valid 2-letter ISO country code (e.g., US, GB, DE, FR, JP)"
            Write-Host "Common codes: US (United States), GB (UK), DE (Germany), FR (France), JP (Japan)"
            Write-Host "              CA (Canada), AU (Australia), NZ (New Zealand), CN (China)"
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
REGULATORY_DOMAIN="$($Script:REGULATORY_DOMAIN)"
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
                "REGULATORY_DOMAIN" { $Script:REGULATORY_DOMAIN = $varValue }
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
    Write-Host "  Mesh SSID: $($Script:MESH_SSID)"
    Write-Host "  Mesh SAE Key: $($Script:MESH_SAE_KEY)"
    Write-Host "  Regulatory Domain: $($Script:REGULATORY_DOMAIN)"
    Write-Host "  LAN CIDR Block: $($Script:LAN_CIDR_BLOCK)"
    Write-Host "  Auto Channel: $($Script:AUTO_CHANNEL)"
    Write-Host "  User password: $($Script:RADIO_PW)"
    Write-Host "----------------------------"
}


# Function to acquire Armbian image for Rock 3A
# Sets $Script:ARMBIAN_IMAGE to the path of a usable .img file
function Get-ArmbianImage {
    Write-Host ""
    Write-Host "--- Armbian Image Setup for Rock 3A ---"
    
    $scriptDir = $PSScriptRoot
    if (-not $scriptDir) {
        $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
    }
    if (-not $scriptDir) {
        $scriptDir = Get-Location
    }
    
    $localImage = Join-Path $scriptDir $ARMBIAN_IMAGE_FILENAME
    $localCompressed = Join-Path $scriptDir "${ARMBIAN_IMAGE_FILENAME}.xz"
    
    # Check if default image exists locally (uncompressed)
    if (Test-Path $localImage) {
        Write-Host "Found local Armbian image: $localImage"
        $Script:ARMBIAN_IMAGE = $localImage
        return $true
    }
    
    # Check for compressed version
    if (Test-Path $localCompressed) {
        Write-Host "Found compressed Armbian image: $localCompressed"
        $result = Expand-XzFile -CompressedPath $localCompressed -OutputPath $localImage
        if ($result) {
            $Script:ARMBIAN_IMAGE = $localImage
            return $true
        } else {
            return $false
        }
    }
    
    Write-Host "Armbian image not found locally."
    Write-Host ""
    Write-Host "Options:"
    Write-Host "  1. Download from Armbian mirror (recommended)"
    Write-Host "     URL: $ARMBIAN_IMAGE_URL"
    Write-Host "  2. Provide path to an existing Armbian Trixie image"
    Write-Host ""
    
    while ($true) {
        $imgChoice = Read-Host "Select option (1 or 2)"
        
        switch ($imgChoice) {
            "1" {
                return Get-ArmbianImageDownload -OutputDir $scriptDir
            }
            "2" {
                return Get-ArmbianImageCustomPath
            }
            default {
                Write-Host "Invalid selection. Please enter 1 or 2." -ForegroundColor Red
            }
        }
    }
}

# Function to download Armbian image from mirror
function Get-ArmbianImageDownload {
    param([string]$OutputDir)
    
    $compressedFile = Join-Path $OutputDir "${ARMBIAN_IMAGE_FILENAME}.xz"
    $outputFile = Join-Path $OutputDir $ARMBIAN_IMAGE_FILENAME
    
    Write-Host ""
    Write-Host "Downloading Armbian image..."
    Write-Host "Source: $ARMBIAN_IMAGE_URL"
    Write-Host "This may take several minutes depending on your connection speed."
    Write-Host ""
    
    try {
        # Use BitsTransfer for better progress display, fallback to Invoke-WebRequest
        if (Get-Command Start-BitsTransfer -ErrorAction SilentlyContinue) {
            Start-BitsTransfer -Source $ARMBIAN_IMAGE_URL -Destination $compressedFile -DisplayName "Downloading Armbian Image"
        } else {
            # Show progress with Invoke-WebRequest
            $ProgressPreference = 'Continue'
            Invoke-WebRequest -Uri $ARMBIAN_IMAGE_URL -OutFile $compressedFile -UseBasicParsing
        }
    } catch {
        Write-Host "ERROR: Download failed: $_" -ForegroundColor Red
        if (Test-Path $compressedFile) {
            Remove-Item $compressedFile -Force
        }
        return $false
    }
    
    if (-not (Test-Path $compressedFile)) {
        Write-Host "ERROR: Download failed - file not created." -ForegroundColor Red
        return $false
    }
    
    Write-Host ""
    Write-Host "Download complete. Decompressing..."
    
    $result = Expand-XzFile -CompressedPath $compressedFile -OutputPath $outputFile
    if ($result) {
        $Script:ARMBIAN_IMAGE = $outputFile
        Write-Host "Image ready: $($Script:ARMBIAN_IMAGE)"
        return $true
    }
    return $false
}

# Function to decompress .xz files
function Expand-XzFile {
    param(
        [string]$CompressedPath,
        [string]$OutputPath
    )
    
    Write-Host "Decompressing $CompressedPath..."
    Write-Host "(This may take a few minutes)"
    
    # Try using 7-Zip if available
    $7zPaths = @(
        "C:\Program Files\7-Zip\7z.exe",
        "C:\Program Files (x86)\7-Zip\7z.exe",
        (Join-Path $env:ProgramFiles "7-Zip\7z.exe")
    )
    
    $7zPath = $null
    foreach ($path in $7zPaths) {
        if (Test-Path $path) {
            $7zPath = $path
            break
        }
    }
    
    if ($7zPath) {
        try {
            $outputDir = Split-Path -Parent $OutputPath
            & $7zPath x $CompressedPath -o"$outputDir" -y | Out-Null
            if (Test-Path $OutputPath) {
                Write-Host "Decompression complete."
                return $true
            }
        } catch {
            Write-Host "7-Zip decompression failed: $_" -ForegroundColor Yellow
        }
    }
    
    # Try using tar (available in Windows 10+)
    if (Get-Command tar -ErrorAction SilentlyContinue) {
        try {
            $outputDir = Split-Path -Parent $OutputPath
            Push-Location $outputDir
            tar -xf $CompressedPath
            Pop-Location
            if (Test-Path $OutputPath) {
                Write-Host "Decompression complete."
                return $true
            }
        } catch {
            Write-Host "tar decompression failed: $_" -ForegroundColor Yellow
        }
    }
    
    Write-Host "ERROR: Could not decompress .xz file." -ForegroundColor Red
    Write-Host "Please install 7-Zip (https://www.7-zip.org/) or manually decompress the file."
    Write-Host "Compressed file location: $CompressedPath"
    return $false
}

# Function to select a custom Armbian image path
function Get-ArmbianImageCustomPath {
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


    Write-Host ""
    Write-Host "==============================================" -ForegroundColor Cyan
    Write-Host "  IMPORTANT: Armbian Image Selection" -ForegroundColor Cyan
    Write-Host "==============================================" -ForegroundColor Cyan
    Write-Host "Please ensure you are selecting an Armbian image"
    Write-Host "that is compatible with the Radxa Rock 3A board."
    Write-Host ""
    Write-Host "       The expected environment is:"
    Write-Host "    minimal/IoT Armbian Trixie ( Debian 13)"
    Write-Host ""
    Write-Host "The image should be an uncompressed .img file."
    Write-Host "If you have a .img.xz file, it will be decompressed."
    Write-Host "==============================================" -ForegroundColor Cyan
    Write-Host ""
    
    while ($true) {
        $customPath = Read-Host "Enter path to Armbian image (or 'cancel' to abort)"
        
        if ($customPath -eq 'cancel') {
            return $false
        }
        
        if ([string]::IsNullOrWhiteSpace($customPath)) {
            Write-Host "No path entered. Please try again." -ForegroundColor Yellow
            continue
        }
        
        # Expand environment variables and resolve path
        $customPath = [Environment]::ExpandEnvironmentVariables($customPath)
        
        # Handle relative paths
        if (-not [System.IO.Path]::IsPathRooted($customPath)) {
            $customPath = Join-Path (Get-Location) $customPath
        }
        
        # Check if it's a compressed file
        if ((Test-Path $customPath) -and ($customPath -match '\.xz$')) {
            Write-Host "Compressed image detected."
            $decompressedPath = $customPath -replace '\.xz$', ''
            $result = Expand-XzFile -CompressedPath $customPath -OutputPath $decompressedPath
            if ($result) {
                $Script:ARMBIAN_IMAGE = $decompressedPath
                Write-Host "Image ready: $($Script:ARMBIAN_IMAGE)"
                return $true
            } else {
                return $false
            }
        } elseif ((Test-Path $customPath) -and ($customPath -match '\.img$')) {
            $Script:ARMBIAN_IMAGE = $customPath
            Write-Host "Using image: $($Script:ARMBIAN_IMAGE)"
            return $true
        } elseif (Test-Path $customPath) {
            Write-Host "WARNING: File exists but doesn't have .img or .img.xz extension." -ForegroundColor Yellow
            $useAnyway = Read-Host "Use this file anyway? (y/N)"
            if ($useAnyway -match "^[Yy]") {
                $Script:ARMBIAN_IMAGE = $customPath
                Write-Host "Using image: $($Script:ARMBIAN_IMAGE)"
                return $true
            }
        } else {
            Write-Host "ERROR: File not found: $customPath" -ForegroundColor Red
            Write-Host "Please check the path and try again."
        }
    }
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
# =============================================================================
# Updated Rock 3A Section for windows.ps1
# =============================================================================
# This replaces the entire r3a block starting from:
#   if ($Script:HARDWARE_MODEL -eq "r3a") {
# down to the matching closing brace before the "else" for rpi-imager
#
# IMPORTANT: This matches linux.sh exactly - uses Armbian's built-in 
# provisioning mechanism where armbian-firstlogin sources /root/provisioning.sh
# We do NOT need a separate systemd service.
# =============================================================================

if ($Script:HARDWARE_MODEL -eq "r3a") {
    
    # Check for ext4 driver first
    Write-Host ""
    Write-Host "Checking for ext4 filesystem driver..."
    
    if (-not (Test-Ext4Driver)) {
        Write-Host ""
        Write-Host "ERROR: No ext4 driver detected!" -ForegroundColor Red
        Write-Host ""
        Write-Host "To flash Rock 3A images on Windows, you need an ext4 driver installed."
        Write-Host "The Armbian image uses ext4 for its root filesystem."
        Write-Host ""
        Write-Host "Recommended: Install Ext2Fsd (free, open source)"
        Write-Host "  - Look for 'Ext2Fsd-x.xx-setup.exe' in the provisioning folder"
        Write-Host "  - Or download from: https://github.com/matt-wu/Ext2Fsd/releases"
        Write-Host ""
        Write-Host "After installing:"
        Write-Host "  1. Run 'Ext2 Volume Manager' from Start Menu"
        Write-Host "  2. Go to Tools -> Service Management -> Start"
        Write-Host "  3. Re-run this script"
        Write-Host ""
        
        # Check if installer exists in script directory
        $scriptDir = $PSScriptRoot
        if (-not $scriptDir) { $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition }
        if (-not $scriptDir) { $scriptDir = Get-Location }
        
        $ext2fsdInstaller = Get-ChildItem -Path $scriptDir -Filter "Ext2Fsd*.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($ext2fsdInstaller) {
            Write-Host "Found installer: $($ext2fsdInstaller.Name)" -ForegroundColor Green
            $installNow = Read-Host "Would you like to run the installer now? (y/N)"
            if ($installNow -match "^[Yy]") {
                Write-Host "Launching installer... Please complete installation and restart this script."
                Start-Process -FilePath $ext2fsdInstaller.FullName -Wait
                Write-Host ""
                Write-Host "Installation complete. Please:"
                Write-Host "  1. Run 'Ext2 Volume Manager' from Start Menu"
                Write-Host "  2. Go to Tools -> Service Management -> Start"
                Write-Host "  3. Re-run this provisioning script"
            }
        }
        
        exit 1
    }
    
    Write-Host "ext4 driver found!" -ForegroundColor Green
    
    # Create temporary working copy of Armbian image
    Write-Host ""
    Write-Host "Creating temporary copy of Armbian image..."
    $tempImage = [System.IO.Path]::GetTempFileName()
    $tempImage = $tempImage -replace '\.tmp$', '.img'
    
    try {
        Copy-Item -Path $Script:ARMBIAN_IMAGE -Destination $tempImage -Force
        Write-Host "Temporary image created at: $tempImage"
        
        # Mount the image
        Write-Host "Mounting image for configuration injection..."
        $mountResult = Mount-DiskImage -ImagePath $tempImage -PassThru
        
        if (-not $mountResult) {
            throw "Failed to mount disk image"
        }
        
        # Wait for mount and driver to recognize partitions
        Start-Sleep -Seconds 3
        
        # Get the disk number of the mounted image
        $imageDisk = Get-DiskImage -ImagePath $tempImage | Get-Disk
        
        if (-not $imageDisk) {
            throw "Could not get disk info for mounted image"
        }
        
        Write-Host "Image mounted as Disk $($imageDisk.Number)"
        
        # Get partition 2 (root filesystem - partition 1 is /boot)
        $rootPartition = Get-Partition -DiskNumber $imageDisk.Number -PartitionNumber 2 -ErrorAction SilentlyContinue
        
        if (-not $rootPartition) {
            Write-Host "Partition 2 not found directly, searching for root partition..."
            $rootPartition = Get-Partition -DiskNumber $imageDisk.Number | 
                Where-Object { $_.PartitionNumber -eq 2 -or $_.Size -gt 1GB } |
                Sort-Object Size -Descending | 
                Select-Object -First 1
        }
        
        if (-not $rootPartition) {
            throw "Could not find root partition (partition 2) in image"
        }
        
        Write-Host "Found root partition: Partition $($rootPartition.PartitionNumber), Size: $([math]::Round($rootPartition.Size / 1GB, 2)) GB"
        
        # Wait for ext4 driver to mount partition
        $driveLetter = $rootPartition.DriveLetter
        $retryCount = 0
        while (-not $driveLetter -and $retryCount -lt 10) {
            Write-Host "Waiting for ext4 driver to mount partition... ($retryCount)"
            Start-Sleep -Seconds 2
            $rootPartition = Get-Partition -DiskNumber $imageDisk.Number -PartitionNumber 2 -ErrorAction SilentlyContinue
            $driveLetter = $rootPartition.DriveLetter
            $retryCount++
        }
        
        if (-not $driveLetter) {
            Write-Host "Attempting to assign drive letter..."
            try {
                $rootPartition | Add-PartitionAccessPath -AssignDriveLetter -ErrorAction Stop
                Start-Sleep -Seconds 2
                $rootPartition = Get-Partition -DiskNumber $imageDisk.Number -PartitionNumber 2
                $driveLetter = $rootPartition.DriveLetter
            } catch {
                Write-Host "Could not auto-assign drive letter: $_" -ForegroundColor Yellow
            }
        }
        
        if (-not $driveLetter) {
            throw "Could not get drive letter for root partition. Make sure Ext2Fsd service is running and configured to auto-mount."
        }
        
        $rootPath = "${driveLetter}:"
        Write-Host "Root partition mounted at: $rootPath" -ForegroundColor Green
        
        # Verify we can access the filesystem
        if (-not (Test-Path (Join-Path $rootPath "etc"))) {
            throw "Cannot access /etc on mounted partition. ext4 driver may not be working correctly."
        }
        
        # Write mesh configuration to /etc/mesh.conf
        Write-Host "Writing /etc/mesh.conf..."
        $etcPath = Join-Path $rootPath "etc"
        
        $meshConfContent = @"
# Mesh Network Configuration
# Generated by provisioning script on $(Get-Date)
hardware_model=$($Script:HARDWARE_MODEL)
eud=$($Script:EUD_CONNECTION)
lan_ap_ssid=$($Script:LAN_AP_SSID)
lan_ap_key=$($Script:LAN_AP_KEY)
max_euds_per_node=$($Script:MAX_EUDS_PER_NODE)
mtx=$($Script:INSTALL_MEDIAMTX)
mumble=$($Script:INSTALL_MUMBLE)
mesh_ssid=$($Script:MESH_SSID)
mesh_key=$($Script:MESH_SAE_KEY)
ipv4_network=$($Script:LAN_CIDR_BLOCK)
acs=$($Script:AUTO_CHANNEL)
regulatory_domain=$($Script:REGULATORY_DOMAIN)
"@
        # Write with Unix line endings (LF only)
        [System.IO.File]::WriteAllText((Join-Path $etcPath "mesh.conf"), $meshConfContent.Replace("`r`n", "`n"))
        
        # Create Armbian firstrun preset file
        Write-Host "Writing /root/.not_logged_in_yet..."
        $rootHomePath = Join-Path $rootPath "root"
        
        $notLoggedInContent = @"
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
PRESET_USER_PASSWORD="radio"
PRESET_USER_KEY=""
PRESET_DEFAULT_REALNAME="radio"
PRESET_USER_SHELL="bash"
"@
        [System.IO.File]::WriteAllText((Join-Path $rootHomePath ".not_logged_in_yet"), $notLoggedInContent.Replace("`r`n", "`n"))
        
        # Write the provisioning script (sourced by armbian-firstlogin)
        # This matches linux.sh EXACTLY - no systemd service needed
        Write-Host "Writing /root/provisioning.sh..."
        
        $provisioningScript = @'
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
        hostnamectl set-hostname "mesh-${HOST_MAC}"
        echo "Hostname set to mesh-${HOST_MAC}"
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
    echo "Downloading Rock 3A install package..."
    wget -q https://www.colorado-governor.com/manet/r3a-install.tar.gz -O /root/morse-pi-install.tar.gz || {
        echo "ERROR: Failed to download Rock 3A install package"
        return 1 2>/dev/null || true
    }

    # Unpack the install tarball AFTER apt updates to avoid kernel overwrites
    echo "Extracting install package..."
    tar -zxf /root/morse-pi-install.tar.gz -C /

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

    # Load modules at boot
    cat << MODEOF > /etc/modules-load.d/morse.conf
mac80211
cfg80211
crc7
morse
dot11ah
MODEOF

    # Morse driver options
    cat << MODEOF > /etc/modprobe.d/morse.conf
options morse country=${REG}
options morse enable_mcast_whitelist=0 enable_mcast_rate_control=1
MODEOF

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

    echo "=== Rock 3A provisioning complete at $(date) ==="

} >> "$PROVISION_LOG" 2>&1

# Show user that provisioning completed
echo ""
echo "Mesh node provisioning complete. See $PROVISION_LOG for details."
echo "System will reboot in 10 seconds to apply changes..."
echo ""

# Schedule reboot after this script returns (don't reboot while sourced)
( sleep 10 && reboot ) &
'@
        [System.IO.File]::WriteAllText((Join-Path $rootHomePath "provisioning.sh"), $provisioningScript.Replace("`r`n", "`n"))
        
        Write-Host "Configuration injection complete." -ForegroundColor Green
        
        # Unmount the image
        Write-Host "Unmounting image..."
        Start-Sleep -Seconds 1
        Dismount-DiskImage -ImagePath $tempImage
        Start-Sleep -Seconds 2
        
        # Wipe target device to avoid stale partition data
        Write-Host "Wiping target device..."
        Clear-Disk -Number $Script:TARGET_DEVICE -RemoveData -RemoveOEM -Confirm:$false -ErrorAction SilentlyContinue
        
        # Flash to target device
        Write-Host "Flashing image to Disk $($Script:TARGET_DEVICE)..."
        Write-Host "This may take several minutes..."
        
        if ($Script:DD_PATH) {
            # Use dd for faster flashing
            $ddTarget = "\\.\PhysicalDrive$($Script:TARGET_DEVICE)"
            Write-Host "Using dd: $($Script:DD_PATH)"
            & $Script:DD_PATH if="$tempImage" of="$ddTarget" bs=4M --progress
            
            if ($LASTEXITCODE -ne 0) {
                throw "dd failed with exit code $LASTEXITCODE"
            }
        } else {
            # PowerShell fallback - slower but works
            Write-Host "Using PowerShell method (this will be slower)..."
            $source = [System.IO.File]::OpenRead($tempImage)
            $target = [System.IO.File]::OpenWrite("\\.\PhysicalDrive$($Script:TARGET_DEVICE)")
            
            try {
                $buffer = New-Object byte[] (4 * 1024 * 1024)  # 4MB buffer
                $totalBytes = $source.Length
                $bytesWritten = 0
                
                while (($bytesRead = $source.Read($buffer, 0, $buffer.Length)) -gt 0) {
                    $target.Write($buffer, 0, $bytesRead)
                    $bytesWritten += $bytesRead
                    $percent = [math]::Round(($bytesWritten / $totalBytes) * 100, 1)
                    Write-Progress -Activity "Flashing image" -Status "$percent% complete" -PercentComplete $percent
                }
                
                $target.Flush()
                Write-Progress -Activity "Flashing image" -Completed
            } finally {
                $source.Close()
                $target.Close()
            }
        }
        
        # Sync and clean up
        Write-Host "Syncing..."
        Start-Sleep -Seconds 2
        
        # Clean up temp image
        Remove-Item $tempImage -Force -ErrorAction SilentlyContinue
        
        Write-Host ""
        Write-Host "Flash complete!" -ForegroundColor Green
        
    } catch {
        Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
        
        # Cleanup on error
        try {
            Dismount-DiskImage -ImagePath $tempImage -ErrorAction SilentlyContinue
        } catch {}
        
        if (Test-Path $tempImage) {
            Remove-Item $tempImage -Force -ErrorAction SilentlyContinue
        }
        
        exit 1
    }
    
} else {
    # Raspberry Pi path - use rpi-imager
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

Write-Host ""
Write-Host "Done! Flashing complete. The device will configure itself on first boot." -ForegroundColor Green
