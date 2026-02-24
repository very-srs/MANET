#Requires -RunAsAdministrator
<#
.SYNOPSIS
    A script to image new mesh radio nodes on Windows
.DESCRIPTION
    Equivalent to linux.sh — flashes Raspberry Pi and Radxa Rock 3A devices
    with mesh network configurations. Rock 3A images are customised by mounting
    the Armbian ext4 root partition via Ext2Fsd. Raspberry Pi images are written
    with rpi-imager.
.NOTES
    Must be run as Administrator.
    Rock 3A support requires Ext2Fsd (https://github.com/matt-wu/Ext2Fsd/releases).
#>

# --- Configuration ---
$TEMPLATE_FILE      = "firstrun.sh.template"
$ROCK3A_TEMPLATE    = "rock3a-provision.sh.template"

$ARMBIAN_IMAGE_URL      = "https://fi.mirror.armbian.de/dl/rock-3a/archive/Armbian_25.11.1_Rock-3a_trixie_vendor_6.1.115_minimal.img.xz"
$ARMBIAN_IMAGE_FILENAME = "Armbian_25.11.1_Rock-3a_trixie_vendor_6.1.115_minimal.img"
$Script:ARMBIAN_IMAGE   = ""   # Set by Get-ArmbianImage

$CONFIG_DIR    = ".mesh-configs"
$OS_IMAGE_URL  = "https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2025-10-02/2025-10-01-raspios-trixie-arm64-lite.img.xz"

# --- Global State ---
$Script:HARDWARE_MODEL    = ""
$Script:TARGET_DEVICE     = ""
$Script:EUD_CONNECTION    = ""
$Script:LAN_AP_SSID       = ""
$Script:LAN_AP_KEY        = ""
$Script:MAX_EUDS_PER_NODE = 0
$Script:INSTALL_MEDIAMTX  = ""
$Script:INSTALL_MUMBLE    = ""
$Script:MESH_SSID         = ""
$Script:MESH_SAE_KEY      = ""
$Script:LAN_CIDR_BLOCK    = ""
$Script:AUTO_CHANNEL      = ""
$Script:RADIO_PW          = ""
$Script:ADMIN_PW          = ""
$Script:AUTO_UPDATE       = ""
$Script:REGULATORY_DOMAIN = ""
$Script:RPI_IMAGER_PATH   = $null


# ============================================================
# Helper Functions
# ============================================================

function Generate-Password {
    param([int]$length = 10)
    $chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    $bytes = New-Object byte[] $length
    [Security.Cryptography.RNGCryptoServiceProvider]::Create().GetBytes($bytes)
    $password = ""
    foreach ($byte in $bytes) { $password += $chars[$byte % $chars.Length] }
    return $password
}

function Test-RegulatoryDomain {
    param([string]$domain)
    $validDomains = @(
        "US","CA","GB","DE","FR","IT","ES","NL","BE","AT","CH","SE","NO","DK","FI",
        "PL","CZ","HU","GR","PT","IE","RO","BG","HR","SI","SK","LT","LV","EE","CY",
        "MT","LU","AU","NZ","JP","KR","TW","SG","MY","TH","PH","ID","VN","IN","CN",
        "BR","AR","MX","CL","CO","PE","ZA","IL","AE","SA","RU","UA","TR","EG","MA"
    )
    $domain = $domain.ToUpper()
    if ($validDomains -contains $domain) { return $domain }
    return $null
}

function Calculate-Capacity {
    param([string]$cidr, [int]$maxEuds)

    if ($cidr -notmatch '^(\d+\.\d+\.\d+\.\d+)/(\d+)$') { return $null }
    $ip     = $Matches[1]
    $prefix = [int]$Matches[2]

    $hostBits   = 32 - $prefix
    $totalHosts = [math]::Pow(2, $hostBits) - 2   # subtract network and broadcast
    $reserved   = 5

    if ($maxEuds -gt 0) {
        $maxNodes = [math]::Floor($totalHosts / (1 + $maxEuds))
        $eudPool  = $maxNodes * $maxEuds
    } else {
        $maxNodes = $totalHosts - $reserved
        $eudPool  = 0
    }

    return @{
        Total    = [int]$totalHosts
        Services = $reserved
        EudPool  = [int]$eudPool
        MaxNodes = [int]$maxNodes
    }
}

# Generates a Linux SHA-512 crypt hash for use in /etc/shadow.
# Tries openssl (Git for Windows), WSL, and Python in turn.
# Returns $null if no suitable tool is found.
function Get-LinuxPasswordHash {
    param([string]$password)

    # Try openssl in PATH (present when Git for Windows is installed)
    $openssl = Get-Command openssl -ErrorAction SilentlyContinue
    if ($openssl) {
        $hash = & openssl passwd -6 $password 2>$null
        if ($LASTEXITCODE -eq 0 -and $hash -and $hash.StartsWith('$6$')) {
            return $hash
        }
    }

    # Try WSL
    $wsl = Get-Command wsl -ErrorAction SilentlyContinue
    if ($wsl) {
        $hash = & wsl openssl passwd -6 $password 2>$null
        if ($LASTEXITCODE -eq 0 -and $hash -and $hash.StartsWith('$6$')) {
            return $hash
        }
    }

    # Try Python (python3 or python)
    foreach ($pyCmd in @("python3", "python")) {
        $py = Get-Command $pyCmd -ErrorAction SilentlyContinue
        if ($py) {
            $escaped = $password -replace "'", "'\"'\"'"
            $hash = & $py -c "import crypt; print(crypt.crypt('$escaped', crypt.mksalt(crypt.METHOD_SHA512)))" 2>$null
            if ($hash -and $hash.StartsWith('$6$')) {
                return $hash
            }
        }
    }

    return $null
}

function Expand-XzFile {
    param([string]$CompressedPath, [string]$OutputPath)

    # Try 7-Zip
    $sevenZipPaths = @(
        "C:\Program Files\7-Zip\7z.exe",
        "C:\Program Files (x86)\7-Zip\7z.exe",
        (Get-Command 7z -ErrorAction SilentlyContinue).Source
    )
    foreach ($p in $sevenZipPaths) {
        if ($p -and (Test-Path $p)) {
            Write-Host "Using 7-Zip for decompression..."
            & $p e $CompressedPath "-o$(Split-Path $OutputPath)" -y | Out-Null
            if ($LASTEXITCODE -eq 0) { Write-Host "Decompression complete."; return $true }
            Write-Host "ERROR: 7-Zip decompression failed." -ForegroundColor Red
            return $false
        }
    }

    # Try built-in tar (Windows 10 1803+)
    $tar = Get-Command tar -ErrorAction SilentlyContinue
    if ($tar) {
        Write-Host "Using built-in tar for decompression..."
        & tar -xf $CompressedPath -C (Split-Path $OutputPath)
        if ($LASTEXITCODE -eq 0) { Write-Host "Decompression complete."; return $true }
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
            while ($true) {
                $customCidr = Read-Host "Enter custom LAN CIDR block (e.g., 10.10.0.0/16)"
                if ($customCidr -notmatch '^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})/(\d{1,2})$') {
                    Write-Host "ERROR: Invalid format. Must be x.x.x.x/yy" -ForegroundColor Red; continue
                }
                $prefixPart = [int]$Matches[2]
                if ($prefixPart -lt 16 -or $prefixPart -gt 26) {
                    Write-Host "ERROR: Prefix /$prefixPart is invalid. Must be between /16 and /26." -ForegroundColor Red; continue
                }
                $octets = $Matches[1].Split('.')
                $o1 = [int]$octets[0]; $o2 = [int]$octets[1]
                $isPrivate = ($o1 -eq 10) -or ($o1 -eq 172 -and $o2 -ge 16 -and $o2 -le 31) -or ($o1 -eq 192 -and $o2 -eq 168)
                if (-not $isPrivate) {
                    Write-Host "ERROR: IP is not in a private range (10.x, 172.16-31.x, 192.168.x)." -ForegroundColor Red; continue
                }
                $Script:LAN_CIDR_BLOCK = $customCidr
                break
            }
        }

        if ($maxEuds -gt 0) {
            $capacity = Calculate-Capacity -cidr $Script:LAN_CIDR_BLOCK -maxEuds $maxEuds
            Write-Host ""
            Write-Host "=== Network Capacity Analysis ==="
            Write-Host "Network: $($Script:LAN_CIDR_BLOCK)"
            Write-Host "  Total usable IPs:        $($capacity.Total)"
            Write-Host "  Reserved for services:   $($capacity.Services)"
            Write-Host "  Reserved for EUD pool:   $($capacity.EudPool) ($maxEuds EUDs x $($capacity.MaxNodes) nodes)"
            Write-Host "  Available for mesh nodes: $($capacity.MaxNodes)"
            Write-Host "=================================="
            if ($capacity.MaxNodes -lt 3) {
                Write-Host "WARNING: Only $($capacity.MaxNodes) mesh nodes fit. Consider a larger network or fewer max EUDs." -ForegroundColor Yellow
            }
            $accept = Read-Host "Accept this configuration? (Y/n)"
            if ([string]::IsNullOrWhiteSpace($accept) -or $accept -match "^[Yy]") { break }
            Write-Host "Let's reconfigure..."
        } else {
            Write-Host "Using network: $($Script:LAN_CIDR_BLOCK)"
            break
        }
    }
}

