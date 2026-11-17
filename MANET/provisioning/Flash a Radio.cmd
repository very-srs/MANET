@echo off
setlocal
set "MANET_LAUNCHER=%~f0"
set "MANET_DEVELOPMENT=0"
set "MANET_LOCAL_SCRIPTS=0"
set "MANET_OFFLINE=0"
set "MANET_MAKE_KIT="
set "MANET_ELEVATED=0"
set "MANET_CONSOLE=0"
:args
if "%~1"=="" goto :start
if /i "%~1"=="--development" (set "MANET_DEVELOPMENT=1") else if /i "%~1"=="--local-scripts" (set "MANET_LOCAL_SCRIPTS=1") else if /i "%~1"=="--offline" (set "MANET_OFFLINE=1") else if /i "%~1"=="--make-kit" (set "MANET_MAKE_KIT=%~2" & shift) else if /i "%~1"=="--console" (set "MANET_CONSOLE=1") else if /i "%~1"=="--elevated" (set "MANET_ELEVATED=1") else (echo Usage: "Flash a Radio.cmd" [--development] [--local-scripts] [--offline] [--make-kit FOLDER] & exit /b 1)
shift
goto :args
:start
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$s=[IO.File]::ReadAllText($env:MANET_LAUNCHER); $marker='# MANET_'+'POWERSHELL_START'; & ([scriptblock]::Create(($s -split [regex]::Escape($marker),2)[1]))"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" pause
exit /b %RC%
# MANET_POWERSHELL_START
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$stage = $null
$headers = @{'User-Agent'='manet-flasher'}
$base = 'https://github.com/very-srs/MANET/releases'
# A folder holding this file is an offline kit, see --make-kit.
$kitMarker = 'MANET-OFFLINE-KIT.txt'

function Test-Manifest($manifest, $tag) {
    if ($manifest.schema -ne 1 -or $manifest.tag -notmatch '^v[0-9]+(?:\.[0-9]+)+$' -or $manifest.tag -ne ('v' + $manifest.version) -or ($tag -and $tag -ne $manifest.tag)) {
        throw 'Invalid MANET release manifest.'
    }
    return $manifest
}

function Test-FileDigest($path, $size, $sha256) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $false }
    if ((Get-Item -LiteralPath $path).Length -ne $size) { return $false }
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant() -eq $sha256
}

# The stable release, or with --development the newest published build.
function Get-ReleaseManifest {
    $manifestUrl = "$base/latest/download/manet-release.json"
    $tag = $null
    if ($env:MANET_DEVELOPMENT -eq '1') {
        $releases = @()
        for ($page = 1; $page -le 100; $page++) {
            $batch = @(Invoke-RestMethod -Headers $headers -TimeoutSec 60 -Uri "https://api.github.com/repos/very-srs/MANET/releases?per_page=100&page=$page")
            $releases += $batch
            if ($batch.Count -lt 100) { break }
        }
        if ($page -gt 100) { throw 'Too many releases.' }
        $eligible = @($releases | Where-Object { -not $_.draft -and $_.published_at -and $_.tag_name -match '^v[0-9]+(?:\.[0-9]+)+$' -and @($_.assets | Where-Object name -eq 'manet-release.json').Count -gt 0 })
        if ($eligible.Count -eq 0) { throw 'No published MANET build is available.' }
        $selected = $eligible | Sort-Object -Property published_at,id -Descending | Select-Object -First 1
        $tag = $selected.tag_name
        $manifestUrl = "$base/download/$tag/manet-release.json"
    }
    $manifest = Test-Manifest (Invoke-RestMethod -Headers $headers -TimeoutSec 60 -Uri $manifestUrl) $tag
    $channel = if ($env:MANET_DEVELOPMENT -eq '1') { 'development' } else { 'stable' }
    Write-Host "Using $channel release $($manifest.version)"
    return $manifest
}

function Save-Manifest($manifest, $path) {
    [IO.File]::WriteAllText($path, ($manifest | ConvertTo-Json -Depth 10), (New-Object Text.UTF8Encoding($false)))
}

