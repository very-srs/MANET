#Requires -RunAsAdministrator
<#
.SYNOPSIS
    A script to image new mesh radio nodes on Windows
.DESCRIPTION
    Equivalent to linux.sh - flashes Raspberry Pi and Radxa Rock 3A devices
    with mesh network configurations. Rock 3A images are customised by mounting
    the Armbian ext4 root partition via Ext2Fsd. Raspberry Pi images are written
    with rpi-imager.
.NOTES
    Must be run as Administrator.
    Rock 3A support requires Ext2Fsd (https://sourceforge.net/projects/ext2fsd/).

    rpi-imager and rpiboot are located at run time rather than assumed to be on
    C:. If they are not found on any fixed drive, the user is asked with a
    file-picker window and the answer is remembered in
    .mesh-configs\tool-paths.json. See Find-ProgramPath.

    -NoRun loads the functions without running anything, so another script can
    dot-source this file and drive the flash itself. manet-flasher.ps1, the
    window front end, does exactly that: it is a front end over these
    functions, not a second flasher, so there is only ever one copy of the
    token substitution and the flashing logic to keep in step with linux.sh.
#>

param(
    # Load the functions and stop. For `. .\windows.ps1 -NoRun` from a host
    # script; without it the console flow runs as it always has.
    [switch]$NoRun
)

# --- Configuration ---
$ScriptDir          = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$TEMPLATE_FILE      = Join-Path $ScriptDir "firstrun.sh.template"
$ROCK3A_TEMPLATE    = Join-Path $ScriptDir "rock3a-provision.sh.template"
$CONFIG_DIR         = Join-Path $ScriptDir ".mesh-configs"

# Operator-supplied setup scripts. Anything dropped in here is baked into the
# generated firstrun.sh and runs once on the node after radio-setup finishes.
# See additional-scripts\README.md.
$ADDITIONAL_SCRIPTS_DIR = Join-Path $ScriptDir "additional-scripts"
# Warn above the first, refuse above the second. Neither is a limit imposed by
# rpi-imager or by FAT32 - the boot partition has ~512 MB and bash parses a
# multi-MB script without complaint. They exist because the whole generated
# file is held in memory as one .NET string here, and because a payload that
# large almost always wants to be fetched by the script at run time instead:
# the node has proven internet before these ever run.
# The line both templates carry, which the block replaces. An anchor rather
# than a plain append because neither template runs off the end: firstrun.sh
# has trailing completion echoes and rock3a-provision.sh ends with `reboot`, so
# anything tacked on after the last line would never execute.
$ADDITIONAL_SCRIPTS_ANCHOR     = '# >>> MANET_ADDITIONAL_SCRIPTS <<<'
# Interpreters present on a stock provisioned node (Debian 13). A script whose
# shebang names anything else is still embedded - an earlier script may well
# install it - but the operator is told, because the alternative is a script
# that fails at first boot with a bare exit 127.
$ADDITIONAL_SCRIPTS_NODE_INTERPRETERS = @(
    'sh','bash','dash','python','python3','perl','lua','awk','mawk')
$ADDITIONAL_SCRIPTS_WARN_BYTES = 262144      # 256 KB
$ADDITIONAL_SCRIPTS_MAX_BYTES  = 2097152     # 2 MB
$Script:ADDITIONAL_SCRIPTS     = @()         # filled by Test-AdditionalScripts

$ARMBIAN_IMAGE_URL      = "https://fi.mirror.armbian.de/dl/rock-3a/archive/Armbian_26.2.1_Rock-3a_trixie_vendor_6.1.115_minimal.img.xz"
$ARMBIAN_IMAGE_FILENAME = "Armbian_26.2.1_Rock-3a_trixie_vendor_6.1.115_minimal.img"
$Script:ARMBIAN_IMAGE   = ""   # Set by Get-ArmbianImage

$OS_IMAGE_URL  = "https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2025-10-02/2025-10-01-raspios-trixie-arm64-lite.img.xz"

$DEVICE_WAIT   = 8   # seconds to wait for the CM4 eMMC to enumerate after rpiboot

# Where a program location the user pointed us at gets remembered, so nobody is
# asked twice. Not a *.conf, so the saved-config picker does not list it.
$TOOL_PATH_CACHE = Join-Path $CONFIG_DIR "tool-paths.json"

# --- Global State ---
$Script:HARDWARE_MODEL    = ""
$Script:TARGET_DEVICE     = ""
$Script:EUD_CONNECTION    = ""
$Script:LAN_AP_SSID       = ""
$Script:LAN_AP_KEY        = ""
$Script:MAX_EUDS_PER_NODE = 0
$Script:INSTALL_MEDIAMTX  = ""
$Script:INSTALL_MUMBLE    = ""
$Script:VOICE_ENABLED     = "n"
$Script:MESH_SSID         = ""
$Script:MESH_SAE_KEY      = ""
$Script:LAN_CIDR_BLOCK    = ""
$Script:AUTO_CHANNEL      = ""
$Script:RADIO_PW          = ""
$Script:ADMIN_PW          = ""
$Script:AUTO_UPDATE       = ""
$Script:REGULATORY_DOMAIN = ""
$Script:HALOW_REGULATORY_DOMAIN = ""
$Script:RPI_IMAGER_PATH   = $null
$Script:RPIBOOT_PATH      = $null
# Set by New-Rock3aCustomImage. "!" means no hash tool was reachable and the
# radio account was left locked, which the completion message has to say.
$Script:R3A_RADIO_HASH    = ""


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

# A function rather than an inline list so the window front end can fill a
# drop-down from the same set the validator accepts.
function Get-ValidRegulatoryDomains {
    return @(
        "US","CA","GB","DE","FR","IT","ES","NL","BE","AT","CH","SE","NO","DK","FI",
        "PL","CZ","HU","GR","PT","IE","RO","BG","HR","SI","SK","LT","LV","EE","CY",
        "MT","LU","AU","NZ","JP","KR","TW","SG","MY","TH","PH","ID","VN","IN","CN",
        "BR","AR","MX","CL","CO","PE","ZA","IL","AE","SA","RU","UA","TR","EG","MA"
    )
}

function Test-RegulatoryDomain {
    param([string]$domain)
    $domain = $domain.ToUpper()
    if ((Get-ValidRegulatoryDomains) -contains $domain) { return $domain }
    return $null
}

function Test-EuHalowRegion {
    param([string]$domain)
    $euHalowDomains = @(
        "AT","BE","BG","HR","CY","CZ","DK","EE","FI","FR","DE","GR","HU","IE",
        "IT","LV","LT","LU","MT","NL","PL","PT","RO","SK","SI","ES","SE",
        "GB","CH","NO"
    )
    return $euHalowDomains -contains $domain.ToUpper()
}

function Get-HalowRegulatoryDomain {
    param([string]$wifiDomain)
    $wifiDomain = $wifiDomain.ToUpper()
    if (Test-EuHalowRegion -domain $wifiDomain) { return "EU" }
    return $wifiDomain
}

function Calculate-Capacity {
    param([string]$cidr, [int]$maxEuds)

    if ($cidr -notmatch '^(\d+\.\d+\.\d+\.\d+)/(\d+)$') { return $null }
    $ip     = $Matches[1]
    $prefix = [int]$Matches[2]

    $hostBits   = 32 - $prefix
    $totalHosts = [math]::Pow(2, $hostBits) - 2   # subtract network and broadcast
    $reserved   = 5

    $available = $totalHosts - $reserved
    if ($maxEuds -gt 0) {
        $maxNodes = [math]::Floor($available / (1 + $maxEuds))
        $eudPool  = $maxNodes * $maxEuds
    } else {
        $maxNodes = $available
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
# Tries openssl (Git for Windows) then WSL.
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
            if ($LASTEXITCODE -eq 0) {
                Write-Host "Decompression complete."
                return $true
            }
        }
    }

    # Try WSL xz
    $wsl = Get-Command wsl -ErrorAction SilentlyContinue
    if ($wsl) {
        Write-Host "Using WSL xz for decompression..."
        $wslInput = $CompressedPath -replace '\\', '/'
        if ($wslInput -match '^([A-Za-z]):(.*)') { $wslInput = "/mnt/$($Matches[1].ToLower())$($Matches[2])" }
        $wslOutput = $OutputPath -replace '\\', '/'
        if ($wslOutput -match '^([A-Za-z]):(.*)') { $wslOutput = "/mnt/$($Matches[1].ToLower())$($Matches[2])" }
        & wsl xz -dk $wslInput 2>$null
        if ($LASTEXITCODE -eq 0 -and (Test-Path $OutputPath)) {
            Write-Host "Decompression complete."
            return $true
        }
    }

    Write-Host "ERROR: Could not decompress .xz file." -ForegroundColor Red
    Write-Host "Please install 7-Zip (https://www.7-zip.org/) or enable WSL."
    return $false
}

# ============================================================
# Finding installed programs (rpi-imager, rpiboot)
# ============================================================
#
# These used to be a hardcoded list of C:\Program Files paths, which finds
# nothing on a machine set up any other way - a second hard drive, a
# non-default install folder, a portable copy. The search below walks every
# fixed drive instead, and when that still comes up empty it ASKS, with a
# file-picker window rather than a console prompt: the people running this are
# not comfortable typing paths into a terminal. What they pick is remembered in
# .mesh-configs\tool-paths.json so they are never asked twice.

# Runs a scriptblock on an STA thread and returns its last output value.
# WinForms dialogs require STA. Windows PowerShell 5.1 already is; pwsh 7 is
# MTA, where ShowDialog() can return immediately or hang, so there the work goes
# to a dedicated STA runspace. Arguments are passed explicitly because the
# scriptblock crosses a runspace boundary and takes no closure with it.
function Invoke-OnStaThread {
    param([scriptblock]$Action, [object[]]$Arguments = @())

    if ([System.Threading.Thread]::CurrentThread.GetApartmentState() -eq 'STA') {
        return (& $Action @Arguments)
    }

    $runspace = [runspacefactory]::CreateRunspace()
    $runspace.ApartmentState = 'STA'
    $runspace.ThreadOptions  = 'ReuseThread'
    $runspace.Open()

    $shell = [powershell]::Create()
    $shell.Runspace = $runspace
    $null = $shell.AddScript($Action.ToString())
    foreach ($arg in $Arguments) { $null = $shell.AddArgument($arg) }

    try {
        $out = $shell.Invoke()

        # Invoke() does NOT rethrow. A failure inside the runspace - WinForms
        # missing, no desktop session - lands in the error stream and returns an
        # empty collection. Without this it would look like a silent "user
        # cancelled" and the caller's console fallback, which exists for exactly
        # that machine, would never run.
        if ($out.Count -eq 0 -and $shell.HadErrors -and $shell.Streams.Error.Count -gt 0) {
            throw $shell.Streams.Error[0].Exception
        }

        if ($out.Count -gt 0) { return $out[$out.Count - 1] }
        return $null
    } finally {
        $shell.Dispose()
        $runspace.Close()
        $runspace.Dispose()
    }
}

# Every place a program might reasonably be installed. Deliberately shallow -
# a handful of Test-Path calls, no recursive disk crawl.
function Get-ProgramSearchRoots {
    $roots = New-Object System.Collections.Generic.List[string]

    foreach ($known in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:ProgramW6432)) {
        if ($known) { $roots.Add($known) }
    }
    if ($env:LOCALAPPDATA) { $roots.Add((Join-Path $env:LOCALAPPDATA 'Programs')) }
    if ($ScriptDir)        { $roots.Add($ScriptDir) }

    # Every fixed drive, not just the system one. A machine with a second hard
    # drive very often has the program under D:\Program Files, or plain D:\.
    $drives = @()
    try {
        $drives = @(Get-CimInstance -ClassName Win32_LogicalDisk -Filter "DriveType=3" -ErrorAction Stop |
                    Select-Object -ExpandProperty DeviceID)
    } catch {
        $drives = @(Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue |
                    Where-Object { $_.Root -match '^[A-Za-z]:\\$' } |
                    ForEach-Object { $_.Root.Substring(0, 2) })
    }
    foreach ($drive in $drives) {
        $roots.Add("$drive\Program Files")
        $roots.Add("$drive\Program Files (x86)")
        $roots.Add("$drive\")
    }

    return @($roots | Where-Object { $_ } | Select-Object -Unique)
}