function Ask-Questions {
    Write-Host "--- Starting New Configuration ---"

    # EUD Connection Type
    Write-Host "`nSelect EUD (client) connection type:"
    Write-Host "1. Wired"
    Write-Host "2. Wireless"
    Write-Host "3. Auto"
    do {
        $choice = Read-Host "Enter choice (1-3)"
        switch ($choice) {
            "1" { $Script:EUD_CONNECTION = "wired";    break }
            "2" { $Script:EUD_CONNECTION = "wireless"; break }
            "3" { $Script:EUD_CONNECTION = "auto";     break }
        }
    } while ($choice -notmatch "^[123]$")

    if ($Script:EUD_CONNECTION -eq "wireless" -or $Script:EUD_CONNECTION -eq "auto") {
        $Script:LAN_AP_SSID = Read-Host "Enter LAN AP SSID Name"
        while ($true) {
            $key = Read-Host "Enter LAN AP WPA2 Key (8-63 chars) [or press Enter to generate]"
            Write-Host ""
            if ([string]::IsNullOrWhiteSpace($key)) {
                $bytes = New-Object byte[] 33
                [Security.Cryptography.RNGCryptoServiceProvider]::Create().GetBytes($bytes)
                $Script:LAN_AP_KEY = [Convert]::ToBase64String($bytes)
                Write-Host "Generated LAN AP Key: $($Script:LAN_AP_KEY)"
                break
            }
            if ($key.Length -lt 8 -or $key.Length -gt 63) {
                Write-Host "ERROR: Key must be between 8 and 63 characters." -ForegroundColor Red
            } else { $Script:LAN_AP_KEY = $key; break }
        }
    } else {
        $Script:LAN_AP_SSID       = ""
        $Script:LAN_AP_KEY        = ""
        $Script:MAX_EUDS_PER_NODE = 0
    }

    # Optional Software
    $r = Read-Host "Install MediaMTX Server? (Y/n)"
    $Script:INSTALL_MEDIAMTX = if ([string]::IsNullOrWhiteSpace($r) -or $r -match "^[Yy]") { "y" } else { "n" }

    $r = Read-Host "Install Mumble Server (murmur)? (Y/n)"
    $Script:INSTALL_MUMBLE = if ([string]::IsNullOrWhiteSpace($r) -or $r -match "^[Yy]") { "y" } else { "n" }

    # Mesh Configuration
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
            Write-Host "ERROR: Key must be between 8 and 63 characters." -ForegroundColor Red
        } else { $Script:MESH_SAE_KEY = $key; break }
    }

    # Regulatory Domain
    while ($true) {
        $domain = Read-Host "Enter WiFi regulatory domain (2-letter country code, default: US)"
        if ([string]::IsNullOrWhiteSpace($domain)) { $domain = "US" }
        $validated = Test-RegulatoryDomain -domain $domain
        if ($validated) {
            $Script:REGULATORY_DOMAIN = $validated
            Write-Host "Using regulatory domain: $($Script:REGULATORY_DOMAIN)"
            break
        } else {
            Write-Host "ERROR: Invalid regulatory domain: $domain" -ForegroundColor Red
            Write-Host "Enter a valid 2-letter ISO country code (e.g., US, GB, DE, JP, AU)"
        }
    }

    # Radio user password
    Write-Host "The device will have a user called 'radio' for SSH access."
    $pw = Read-Host "Enter a password for the radio user [or press Enter to default to 'radio']"
    Write-Host ""
    $Script:RADIO_PW = if ([string]::IsNullOrWhiteSpace($pw)) { Write-Host "Setting default password"; "radio" } else { $pw }
    Write-Host "Radio password set to: $($Script:RADIO_PW)"

    # Admin password
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

    # Automatic updates
    Write-Host ""
    $r = Read-Host "Enable automatic updates for MANET tools? (Y/n)"
    if ([string]::IsNullOrWhiteSpace($r) -or $r -match "^[Yy]") {
        $Script:AUTO_UPDATE = "y"; Write-Host "Automatic updates enabled."
    } else {
        $Script:AUTO_UPDATE = "n"; Write-Host "Automatic updates disabled."
    }

    # Max EUDs per node (only for wireless/auto modes)
    if ($Script:EUD_CONNECTION -eq "wireless" -or $Script:EUD_CONNECTION -eq "auto") {
        while ($true) {
            $input = Read-Host "Maximum EUDs per node's AP (1-20)"
            if ($input -match '^\d+$' -and [int]$input -ge 1 -and [int]$input -le 20) {
                $Script:MAX_EUDS_PER_NODE = [int]$input; break
            } else {
                Write-Host "ERROR: Please enter a number between 1 and 20." -ForegroundColor Red
            }
        }
    }

    # CIDR block
    Ask-LanCidr -maxEuds $Script:MAX_EUDS_PER_NODE

    # Auto Channel Selection (incompatible with wireless/auto EUD modes)
    if ($Script:EUD_CONNECTION -eq "wireless" -or $Script:EUD_CONNECTION -eq "auto") {
        $Script:AUTO_CHANNEL = "n"
        Write-Host "Automatic WiFi Channel Selection disabled (not compatible with Wireless/Auto EUD mode)"
    } else {
        $r = Read-Host "Use Automatic WiFi Channel Selection? (Y/n)"
        $Script:AUTO_CHANNEL = if ([string]::IsNullOrWhiteSpace($r) -or $r -match "^[Yy]") { "y" } else { "n" }
    }

    Write-Host "----------------------------------"
}