# Check a flasher ZIP against its manifest entry, then extract it.
function Expand-Flasher($bundle, $asset, $destination) {
    if (-not $asset -or $asset.size -gt 16777216 -or $asset.sha256 -notmatch '^[0-9a-f]{64}$') { throw 'Invalid flasher asset.' }
    if (-not (Test-FileDigest $bundle $asset.size $asset.sha256)) {
        throw 'Flasher download does not match its checksum.'
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($bundle)
    try {
        $expanded = 0
        foreach ($entry in $archive.Entries) {
            $expanded += $entry.Length
            if ($entry.FullName -match '(^/|\\|(^|/)\.\.(/|$)|:)') { throw 'Unsafe flasher archive path.' }
        }
        if ($expanded -gt 33554432) { throw 'Flasher archive is too large.' }
    } finally { $archive.Dispose() }
    Expand-Archive -LiteralPath $bundle -DestinationPath $destination
    return $destination
}

# The offline bundle and Raspberry Pi OS image a kit carries.
function Get-OfflineAssets($manifest) {
    $image = $manifest.os_image
    if (-not $manifest.assets.'cm4-offline.tar' -or -not $image) {
        throw "Release $($manifest.version) has no offline bundle. Use a newer release."
    }
    $name = ($image.url -split '/')[-1]
    if ($name -notmatch '^[A-Za-z0-9_.-]+\.img\.xz$') { throw 'Invalid Raspberry Pi OS image in the release manifest.' }
    return [pscustomobject]@{ Image = $image; ImageName = $name }
}

function Save-KitFile($kit, $name, $url, $size, $sha256) {
    $target = Join-Path $kit $name
    if (Test-FileDigest $target $size $sha256) { Write-Host "  ${name}: already in the kit"; return }
    Write-Host ('  {0}: downloading {1:N0} MiB' -f $name, ($size / 1MB))
    $partial = "$target.partial"
    Invoke-WebRequest -UseBasicParsing -Headers $headers -TimeoutSec 3600 -Uri $url -OutFile $partial
    if (-not (Test-FileDigest $partial $size $sha256)) {
        Remove-Item -LiteralPath $partial -Force
        throw "$name does not match its checksum."
    }
    Move-Item -LiteralPath $partial -Destination $target -Force
}

# Fill a folder, such as a USB stick, with the launchers, setup scripts, image
# and packages for one release, so a computer without internet can flash CM4
# radios from it. Writing a folder needs no administrator access.
function New-OfflineKit($kit) {
    if ($env:MANET_OFFLINE -eq '1' -or $env:MANET_LOCAL_SCRIPTS -eq '1') {
        throw '--make-kit cannot be combined with --offline or --local-scripts.'
    }
    $manifest = Get-ReleaseManifest
    $offline = Get-OfflineAssets $manifest
    New-Item -ItemType Directory -Path $kit -Force | Out-Null
    $kit = (Resolve-Path -LiteralPath $kit).Path
    # Until the marker is written again, a half-filled kit is not a kit.
    Remove-Item -LiteralPath (Join-Path $kit $kitMarker) -Force -ErrorAction SilentlyContinue
    Write-Host "Filling $kit"
    foreach ($name in @('manet-flasher.zip', 'flash-a-radio.sh', 'Flash-a-Radio.cmd', 'cm4-install.tar.gz', 'cm4-offline.tar')) {
        $asset = $manifest.assets.$name
        if (-not $asset) { throw "Release $($manifest.version) is missing $name." }
        Save-KitFile $kit $name "$base/download/$($manifest.tag)/$name" $asset.size $asset.sha256
    }
    Save-KitFile $kit $offline.ImageName $offline.Image.url $offline.Image.size $offline.Image.sha256
    Save-Manifest $manifest (Join-Path $kit 'manet-release.json')
    $text = "MANET offline kit, release $($manifest.version)`r`n`r`n" +
        "This folder holds everything needed to flash CM4 radios that set`r`n" +
        "themselves up without internet, on a computer with no network.`r`n`r`n" +
        "Linux:   bash flash-a-radio.sh`r`n" +
        "Windows: double-click Flash-a-Radio.cmd`r`n`r`n" +
        "The computer needs Raspberry Pi Imager and rpiboot installed. Windows`r`n" +
        "needs Raspberry Pi Imager 2.0 or later. Saved settings are kept in a`r`n" +
        "folder here.`r`n"
    [IO.File]::WriteAllText((Join-Path $kit $kitMarker), $text)
    Write-Host "Offline kit for release $($manifest.version) is ready in $kit"
}

try {
    if ($env:MANET_MAKE_KIT) {
        New-OfflineKit $env:MANET_MAKE_KIT
        exit 0
    }
    $launcherDir = Split-Path -Parent $env:MANET_LAUNCHER
    $kit = $null
    foreach ($folder in @($launcherDir, (Split-Path -Parent $launcherDir))) {
        if ($folder -and (Test-Path -LiteralPath (Join-Path $folder $kitMarker))) { $kit = $folder; break }
    }
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin) {
        if ($env:MANET_ELEVATED -eq '1') { throw 'Administrator access is required to write a card.' }
        $arguments = @('--elevated')
        if ($env:MANET_CONSOLE -eq '1') { $arguments += '--console' }
        if ($env:MANET_DEVELOPMENT -eq '1') { $arguments += '--development' }
        if ($env:MANET_LOCAL_SCRIPTS -eq '1') { $arguments += '--local-scripts' }
        if ($env:MANET_OFFLINE -eq '1') { $arguments += '--offline' }
        $process = Start-Process -FilePath $env:MANET_LAUNCHER -ArgumentList $arguments -Verb RunAs -Wait -PassThru
        exit $process.ExitCode
    }
    $work = $launcherDir
    if (-not (Test-Path (Join-Path $work 'windows.ps1')) -and (Split-Path -Leaf $work) -ne 'MANET Flasher') {
        $work = Join-Path $work 'MANET Flasher'
        New-Item -ItemType Directory -Path $work -Force | Out-Null
        Copy-Item -LiteralPath $env:MANET_LAUNCHER -Destination (Join-Path $work 'Flash a Radio.cmd') -Force
    }
    $stage = Join-Path $work ('.release-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $stage | Out-Null
    $env:MANET_KIT_DIR = ''
    $env:MANET_OS_IMAGE = ''
    if ($kit) {
        # Everything comes from the kit. Nothing here uses the network.
        if ($env:MANET_DEVELOPMENT -eq '1' -or $env:MANET_LOCAL_SCRIPTS -eq '1') {
            throw 'An offline kit is fixed to one release; make a new kit to change it.'
        }
        $manifestFile = Join-Path $kit 'manet-release.json'
        $manifest = Test-Manifest (Get-Content -LiteralPath $manifestFile -Raw | ConvertFrom-Json) $null
        $offline = Get-OfflineAssets $manifest
        Write-Host "Using the offline kit for release $($manifest.version) in $kit"
        $scripts = Expand-Flasher (Join-Path $kit 'manet-flasher.zip') $manifest.assets.'manet-flasher.zip' (Join-Path $stage 'scripts')
        Write-Host 'Checking the Raspberry Pi OS image in the kit...'
        $image = Join-Path $kit $offline.ImageName
        if (-not (Test-FileDigest $image $offline.Image.size $offline.Image.sha256)) {
            throw "$($offline.ImageName) in the kit is missing or damaged. Make the kit again."
        }
        $env:MANET_OFFLINE = '1'
        $env:MANET_KIT_DIR = $kit
        $env:MANET_OS_IMAGE = $image
    } else {
        $manifest = Get-ReleaseManifest
        $manifestFile = Join-Path $stage 'manet-release.json'
        Save-Manifest $manifest $manifestFile
        if ($env:MANET_LOCAL_SCRIPTS -eq '1') {
            $scripts = $launcherDir
            if (-not (Test-Path (Join-Path $scripts 'windows.ps1'))) { throw '--local-scripts requires a source checkout.' }
        } else {
            $bundle = Join-Path $stage 'manet-flasher.zip'
            Invoke-WebRequest -UseBasicParsing -Headers $headers -TimeoutSec 120 -Uri "$base/download/$($manifest.tag)/manet-flasher.zip" -OutFile $bundle
            $scripts = Expand-Flasher $bundle $manifest.assets.'manet-flasher.zip' (Join-Path $stage 'scripts')
        }
    }
    $env:MANET_RELEASE_FILE = $manifestFile
    $env:MANET_FLASHER_WORK = $work
    $entry = if ($env:MANET_CONSOLE -eq '1') { 'windows.ps1' } else { 'manet-flasher.ps1' }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -STA -File (Join-Path $scripts $entry)
    $result = $LASTEXITCODE
} catch {
    Write-Host ('Unable to start MANET flasher: ' + $_.Exception.Message) -ForegroundColor Red
    $result = 1
} finally {
    if ($stage -and (Test-Path -LiteralPath $stage)) { Remove-Item -LiteralPath $stage -Recurse -Force }
}
exit $result