function Get-CachedToolPath {
    param([string]$Key)

    if (-not (Test-Path $TOOL_PATH_CACHE)) { return $null }
    try {
        $cache = Get-Content $TOOL_PATH_CACHE -Raw | ConvertFrom-Json
        $value = $cache.$Key
        # Revalidate: a remembered path is stale if the program was moved or
        # uninstalled, and we would rather search again than fail at flash time.
        if ($value -and (Test-Path -LiteralPath $value -PathType Leaf)) { return $value }
    } catch { }
    return $null
}

function Set-CachedToolPath {
    param([string]$Key, [string]$Path)

    try {
        $cache = @{}
        if (Test-Path $TOOL_PATH_CACHE) {
            $existing = Get-Content $TOOL_PATH_CACHE -Raw | ConvertFrom-Json
            foreach ($property in $existing.PSObject.Properties) { $cache[$property.Name] = $property.Value }
        }
        $cache[$Key] = $Path

        $cacheDir = Split-Path -Parent $TOOL_PATH_CACHE
        if (-not (Test-Path $cacheDir)) { New-Item -ItemType Directory -Path $cacheDir -Force | Out-Null }
        ($cache | ConvertTo-Json) | Set-Content -Path $TOOL_PATH_CACHE -Encoding UTF8
    } catch {
        # Never let a cache write stop a flash - it only costs one extra prompt.
        Write-Host "(Could not save the $Key location for next time: $($_.Exception.Message))" -ForegroundColor DarkGray
    }
}

# Forget a remembered location. Called when the program turns out not to run,
# so a bad answer costs one failed attempt rather than wedging every future run
# behind a JSON file the user will never find.
function Remove-CachedToolPath {
    param([string]$Key, [string]$Reason = '')

    if (-not (Test-Path $TOOL_PATH_CACHE)) { return }
    try {
        $cache    = @{}
        $existing = Get-Content $TOOL_PATH_CACHE -Raw | ConvertFrom-Json
        foreach ($property in $existing.PSObject.Properties) {
            if ($property.Name -ne $Key) { $cache[$property.Name] = $property.Value }
        }
        ($cache | ConvertTo-Json) | Set-Content -Path $TOOL_PATH_CACHE -Encoding UTF8

        Write-Host ""
        Write-Host "Forgetting the remembered location for '$Key'$(if ($Reason) { " - $Reason" })." -ForegroundColor Yellow
        Write-Host "You will be asked for it again next time you run this script."
    } catch { }
}

# The "we couldn't find it" window. Three plainly-worded buttons instead of a
# stock Yes/No/Cancel box. Returns 'browse', 'download' or 'quit'.
function Show-ProgramNotFoundWindow {
    param([string]$DisplayName, [string]$UsualLocation, [string]$DownloadUrl)

    $dialog = {
        param($DisplayName, $UsualLocation, $DownloadUrl)

        Add-Type -AssemblyName System.Windows.Forms
        Add-Type -AssemblyName System.Drawing

        $form                 = New-Object System.Windows.Forms.Form
        $form.Text            = "$DisplayName was not found"
        $form.ClientSize      = New-Object System.Drawing.Size(480, 235)
        $form.FormBorderStyle = 'FixedDialog'
        $form.StartPosition   = 'CenterScreen'
        $form.MaximizeBox     = $false
        $form.MinimizeBox     = $false
        $form.TopMost         = $true

        $lines = @(
            "This script needs $DisplayName to flash the card, but it is not",
            "installed in any of the usual places.",
            "",
            "It is normally found in:",
            "     $UsualLocation",
            "",
            "If you know it is installed somewhere else on this computer - on a",
            "second hard drive, say - choose 'Find it for me' and point to it.",
            "",
            "If it is not installed at all, choose 'Download it' to open:",
            "     $DownloadUrl"
        )

        $label          = New-Object System.Windows.Forms.Label
        $label.Location = New-Object System.Drawing.Point(16, 16)
        $label.Size     = New-Object System.Drawing.Size(450, 165)
        $label.Text     = ($lines -join "`r`n")
        $form.Controls.Add($label)

        $browse              = New-Object System.Windows.Forms.Button
        $browse.Text         = 'Find it for me...'
        $browse.Location     = New-Object System.Drawing.Point(16, 190)
        $browse.Size         = New-Object System.Drawing.Size(140, 30)
        $browse.DialogResult = [System.Windows.Forms.DialogResult]::OK

        $download              = New-Object System.Windows.Forms.Button
        $download.Text         = 'Download it'
        $download.Location     = New-Object System.Drawing.Point(170, 190)
        $download.Size         = New-Object System.Drawing.Size(140, 30)
        $download.DialogResult = [System.Windows.Forms.DialogResult]::Retry

        $quit              = New-Object System.Windows.Forms.Button
        $quit.Text         = 'Quit'
        $quit.Location     = New-Object System.Drawing.Point(345, 190)
        $quit.Size         = New-Object System.Drawing.Size(120, 30)
        $quit.DialogResult = [System.Windows.Forms.DialogResult]::Cancel

        $form.Controls.AddRange(@($browse, $download, $quit))
        $form.AcceptButton = $browse
        $form.CancelButton = $quit

        switch ($form.ShowDialog()) {
            ([System.Windows.Forms.DialogResult]::OK)    { 'browse' }
            ([System.Windows.Forms.DialogResult]::Retry) { 'download' }
            default                                     { 'quit' }
        }
    }

    try {
        return (Invoke-OnStaThread -Action $dialog -Arguments @($DisplayName, $UsualLocation, $DownloadUrl))
    } catch {
        # No GUI available (Server Core, a stripped PowerShell). Fall back to
        # the console rather than dying.
        Write-Host ""
        Write-Host "$DisplayName was not found. It normally lives in $UsualLocation" -ForegroundColor Yellow
        Write-Host "  1. Let me type where it is"
        Write-Host "  2. Open the download page"
        Write-Host "  3. Quit"
        switch (Read-Host "Choose 1-3") {
            "1"     { return 'browse' }
            "2"     { return 'download' }
            default { return 'quit' }
        }
    }
}

function Show-ProgramFilePicker {
    param([string]$DisplayName, [string]$Filter, [string]$InitialDirectory)

    $dialog = {
        param($DisplayName, $Filter, $InitialDirectory)

        Add-Type -AssemblyName System.Windows.Forms

        $picker                 = New-Object System.Windows.Forms.OpenFileDialog
        $picker.Title           = "Show me where $DisplayName is installed"
        $picker.Filter          = $Filter
        $picker.CheckFileExists = $true
        $picker.Multiselect     = $false
        if ($InitialDirectory -and (Test-Path $InitialDirectory)) {
            $picker.InitialDirectory = $InitialDirectory
        }

        # An elevated console can leave the dialog behind other windows, which
        # looks to the user like nothing happened. Owning it to a topmost form
        # keeps it in front.
        $owner         = New-Object System.Windows.Forms.Form
        $owner.TopMost = $true

        if ($picker.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
            $picker.FileName
        } else {
            ''
        }
    }

    try {
        $picked = Invoke-OnStaThread -Action $dialog -Arguments @($DisplayName, $Filter, $InitialDirectory)
        if ($picked) { return [string]$picked }
        return $null
    } catch {
        Write-Host "Could not open a file browser window: $($_.Exception.Message)" -ForegroundColor Yellow
        $typed = Read-Host "Enter the full path to $DisplayName (or press Enter to give up)"
        if ($typed) { return $typed.Trim().Trim('"') }
        return $null
    }
}

# Picked a file that is not named anything like the program we asked for -
# check rather than failing confusingly later.
function Confirm-UnexpectedProgram {
    param([string]$DisplayName, [string]$FileName)

    $dialog = {
        param($DisplayName, $FileName)

        Add-Type -AssemblyName System.Windows.Forms
        $owner         = New-Object System.Windows.Forms.Form
        $owner.TopMost = $true

        [System.Windows.Forms.MessageBox]::Show($owner,
            "You picked '$FileName', which does not look like $DisplayName.`r`n`r`nUse it anyway?",
            "Is this the right program?",
            [System.Windows.Forms.MessageBoxButtons]::YesNo,
            [System.Windows.Forms.MessageBoxIcon]::Warning) -eq [System.Windows.Forms.DialogResult]::Yes
    }

    try {
        return [bool](Invoke-OnStaThread -Action $dialog -Arguments @($DisplayName, $FileName))
    } catch {
        $reply = Read-Host "'$FileName' does not look like $DisplayName. Use it anyway? (y/N)"
        return ($reply -match '^[Yy]')
    }
}

# Try the expected locations, then ask. Returns the full path, or $null if the
# user gave up or chose to go and install it.
function Find-ProgramPath {
    param(
        [string]  $Key,
        [string]  $DisplayName,
        [string[]]$CommandNames  = @(),
        [string[]]$RelativePaths = @(),
        [string]  $NamePattern   = '*',
        [string]  $Filter        = 'Programs (*.exe;*.cmd;*.bat)|*.exe;*.cmd;*.bat|All files (*.*)|*.*',
        [string]  $DownloadUrl   = '',
        [string]  $UsualLocation = ''
    )

    # 1. Where the user pointed us last time.
    $cached = Get-CachedToolPath -Key $Key
    if ($cached) {
        Write-Host "Found $DisplayName (remembered): $cached"
        return $cached
    }

    # 2. On PATH.
    foreach ($name in $CommandNames) {
        $onPath = (Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1).Source
        if ($onPath -and (Test-Path -LiteralPath $onPath -PathType Leaf)) {
            Write-Host "Found $DisplayName on PATH: $onPath"
            return $onPath
        }
    }

    # 3. Under every plausible install root, on every fixed drive.
    Write-Host "Looking for $DisplayName..."
    foreach ($root in (Get-ProgramSearchRoots)) {
        foreach ($relative in $RelativePaths) {
            $candidate = Join-Path $root $relative
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                Write-Host "Found ${DisplayName}: $candidate"
                return $candidate
            }
        }
    }

    # 4. Ask.
    while ($true) {
        $answer = Show-ProgramNotFoundWindow -DisplayName $DisplayName `
                                             -UsualLocation $UsualLocation `
                                             -DownloadUrl   $DownloadUrl

        if ($answer -eq 'download') {
            if ($DownloadUrl) {
                Write-Host "Opening $DownloadUrl in your browser..."
                try { Start-Process $DownloadUrl } catch {
                    Write-Host "Could not open a browser. Go to: $DownloadUrl" -ForegroundColor Yellow
                }
            }
            Write-Host "Install $DisplayName, then run this script again." -ForegroundColor Yellow
            return $null
        }
        if ($answer -ne 'browse') { return $null }

        $picked = Show-ProgramFilePicker -DisplayName $DisplayName -Filter $Filter -InitialDirectory $env:ProgramFiles
        if (-not $picked) { return $null }

        if (-not (Test-Path -LiteralPath $picked -PathType Leaf)) {
            Write-Host "That file does not exist: $picked" -ForegroundColor Red
            continue
        }

        $leaf     = Split-Path -Leaf $picked
        $nameOkay = ($NamePattern -eq '*' -or $leaf -like $NamePattern)

        if (-not $nameOkay) {
            if (-not (Confirm-UnexpectedProgram -DisplayName $DisplayName -FileName $leaf)) { continue }
        }

        # Only remember a pick that looks like the right program. An override is
        # honoured for this run but deliberately NOT persisted: if someone picks
        # notepad.exe and clicks through the warning, that must not become the
        # remembered answer on every future run.
        if ($nameOkay) {
            Set-CachedToolPath -Key $Key -Path $picked
        } else {
            Write-Host "Using '$leaf' for this run only - it will not be remembered." -ForegroundColor Yellow
        }

        Write-Host "Using $DisplayName at: $picked" -ForegroundColor Green
        return $picked
    }
}

