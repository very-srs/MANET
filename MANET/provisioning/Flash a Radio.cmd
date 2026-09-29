@echo off
setlocal
set "MANET_LAUNCHER=%~f0"
set "MANET_DEVELOPMENT=0"
set "MANET_LOCAL_SCRIPTS=0"
set "MANET_ELEVATED=0"
set "MANET_CONSOLE=0"
:args
if "%~1"=="" goto :start
if /i "%~1"=="--development" (set "MANET_DEVELOPMENT=1") else if /i "%~1"=="--local-scripts" (set "MANET_LOCAL_SCRIPTS=1") else if /i "%~1"=="--console" (set "MANET_CONSOLE=1") else if /i "%~1"=="--elevated" (set "MANET_ELEVATED=1") else (echo Usage: "Flash a Radio.cmd" [--development] [--local-scripts] & exit /b 1)
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
try {
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin) {
        if ($env:MANET_ELEVATED -eq '1') { throw 'Administrator access is required to write a card.' }
        $arguments = @('--elevated')
        if ($env:MANET_CONSOLE -eq '1') { $arguments += '--console' }
        if ($env:MANET_DEVELOPMENT -eq '1') { $arguments += '--development' }
        if ($env:MANET_LOCAL_SCRIPTS -eq '1') { $arguments += '--local-scripts' }
        $process = Start-Process -FilePath $env:MANET_LAUNCHER -ArgumentList $arguments -Verb RunAs -Wait -PassThru
        exit $process.ExitCode
    }
    $work = Split-Path -Parent $env:MANET_LAUNCHER
    if (-not (Test-Path (Join-Path $work 'windows.ps1')) -and (Split-Path -Leaf $work) -ne 'MANET Flasher') {
        $work = Join-Path $work 'MANET Flasher'
        New-Item -ItemType Directory -Path $work -Force | Out-Null
        Copy-Item -LiteralPath $env:MANET_LAUNCHER -Destination (Join-Path $work 'Flash a Radio.cmd') -Force
    }
    $stage = Join-Path $work ('.release-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $stage | Out-Null
    $headers = @{'User-Agent'='manet-flasher'}
    $base = 'https://github.com/very-srs/MANET/releases'
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
    $manifest = Invoke-RestMethod -Headers $headers -TimeoutSec 60 -Uri $manifestUrl
    if ($manifest.schema -ne 1 -or $manifest.tag -notmatch '^v[0-9]+(?:\.[0-9]+)+$' -or $manifest.tag -ne ('v' + $manifest.version) -or ($tag -and $tag -ne $manifest.tag)) {
        throw 'Invalid MANET release manifest.'
    }
    $channel = if ($env:MANET_DEVELOPMENT -eq '1') { 'development' } else { 'stable' }
    Write-Host "Using $channel release $($manifest.version)"
    $manifestFile = Join-Path $stage 'manet-release.json'
    [IO.File]::WriteAllText($manifestFile, ($manifest | ConvertTo-Json -Depth 10), (New-Object Text.UTF8Encoding($false)))
    if ($env:MANET_LOCAL_SCRIPTS -eq '1') {
        $scripts = Split-Path -Parent $env:MANET_LAUNCHER
        if (-not (Test-Path (Join-Path $scripts 'windows.ps1'))) { throw '--local-scripts requires a source checkout.' }
    } else {
        $asset = $manifest.assets.'manet-flasher.zip'
        if (-not $asset -or $asset.size -gt 16777216 -or $asset.sha256 -notmatch '^[0-9a-f]{64}$') { throw 'Invalid flasher asset.' }
        $bundle = Join-Path $stage 'manet-flasher.zip'
        Invoke-WebRequest -UseBasicParsing -Headers $headers -TimeoutSec 120 -Uri "$base/download/$($manifest.tag)/manet-flasher.zip" -OutFile $bundle
        if ((Get-Item $bundle).Length -ne $asset.size -or (Get-FileHash -Algorithm SHA256 -LiteralPath $bundle).Hash.ToLowerInvariant() -ne $asset.sha256) {
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
        $scripts = Join-Path $stage 'scripts'
        Expand-Archive -LiteralPath $bundle -DestinationPath $scripts
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