function Save-Config {
    Write-Host ""
    $save_choice = Read-Host "Save this configuration? (Y/n)"
    if (-not ([string]::IsNullOrWhiteSpace($save_choice) -or $save_choice -match "^[Yy]")) { return }

    $config_name = Read-Host "Enter a name for this config"
    if ([string]::IsNullOrWhiteSpace($config_name)) { Write-Host "Invalid name, skipping save."; return }

    $CONFIG_FILE = Join-Path $CONFIG_DIR "$config_name.conf"
    $content = @"
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
    [System.IO.File]::WriteAllText($CONFIG_FILE, $content.Replace("`r`n", "`n"))
    Write-Host "Configuration saved to $CONFIG_FILE"
}

function Load-Config {
    param([string]$ConfigFile)
    Write-Host "Loading config from $ConfigFile..."

    Get-Content $ConfigFile | ForEach-Object {
        if ($_ -match '^([^=]+)="([^"]*)"') {
            switch ($Matches[1]) {
                "EUD_CONNECTION"    { $Script:EUD_CONNECTION    = $Matches[2] }
                "LAN_AP_SSID"       { $Script:LAN_AP_SSID       = $Matches[2] }
                "LAN_AP_KEY"        { $Script:LAN_AP_KEY        = $Matches[2] }
                "MAX_EUDS_PER_NODE" { $Script:MAX_EUDS_PER_NODE = [int]$Matches[2] }
                "INSTALL_MEDIAMTX"  { $Script:INSTALL_MEDIAMTX  = $Matches[2] }
                "INSTALL_MUMBLE"    { $Script:INSTALL_MUMBLE    = $Matches[2] }
                "REGULATORY_DOMAIN" { $Script:REGULATORY_DOMAIN = $Matches[2] }
                "MESH_SSID"         { $Script:MESH_SSID         = $Matches[2] }
                "MESH_SAE_KEY"      { $Script:MESH_SAE_KEY      = $Matches[2] }
                "LAN_CIDR_BLOCK"    { $Script:LAN_CIDR_BLOCK    = $Matches[2] }
                "AUTO_CHANNEL"      { $Script:AUTO_CHANNEL      = $Matches[2] }
                "RADIO_PW"          { $Script:RADIO_PW          = $Matches[2] }
                "ADMIN_PW"          { $Script:ADMIN_PW          = $Matches[2] }
                "AUTO_UPDATE"       { $Script:AUTO_UPDATE       = $Matches[2] }
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

# ============================================================
# Rock 3A Image Acquisition
# ============================================================

function Get-ArmbianImage {
    Write-Host ""
    Write-Host "--- Armbian Image Setup for Rock 3A ---"

    $scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Get-Location }
    $localImage      = Join-Path $scriptDir $ARMBIAN_IMAGE_FILENAME
    $localCompressed = Join-Path $scriptDir "${ARMBIAN_IMAGE_FILENAME}.xz"

    if (Test-Path $localImage) {
        Write-Host "Found local Armbian image: $localImage"
        $Script:ARMBIAN_IMAGE = $localImage
        return $true
    }

    if (Test-Path $localCompressed) {
        Write-Host "Found compressed Armbian image: $localCompressed"
        Write-Host "Decompressing (this may take a moment)..."
        $result = Expand-XzFile -CompressedPath $localCompressed -OutputPath $localImage
        if ($result) { $Script:ARMBIAN_IMAGE = $localImage; return $true }
        return $false
    }

    Write-Host "Armbian image not found locally."
    Write-Host ""
    Write-Host "Options:"
    Write-Host "  1. Download from Armbian mirror (recommended)"
    Write-Host "     URL: $ARMBIAN_IMAGE_URL"
    Write-Host "  2. Provide path to an existing Armbian Trixie image for Rock 3A"
    Write-Host ""

    while ($true) {
        $choice = Read-Host "Select option (1 or 2)"
        switch ($choice) {
            "1" { return Download-ArmbianImage }
            "2" { return Select-CustomArmbianImage }
            default { Write-Host "Invalid selection. Please enter 1 or 2." }
        }
    }
}

function Download-ArmbianImage {
    $scriptDir       = if ($PSScriptRoot) { $PSScriptRoot } else { Get-Location }
    $compressedFile  = Join-Path $scriptDir "${ARMBIAN_IMAGE_FILENAME}.xz"
    $outputFile      = Join-Path $scriptDir $ARMBIAN_IMAGE_FILENAME

    Write-Host ""
    Write-Host "Downloading Armbian image..."
    Write-Host "Source: $ARMBIAN_IMAGE_URL"
    Write-Host ""

    try {
        $ProgressPreference = 'SilentlyContinue'
        Invoke-WebRequest -Uri $ARMBIAN_IMAGE_URL -OutFile $compressedFile -UseBasicParsing
        $ProgressPreference = 'Continue'
    } catch {
        Write-Host "ERROR: Download failed: $_" -ForegroundColor Red
        return $false
    }

    Write-Host "Download complete. Decompressing..."
    $result = Expand-XzFile -CompressedPath $compressedFile -OutputPath $outputFile
    if ($result) { $Script:ARMBIAN_IMAGE = $outputFile; Write-Host "Image ready: $outputFile"; return $true }
    return $false
}

function Select-CustomArmbianImage {
    Write-Host ""
    Write-Host "Please ensure you are selecting an Armbian image for the Radxa Rock 3A."
    Write-Host "The expected environment is: minimal/IoT Armbian Trixie (Debian 13)"
    Write-Host "The image should be an uncompressed .img file (.img.xz will be decompressed)."
    Write-Host ""

    $scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Get-Location }

    while ($true) {
        $customPath = Read-Host "Enter path to Armbian image"
        if ([string]::IsNullOrWhiteSpace($customPath)) { Write-Host "No path entered."; continue }
        $customPath = [Environment]::ExpandEnvironmentVariables($customPath)

        if ($customPath.EndsWith(".xz") -and (Test-Path $customPath)) {
            Write-Host "Compressed image detected. Decompressing..."
            $decompressedPath = $customPath -replace '\.xz$', ''
            $result = Expand-XzFile -CompressedPath $customPath -OutputPath $decompressedPath
            if ($result) { $Script:ARMBIAN_IMAGE = $decompressedPath; return $true }
            return $false
        } elseif ($customPath.EndsWith(".img") -and (Test-Path $customPath)) {
            $Script:ARMBIAN_IMAGE = $customPath
            Write-Host "Using image: $($Script:ARMBIAN_IMAGE)"
            return $true
        } elseif (Test-Path $customPath) {
            Write-Host "WARNING: File does not have .img or .img.xz extension." -ForegroundColor Yellow
            $use = Read-Host "Use this file anyway? (y/N)"
            if ($use -match "^[Yy]") { $Script:ARMBIAN_IMAGE = $customPath; return $true }
        } else {
            Write-Host "ERROR: File not found: $customPath" -ForegroundColor Red
        }
    }
}

# ============================================================
# Hardware and Device Selection
# ============================================================

function Test-Ext4Driver {
    # Check if the Ext2Fsd service is running
    $svc = Get-Service -Name "Ext2Srv" -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -eq "Running") { return $true }

    # Try to start it if it exists but isn't running
    if ($svc) {
        try {
            Start-Service "Ext2Srv" -ErrorAction Stop
            Start-Sleep -Seconds 2
            $svc = Get-Service -Name "Ext2Srv" -ErrorAction SilentlyContinue
            return ($svc -and $svc.Status -eq "Running")
        } catch { }
    }
    return $false
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
            "1" { $Script:HARDWARE_MODEL = "r3a";  break }
            "2" { $Script:HARDWARE_MODEL = "rpi5"; break }
            "3" { $Script:HARDWARE_MODEL = "rpi4"; break }
            "4" { $Script:HARDWARE_MODEL = "rpi4"; break }   # CM4 uses rpi4 config
        }
    } while ($choice -notmatch "^[1234]$")

    # For Raspberry Pi targets, locate rpi-imager
    if ($Script:HARDWARE_MODEL -ne "r3a") {
        $rpiImagerPaths = @(
            "C:\Program Files (x86)\Raspberry Pi Imager\rpi-imager.exe",
            "C:\Program Files\Raspberry Pi Imager\rpi-imager.exe",
            (Get-Command rpi-imager -ErrorAction SilentlyContinue).Source
        )
        foreach ($p in $rpiImagerPaths) {
            if ($p -and (Test-Path $p)) { $Script:RPI_IMAGER_PATH = $p; break }
        }
        if (-not $Script:RPI_IMAGER_PATH) {
            Write-Host "ERROR: Raspberry Pi Imager not found!" -ForegroundColor Red
            Write-Host "Please install from: https://www.raspberrypi.com/software/"
            exit 1
        }
    }

    # CM4 note
    if ($choice -eq "4") {
        Write-Host ""
        Write-Host "NOTE: For CM4 on Windows, you must run rpiboot manually before continuing." -ForegroundColor Yellow
        Write-Host "Once rpiboot has mounted the eMMC, press Enter to continue."
        Read-Host "Press Enter when the CM4 eMMC is mounted and ready"
    }

    Write-Host ""
    Write-Host "--- 2. Select Target Device ---"

    $bootDisk = (Get-Disk | Where-Object { $_.IsBoot -eq $true }).Number
    $disks = Get-Disk | Where-Object {
        $_.Number -ne $bootDisk -and
        $_.OperationalStatus -eq "Online" -and
        $_.Size -gt 0
    }

    if ($disks.Count -eq 0) {
        Write-Host "ERROR: No suitable target devices found." -ForegroundColor Red
        Write-Host "Please ensure your SD card reader, USB drive, or CM4 eMMC is connected."
        exit 1
    }

    Write-Host "Available devices:"
    $i = 1; $diskMap = @{}
    foreach ($disk in $disks) {
        $sizeGB = [math]::Round($disk.Size / 1GB, 2)
        Write-Host "$i. Disk $($disk.Number): $($disk.FriendlyName) - ${sizeGB}GB"
        $diskMap[$i] = $disk; $i++
    }
    Write-Host "$i. Quit"

    do {
        $c = Read-Host "Enter device number (1-$i)"
        $n = 0
        if ([int]::TryParse($c, [ref]$n)) {
            if ($n -eq $i) { Write-Host "Aborting."; exit 0 }
            if ($diskMap.ContainsKey($n)) {
                $Script:TARGET_DEVICE = $diskMap[$n].Number
                Write-Host "Selected: Disk $($Script:TARGET_DEVICE) - $($diskMap[$n].FriendlyName)"
                break
            }
        }
        Write-Host "Invalid selection." -ForegroundColor Red
    } while ($true)
}

