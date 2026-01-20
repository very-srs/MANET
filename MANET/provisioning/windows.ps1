#Requires -RunAsAdministrator
<#
.SYNOPSIS
    A script to image new mesh radio nodes on Windows
.DESCRIPTION
    This PowerShell script provides the same functionality as the Linux bash version
    for flashing Raspberry Pi and Radxa Rock 3A devices with mesh network configurations.
.NOTES
    Must be run as Administrator
#>

# --- Configuration ---
$TEMPLATE_FILE = "firstrun.sh.template"

$ARMBIAN_IMAGE_URL = "https://fi.mirror.armbian.de/dl/rock-3a/archive/Armbian_25.11.1_Rock-3a_trixie_vendor_6.1.115_minimal.img.xz"
$ARMBIAN_IMAGE_FILENAME = "Armbian_25.11.1_Rock-3a_trixie_vendor_6.1.115_minimal.img"
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
$Script:ADMIN_PW = ""
$Script:AUTO_UPDATE = ""
$Script:DD_PATH = $null
$Script:RPI_IMAGER_PATH = $null
$Script:REGULATORY_DOMAIN = ""

# --- Helper Functions ---

function Generate-Password {
    param([int]$length = 10)
    
    $chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    $bytes = New-Object byte[] $length
    [Security.Cryptography.RNGCryptoServiceProvider]::Create().GetBytes($bytes)
    $password = ""
    foreach ($byte in $bytes) {
        $password += $chars[$byte % $chars.Length]
    }
    return $password
}

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
    
    # Check for Ext2Fsd service
    $ext2fsd = Get-Service -Name "Ext2Fsd" -ErrorAction SilentlyContinue
    if ($ext2fsd -and $ext2fsd.Status -eq "Running") {
        return $true
    }
    
    # Check for Paragon ExtFS service
    $paragon = Get-Service -Name "ufsd_extfs*" -ErrorAction SilentlyContinue
    if ($paragon -and $paragon.Status -eq "Running") {
        return $true
    }
    
    # Check if Ext2Fsd is installed but not running
    if ($ext2fsd) {
        Write-Host "Ext2Fsd service found but not running. Attempting to start..." -ForegroundColor Yellow
        try {
            Start-Service -Name "Ext2Fsd"
            Start-Sleep -Seconds 2
            $ext2fsd = Get-Service -Name "Ext2Fsd"
            if ($ext2fsd.Status -eq "Running") {
                Write-Host "Ext2Fsd service started successfully." -ForegroundColor Green
                return $true
            }
        } catch {
            Write-Host "Failed to start Ext2Fsd service: $_" -ForegroundColor Red
        }
    }
    
    return $false
}