# CM4 only: put the module into USB-boot mode so its eMMC appears as a disk.
# linux.sh runs rpiboot itself; this does the same rather than sending the user
# off to run a second program by hand.
function Invoke-RpiBoot {
    Write-Host ""
    Write-Host "--- Compute Module 4: USB boot ---"

    $Script:RPIBOOT_PATH = Find-ProgramPath `
        -Key           'rpiboot' `
        -DisplayName   'rpiboot' `
        -CommandNames  @('rpiboot.exe', 'rpiboot') `
        -RelativePaths @(
            'Raspberry Pi\rpiboot.exe',
            'Raspberry Pi\usbboot\rpiboot.exe',
            'Raspberry Pi Ltd\rpiboot\rpiboot.exe',
            'usbboot\rpiboot.exe',
            'rpiboot\rpiboot.exe',
            'rpiboot.exe'
        ) `
        -NamePattern   'rpiboot*' `
        -Filter        'rpiboot (rpiboot.exe)|rpiboot.exe|Programs (*.exe)|*.exe|All files (*.*)|*.*' `
        -DownloadUrl   'https://github.com/raspberrypi/usbboot/releases' `
        -UsualLocation 'C:\Program Files (x86)\Raspberry Pi\rpiboot.exe'

    if (-not $Script:RPIBOOT_PATH) {
        # Not found and not supplied - fall back to the old manual flow rather
        # than dead-ending, since the eMMC may already be mounted.
        Write-Host ""
        Write-Host "Carrying on without rpiboot." -ForegroundColor Yellow
        Write-Host "Run rpiboot yourself now, if the CM4 eMMC is not already showing as a drive."
        Read-Host "Press Enter when the CM4 eMMC is mounted and ready"
        return
    }

    Write-Host ""
    Write-Host "Connect the CM4 to this computer in USB-boot mode:"
    Write-Host "  1. Fit the boot jumper (nRPIBOOT to GND) on the carrier board"
    Write-Host "  2. Plug the USB cable into the carrier's USB slave port"
    Write-Host "  3. Power the board on"
    Read-Host "Press Enter to run rpiboot"

    $disksBefore = @(Get-Disk | Select-Object -ExpandProperty Number)

    Write-Host "Running rpiboot..."
    try {
        & $Script:RPIBOOT_PATH
        if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {
            # Ambiguous on its own - rpiboot also exits non-zero when no module
            # is connected - so this warns but keeps the remembered path.
            Write-Host "WARNING: rpiboot exited with code $LASTEXITCODE." -ForegroundColor Yellow
        }
    } catch {
        Write-Host "ERROR: could not run $($Script:RPIBOOT_PATH)" -ForegroundColor Red
        Write-Host "       $($_.Exception.Message)" -ForegroundColor Red
        Remove-CachedToolPath -Key 'rpiboot' -Reason "it could not be run"
    }

    Write-Host "Waiting $DEVICE_WAIT seconds for the eMMC to appear..."
    Start-Sleep -Seconds $DEVICE_WAIT

    $disksAfter = @(Get-Disk | Select-Object -ExpandProperty Number)
    $newDisks   = @($disksAfter | Where-Object { $disksBefore -notcontains $_ })

    if ($newDisks.Count -gt 0) {
        foreach ($number in $newDisks) {
            $disk = Get-Disk -Number $number
            Write-Host ("Detected CM4 eMMC: Disk {0} - {1} ({2}GB)" -f `
                $disk.Number, $disk.FriendlyName, [math]::Round($disk.Size / 1GB, 2)) -ForegroundColor Green
        }
    } else {
        Write-Host "No new disk appeared." -ForegroundColor Yellow
        Write-Host "If the eMMC was already mounted from an earlier run, carry on and pick it below."
        Write-Host "Otherwise check the boot jumper and the USB cable, and start again."
        Read-Host "Press Enter to continue"
    }
}

# ============================================================
# Armbian Image Acquisition (Rock 3A)
# ============================================================

function Test-ArmbianChecksum {
    param([string]$ImagePath, [string]$ChecksumFile)

    if (-not (Test-Path $ChecksumFile)) {
        return $true
    }

    Write-Host "Verifying image checksum..."
    $expected = (Get-Content $ChecksumFile -Raw).Trim() -replace '\s.*', ''
    $actual   = (Get-FileHash -Algorithm SHA256 -Path $ImagePath).Hash.ToLower()

    if ($expected.ToLower() -eq $actual) {
        Write-Host "Checksum OK."
        return $true
    } else {
        Write-Host "ERROR: Checksum mismatch!" -ForegroundColor Red
        Write-Host "  Expected: $expected"
        Write-Host "  Actual:   $actual"
        return $false
    }
}

function Save-ArmbianChecksum {
    param([string]$ImagePath, [string]$ChecksumFile)

    Write-Host "Saving checksum to $ChecksumFile..."
    $hash = (Get-FileHash -Algorithm SHA256 -Path $ImagePath).Hash.ToLower()
    [System.IO.File]::WriteAllText($ChecksumFile, "$hash`n")
}

function Get-ArmbianImage {
    Write-Host ""
    Write-Host "--- Armbian Image Setup for Rock 3A ---"

    $localImage      = Join-Path $ScriptDir $ARMBIAN_IMAGE_FILENAME
    $localCompressed = Join-Path $ScriptDir "${ARMBIAN_IMAGE_FILENAME}.xz"
    $checksumFile    = Join-Path $ScriptDir "${ARMBIAN_IMAGE_FILENAME}.sha256"

    if (Test-Path $localImage) {
        Write-Host "Found local Armbian image: $localImage"
        if (Test-ArmbianChecksum -ImagePath $localImage -ChecksumFile $checksumFile) {
            $Script:ARMBIAN_IMAGE = $localImage
            return $true
        } else {
            Write-Host "Local image failed checksum - re-downloading."
            Remove-Item $localImage -Force
        }
    }

    if (Test-Path $localCompressed) {
        Write-Host "Found compressed Armbian image: $localCompressed"
        Write-Host "Decompressing (this may take a moment)..."
        $result = Expand-XzFile -CompressedPath $localCompressed -OutputPath $localImage
        if ($result) {
            if (Test-ArmbianChecksum -ImagePath $localImage -ChecksumFile $checksumFile) {
                $Script:ARMBIAN_IMAGE = $localImage
                return $true
            } else {
                Write-Host "Decompressed image failed checksum - re-downloading."
                Remove-Item $localImage -Force -ErrorAction SilentlyContinue
                Remove-Item $localCompressed -Force -ErrorAction SilentlyContinue
            }
        } else {
            return $false
        }
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
            "1" {
                $ok = Download-ArmbianImage
                if ($ok) { Save-ArmbianChecksum -ImagePath $Script:ARMBIAN_IMAGE -ChecksumFile $checksumFile }
                return $ok
            }
            "2" {
                $ok = Select-CustomArmbianImage
                if ($ok) { Save-ArmbianChecksum -ImagePath $Script:ARMBIAN_IMAGE -ChecksumFile $checksumFile }
                return $ok
            }
            default { Write-Host "Invalid selection. Please enter 1 or 2." }
        }
    }
}

function Download-ArmbianImage {
    $compressedFile  = Join-Path $ScriptDir "${ARMBIAN_IMAGE_FILENAME}.xz"
    $outputFile      = Join-Path $ScriptDir $ARMBIAN_IMAGE_FILENAME

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
    $svc = Get-Service -Name "Ext2Srv" -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -eq "Running") { return $true }

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

# Every disk that could plausibly be the card, as objects rather than printed
# lines, so the console selector and the window front end describe a disk the
# same way. Bus type and volume labels are gathered because "Disk 2 -
# 29.7GB" is not enough for someone who does not already know what their
# disks are called, and this list is about to be erased.
function Get-CandidateDisks {
    $bootDisk = (Get-Disk | Where-Object { $_.IsBoot -eq $true } | Select-Object -First 1).Number

    $disks = @(Get-Disk | Where-Object {
        $_.Number -ne $bootDisk -and
        $_.OperationalStatus -eq "Online" -and
        $_.Size -gt 0
    })

    foreach ($disk in $disks) {
        # Drive letters and labels, when Windows has mounted anything. A card
        # that has been flashed before shows up as "bootfs", which is the
        # clearest possible confirmation that the right disk is selected.
        $labels = @()
        try {
            $labels = @(Get-Partition -DiskNumber $disk.Number -ErrorAction Stop |
                        Where-Object { $_.DriveLetter -and $_.DriveLetter -ne "`0" } |
                        ForEach-Object {
                            $vol = Get-Volume -DriveLetter $_.DriveLetter -ErrorAction SilentlyContinue
                            if ($vol -and $vol.FileSystemLabel) {
                                "$($_.DriveLetter): $($vol.FileSystemLabel)"
                            } else {
                                "$($_.DriveLetter):"
                            }
                        })
        } catch { }

        $bus = if ($disk.BusType) { [string]$disk.BusType } else { "Unknown" }

        # Removable media is what we expect: an SD card, a USB reader, or a
        # CM4 eMMC exposed by rpiboot (which enumerates as USB). Anything else
        # is very likely a real hard drive that the operator wants to keep.
        $removable = ($bus -in @('USB', 'SD', 'MMC'))

        [pscustomobject]@{
            Number       = $disk.Number
            FriendlyName = $disk.FriendlyName
            SizeGB       = [math]::Round($disk.Size / 1GB, 2)
            BusType      = $bus
            IsRemovable  = $removable
            IsSystem     = [bool]$disk.IsSystem
            Volumes      = $labels
            Description  = ("Disk {0}: {1} - {2}GB [{3}]{4}" -f `
                                $disk.Number, $disk.FriendlyName,
                                [math]::Round($disk.Size / 1GB, 2), $bus,
                                $(if ($labels.Count) { "  (" + ($labels -join ', ') + ")" } else { "" }))
        }
    }
}

# The console target picker, used for the first card and for every "flash
# another" afterwards. Returns a disk number, or $null if the user chose to
# stop. This was written out three times with slightly different wording;
# one copy is enough.
function Select-TargetDiskConsole {
    param([string]$LastChoiceLabel = "Quit")

    $disks = @(Get-CandidateDisks)
    if ($disks.Count -eq 0) {
        Write-Host "ERROR: No suitable target devices found." -ForegroundColor Red
        Write-Host "Please ensure your SD card reader, USB drive, or CM4 eMMC is connected."
        return $null
    }

    Write-Host "Available devices:"
    $i = 1; $diskMap = @{}
    foreach ($disk in $disks) {
        $warn = if ($disk.IsRemovable) { "" } else { "   <-- NOT removable media, check this is right" }
        Write-Host "$i. $($disk.Description)$warn"
        $diskMap[$i] = $disk; $i++
    }
    Write-Host "$i. $LastChoiceLabel"

    while ($true) {
        $c = Read-Host "Enter device number (1-$i)"
        $n = 0
        if ([int]::TryParse($c, [ref]$n)) {
            if ($n -eq $i) { return $null }
            if ($diskMap.ContainsKey($n)) {
                Write-Host "Selected: $($diskMap[$n].Description)"
                return $diskMap[$n].Number
            }
        }
        Write-Host "Invalid selection." -ForegroundColor Red
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
            "1" { $Script:HARDWARE_MODEL = "r3a";  break }
            "2" { $Script:HARDWARE_MODEL = "rpi5"; break }
            "3" { $Script:HARDWARE_MODEL = "rpi4"; break }
            "4" { $Script:HARDWARE_MODEL = "rpi4"; break }
        }
    } while ($choice -notmatch "^[1234]$")

    if ($Script:HARDWARE_MODEL -ne "r3a") {
        $Script:RPI_IMAGER_PATH = Find-ProgramPath `
            -Key           'rpi-imager' `
            -DisplayName   'Raspberry Pi Imager' `
            -CommandNames  @('rpi-imager-cli.cmd', 'rpi-imager.exe', 'rpi-imager') `
            -RelativePaths @(
                'Raspberry Pi Ltd\Imager\rpi-imager-cli.cmd',
                'Raspberry Pi Ltd\Imager\rpi-imager.exe',
                'Raspberry Pi Imager\rpi-imager.exe',
                'Raspberry Pi\Imager\rpi-imager.exe'
            ) `
            -NamePattern   'rpi-imager*' `
            -Filter        'Raspberry Pi Imager|rpi-imager*.exe;rpi-imager*.cmd|Programs (*.exe;*.cmd)|*.exe;*.cmd|All files (*.*)|*.*' `
            -DownloadUrl   'https://www.raspberrypi.com/software/' `
            -UsualLocation 'C:\Program Files\Raspberry Pi Ltd\Imager\'

        if (-not $Script:RPI_IMAGER_PATH) {
            Write-Host ""
            Write-Host "ERROR: Raspberry Pi Imager is needed to flash the card, and was not found." -ForegroundColor Red
            Write-Host "Install it from https://www.raspberrypi.com/software/ and run this script again."
            exit 1
        }
    }

    if ($choice -eq "4") {
        Invoke-RpiBoot
    }

    Write-Host ""
    Write-Host "--- 2. Select Target Device ---"

    $picked = Select-TargetDiskConsole -LastChoiceLabel "Quit"
    if ($null -eq $picked) { Write-Host "Aborting."; exit 1 }
    $Script:TARGET_DEVICE = $picked
}

# ============================================================
# Operator setup scripts
# ============================================================
# These are embedded in the generated firstrun.sh as one quoted heredoc per
# file, and written out to /var/lib/manet-user-scripts on the node's first
# boot. manet-user-scripts.service runs them once, after radio-setup has
# finished (radio-setup.sh starts it as its last act).
#
# Validation happens BEFORE any card is touched. Baking a syntactically broken
# script into an image is a wasted flash and a node that silently does not do
# what the operator asked; failing here costs nothing.

# Read an operator script and put right the things a Windows editor does to a
# file without telling anyone. Returns the corrected text, a list of what was
# corrected so it can be reported, and a hard error for the things that cannot
# be guessed at.
#
# The corrections happen in memory. The operator's file is never rewritten:
# they wrote it, and a flasher that silently edits the input is worse than one
# that explains itself.
function Read-AdditionalScriptContent {
    param([string]$Path)

    $result = @{ Ok = $true; Reason = ''; Text = ''; Fixes = @() }
    $fixes  = New-Object System.Collections.ArrayList

    # Not FileInfo.Length: Get-ChildItem fills that from the directory entry,
    # which Windows updates lazily, so a file an editor has only just written
    # can still read as zero bytes.
    $bytes = [System.IO.File]::ReadAllBytes($Path)

    if ($bytes.Length -eq 0) {
        return @{ Ok = $false; Reason = 'empty file'; Text = ''; Fixes = @() }
    }

    # Encoding, decided by the byte order mark the editor left behind. Notepad
    # writes one for "UTF-8 with BOM" and for "Unicode", which is UTF-16 LE.
    $strict = New-Object System.Text.UTF8Encoding($false, $true)
    $text   = $null

    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        # A BOM sits in front of the shebang, where the kernel does not look,
        # so the node would run nothing at all.
        try { $text = $strict.GetString($bytes, 3, $bytes.Length - 3) }
        catch { return @{ Ok = $false; Reason = 'not valid UTF-8 text'; Text = ''; Fixes = @() } }
        [void]$fixes.Add('byte order mark removed')

    } elseif ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) {
        $text = (New-Object System.Text.UnicodeEncoding($false, $false)).GetString($bytes, 2, $bytes.Length - 2)
        [void]$fixes.Add('converted from UTF-16')

    } elseif ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFE -and $bytes[1] -eq 0xFF) {
        $text = (New-Object System.Text.UnicodeEncoding($true, $false)).GetString($bytes, 2, $bytes.Length - 2)
        [void]$fixes.Add('converted from UTF-16')

    } else {
        # Strict: throw on anything that is not valid UTF-8 rather than
        # silently substituting U+FFFD and shipping mangled bytes to the node.
        # Nothing is guessed here. An encoding with no mark could be any of a
        # dozen code pages and picking wrong corrupts the script quietly.
        try { $text = $strict.GetString($bytes) }
        catch { return @{ Ok = $false; Reason = 'not valid UTF-8 text'; Text = ''; Fixes = @() } }
    }

    # After decoding, not before: a UTF-16 file is full of zero bytes that are
    # not NULs in the text at all. A heredoc is a byte stream through bash, and
    # bash cannot carry a real NUL. This is the one hard technical limit.
    $nulAt = $text.IndexOf([char]0)
    if ($nulAt -ge 0) {
        return @{ Ok = $false
                  Reason = "binary content (NUL byte at offset $nulAt) - fetch binaries at run time"
                  Text = ''; Fixes = @() }
    }

    # A shebang with a trailing CR makes the kernel look for an interpreter
    # whose name ends in CR, and the script dies with a bare "not found" that
    # names the right path.
    $lf = $text -replace "`r`n", "`n" -replace "`r", "`n"
    if ($lf -ne $text) { [void]$fixes.Add('Windows line endings converted') }
    $text = $lf

    # A file with no trailing newline would glue the heredoc delimiter onto its
    # last line, and the delimiter would not be recognised.
    if (-not $text.EndsWith("`n")) {
        $text += "`n"
        [void]$fixes.Add('missing final newline added')
    }

    $result.Text  = $text
    $result.Fixes = $fixes.ToArray()
    return $result
}

# Run a checker and come back whatever it does. Every one of these is a child
# process started from the window thread, so an unbounded wait is a locked up
# window with no way out but Task Manager: wsl.exe with no distribution
# installed can sit waiting on the Microsoft Store rather than returning.
#
# Output is redirected to files rather than captured with 2>&1. Windows
# PowerShell 5.1 turns each stderr line of a native command into an ErrorRecord
# when redirected that way, and the window's ErrorActionPreference of 'Stop'
# makes the first one terminating, so a script with a syntax error would take
# the flasher down instead of being reported in a column.
function Invoke-CheckerProcess {
    param([string]$Path, [string[]]$Arguments = @(), [int]$TimeoutSeconds = 15)

    $outFile = [System.IO.Path]::GetTempFileName()
    $errFile = [System.IO.Path]::GetTempFileName()

    try {
        $argLine = (@($Arguments | ForEach-Object {
            if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
        }) -join ' ')

        $splat = @{
            FilePath               = $Path
            NoNewWindow            = $true
            PassThru               = $true
            RedirectStandardOutput = $outFile
            RedirectStandardError  = $errFile
        }
        if ($argLine) { $splat['ArgumentList'] = $argLine }

        $proc = Start-Process @splat
        if (-not $proc.WaitForExit($TimeoutSeconds * 1000)) {
            try { $proc.Kill() } catch { }
            return @{ TimedOut = $true; ExitCode = -1; Output = '' }
        }

        $text = ''
        foreach ($f in @($outFile, $errFile)) {
            $part = Get-Content -LiteralPath $f -Raw -ErrorAction SilentlyContinue
            if ($part) { $text += $part }
        }
        return @{ TimedOut = $false; ExitCode = $proc.ExitCode; Output = $text.Trim() }

    } catch {
        # Could not be started at all. Treated as "no checker here" rather than
        # as a fault in the operator's script.
        return @{ TimedOut = $false; ExitCode = -1; Output = ''; Failed = $true }
    } finally {
        Remove-Item $outFile, $errFile -Force -ErrorAction SilentlyContinue
    }
}

# Windows 11 ships wsl.exe whether or not a distribution was ever installed,
# and System32\bash.exe is that same launcher rather than a shell. Both answer
# Get-Command and both then complain instead of parsing anything. Their
# complaint is not a syntax error in the operator's script, and reporting it as
# one would be a wrong FAIL that blocks a flash.
function Test-WslUnavailable {
    param([string]$Text)
    return ($Text -match 'no installed distributions|WslRegisterDistribution|Windows Subsystem for Linux|wsl\.exe --install|has not been installed')
}

# The first bash on PATH that is a real shell. Anything under System32 is the
# WSL launcher wearing the name, and starting it on a machine with no
# distribution is exactly the call that hangs.
function Get-RealBashPath {
    # Guarded: an unset SystemRoot would otherwise take Join-Path, and this
    # whole function, down with it.
    $system32 = if ($env:SystemRoot) { Join-Path $env:SystemRoot 'System32' } else { $null }
    foreach ($c in @(Get-Command bash -All -ErrorAction SilentlyContinue)) {
        if (-not $c.Source) { continue }
        if ($system32 -and $c.Source -like "$system32*") { continue }
        return $c.Source
    }
    foreach ($p in @("$env:ProgramFiles\Git\bin\bash.exe",
                     "$env:ProgramFiles\Git\usr\bin\bash.exe",
                     "${env:ProgramFiles(x86)}\Git\bin\bash.exe")) {
        if (Test-Path -LiteralPath $p -PathType Leaf) { return $p }
    }
    return $null
}

# Syntax-check a shell script if any Linux shell is reachable from Windows.
# Returns "" for clean, the message for a syntax error, and $null when there is
# nothing here that can check it.
function Test-ShellSyntax {
    param([string]$Path)

    # Git for Windows ships bash, and it is the common case on a machine that
    # already has rpi-imager and Git installed.
    $bash = Get-RealBashPath
    if ($bash) {
        $r = Invoke-CheckerProcess -Path $bash -Arguments @('-n', $Path)
        if ($r.TimedOut -or $r.Failed) { return $null }
        if ($r.ExitCode -eq 0) { return "" }
        if (Test-WslUnavailable -Text $r.Output) { return $null }
        # bash prefixes the message with the full path; the filename is already
        # in the column to the left of this, so strip it. Matches linux.sh.
        return ((($r.Output -split "`n")[0]).Trim() -replace '^[^:]*: ', '')
    }

    $wsl = Get-Command wsl -ErrorAction SilentlyContinue
    if ($wsl -and $wsl.Source) {
        $wslPath = $Path -replace '\\', '/'
        if ($wslPath -match '^([A-Za-z]):(.*)') {
            $wslPath = "/mnt/$($Matches[1].ToLower())$($Matches[2])"
        }
        $r = Invoke-CheckerProcess -Path $wsl.Source -Arguments @('bash', '-n', $wslPath)
        if ($r.TimedOut -or $r.Failed) { return $null }
        if ($r.ExitCode -eq 0) { return "" }
        if (Test-WslUnavailable -Text $r.Output) { return $null }
        return ((($r.Output -split "`n")[0]).Trim() -replace '^[^:]*: ', '')
    }

    return $null
}

function Test-AdditionalScript {
    param([System.IO.FileInfo]$File)

    # Same reason as Test-ShellSyntax: the python checker below is a native
    # command whose stderr is redirected, and under the window's Stop
    # preference a script with a syntax error would abort the flasher rather
    # than be reported as one.
    $ErrorActionPreference = 'Continue'

    # Filename becomes a path on the node and appears in a shell heredoc
    # header, so keep it boring.
    if ($File.Name -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') {
        return @{ Verdict = 'FAIL'; Reason = 'filename must match [A-Za-z0-9][A-Za-z0-9._-]*' }
    }

    $read = Read-AdditionalScriptContent -Path $File.FullName
    if (-not $read.Ok) { return @{ Verdict = 'FAIL'; Reason = $read.Reason } }

    $text  = $read.Text
    $fixed = if ($read.Fixes.Count -gt 0) { " [" + ($read.Fixes -join ', ') + "]" } else { "" }

    # Shebang decides whether this is a script at all. No shebang is a skip,
    # not an error: a README or a notes file living in the directory is
    # perfectly reasonable and must not be executed as root on a mesh node.
    $firstLine = ($text -split "`n", 2)[0]
    if (-not $firstLine.StartsWith('#!')) {
        return @{ Verdict = 'SKIP'; Reason = 'no #! on line 1' }
    }

    # The interpreter, as a bare name: "#!/usr/bin/env python3" -> python3,
    # "#!/usr/bin/perl -w" -> perl.
    $words  = ($firstLine.Substring(2).Trim() -split '\s+')
    $interp = if ($words[0] -match '/env$' -and $words.Count -gt 1) { $words[1] } else { $words[0] }
    $interp = ($interp -split '[/\\]')[-1]

    $note = ''
    if ($ADDITIONAL_SCRIPTS_NODE_INTERPRETERS -notcontains $interp) {
        $note = " - $interp is not installed on a stock node"
    }

    # Checked against the corrected text, not the file on disk. A UTF-16 file
    # would choke bash before it got as far as the syntax.
    $tmp = [System.IO.Path]::GetTempFileName()
    [System.IO.File]::WriteAllText($tmp, $text, (New-Object System.Text.UTF8Encoding($false)))

    try {
        # Any interpreter is accepted. Only those that can be checked *without
        # executing the script* are checked: shell via `bash -n`, python via
        # ast.parse. perl is deliberately excluded - `perl -c` executes BEGIN
        # blocks and resolves `use` against the host's module path, so it would
        # run operator code on the wrong machine and fail wrongly on modules
        # that exist only on the node. A wrong FAIL blocks a flash.
        switch -Regex ($interp) {
            '^(sh|bash|dash)$' {
                $syntaxError = Test-ShellSyntax -Path $tmp
                if ($null -eq $syntaxError) {
                    return @{ Verdict = 'OK'; Reason = "shell (not syntax-checked, no bash here)$note$fixed" }
                }
                if ($syntaxError -ne "") {
                    return @{ Verdict = 'FAIL'; Reason = "shell syntax error: $syntaxError" }
                }
                return @{ Verdict = 'OK'; Reason = "bash -n clean$note$fixed" }
            }
            '^python[23]?$' {
                $py = Get-Command python3 -ErrorAction SilentlyContinue
                if (-not $py) { $py = Get-Command python -ErrorAction SilentlyContinue }
                if ($py) {
                    $code = @'
import ast,sys
try:
    ast.parse(open(sys.argv[1], encoding="utf-8", errors="replace").read())
except SyntaxError as e:
    sys.stderr.write("line %s: %s" % (e.lineno, e.msg)); sys.exit(1)
'@
                    $checker = [System.IO.Path]::GetTempFileName() + '.py'
                    [System.IO.File]::WriteAllText($checker, $code)
                    # Bounded, like the shell check. The Microsoft Store stub
                    # that answers to "python" on a machine with no Python
                    # installed opens the Store and never returns.
                    $r = Invoke-CheckerProcess -Path $py.Source -Arguments @($checker, $tmp)
                    Remove-Item $checker -Force -ErrorAction SilentlyContinue
                    if ($r.TimedOut -or $r.Failed) {
                        return @{ Verdict = 'OK'; Reason = "$interp (not syntax-checked, python did not answer)$note$fixed" }
                    }
                    if ($r.ExitCode -ne 0) {
                        return @{ Verdict = 'FAIL'; Reason = "python syntax error: $($r.Output)" }
                    }
                    return @{ Verdict = 'OK'; Reason = "python syntax clean$note$fixed" }
                }
                return @{ Verdict = 'OK'; Reason = "$interp (not syntax-checked, no python here)$note$fixed" }
            }
        }

        return @{ Verdict = 'OK'; Reason = "$interp (not syntax-checked)$note$fixed" }
    } finally {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    }
}

function Get-AdditionalScriptReport {
    $report = [pscustomobject]@{
        Directory   = $ADDITIONAL_SCRIPTS_DIR
        Results     = @()
        Accepted    = @()
        TotalBytes  = 0
        Failed      = $false
        OverMax     = $false
        OverWarn    = $false
        MaxBytes    = $ADDITIONAL_SCRIPTS_MAX_BYTES
        WarnBytes   = $ADDITIONAL_SCRIPTS_WARN_BYTES
    }

    if (-not (Test-Path $ADDITIONAL_SCRIPTS_DIR)) { return $report }

    $found = @(Get-ChildItem -Path $ADDITIONAL_SCRIPTS_DIR -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -notmatch '^\.' -and
                       $_.Name -notmatch '\.(disabled|bak|orig)$' -and
                       $_.Name -notmatch '~$' })

    if ($found.Count -eq 0) { return $report }

    # Ordinal sort, to match `LC_ALL=C sort` in linux.sh exactly. A culture
    # sort would order 10-/20-/90- differently from the Linux flasher on some
    # locales, and the whole point of the numeric prefix convention is that
    # the operator can predict the order.
    $names = @($found | ForEach-Object { $_.Name })
    [Array]::Sort($names, [System.StringComparer]::Ordinal)
    $byName = @{}
    foreach ($f in $found) { $byName[$f.Name] = $f }
    $candidates = @($names | ForEach-Object { $byName[$_] })

    $results    = New-Object System.Collections.ArrayList
    $accepted   = New-Object System.Collections.ArrayList
    $totalBytes = 0

    foreach ($file in $candidates) {
        $result = Test-AdditionalScript -File $file

        # Same reason the validator does not trust FileInfo.Length: the
        # directory entry can lag what is actually on disk.
        $size = 0
        try { $size = [System.IO.File]::ReadAllBytes($file.FullName).Length } catch { }

        [void]$results.Add([pscustomobject]@{
            Name    = $file.Name
            Verdict = $result.Verdict
            Reason  = $result.Reason
            Bytes   = $size
            File    = $file
        })
        switch ($result.Verdict) {
            'OK'   { [void]$accepted.Add($file); $totalBytes += $size }
            'FAIL' { $report.Failed = $true }
        }
    }

    $report.Results    = $results.ToArray()
    $report.Accepted   = $accepted.ToArray()
    $report.TotalBytes = $totalBytes
    $report.OverMax    = ($totalBytes -gt $ADDITIONAL_SCRIPTS_MAX_BYTES)
    $report.OverWarn   = ($totalBytes -gt $ADDITIONAL_SCRIPTS_WARN_BYTES)
    return $report
}

function Test-AdditionalScripts {
    $Script:ADDITIONAL_SCRIPTS = @()

    $report = Get-AdditionalScriptReport
    if ($report.Results.Count -eq 0) { return }

    Write-Host ""
    Write-Host "==============================================" -ForegroundColor Cyan
    Write-Host " Additional setup scripts" -ForegroundColor Cyan
    Write-Host "==============================================" -ForegroundColor Cyan
    Write-Host " From: $ADDITIONAL_SCRIPTS_DIR"
    Write-Host ""

    foreach ($r in $report.Results) {
        $label = $r.Name.PadRight(36)
        switch ($r.Verdict) {
            'OK'   { Write-Host "   $label ok    $($r.Reason)" }
            'SKIP' { Write-Host "   $label SKIP  $($r.Reason)" -ForegroundColor DarkGray }
            'FAIL' { Write-Host "   $label FAIL  $($r.Reason)" -ForegroundColor Red }
        }
    }

    if ($report.Failed) {
        Write-Host ""
        Write-Host " ERROR: one or more scripts did not pass validation." -ForegroundColor Red
        Write-Host "        Nothing has been written to any card. Fix the files"
        Write-Host "        above (or rename them to .disabled) and run again."
        exit 1
    }

    if ($report.Accepted.Count -eq 0) {
        Write-Host ""
        Write-Host " Nothing to embed."
        return
    }

    if ($report.OverMax) {
        Write-Host ""
        Write-Host " ERROR: embedded scripts total $($report.TotalBytes) bytes, over the" -ForegroundColor Red
        Write-Host "        $ADDITIONAL_SCRIPTS_MAX_BYTES-byte limit."
        Write-Host "        Have a script fetch the bulk at run time instead - the"
        Write-Host "        node has confirmed internet before these are run."
        exit 1
    }
    if ($report.OverWarn) {
        Write-Host ""
        Write-Host " NOTE: $($report.TotalBytes) bytes of scripts is a lot to bake into an" -ForegroundColor Yellow
        Write-Host "       image. Consider fetching large payloads at run time."
    }

    $Script:ADDITIONAL_SCRIPTS = $report.Accepted

    Write-Host ""
    Write-Host " $($report.Accepted.Count) script(s) will be embedded and run ONCE as"
    Write-Host " root on each node, after setup completes."
    Write-Host ""
    Write-Host " Note: firstrun.sh is stored unencrypted on the boot partition and" -ForegroundColor Yellow
    Write-Host " is not deleted. Anyone who reads the card reads these scripts. Do" -ForegroundColor Yellow
    Write-Host " not put private keys or long-lived secrets in them." -ForegroundColor Yellow
    Write-Host "==============================================" -ForegroundColor Cyan
    Write-Host ""
}

# Pick a heredoc terminator that cannot appear in the file. A quoted heredoc
# ends at the first line consisting solely of the delimiter, so an operator
# script that legitimately contains our default - one that embeds its own
# heredoc, say - would truncate itself and leave the rest as loose shell.
function Get-HeredocDelimiter {
    param([string[]]$Lines)

    $delim = 'MANET_USER_SCRIPT_EOF'
    $i = 0
    while ($Lines -ccontains $delim) {
        $i++
        if ($i -gt 64) { return $null }
        $delim = "MANET_USER_SCRIPT_EOF_$i"
    }
    return $delim
}

# Build the embedding block to append to an already-generated setup script.
#
# Called AFTER token substitution, deliberately. The flasher rewrites every
# __TOKEN__ in the template with -replace; running that over operator content
# would silently rewrite a script that happens to mention __ADMIN_PW__ or any
# other token, which is both wrong and invisible.
function Get-AdditionalScriptsBlock {
    if ($Script:ADDITIONAL_SCRIPTS.Count -eq 0) { return "" }

    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append("`n")
    [void]$sb.Append("# ====================================================================`n")
    [void]$sb.Append("# Operator setup scripts, embedded at flash time from`n")
    [void]$sb.Append("# additional-scripts/. Written out here; run once by`n")
    [void]$sb.Append("# manet-user-scripts.service, which radio-setup.sh starts after`n")
    [void]$sb.Append("# provisioning completes.`n")
    [void]$sb.Append("# ====================================================================`n")
    [void]$sb.Append("echo `"Writing operator setup scripts...`"`n")
    [void]$sb.Append("mkdir -p /var/lib/manet-user-scripts`n")

    foreach ($file in $Script:ADDITIONAL_SCRIPTS) {
        # The same reader the validator used, so what lands on the card is
        # exactly what was checked: byte order mark gone, UTF-16 decoded, LF
        # line endings, and a trailing newline the heredoc delimiter needs.
        $content = (Read-AdditionalScriptContent -Path $file.FullName).Text

        $delim = Get-HeredocDelimiter -Lines ($content -split "`n")
        if ($null -eq $delim) {
            Write-Host "ERROR: could not find a safe heredoc delimiter for $($file.Name)" -ForegroundColor Red
            exit 1
        }


        [void]$sb.Append("`n")
        [void]$sb.Append("cat > '/var/lib/manet-user-scripts/$($file.Name)' << '$delim'`n")
        [void]$sb.Append($content)
        [void]$sb.Append("$delim`n")
        [void]$sb.Append("chmod 0755 '/var/lib/manet-user-scripts/$($file.Name)'`n")
    }

    [void]$sb.Append("`n")
    [void]$sb.Append("echo `"Operator setup scripts staged: `$(ls -1 /var/lib/manet-user-scripts | wc -l)`"`n")

    Write-Host "Embedded $($Script:ADDITIONAL_SCRIPTS.Count) operator setup script(s)."
    return $sb.ToString()
}

# Substitute the block for the anchor line in an already-generated setup
# script. Called with content whose newlines are already normalised to LF.
#
# The block replaces the anchor rather than being appended, because neither
# template runs off the end - firstrun.sh has trailing completion echoes and
# rock3a-provision.sh ends with `reboot`. A run with no scripts still comes
# through here, so the marker is stripped out of every generated image.
function Add-AdditionalScriptsBlock {
    param([string]$Content)

    $block = Get-AdditionalScriptsBlock

    $lines  = $Content -split "`n"
    $anchor = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -ceq $ADDITIONAL_SCRIPTS_ANCHOR) { $anchor = $i; break }
    }

    if ($anchor -lt 0) {
        if ($Script:ADDITIONAL_SCRIPTS.Count -gt 0) {
            Write-Host "ERROR: template has no anchor line:" -ForegroundColor Red
            Write-Host "       $ADDITIONAL_SCRIPTS_ANCHOR" -ForegroundColor Red
            Write-Host "       Cannot place the operator setup scripts. Aborting." -ForegroundColor Red
            exit 1
        }
        return $Content
    }

    # Drop the trailing newline the block always ends with: the anchor line it
    # replaces did not carry one of its own once the array was split.
    $blockLines = @()
    if ($block -ne "") { $blockLines = @($block.TrimEnd("`n") -split "`n") }

    $out = New-Object System.Collections.ArrayList
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($i -eq $anchor) {
            foreach ($bl in $blockLines) { [void]$out.Add($bl) }
        } else {
            [void]$out.Add($lines[$i])
        }
    }
    return ($out -join "`n")
}

function Confirm-Flash {
    param([int]$DiskNumber)
    $disk   = Get-Disk -Number $DiskNumber
    $sizeGB = [math]::Round($disk.Size / 1GB, 2)

    Write-Host ""
    Write-Host "==============================================" -ForegroundColor Yellow
    Write-Host "         *** FINAL CONFIRMATION ***"           -ForegroundColor Yellow
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
    Write-Host "WARNING: ALL DATA ON DISK $DiskNumber WILL BE DESTROYED!" -ForegroundColor Red
    Write-Host ""
    Write-Host "==============================================" -ForegroundColor Yellow
    Write-Host ""

    $confirm = Read-Host "Type 'yes' to proceed, anything else to abort"
    if ($confirm -ne "yes") { Write-Host ""; Write-Host "Aborted by user."; exit 0 }
    Write-Host ""; Write-Host "Proceeding with flash..."
}

# ============================================================
# Configuration Questions / Save / Load
# ============================================================

function Ask-LanCidr {
    param([int]$maxEuds)

    $defaultCidr = "10.30.2.0/24"

    while ($true) {
        $confirm = Read-Host "Use default mesh network range ( $defaultCidr )? (Y/n)"
        if ([string]::IsNullOrWhiteSpace($confirm) -or $confirm -match "^[Yy]") {
            $Script:LAN_CIDR_BLOCK = $defaultCidr
        } else {
            while ($true) {
                $custom = Read-Host "Enter custom CIDR block for the mesh (e.g., 10.10.0.0/16)"
                if ($custom -notmatch '^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/(\d{1,2})$') {
                    Write-Host "ERROR: Invalid format. Must be x.x.x.x/yy" -ForegroundColor Red
                    continue
                }
                $prefix = [int]$Matches[1]
                if ($prefix -lt 16 -or $prefix -gt 26) {
                    Write-Host "ERROR: Prefix /$prefix is invalid. Must be between /16 and /26." -ForegroundColor Red
                    continue
                }
                $ipPart = ($custom -split '/')[0]
                $octets = $ipPart -split '\.'
                $o1 = [int]$octets[0]; $o2 = [int]$octets[1]
                $isPrivate = ($o1 -eq 10) -or
                             ($o1 -eq 172 -and $o2 -ge 16 -and $o2 -le 31) -or
                             ($o1 -eq 192 -and $o2 -eq 168)
                if (-not $isPrivate) {
                    Write-Host "ERROR: $ipPart is not in a private range (10.x, 172.16-31.x, 192.168.x)." -ForegroundColor Red
                    continue
                }
                $Script:LAN_CIDR_BLOCK = $custom
                break
            }
        }

        if ($maxEuds -gt 0) {
            $cap = Calculate-Capacity -cidr $Script:LAN_CIDR_BLOCK -maxEuds $maxEuds
            if ($cap) {
                Write-Host ""
                Write-Host "=== Network Capacity Analysis ==="
                Write-Host "Network: $($Script:LAN_CIDR_BLOCK)"
                Write-Host "  Total usable IPs        : $($cap.Total)"
                Write-Host "  Reserved for services   : $($cap.Services)"
                Write-Host "  Reserved for EUD pool   : $($cap.EudPool) (${maxEuds} EUDs x $($cap.MaxNodes) nodes)"
                Write-Host "  Available for mesh nodes: $($cap.MaxNodes)"
                Write-Host "=================================="
                if ($cap.MaxNodes -lt 5) {
                    Write-Host "WARNING: Only $($cap.MaxNodes) mesh node addresses. Consider a larger network or fewer EUDs." -ForegroundColor Yellow
                }
                $accept = Read-Host "Accept this configuration? (Y/n)"
                if ([string]::IsNullOrWhiteSpace($accept) -or $accept -match "^[Yy]") { break }
                Write-Host "Let's reconfigure..."
            } else {
                Write-Host "Using network: $($Script:LAN_CIDR_BLOCK)"
                break
            }
        } else {
            Write-Host "Using network: $($Script:LAN_CIDR_BLOCK)"
            break
        }
    }
}

function Ask-Questions {
    Write-Host "--- Starting New Configuration ---"

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
        Write-Host "EUD wifi network name. This name will have the last 4 of the ethernet MAC address appended to it for node identification."
        $Script:LAN_AP_SSID = Read-Host "Enter EUD access point SSID name"
        while ($true) {
            $key = Read-Host "Enter LAN AP WPA2 Key (8-63 chars) [or press Enter to generate]"
            Write-Host ""
            if ([string]::IsNullOrWhiteSpace($key)) {
                # Sixteen letters and digits, matching linux.sh's
                # 'generate_password 16'. Base64 of ten bytes ends in '=='
                # padding every time, and this is the key somebody types into
                # a phone by hand, so it does without '+' and '/' as well.
                $Script:LAN_AP_KEY = Generate-Password -length 16
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

    $r = Read-Host "Install MediaMTX Server? (y/N)"
    $Script:INSTALL_MEDIAMTX = if ($r -match "^[Yy]") { "y" } else { "n" }

    $r = Read-Host "Install Mumble Server (murmur)? (y/N)"
    $Script:INSTALL_MUMBLE = if ($r -match "^[Yy]") { "y" } else { "n" }

    # Mesh PTT voice is on by default: a node that ships with the OpenVLM board
    # fitted is the normal build now. Without the board the daemon simply has
    # nothing to drive; turning it off later is a mesh.conf edit and a restart.
    $r = Read-Host "Enable mesh PTT voice (needs an OpenVLM board)? (Y/n)"
    $Script:VOICE_ENABLED = if ([string]::IsNullOrWhiteSpace($r) -or $r -match "^[Yy]") { "y" } else { "n" }

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

    while ($true) {
        $domain = Read-Host "Enter WiFi regulatory domain (2-letter country code, default: US)"
        if ([string]::IsNullOrWhiteSpace($domain)) { $domain = "US" }
        $validated = Test-RegulatoryDomain -domain $domain
        if ($validated) {
            $Script:REGULATORY_DOMAIN       = $validated
            $Script:HALOW_REGULATORY_DOMAIN = Get-HalowRegulatoryDomain -wifiDomain $validated
            Write-Host "Using regulatory domain: $($Script:REGULATORY_DOMAIN)"
            if ($Script:HALOW_REGULATORY_DOMAIN -ne $Script:REGULATORY_DOMAIN) {
                Write-Host "Using HaLow regulatory region: $($Script:HALOW_REGULATORY_DOMAIN)"
            }
            break
        } else {
            Write-Host "ERROR: Invalid regulatory domain: $domain" -ForegroundColor Red
            Write-Host "Enter a valid 2-letter ISO country code (e.g., US, GB, DE, JP, AU)"
            Write-Host "NOTE: EU is not a country code, use your actual country"
        }
    }

    Write-Host "The device will have a user called 'radio' for SSH access."
    $pw = Read-Host "Enter a password for the radio user [or press Enter to default to 'radio']"
    Write-Host ""
    $Script:RADIO_PW = if ([string]::IsNullOrWhiteSpace($pw)) { Write-Host "Setting default password"; "radio" } else { $pw }
    Write-Host "Radio password set to: $($Script:RADIO_PW)"

    Write-Host ""
    Write-Host "The network administrator password is used to access the mesh admin interface."
    $adminPw = Read-Host "Enter network admin password [or press Enter to generate 10-char random]"
    Write-Host ""
    if ([string]::IsNullOrWhiteSpace($adminPw)) {
        $Script:ADMIN_PW = Generate-Password -length 10
        Write-Host "Generated network admin password: $($Script:ADMIN_PW)"
    } else {
        $Script:ADMIN_PW = $adminPw
        Write-Host "Network admin password set."
    }

    Write-Host ""
    $r = Read-Host "Enable automatic updates for MANET tools? (Y/n)"
    if ([string]::IsNullOrWhiteSpace($r) -or $r -match "^[Yy]") {
        $Script:AUTO_UPDATE = "y"; Write-Host "Automatic updates enabled."
    } else {
        $Script:AUTO_UPDATE = "n"; Write-Host "Automatic updates disabled."
    }

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

    Ask-LanCidr -maxEuds $Script:MAX_EUDS_PER_NODE

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
VOICE_ENABLED="$($Script:VOICE_ENABLED)"
REGULATORY_DOMAIN="$($Script:REGULATORY_DOMAIN)"
HALOW_REGULATORY_DOMAIN="$($Script:HALOW_REGULATORY_DOMAIN)"
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
                "EUD_CONNECTION"          { $Script:EUD_CONNECTION          = $Matches[2] }
                "LAN_AP_SSID"             { $Script:LAN_AP_SSID             = $Matches[2] }
                "LAN_AP_KEY"              { $Script:LAN_AP_KEY               = $Matches[2] }
                "MAX_EUDS_PER_NODE"       { $Script:MAX_EUDS_PER_NODE        = [int]$Matches[2] }
                "INSTALL_MEDIAMTX"        { $Script:INSTALL_MEDIAMTX         = $Matches[2] }
                "INSTALL_MUMBLE"          { $Script:INSTALL_MUMBLE            = $Matches[2] }
                # Absent from configs saved before voice existed; the defaults
                # at the top of the script stand in that case.
                "VOICE_ENABLED"           { $Script:VOICE_ENABLED             = $Matches[2] }
                "REGULATORY_DOMAIN"       { $Script:REGULATORY_DOMAIN        = $Matches[2] }
                "HALOW_REGULATORY_DOMAIN" { $Script:HALOW_REGULATORY_DOMAIN  = $Matches[2] }
                "MESH_SSID"               { $Script:MESH_SSID                = $Matches[2] }
                "MESH_SAE_KEY"            { $Script:MESH_SAE_KEY              = $Matches[2] }
                "LAN_CIDR_BLOCK"          { $Script:LAN_CIDR_BLOCK            = $Matches[2] }
                "AUTO_CHANNEL"            { $Script:AUTO_CHANNEL              = $Matches[2] }
                "RADIO_PW"                { $Script:RADIO_PW                  = $Matches[2] }
                "ADMIN_PW"                { $Script:ADMIN_PW                  = $Matches[2] }
                "AUTO_UPDATE"             { $Script:AUTO_UPDATE               = $Matches[2] }
            }
        }
    }

    if (-not $Script:HALOW_REGULATORY_DOMAIN) {
        $Script:HALOW_REGULATORY_DOMAIN = Get-HalowRegulatoryDomain -wifiDomain $Script:REGULATORY_DOMAIN
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
    Write-Host "  Mesh PTT voice: $($Script:VOICE_ENABLED)"
    Write-Host "  Regulatory Domain: $($Script:REGULATORY_DOMAIN)"
    Write-Host "  HaLow Regulatory Region: $($Script:HALOW_REGULATORY_DOMAIN)"
    Write-Host "  Mesh SSID: $($Script:MESH_SSID)"
    Write-Host "  Mesh SAE Key: $($Script:MESH_SAE_KEY)"
    Write-Host "  LAN CIDR Block: $($Script:LAN_CIDR_BLOCK)"
    Write-Host "  Auto Channel: $($Script:AUTO_CHANNEL)"
    Write-Host "  User password: $($Script:RADIO_PW)"
    Write-Host "  Network admin password: $(if ($Script:ADMIN_PW) { $Script:ADMIN_PW } else { '(not set)' })"
    Write-Host "  Auto Update: $(if ($Script:AUTO_UPDATE) { $Script:AUTO_UPDATE } else { 'n' })"
    Write-Host "----------------------------"
}


# ============================================================
# Image content builders and flash primitives
# ============================================================
#
# Pure enough to be called twice, and callable from a host script. Everything
# that decides what lands on a card lives here so the console flow and the
# window front end cannot produce different images from the same answers.

# The one token list in this file. Both templates take the same set, so the
# two copies that used to sit inline in the Rock 3A and Raspberry Pi paths
# were an invitation to update one and not the other. Adding a flash-time
# setting still means the token in both templates and the substitution list in
# both flashers, as the provisioning README says - it is just one list here
# now instead of two.
function Expand-ProvisioningTokens {
    param([string]$Content)

    return ($Content `
        -replace '__HARDWARE_MODEL__',          $Script:HARDWARE_MODEL `
        -replace '__EUD_CONNECTION__',          $Script:EUD_CONNECTION `
        -replace '__LAN_AP_SSID__',             $Script:LAN_AP_SSID `
        -replace '__LAN_AP_KEY__',              $Script:LAN_AP_KEY `
        -replace '__MAX_EUDS_PER_NODE__',       $Script:MAX_EUDS_PER_NODE `
        -replace '__INSTALL_MEDIAMTX__',        $Script:INSTALL_MEDIAMTX `
        -replace '__INSTALL_MUMBLE__',          $Script:INSTALL_MUMBLE `
        -replace '__VOICE_ENABLED__',           $Script:VOICE_ENABLED `
        -replace '__MESH_SSID__',               $Script:MESH_SSID `
        -replace '__MESH_SAE_KEY__',            $Script:MESH_SAE_KEY `
        -replace '__LAN_CIDR_BLOCK__',          $Script:LAN_CIDR_BLOCK `
        -replace '__AUTO_CHANNEL__',            $Script:AUTO_CHANNEL `
        -replace '__RADIO_PW__',                $Script:RADIO_PW `
        -replace '__REGULATORY_DOMAIN__',       $Script:REGULATORY_DOMAIN `
        -replace '__HALOW_REGULATORY_DOMAIN__', $Script:HALOW_REGULATORY_DOMAIN `
        -replace '__ADMIN_PW__',                $Script:ADMIN_PW `
        -replace '__AUTO_UPDATE__',             $Script:AUTO_UPDATE)
}

# Substitute, normalise, then embed the operator scripts. The order matters:
# operator scripts must not have their own __TOKEN__-looking text rewritten,
# and the anchor is matched line by line so CRLF has to go first.
function Build-ProvisioningScript {
    param([string]$TemplatePath)

    if (-not (Test-Path $TemplatePath)) {
        throw "Template file '$TemplatePath' not found."
    }
    $content = [System.IO.File]::ReadAllText($TemplatePath)
    $content = Expand-ProvisioningTokens -Content $content
    $content = $content.Replace("`r`n", "`n")
    return (Add-AdditionalScriptsBlock -Content $content)
}

function Build-MeshConf {
    $meshConf = @"
# Mesh Network Configuration
# Generated by provisioning script
hardware_model=$($Script:HARDWARE_MODEL)
eud=$($Script:EUD_CONNECTION)
lan_ap_ssid=$($Script:LAN_AP_SSID)
lan_ap_key=$($Script:LAN_AP_KEY)
max_euds_per_node=$($Script:MAX_EUDS_PER_NODE)
mtx=$($Script:INSTALL_MEDIAMTX)
mumble=$($Script:INSTALL_MUMBLE)
voice=$($Script:VOICE_ENABLED)
# Every node ships on talk group 1; changed from the web UI, not at flash time.
voice_channel=1
mesh_ssid=$($Script:MESH_SSID)
mesh_key=$($Script:MESH_SAE_KEY)
ipv4_network=$($Script:LAN_CIDR_BLOCK)
acs=$($Script:AUTO_CHANNEL)
regulatory_domain=$($Script:REGULATORY_DOMAIN)
halow_regulatory_domain=$($Script:HALOW_REGULATORY_DOMAIN)
admin_password=$($Script:ADMIN_PW)
auto_update=$($Script:AUTO_UPDATE)
"@
    return $meshConf.Replace("`r`n", "`n")
}

# One card, one rpi-imager run. Returns a result rather than printing a verdict
# so a caller with a window can show it its own way. Dropping a bad remembered
# path stays here: it must happen however the flash was started.
function Invoke-RpiImagerFlash {
    param([int]$DiskNumber, [string]$ScriptPath)

    $targetDrive = "\\.\PhysicalDrive$DiskNumber"

    try {
        & $Script:RPI_IMAGER_PATH --cli $OS_IMAGE_URL $targetDrive --first-run-script "$ScriptPath"
        if ($LASTEXITCODE -ne 0) {
            Remove-CachedToolPath -Key 'rpi-imager' -Reason "it did not flash the card"
            return [pscustomobject]@{ Ok = $false; Error = "exited with code $LASTEXITCODE" }
        }
    } catch {
        # Could not be executed at all - the surest sign the path is wrong.
        Remove-CachedToolPath -Key 'rpi-imager' -Reason "it could not be run"
        return [pscustomobject]@{ Ok = $false; Error = "could not be run: $($_.Exception.Message)" }
    }

    return [pscustomobject]@{ Ok = $true; Error = "" }
}

# Raw sector write, used by the Rock 3A path. OnProgress is handed a percentage
# so a window can drive a progress bar; without one it falls back to
# Write-Progress, which is what the console flow has always shown.
function Write-RawImageToDisk {
    param(
        [string]      $ImagePath,
        [int]         $DiskNumber,
        [scriptblock] $OnProgress
    )

    $bufferSize = 4MB
    $buffer     = New-Object byte[] $bufferSize
    $physDrive  = "\\.\PhysicalDrive$DiskNumber"

    $src  = [System.IO.File]::OpenRead($ImagePath)
    $dest = [System.IO.File]::Open($physDrive, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    try {
        $totalBytes = $src.Length
        $written    = 0
        $lastPct    = -1
        while (($read = $src.Read($buffer, 0, $buffer.Length)) -gt 0) {
            $dest.Write($buffer, 0, $read)
            $written += $read
            $pct = [math]::Round(($written / $totalBytes) * 100, 1)
            if ($pct -ne $lastPct) {
                $lastPct = $pct
                if ($OnProgress) { & $OnProgress $pct }
                else { Write-Progress -Activity "Flashing" -Status "$pct% complete" -PercentComplete $pct }
            }
        }
        $dest.Flush()
    } finally {
        $src.Close()
        $dest.Close()
        if (-not $OnProgress) { Write-Progress -Activity "Flashing" -Completed }
    }
}

# Copy the Armbian image, mount its ext4 root through Ext2Fsd, write the mesh
# config and the provisioning unit into it, and hand back the path to the
# customised copy. The caller writes that copy to as many cards as it likes and
# is responsible for deleting it. Throws rather than exiting so a window can
# report the failure and stay open.
function New-Rock3aCustomImage {
    $tempImage = [System.IO.Path]::GetTempFileName() -replace '\.tmp$', '.img'
    Write-Host "Creating temporary copy of $($Script:ARMBIAN_IMAGE)..."
    Copy-Item $Script:ARMBIAN_IMAGE $tempImage

    try {
        Write-Host "Mounting image as virtual disk..."
        $vdisk = Mount-DiskImage -ImagePath $tempImage -PassThru
        Start-Sleep -Seconds 3
        $disk = Get-Disk | Where-Object { $_.Location -eq $tempImage }
        if (-not $disk) { throw "Could not find mounted virtual disk." }

        $partition = Get-Partition -DiskNumber $disk.Number | Where-Object { $_.PartitionNumber -eq 1 }
        if (-not $partition) { throw "Could not find root partition (partition 1) on mounted image." }

        if (-not $partition.DriveLetter -or $partition.DriveLetter -eq "`0") {
            Add-PartitionAccessPath -DiskNumber $disk.Number -PartitionNumber 1 -AssignDriveLetter
            Start-Sleep -Seconds 3
            $partition = Get-Partition -DiskNumber $disk.Number -PartitionNumber 1
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

        Write-Host "Writing /etc/mesh.conf..."
        [System.IO.File]::WriteAllText((Join-Path $rootPath "etc\mesh.conf"), (Build-MeshConf))

        Write-Host "Removing .not_logged_in_yet to bypass interactive setup..."
        $notLoggedIn = Join-Path $rootPath "root\.not_logged_in_yet"
        if (Test-Path $notLoggedIn) { Remove-Item $notLoggedIn -Force }

        Write-Host "Generating password hash..."
        $radioHash = Get-LinuxPasswordHash -password $Script:RADIO_PW
        if (-not $radioHash) {
            Write-Host ""
            Write-Host "  WARNING: Could not generate password hash." -ForegroundColor Yellow
            Write-Host "         openssl and WSL were both unavailable."
            Write-Host "         The radio user will be created without a password."
            Write-Host "         You will need to set it manually after first boot:"
            Write-Host "         (log in as root with password '1234', then: passwd radio)"
            $radioHash = "!"
        }
        $Script:R3A_RADIO_HASH = $radioHash

        Write-Host "Creating radio user..."

        # The "^radio:" tests all need (?m). Without it "^" anchors to the start
        # of the whole file, so an existing radio entry anywhere below line 1 is
        # not seen and a duplicate gets appended.
        $passwdPath    = Join-Path $rootPath "etc\passwd"
        $passwdContent = [System.IO.File]::ReadAllText($passwdPath)
        if ($passwdContent -notmatch "(?m)^radio:") {
            [System.IO.File]::WriteAllText($passwdPath, ($passwdContent.TrimEnd() + "`nradio:x:1000:1000:radio:/home/radio:/bin/bash`n").Replace("`r`n", "`n"))
        }

        # Done line by line. The old one-liner anchored its ",radio" tidy-up to
        # the end of the *string*, so a memberless sudo line anywhere but the
        # last line of the file was left as "sudo:x:27:,radio" - an empty member
        # name - and the ",," collapse was a blind replace over the whole file.
        $groupPath  = Join-Path $rootPath "etc\group"
        $groupLines = @(([System.IO.File]::ReadAllText($groupPath).Replace("`r`n", "`n")).TrimEnd("`n") -split "`n")

        if (-not ($groupLines -match '^radio:')) {
            $groupLines += "radio:x:1000:"
        }

        $groupLines = @($groupLines | ForEach-Object {
            if ($_ -match '^(sudo:[^:]*:[0-9]+:)(.*)$') {
                $prefix  = $Matches[1]
                $members = @($Matches[2] -split ',' | Where-Object { $_ -ne '' })
                if ($members -notcontains 'radio') { $members += 'radio' }
                "$prefix$($members -join ',')"
            } else {
                $_
            }
        })

        [System.IO.File]::WriteAllText($groupPath, (($groupLines -join "`n") + "`n"))

        $shadowPath    = Join-Path $rootPath "etc\shadow"
        $shadowContent = [System.IO.File]::ReadAllText($shadowPath)
        if ($shadowContent -notmatch "(?m)^radio:") {
            $shadowContent += "radio:${radioHash}:19700:0:99999:7:::`n"
            [System.IO.File]::WriteAllText($shadowPath, $shadowContent.Replace("`r`n", "`n"))
        }

        $sudoersDir = Join-Path $rootPath "etc\sudoers.d"
        if (-not (Test-Path $sudoersDir)) { New-Item -ItemType Directory -Path $sudoersDir | Out-Null }
        [System.IO.File]::WriteAllText((Join-Path $sudoersDir "radio"), "radio ALL=(ALL) NOPASSWD: ALL`n")

        $radioHome = Join-Path $rootPath "home\radio"
        if (-not (Test-Path $radioHome)) { New-Item -ItemType Directory -Path $radioHome | Out-Null }

        Write-Host "Installing provisioning script from template..."
        $provisionScript = Build-ProvisioningScript -TemplatePath $ROCK3A_TEMPLATE

        $usrLocalBin = Join-Path $rootPath "usr\local\bin"
        if (-not (Test-Path $usrLocalBin)) { New-Item -ItemType Directory -Path $usrLocalBin | Out-Null }
        [System.IO.File]::WriteAllText((Join-Path $usrLocalBin "provision-mesh.sh"), $provisionScript)

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

        Write-Host "Creating provisioning trigger flag..."
        [System.IO.File]::WriteAllText((Join-Path $rootPath "root\.mesh-not-provisioned"), "")

        $wantsDir = Join-Path $systemdDir "multi-user.target.wants"
        if (-not (Test-Path $wantsDir)) { New-Item -ItemType Directory -Path $wantsDir | Out-Null }
        Copy-Item (Join-Path $systemdDir "mesh-provision.service") (Join-Path $wantsDir "mesh-provision.service")

        Write-Host "Unmounting image..."
        Dismount-DiskImage -ImagePath $tempImage | Out-Null
        Start-Sleep -Seconds 2

        return $tempImage

    } catch {
        try { Dismount-DiskImage -ImagePath $tempImage -ErrorAction SilentlyContinue | Out-Null } catch { }
        if (Test-Path $tempImage) { Remove-Item $tempImage -Force -ErrorAction SilentlyContinue }
        throw
    }
}


# ============================================================
# Main Script
# ============================================================
#
# Everything below is the console flow. It is a function so that a host script
# can dot-source this file with -NoRun and drive the pieces itself; nothing
# here is called when it does.

function Invoke-Main {

    $currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Host "ERROR: This script must be run as Administrator!" -ForegroundColor Red
        Write-Host "Right-click PowerShell and select 'Run as Administrator'"
        exit 1
    }

    Write-Host "Script directory: $ScriptDir"

    Select-HardwareAndTargetDevice

    if (-not (Test-Path $TEMPLATE_FILE)) {
        Write-Host "ERROR: Template file '$TEMPLATE_FILE' not found." -ForegroundColor Red
        exit 1
    }
    if ($Script:HARDWARE_MODEL -eq "r3a" -and -not (Test-Path $ROCK3A_TEMPLATE)) {
        Write-Host "ERROR: Rock 3A template '$ROCK3A_TEMPLATE' not found." -ForegroundColor Red
        exit 1
    }

    if (-not (Test-Path $CONFIG_DIR)) {
        New-Item -ItemType Directory -Path $CONFIG_DIR | Out-Null
    }

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

    # Validate operator setup scripts before anything is written to any card: a
    # bad script here is a wasted flash, and the operator should find out while
    # the card is still safely in their hand.
    Test-AdditionalScripts

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

        Write-Host ""
        Write-Host "Checking for ext4 filesystem driver (Ext2Fsd)..."
        if (-not (Test-Ext4Driver)) {
            Write-Host ""
            Write-Host "ERROR: Ext2Fsd service not found or not running." -ForegroundColor Red
            Write-Host ""
            Write-Host "To flash Rock 3A images on Windows you need Ext2Fsd installed."
            Write-Host "Download from: https://sourceforge.net/projects/ext2fsd/"
            Write-Host ""
            Write-Host "After installing:"
            Write-Host "  1. Run 'Ext2 Volume Manager' from the Start Menu"
            Write-Host "  2. Go to Tools -> Service Management -> Start"
            Write-Host "  3. Re-run this script"
            Write-Host ""

            $installer = Get-ChildItem -Path $ScriptDir -Filter "Ext2Fsd*.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
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

        try {
            $tempImage = New-Rock3aCustomImage
        } catch {
            Write-Host "ERROR during image customisation: $($_.Exception.Message)" -ForegroundColor Red
            exit 1
        }

        $r3aFlashCount = 0
        $r3aKeepFlashing = $true
        while ($r3aKeepFlashing) {
            if ($r3aFlashCount -gt 0) {
                Write-Host ""
                Read-Host "Insert the next SD card and then press Enter"
                Write-Host "Select the target device."
                $picked2 = Select-TargetDiskConsole -LastChoiceLabel "Done (stop flashing)"
                if ($null -eq $picked2) { break }
                $Script:TARGET_DEVICE = $picked2
            }

            Confirm-Flash -DiskNumber $Script:TARGET_DEVICE

            Write-Host "Wiping target disk..."
            Clear-Disk -Number $Script:TARGET_DEVICE -RemoveData -Confirm:$false -ErrorAction SilentlyContinue

            Write-Host "Flashing image to Disk $($Script:TARGET_DEVICE)..."
            Write-RawImageToDisk -ImagePath $tempImage -DiskNumber $Script:TARGET_DEVICE

            $r3aFlashCount++
            Write-Host ""
            Write-Host "==============================================" -ForegroundColor Green
            Write-Host "           DONE: Flash complete!" -ForegroundColor Green
            Write-Host "==============================================" -ForegroundColor Green
            Write-Host ""
            Write-Host "You can now remove the SD card and boot your Rock 3A."
            Write-Host "First boot provisioning will run automatically when connected to the internet."
            Write-Host ""
            Write-Host "  - Root password: 1234 (Armbian default - change this)"
            Write-Host "  - Radio user: radio / $($Script:RADIO_PW)"
            if ($Script:R3A_RADIO_HASH -eq "!") {
                Write-Host ""
                Write-Host "  WARNING: Password hash could not be generated." -ForegroundColor Yellow
                Write-Host "     Log in as root and run: passwd radio" -ForegroundColor Yellow
            }
            Write-Host ""
            Write-Host " ONCE BOOTED, THE MESH NODE WILL AUTOMATICALLY START"
            Write-Host " SETTING ITSELF UP AND WILL REBOOT MULTIPLE TIMES"
            Write-Host " Just leave it alone, this process takes about ten minutes"
            Write-Host ""

            Write-Host "==============================================" -ForegroundColor Cyan
            $again = Read-Host "Flash another card with the same settings? (y/N)"
            if ($again -notmatch "^[Yy]") { $r3aKeepFlashing = $false }
        }

        Remove-Item $tempImage -Force -ErrorAction SilentlyContinue
        Write-Host ""
        Write-Host "=============================================="
        Write-Host "  Done. $r3aFlashCount Rock 3A card(s) flashed."
        Write-Host "=============================================="
        Write-Host ""

    # ============================================================
    # Raspberry Pi Flashing Path (all Pi models including CM4)
    # ============================================================

    } else {

        Write-Host "Generating firstrun script from template..."
        $templateContent = Build-ProvisioningScript -TemplatePath $TEMPLATE_FILE

        $flashCount = 0
        $keepFlashing = $true

        while ($keepFlashing) {

            $tempScript = Join-Path $ScriptDir "firstrun.sh"
            Write-Host "Writing firstrun script to: $tempScript"
            [System.IO.File]::WriteAllText($tempScript, $templateContent.Replace("`r`n", "`n"))

            if (-not (Test-Path $tempScript)) {
                Write-Host "ERROR: Failed to write firstrun script!" -ForegroundColor Red
                exit 1
            }

            Confirm-Flash -DiskNumber $Script:TARGET_DEVICE

            Write-Host "Running Raspberry Pi Imager..."
            $result = Invoke-RpiImagerFlash -DiskNumber $Script:TARGET_DEVICE -ScriptPath $tempScript

            Remove-Item $tempScript -Force -ErrorAction SilentlyContinue

            if ($result.Ok) {
                $flashCount++
                Write-Host ""
                Write-Host "==============================================" -ForegroundColor Green
                Write-Host "           DONE: Flash complete!" -ForegroundColor Green
                Write-Host "==============================================" -ForegroundColor Green
                Write-Host ""
                Write-Host " ONCE BOOTED, THE MESH NODE WILL AUTOMATICALLY START"
                Write-Host " SETTING ITSELF UP AND WILL REBOOT MULTIPLE TIMES"
                Write-Host " Just leave it alone, this process takes about ten minutes"
                Write-Host ""
            } else {
                Write-Host ""
                Write-Host "ERROR: $($Script:RPI_IMAGER_PATH)" -ForegroundColor Red
                Write-Host "       $($result.Error)" -ForegroundColor Red
                Write-Host ""
            }

            Write-Host "==============================================" -ForegroundColor Cyan
            $again = Read-Host "Flash another card with the same settings? (y/N)"
            if ($again -notmatch "^[Yy]") {
                $keepFlashing = $false
            } else {
                Write-Host ""
                Read-Host "Insert the next SD card and then press Enter"
                Write-Host "Select the target device."

                $pickedNext = Select-TargetDiskConsole -LastChoiceLabel "Done (stop flashing)"
                if ($null -eq $pickedNext) {
                    $keepFlashing = $false
                } else {
                    $Script:TARGET_DEVICE = $pickedNext
                }
            }
        }

        Write-Host ""
        Write-Host "=============================================="
        Write-Host "  Done. $flashCount card(s) flashed."
        Write-Host "=============================================="
        Write-Host ""
    }
}

if (-not $NoRun) { Invoke-Main }