function Confirm-Flash {
    param([int]$DiskNumber)
    $disk   = Get-Disk -Number $DiskNumber
    $sizeGB = [math]::Round($disk.Size / 1GB, 2)

    Write-Host ""
    Write-Host "==============================================" -ForegroundColor Yellow
    Write-Host "         ⚠️  FINAL CONFIRMATION  ⚠️"          -ForegroundColor Yellow
    Write-Host "==============================================" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "You are about to ERASE and FLASH:"
    Write-Host ""
    Write-Host "  Device: Disk $DiskNumber - $($disk.FriendlyName)"
    Write-Host "  Size:   ${sizeGB}GB"
    Write-Host ""
    Write-Host "  Hardware:  $($Script:HARDWARE_MODEL)"
    Write-Host "  Mesh SSID: $($Script:MESH_SSID)"
    Write-Host "  Network:   $($Script:LAN_CIDR_BLOCK)"
    Write-Host ""
    Write-Host "⚠️  ALL DATA ON DISK $DiskNumber WILL BE DESTROYED! ⚠️" -ForegroundColor Red
    Write-Host ""
    Write-Host "==============================================" -ForegroundColor Yellow
    Write-Host ""

    $confirm = Read-Host "Type 'yes' to proceed, anything else to abort"
    if ($confirm -ne "yes") { Write-Host ""; Write-Host "Aborted by user."; exit 0 }
    Write-Host ""; Write-Host "Proceeding with flash..."
}