function Expand-XzFile {
    param(
        [string]$CompressedPath,
        [string]$OutputPath
    )
    
    Write-Host "Decompressing (this may take a moment)..."
    
    # Check for 7-Zip
    $sevenZipPaths = @(
        "C:\Program Files\7-Zip\7z.exe",
        "C:\Program Files (x86)\7-Zip\7z.exe",
        (Get-Command 7z -ErrorAction SilentlyContinue).Source
    )
    
    $sevenZip = $null
    foreach ($path in $sevenZipPaths) {
        if ($path -and (Test-Path $path)) {
            $sevenZip = $path
            break
        }
    }
    
    if ($sevenZip) {
        Write-Host "Using 7-Zip for decompression..."
        & $sevenZip x -y "-o$(Split-Path $OutputPath)" $CompressedPath
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Decompression complete."
            return $true
        } else {
            Write-Host "ERROR: 7-Zip decompression failed." -ForegroundColor Red
            return $false
        }
    }
    
    # Try built-in tar (Windows 10 1803+)
    $tar = Get-Command tar -ErrorAction SilentlyContinue
    if ($tar) {
        Write-Host "Using built-in tar for decompression..."
        & tar -xf $CompressedPath -C (Split-Path $OutputPath)
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Decompression complete."
            return $true
        }
    }
    
    Write-Host "ERROR: No decompression tool found. Please install 7-Zip." -ForegroundColor Red
    Write-Host "Download from: https://www.7-zip.org/"
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
            # Custom CIDR input
            while ($true) {
                $customCidr = Read-Host "Enter custom LAN CIDR block (e.g., 10.10.0.0/16)"
                
                # Validate format
                if ($customCidr -notmatch '^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})/(\d{1,2})$') {
                    Write-Host "ERROR: Invalid format. Must be x.x.x.x/yy" -ForegroundColor Red
                    continue
                }
                
                $ipPart = $Matches[1]
                $prefixPart = [int]$Matches[2]
                
                # Validate prefix
                if ($prefixPart -lt 16 -or $prefixPart -gt 26) {
                    Write-Host "ERROR: Prefix /$prefixPart is invalid. Must be between /16 and /26." -ForegroundColor Red
                    continue
                }
                
                # Validate private range
                $octets = $ipPart.Split('.')
                $o1 = [int]$octets[0]
                $o2 = [int]$octets[1]
                
                $isPrivate = $false
                if ($o1 -eq 10) { $isPrivate = $true }
                elseif ($o1 -eq 172 -and $o2 -ge 16 -and $o2 -le 31) { $isPrivate = $true }
                elseif ($o1 -eq 192 -and $o2 -eq 168) { $isPrivate = $true }
                
                if (-not $isPrivate) {
                    Write-Host "ERROR: IP $ipPart is not in a private range." -ForegroundColor Red
                    Write-Host "Must be in 10.0.0.0/8, 172.16.0.0/12, or 192.168.0.0/16."
                    continue
                }
                
                $Script:LAN_CIDR_BLOCK = $customCidr
                break
            }
        }
        
        # Show capacity if EUDs configured
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

    # 4. Regulatory Domain
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

    # 5. Radio user password
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

    # 6. Network administrator password
    Write-Host ""
    Write-Host "The network administrator password is used to access the mesh admin interface."
    $adminPw = Read-Host "Enter admin password [or press Enter to generate 10-char random]"
    Write-Host ""
    
    if ([string]::IsNullOrWhiteSpace($adminPw)) {
        $Script:ADMIN_PW = Generate-Password -length 10
        Write-Host "Generated admin password: $($Script:ADMIN_PW)"
    } else {
        $Script:ADMIN_PW = $adminPw
        Write-Host "Admin password set."
    }

    # 7. Automatic updates for MANET tools
    Write-Host ""
    $response = Read-Host "Enable automatic updates for MANET tools? (Y/n)"
    if ([string]::IsNullOrWhiteSpace($response) -or $response -match "^[Yy]") {
        $Script:AUTO_UPDATE = "y"
        Write-Host "Automatic updates enabled."
    } else {
        $Script:AUTO_UPDATE = "n"
        Write-Host "Automatic updates disabled."
    }

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
# Mesh Config: $config_name
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
ADMIN_PW="$($Script:ADMIN_PW)"
AUTO_UPDATE="$($Script:AUTO_UPDATE)"
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
                "ADMIN_PW" { $Script:ADMIN_PW = $varValue }
                "AUTO_UPDATE" { $Script:AUTO_UPDATE = $varValue }
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
    Write-Host "  Regulatory Domain: $($Script:REGULATORY_DOMAIN)"
    Write-Host "  Mesh SSID: $($Script:MESH_SSID)"
    Write-Host "  Mesh SAE Key: $($Script:MESH_SAE_KEY)"
    Write-Host "  LAN CIDR Block: $($Script:LAN_CIDR_BLOCK)"
    Write-Host "  Auto Channel: $($Script:AUTO_CHANNEL)"
    Write-Host "  User password: $($Script:RADIO_PW)"
    Write-Host "  Admin password: $(if ($Script:ADMIN_PW) { $Script:ADMIN_PW } else { '(not set)' })"
    Write-Host "  Auto Update: $(if ($Script:AUTO_UPDATE) { $Script:AUTO_UPDATE } else { 'n' })"
    Write-Host "----------------------------"
}

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
        $choice = Read-Host "Select option (1 or 2)"
        switch ($choice) {
            "1" {
                return Download-ArmbianImage
            }
            "2" {
                return Select-CustomArmbianImage
            }
            default {
                Write-Host "Invalid selection. Please enter 1 or 2."
            }
        }
    }
}

function Download-ArmbianImage {
    $scriptDir = $PSScriptRoot
    if (-not $scriptDir) { $scriptDir = Get-Location }
    
    $compressedFile = Join-Path $scriptDir "${ARMBIAN_IMAGE_FILENAME}.xz"
    $outputFile = Join-Path $scriptDir $ARMBIAN_IMAGE_FILENAME
    
    Write-Host ""
    Write-Host "Downloading Armbian image..."
    Write-Host "Source: $ARMBIAN_IMAGE_URL"
    Write-Host ""
    
    try {
        $ProgressPreference = 'SilentlyContinue'  # Speeds up download
        Invoke-WebRequest -Uri $ARMBIAN_IMAGE_URL -OutFile $compressedFile -UseBasicParsing
        $ProgressPreference = 'Continue'
    } catch {
        Write-Host "ERROR: Download failed: $_" -ForegroundColor Red
        return $false
    }
    
    Write-Host "Download complete. Decompressing..."
    $result = Expand-XzFile -CompressedPath $compressedFile -OutputPath $outputFile
    
    if ($result) {
        $Script:ARMBIAN_IMAGE = $outputFile
        Write-Host "Image ready: $($Script:ARMBIAN_IMAGE)"
        return $true
    }
    
    return $false
}

function Select-CustomArmbianImage {
    Write-Host ""
    Write-Host "=============================================="
    Write-Host "  IMPORTANT: Armbian Image Selection"
    Write-Host "=============================================="
    Write-Host "Please ensure you are selecting an Armbian image"
    Write-Host "that is compatible with the Radxa Rock 3A board."
    Write-Host ""
    Write-Host "       The expected environment is:"
    Write-Host "    minimal/IoT Armbian Trixie (Debian 13)"
    Write-Host ""
    Write-Host "The image should be an uncompressed .img file."
    Write-Host "If you have a .img.xz file, it will be decompressed."
    Write-Host "=============================================="
    Write-Host ""
    
    while ($true) {
        $customPath = Read-Host "Enter path to Armbian image"
        
        if ([string]::IsNullOrWhiteSpace($customPath)) {
            Write-Host "No path entered. Please try again or press Ctrl+C to cancel."
            continue
        }
        
        # Expand environment variables
        $customPath = [Environment]::ExpandEnvironmentVariables($customPath)
        
        if ($customPath.EndsWith(".xz") -and (Test-Path $customPath)) {
            Write-Host "Compressed image detected. Decompressing..."
            $decompressedPath = $customPath -replace '\.xz$', ''
            $result = Expand-XzFile -CompressedPath $customPath -OutputPath $decompressedPath
            if ($result) {
                $Script:ARMBIAN_IMAGE = $decompressedPath
                Write-Host "Image ready: $($Script:ARMBIAN_IMAGE)"
                return $true
            } else {
                return $false
            }
        } elseif ($customPath.EndsWith(".img") -and (Test-Path $customPath)) {
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
    
    Write-Host "Select hardware platform:"
    Write-Host "1. Radxa Rock 3A"
    Write-Host "2. Raspberry Pi 5"
    Write-Host "3. Raspberry Pi 4B"
    Write-Host "4. Compute Module 4 (CM4)"
    
    do {
        $choice = Read-Host "Enter choice (1-4)"
        switch ($choice) {
            "1" { $Script:HARDWARE_MODEL = "r3a"; break }
            "2" { $Script:HARDWARE_MODEL = "rpi5"; break }
            "3" { $Script:HARDWARE_MODEL = "rpi4"; break }
            "4" { $Script:HARDWARE_MODEL = "rpi4"; break }  # CM4 uses rpi4 config
        }
    } while ($choice -notmatch "^[1234]$")
    
    # Check for rpi-imager if using Raspberry Pi
    if ($Script:HARDWARE_MODEL -ne "r3a") {
        # Look for rpi-imager
        $rpiImagerPaths = @(
            "C:\Program Files (x86)\Raspberry Pi Imager\rpi-imager.exe",
            "C:\Program Files\Raspberry Pi Imager\rpi-imager.exe",
            (Get-Command rpi-imager -ErrorAction SilentlyContinue).Source
        )
        
        foreach ($path in $rpiImagerPaths) {
            if ($path -and (Test-Path $path)) {
                $Script:RPI_IMAGER_PATH = $path
                break
            }
        }
        
        if (-not $Script:RPI_IMAGER_PATH) {
            Write-Host "ERROR: Raspberry Pi Imager not found!" -ForegroundColor Red
            Write-Host "Please install from: https://www.raspberrypi.com/software/"
            exit 1
        }
    }
    
    Write-Host ""
    Write-Host "--- 2. Select Target Device ---"
    
    # Get available disks (excluding boot disk)
    $bootDisk = (Get-Disk | Where-Object { $_.IsBoot -eq $true }).Number
    $disks = Get-Disk | Where-Object { 
        $_.Number -ne $bootDisk -and 
        $_.OperationalStatus -eq "Online" -and
        $_.Size -gt 0
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
    Write-Host "$i. Quit"
    
    do {
        $choice = Read-Host "Enter device number (1-$i)"
        $choiceNum = 0
        if ([int]::TryParse($choice, [ref]$choiceNum)) {
            if ($choiceNum -eq $i) {
                Write-Host "Aborting."
                exit 0
            }
            if ($diskMap.ContainsKey($choiceNum)) {
                $selectedDisk = $diskMap[$choiceNum]
                $Script:TARGET_DEVICE = $selectedDisk.Number
                Write-Host "Selected: Disk $($Script:TARGET_DEVICE) - $($selectedDisk.FriendlyName)"
                break
            }
        }
        Write-Host "Invalid selection." -ForegroundColor Red
    } while ($true)
}

function Confirm-Flash {
    param(
        [int]$DiskNumber
    )
    
    $disk = Get-Disk -Number $DiskNumber
    $sizeGB = [math]::Round($disk.Size / 1GB, 2)
    
    Write-Host ""
    Write-Host "==============================================" -ForegroundColor Yellow
    Write-Host "         ⚠️  FINAL CONFIRMATION  ⚠️" -ForegroundColor Yellow
    Write-Host "==============================================" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "You are about to ERASE and FLASH:"
    Write-Host ""
    Write-Host "  Device: Disk $DiskNumber - $($disk.FriendlyName)"
    Write-Host "  Size:   ${sizeGB}GB"
    Write-Host ""
    Write-Host "  Hardware: $($Script:HARDWARE_MODEL)"
    Write-Host "  Mesh SSID: $($Script:MESH_SSID)"
    Write-Host "  Network: $($Script:LAN_CIDR_BLOCK)"
    Write-Host ""
    Write-Host "⚠️  ALL DATA ON DISK $DiskNumber WILL BE DESTROYED! ⚠️" -ForegroundColor Red
    Write-Host ""
    Write-Host "==============================================" -ForegroundColor Yellow
    Write-Host ""
    
    $confirm = Read-Host "Type 'yes' to proceed, anything else to abort"
    if ($confirm -ne "yes") {
        Write-Host ""
        Write-Host "Aborted by user."
        exit 0
    }
    
    Write-Host ""
    Write-Host "Proceeding with flash..."
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

# Check for dd.exe (optional, for faster flashing)
$ddPaths = @(
    (Join-Path $PSScriptRoot "ddrelease64.exe"),
    (Join-Path (Get-Location) "ddrelease64.exe"),
    (Get-Command dd -ErrorAction SilentlyContinue).Source
)

foreach ($path in $ddPaths) {
    if ($path -and (Test-Path $path)) {
        $Script:DD_PATH = $path
        Write-Host "Found dd: $($Script:DD_PATH)" -ForegroundColor Green
        break
    }
}

if (-not $Script:DD_PATH) {
    Write-Host "Note: dd.exe not found. Will use slower PowerShell method for flashing." -ForegroundColor Yellow
    Write-Host "For faster flashing, place ddrelease64.exe in the script directory."
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

# --- Rock 3A Flashing ---
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
    
    # Get Armbian image
    if (-not (Get-ArmbianImage)) {
        Write-Host "ERROR: Failed to acquire Armbian image." -ForegroundColor Red
        exit 1
    }
    
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
        
        # Find the root partition (partition 2 on Armbian)
        $partitions = Get-Partition -DiskNumber $imageDisk.Number -ErrorAction SilentlyContinue
        $rootPartition = $partitions | Where-Object { $_.PartitionNumber -eq 2 }
        
        if (-not $rootPartition) {
            throw "Could not find root partition (partition 2) on mounted image"
        }
        
        # Get drive letter assigned by ext4 driver
        $driveLetter = $rootPartition.DriveLetter
        
        if (-not $driveLetter) {
            # Try to assign a drive letter
            Write-Host "Attempting to assign drive letter to root partition..."
            $rootPartition | Add-PartitionAccessPath -AssignDriveLetter -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
            $rootPartition = Get-Partition -DiskNumber $imageDisk.Number -PartitionNumber 2
            $driveLetter = $rootPartition.DriveLetter
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
admin_password=$($Script:ADMIN_PW)
auto_update=$($Script:AUTO_UPDATE)
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
        
        # Final confirmation before flashing
        Confirm-Flash -DiskNumber $Script:TARGET_DEVICE
        
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
        Write-Host "==============================================" -ForegroundColor Green
        Write-Host "           ✅ Flash complete!" -ForegroundColor Green
        Write-Host "==============================================" -ForegroundColor Green
        Write-Host ""
        Write-Host "You can now remove the SD card and boot your"
        Write-Host "Rock 3A. First boot provisioning will run"
        Write-Host "automatically when connected to the internet."
        Write-Host ""
        
    } catch {
        Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
        
        # Clean up on error
        if ($tempImage -and (Test-Path $tempImage)) {
            Dismount-DiskImage -ImagePath $tempImage -ErrorAction SilentlyContinue
            Remove-Item $tempImage -Force -ErrorAction SilentlyContinue
        }
        
        exit 1
    }
    
} else {
    # Raspberry Pi path - use rpi-imager
    
    # Final confirmation before flashing
    Confirm-Flash -DiskNumber $Script:TARGET_DEVICE
    
    # Generate the firstrun script from template
    Write-Host "Generating firstrun script from template..."
    $templateContent = Get-Content $TEMPLATE_FILE -Raw
    
    # Replace placeholders
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
    $templateContent = $templateContent -replace '__REGULATORY_DOMAIN__', $Script:REGULATORY_DOMAIN
    $templateContent = $templateContent -replace '__ADMIN_PW__', $Script:ADMIN_PW
    $templateContent = $templateContent -replace '__AUTO_UPDATE__', $Script:AUTO_UPDATE
    
    # Write to temp file with Unix line endings
    $tempScript = [System.IO.Path]::GetTempFileName()
    [System.IO.File]::WriteAllText($tempScript, $templateContent.Replace("`r`n", "`n"))
    
    # Run rpi-imager
    Write-Host "Running Raspberry Pi Imager..."
    $targetDrive = "\\.\PhysicalDrive$($Script:TARGET_DEVICE)"
    
    & $Script:RPI_IMAGER_PATH --cli $OS_IMAGE_URL $targetDrive --first-run-script $tempScript
    
    if ($LASTEXITCODE -eq 0) {
        Write-Host ""
        Write-Host "==============================================" -ForegroundColor Green
        Write-Host "           ✅ Flash complete!" -ForegroundColor Green
        Write-Host "==============================================" -ForegroundColor Green
        Write-Host ""
        Write-Host "You can now remove the SD card and boot your"
        Write-Host "Raspberry Pi. First boot provisioning will run"
        Write-Host "automatically when connected to the internet."
        Write-Host ""
    } else {
        Write-Host "ERROR: rpi-imager failed with exit code $LASTEXITCODE" -ForegroundColor Red
    }
    
    # Clean up
    Remove-Item $tempScript -Force -ErrorAction SilentlyContinue
}