# ============================================================
# Main Script
# ============================================================

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
if (-not (Test-Path $ROCK3A_TEMPLATE)) {
    Write-Host "ERROR: Rock 3A template '$ROCK3A_TEMPLATE' not found." -ForegroundColor Red
    exit 1
}

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
            $i = 1; $configMap = @{}
            foreach ($file in $configFiles) {
                Write-Host "$i. $($file.BaseName)"
                $configMap[$i] = $file.FullName; $i++
            }
            Write-Host "$i. Cancel"
            do {
                $cc = Read-Host "Enter number (1-$i)"
                $cn = 0
                if ([int]::TryParse($cc, [ref]$cn)) {
                    if ($cn -eq $i) { Write-Host "Aborting."; exit 0 }
                    if ($configMap.ContainsKey($cn)) { Load-Config -ConfigFile $configMap[$cn]; break }
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

# --- 3. Select Hardware and Acquire Image ---

Write-Host ""
Write-Host "--- Image & Device ---"

Select-HardwareAndTargetDevice

if ($Script:HARDWARE_MODEL -eq "r3a") {
    $imageOk = Get-ArmbianImage
    if (-not $imageOk) {
        Write-Host "ERROR: Could not obtain Armbian image." -ForegroundColor Red
        exit 1
    }
}

# ============================================================
# Rock 3A Flashing Path
# ============================================================

if ($Script:HARDWARE_MODEL -eq "r3a") {

    # Verify Ext2Fsd is available
    Write-Host ""
    Write-Host "Checking for ext4 filesystem driver (Ext2Fsd)..."
    if (-not (Test-Ext4Driver)) {
        Write-Host ""
        Write-Host "ERROR: Ext2Fsd service not found or not running." -ForegroundColor Red
        Write-Host ""
        Write-Host "To flash Rock 3A images on Windows you need Ext2Fsd installed."
        Write-Host "Download from: https://github.com/matt-wu/Ext2Fsd/releases"
        Write-Host ""
        Write-Host "After installing:"
        Write-Host "  1. Run 'Ext2 Volume Manager' from the Start Menu"
        Write-Host "  2. Go to Tools -> Service Management -> Start"
        Write-Host "  3. Re-run this script"
        Write-Host ""

        # Offer to launch installer if present alongside the script
        $scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Get-Location }
        $installer = Get-ChildItem -Path $scriptDir -Filter "Ext2Fsd*.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($installer) {
            Write-Host "Found installer: $($installer.Name)" -ForegroundColor Green
            $run = Read-Host "Run the installer now? (y/N)"
            if ($run -match "^[Yy]") {
                Write-Host "Launching installer. Please complete it and then re-run this script."
                Start-Process $installer.FullName -Wait
            }
        }
        exit 1
    }
    Write-Host "Ext2Fsd service is running." -ForegroundColor Green

    # Create temp copy of image to avoid modifying the original
    $tempImage = [System.IO.Path]::GetTempFileName() -replace '\.tmp$', '.img'
    Write-Host "Creating temporary copy of $($Script:ARMBIAN_IMAGE)..."
    Copy-Item $Script:ARMBIAN_IMAGE $tempImage

    try {
        # Mount the image as a virtual disk
        Write-Host "Mounting image as virtual disk..."
        $vdisk = Mount-DiskImage -ImagePath $tempImage -PassThru
        Start-Sleep -Seconds 3
        $disk = Get-Disk | Where-Object { $_.Location -eq $tempImage }
        if (-not $disk) { throw "Could not find mounted virtual disk." }

        # Find the root partition (partition 2 on Armbian)
        $partition = Get-Partition -DiskNumber $disk.Number | Where-Object { $_.PartitionNumber -eq 2 }
        if (-not $partition) { throw "Could not find root partition (partition 2) on mounted image." }

        # Assign a drive letter so Ext2Fsd can mount it
        if (-not $partition.DriveLetter -or $partition.DriveLetter -eq "`0") {
            Add-PartitionAccessPath -DiskNumber $disk.Number -PartitionNumber 2 -AssignDriveLetter
            Start-Sleep -Seconds 3
            $partition = Get-Partition -DiskNumber $disk.Number -PartitionNumber 2
        }

        $driveLetter = $partition.DriveLetter
        if (-not $driveLetter -or $driveLetter -eq "`0") {
            throw "Failed to assign a drive letter to the root partition."
        }

        $rootPath = "${driveLetter}:"
        Write-Host "Root partition mounted at: $rootPath" -ForegroundColor Green

        if (-not (Test-Path (Join-Path $rootPath "etc"))) {
            throw "Cannot access /etc on mounted partition. Ext2Fsd may not be working correctly."
        }

        # --------------------------------------------------------
        # Write /etc/mesh.conf
        # --------------------------------------------------------
        Write-Host "Writing /etc/mesh.conf..."
        $meshConf = @"
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
        [System.IO.File]::WriteAllText((Join-Path $rootPath "etc\mesh.conf"), $meshConf.Replace("`r`n", "`n"))

        # --------------------------------------------------------
        # Bypass Armbian firstlogin — pre-create the radio user
        # --------------------------------------------------------
        Write-Host "Bypassing Armbian firstlogin wizard..."
        $notLoggedIn = Join-Path $rootPath "root\.not_logged_in_yet"
        if (Test-Path $notLoggedIn) { Remove-Item $notLoggedIn -Force }

        # Generate SHA-512 shadow hash
        Write-Host "Generating password hash for radio user..."
        $radioHash = Get-LinuxPasswordHash -password $Script:RADIO_PW
        if (-not $radioHash) {
            Write-Host "WARNING: Could not generate SHA-512 password hash." -ForegroundColor Yellow
            Write-Host "         openssl, WSL, and Python were all unavailable."
            Write-Host "         The radio user will be created without a password."
            Write-Host "         You will need to set it manually after first boot:"
            Write-Host "         (log in as root with password '1234', then: passwd radio)"
            $radioHash = "!"   # locked account placeholder
        }

        Write-Host "Creating radio user..."

        # /etc/passwd
        $passwdPath = Join-Path $rootPath "etc\passwd"
        $passwdContent = [System.IO.File]::ReadAllText($passwdPath)
        if ($passwdContent -notmatch "^radio:") {
            Add-Content -Path $passwdPath -Value "`nradio:x:1000:1000:radio:/home/radio:/bin/bash" -NoNewline
            [System.IO.File]::WriteAllText($passwdPath, ($passwdContent.TrimEnd() + "`nradio:x:1000:1000:radio:/home/radio:/bin/bash`n").Replace("`r`n", "`n"))
        }

        # /etc/group — add radio group and add radio to sudo group
        $groupPath = Join-Path $rootPath "etc\group"
        $groupContent = [System.IO.File]::ReadAllText($groupPath)
        if ($groupContent -notmatch "^radio:") {
            $groupContent += "radio:x:1000:`n"
        }
        # Add radio to the sudo group
        $groupContent = $groupContent -replace '(?m)^(sudo:x:\d+:)(.*)', '$1$2,radio' -replace ',radio$', 'radio' -replace ',,', ','
        [System.IO.File]::WriteAllText($groupPath, $groupContent.Replace("`r`n", "`n"))

        # /etc/shadow
        $shadowPath = Join-Path $rootPath "etc\shadow"
        $shadowContent = [System.IO.File]::ReadAllText($shadowPath)
        if ($shadowContent -notmatch "^radio:") {
            $shadowContent += "radio:${radioHash}:19700:0:99999:7:::`n"
            [System.IO.File]::WriteAllText($shadowPath, $shadowContent.Replace("`r`n", "`n"))
        }

        # /etc/sudoers.d/radio
        $sudoersDir = Join-Path $rootPath "etc\sudoers.d"
        if (-not (Test-Path $sudoersDir)) { New-Item -ItemType Directory -Path $sudoersDir | Out-Null }
        [System.IO.File]::WriteAllText((Join-Path $sudoersDir "radio"), "radio ALL=(ALL) NOPASSWD: ALL`n")

        # /home/radio directory
        $radioHome = Join-Path $rootPath "home\radio"
        if (-not (Test-Path $radioHome)) { New-Item -ItemType Directory -Path $radioHome | Out-Null }

        # --------------------------------------------------------
        # Install provisioning script from rock3a-provision.sh.template
        # --------------------------------------------------------
        Write-Host "Installing provisioning script from template..."
        if (-not (Test-Path $ROCK3A_TEMPLATE)) {
            throw "Rock 3A template file '$ROCK3A_TEMPLATE' not found in script directory."
        }

        $provisionScript = [System.IO.File]::ReadAllText($ROCK3A_TEMPLATE)
        $provisionScript = $provisionScript `
            -replace '__HARDWARE_MODEL__',    $Script:HARDWARE_MODEL `
            -replace '__EUD_CONNECTION__',    $Script:EUD_CONNECTION `
            -replace '__LAN_AP_SSID__',       $Script:LAN_AP_SSID `
            -replace '__LAN_AP_KEY__',        $Script:LAN_AP_KEY `
            -replace '__MAX_EUDS_PER_NODE__', $Script:MAX_EUDS_PER_NODE `
            -replace '__INSTALL_MEDIAMTX__',  $Script:INSTALL_MEDIAMTX `
            -replace '__INSTALL_MUMBLE__',    $Script:INSTALL_MUMBLE `
            -replace '__MESH_SSID__',         $Script:MESH_SSID `
            -replace '__MESH_SAE_KEY__',      $Script:MESH_SAE_KEY `
            -replace '__LAN_CIDR_BLOCK__',    $Script:LAN_CIDR_BLOCK `
            -replace '__AUTO_CHANNEL__',      $Script:AUTO_CHANNEL `
            -replace '__RADIO_PW__',          $Script:RADIO_PW `
            -replace '__REGULATORY_DOMAIN__', $Script:REGULATORY_DOMAIN `
            -replace '__ADMIN_PW__',          $Script:ADMIN_PW `
            -replace '__AUTO_UPDATE__',       $Script:AUTO_UPDATE

        $provisionScript = $provisionScript.Replace("`r`n", "`n")

        $usrLocalBin = Join-Path $rootPath "usr\local\bin"
        if (-not (Test-Path $usrLocalBin)) { New-Item -ItemType Directory -Path $usrLocalBin | Out-Null }
        [System.IO.File]::WriteAllText((Join-Path $usrLocalBin "provision-mesh.sh"), $provisionScript)

        # --------------------------------------------------------
        # Create systemd service (matches linux.sh exactly)
        # --------------------------------------------------------
        Write-Host "Creating mesh-provision systemd service..."
        $serviceContent = @"
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
"@
        $systemdDir = Join-Path $rootPath "etc\systemd\system"
        if (-not (Test-Path $systemdDir)) { New-Item -ItemType Directory -Path $systemdDir | Out-Null }
        [System.IO.File]::WriteAllText((Join-Path $systemdDir "mesh-provision.service"), $serviceContent.Replace("`r`n", "`n"))

        # Create the flag file that triggers provisioning on first boot
        Write-Host "Creating provisioning trigger flag..."
        [System.IO.File]::WriteAllText((Join-Path $rootPath "root\.mesh-not-provisioned"), "")

        # Enable the service by creating the symlink (as a file, since Windows can't make
        # Linux-style symlinks on a mounted ext4 volume; the systemd unit file is enough
        # because the service already has WantedBy=multi-user.target — Armbian will enable
        # it on first boot via the preset mechanism, or we create the wants symlink directly)
        $wantsDir = Join-Path $systemdDir "multi-user.target.wants"
        if (-not (Test-Path $wantsDir)) { New-Item -ItemType Directory -Path $wantsDir | Out-Null }
        # Write a small text file that acts as a placeholder; the real symlink will be
        # resolved on the target's first boot when systemd reads the unit.
        # Actually: copy the service file to the wants directory (not a symlink, but systemd
        # will accept a regular file as a unit override on Armbian/Debian).
        Copy-Item (Join-Path $systemdDir "mesh-provision.service") (Join-Path $wantsDir "mesh-provision.service")

        # --------------------------------------------------------
        # Unmount and flash
        # --------------------------------------------------------
        Write-Host "Unmounting image..."
        Dismount-DiskImage -ImagePath $tempImage | Out-Null
        Start-Sleep -Seconds 2

        # Final confirmation before writing to device
        Confirm-Flash -DiskNumber $Script:TARGET_DEVICE

        # Wipe and flash
        Write-Host "Wiping target disk..."
        $physDrive = "\\.\PhysicalDrive$($Script:TARGET_DEVICE)"
        Clear-Disk -Number $Script:TARGET_DEVICE -RemoveData -Confirm:$false -ErrorAction SilentlyContinue

        Write-Host "Flashing image to Disk $($Script:TARGET_DEVICE)..."
        $bufferSize = 4MB
        $buffer     = New-Object byte[] $bufferSize

        $src  = [System.IO.File]::OpenRead($tempImage)
        $dest = [System.IO.File]::Open($physDrive, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)

        try {
            $totalBytes = $src.Length
            $written    = 0
            while (($read = $src.Read($buffer, 0, $buffer.Length)) -gt 0) {
                $dest.Write($buffer, 0, $read)
                $written += $read
                $pct = [math]::Round(($written / $totalBytes) * 100, 1)
                Write-Progress -Activity "Flashing" -Status "$pct% complete" -PercentComplete $pct
            }
            $dest.Flush()
        } finally {
            $src.Close()
            $dest.Close()
            Write-Progress -Activity "Flashing" -Completed
        }

        Remove-Item $tempImage -Force -ErrorAction SilentlyContinue

        Write-Host ""
        Write-Host "==============================================" -ForegroundColor Green
        Write-Host "           ✅ Flash complete!" -ForegroundColor Green
        Write-Host "==============================================" -ForegroundColor Green
        Write-Host ""
        Write-Host "You can now remove the SD card and boot your Rock 3A."
        Write-Host "First boot provisioning will run automatically when connected to the internet."
        Write-Host ""
        Write-Host "  - Root password: 1234 (Armbian default — change this)"
        Write-Host "  - Radio user: radio / $($Script:RADIO_PW)"
        if ($radioHash -eq "!") {
            Write-Host ""
            Write-Host "  ⚠️  Password hash could not be generated." -ForegroundColor Yellow
            Write-Host "     Log in as root and run: passwd radio" -ForegroundColor Yellow
        }
        Write-Host ""

    } catch {
        Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red

        # Clean up on error
        try { Dismount-DiskImage -ImagePath $tempImage -ErrorAction SilentlyContinue | Out-Null } catch { }
        if (Test-Path $tempImage) { Remove-Item $tempImage -Force -ErrorAction SilentlyContinue }

        exit 1
    }

# ============================================================
# Raspberry Pi Flashing Path (all Pi models including CM4)
# ============================================================

} else {

    # Generate firstrun script from template
    Write-Host "Generating firstrun script from template..."
    $templateContent = [System.IO.File]::ReadAllText($TEMPLATE_FILE)

    $templateContent = $templateContent `
        -replace '__HARDWARE_MODEL__',    $Script:HARDWARE_MODEL `
        -replace '__EUD_CONNECTION__',    $Script:EUD_CONNECTION `
        -replace '__LAN_AP_SSID__',       $Script:LAN_AP_SSID `
        -replace '__LAN_AP_KEY__',        $Script:LAN_AP_KEY `
        -replace '__MAX_EUDS_PER_NODE__', $Script:MAX_EUDS_PER_NODE `
        -replace '__INSTALL_MEDIAMTX__',  $Script:INSTALL_MEDIAMTX `
        -replace '__INSTALL_MUMBLE__',    $Script:INSTALL_MUMBLE `
        -replace '__MESH_SSID__',         $Script:MESH_SSID `
        -replace '__MESH_SAE_KEY__',      $Script:MESH_SAE_KEY `
        -replace '__LAN_CIDR_BLOCK__',    $Script:LAN_CIDR_BLOCK `
        -replace '__AUTO_CHANNEL__',      $Script:AUTO_CHANNEL `
        -replace '__RADIO_PW__',          $Script:RADIO_PW `
        -replace '__REGULATORY_DOMAIN__', $Script:REGULATORY_DOMAIN `
        -replace '__ADMIN_PW__',          $Script:ADMIN_PW `
        -replace '__AUTO_UPDATE__',       $Script:AUTO_UPDATE

    $tempScript = [System.IO.Path]::GetTempFileName()
    [System.IO.File]::WriteAllText($tempScript, $templateContent.Replace("`r`n", "`n"))

    # Final confirmation
    Confirm-Flash -DiskNumber $Script:TARGET_DEVICE

    # Flash via rpi-imager
    Write-Host "Running Raspberry Pi Imager..."
    $targetDrive = "\\.\PhysicalDrive$($Script:TARGET_DEVICE)"
    & $Script:RPI_IMAGER_PATH --cli $OS_IMAGE_URL $targetDrive --first-run-script $tempScript

    Remove-Item $tempScript -Force -ErrorAction SilentlyContinue

    if ($LASTEXITCODE -eq 0) {
        Write-Host ""
        Write-Host "==============================================" -ForegroundColor Green
        Write-Host "           ✅ Flash complete!" -ForegroundColor Green
        Write-Host "==============================================" -ForegroundColor Green
        Write-Host ""
        Write-Host "You can now boot your Raspberry Pi."
        Write-Host "Connect Ethernet for internet access during first boot provisioning."
        Write-Host ""
        Write-Host "  - Radio user: radio / $($Script:RADIO_PW)"
        Write-Host ""
    } else {
        Write-Host "ERROR: rpi-imager exited with code $LASTEXITCODE" -ForegroundColor Red
        exit 1
    }
}
