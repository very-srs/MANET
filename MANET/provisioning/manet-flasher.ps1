#Requires -RunAsAdministrator
<#
.SYNOPSIS
    A window for flashing mesh radio nodes on Windows.
.DESCRIPTION
    A front end over windows.ps1, not a second flasher. It dot-sources
    windows.ps1 with -NoRun and calls the same functions the console flow
    calls, so the image a card receives is decided in exactly one place and
    stays in step with linux.sh.

    It needs nothing installed. Windows PowerShell 5.1 and Windows Forms are
    part of Windows, and rpi-imager and rpiboot are downloaded and installed
    from the Prerequisites page for anyone who does not have them.

    Start it by double-clicking "Flash a Radio.cmd", which asks for
    Administrator and gets past the execution policy. Running this .ps1
    directly works too, from an elevated prompt.
#>

param(
    # Skip the elevation and Windows-only checks and just build the window.
    # For looking at the layout, nothing else; every flash path needs admin.
    [switch]$Preview
)

$ErrorActionPreference = 'Stop'

# ============================================================
# Bootstrap
# ============================================================

$FlasherDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$EnginePath = Join-Path $FlasherDir 'windows.ps1'

if (-not (Test-Path $EnginePath)) {
    [void][System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms')
    [System.Windows.Forms.MessageBox]::Show(
        "windows.ps1 is missing from:`r`n$FlasherDir`r`n`r`nThis window is only the front end; the flashing itself lives in windows.ps1.`r`n`r`nStart 'Flash a Radio.cmd' instead. It fetches whatever is missing.",
        "Files are missing", 'OK', 'Error') | Out-Null
    exit 1
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# Without this the window is drawn at 96 DPI and scaled up by Windows, which
# on the laptop screens these get run on looks like a blurred screenshot.
# SetProcessDPIAware has to be called before the first window is created.
try {
    Add-Type -Namespace Win32 -Name Dpi -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool SetProcessDPIAware();
'@
    [void][Win32.Dpi]::SetProcessDPIAware()
} catch { }

[System.Windows.Forms.Application]::EnableVisualStyles()
[System.Windows.Forms.Application]::SetCompatibleTextRenderingDefault($false)

# Dot-sourced, so the engine's $Script: variables land in this script's scope.
# Setting $Script:MESH_SSID here is the same variable Build-ProvisioningScript
# reads there.
. $EnginePath -NoRun

# ============================================================
# Look and feel
# ============================================================

$UI = @{
    Bg        = [System.Drawing.Color]::FromArgb(250, 250, 250)
    Panel     = [System.Drawing.Color]::White
    Header    = [System.Drawing.Color]::FromArgb(31, 56, 84)
    HeaderTxt = [System.Drawing.Color]::White
    Text      = [System.Drawing.Color]::FromArgb(28, 28, 28)
    Muted     = [System.Drawing.Color]::FromArgb(105, 105, 105)
    Good      = [System.Drawing.Color]::FromArgb(21, 128, 61)
    Warn      = [System.Drawing.Color]::FromArgb(180, 83, 9)
    Bad       = [System.Drawing.Color]::FromArgb(185, 28, 28)
    Font      = New-Object System.Drawing.Font("Segoe UI", 9.75)
    FontBold  = New-Object System.Drawing.Font("Segoe UI", 9.75, [System.Drawing.FontStyle]::Bold)
    FontTitle = New-Object System.Drawing.Font("Segoe UI", 15, [System.Drawing.FontStyle]::Regular)
    FontSmall = New-Object System.Drawing.Font("Segoe UI", 8.75)
    FontMono  = New-Object System.Drawing.Font("Consolas", 9)
}

function New-Text {
    param([string]$Text, [int]$X, [int]$Y, [int]$W = 560, [int]$H = 20,
          [System.Drawing.Font]$Font = $null, [System.Drawing.Color]$Color = $UI.Text)
    $l          = New-Object System.Windows.Forms.Label
    $l.Text     = $Text
    $l.Location = New-Object System.Drawing.Point($X, $Y)
    $l.Size     = New-Object System.Drawing.Size($W, $H)
    $l.Font     = if ($Font) { $Font } else { $UI.Font }
    $l.ForeColor= $Color
    return $l
}

function New-Input {
    param([int]$X, [int]$Y, [int]$W = 300, [switch]$Password)
    $t          = New-Object System.Windows.Forms.TextBox
    $t.Location = New-Object System.Drawing.Point($X, $Y)
    $t.Size     = New-Object System.Drawing.Size($W, 24)
    $t.Font     = $UI.Font
    if ($Password) { $t.UseSystemPasswordChar = $false }
    return $t
}

function New-Btn {
    param([string]$Text, [int]$X, [int]$Y, [int]$W = 110, [int]$H = 28, [scriptblock]$OnClick)
    $b           = New-Object System.Windows.Forms.Button
    $b.Text      = $Text
    $b.Location  = New-Object System.Drawing.Point($X, $Y)
    $b.Size      = New-Object System.Drawing.Size($W, $H)
    $b.Font      = $UI.Font
    $b.FlatStyle = 'System'
    if ($OnClick) { $b.Add_Click($OnClick) }
    return $b
}

function New-Check {
    param([string]$Text, [int]$X, [int]$Y, [int]$W = 420, [bool]$Checked = $false)
    $c           = New-Object System.Windows.Forms.CheckBox
    $c.Text      = $Text
    $c.Location  = New-Object System.Drawing.Point($X, $Y)
    $c.Size      = New-Object System.Drawing.Size($W, 22)
    $c.Font      = $UI.Font
    $c.Checked   = $Checked
    return $c
}

function New-Combo {
    param([int]$X, [int]$Y, [int]$W = 300, [string[]]$Items)
    $c               = New-Object System.Windows.Forms.ComboBox
    $c.Location      = New-Object System.Drawing.Point($X, $Y)
    $c.Size          = New-Object System.Drawing.Size($W, 24)
    $c.Font          = $UI.Font
    $c.DropDownStyle = 'DropDownList'
    foreach ($i in $Items) { [void]$c.Items.Add($i) }
    return $c
}

function New-GroupBox {
    param([string]$Text, [int]$X, [int]$Y, [int]$W, [int]$H)
    $g          = New-Object System.Windows.Forms.GroupBox
    $g.Text     = $Text
    $g.Location = New-Object System.Drawing.Point($X, $Y)
    $g.Size     = New-Object System.Drawing.Size($W, $H)
    $g.Font     = $UI.FontBold
    $g.ForeColor= $UI.Header
    return $g
}

# ============================================================
# Window state
# ============================================================

$Script:PageOrder   = @('Hardware', 'Prereqs', 'Config', 'Scripts', 'Target', 'Confirm', 'Flash', 'Done')
$Script:PageIndex   = 0
$Script:Pages       = @{}
$Script:PageTitles  = @{
    Hardware = 'Which radio are you building?'
    Prereqs  = 'Checking what this computer needs'
    Config   = 'Mesh settings'
    Scripts  = 'Your own setup scripts'
    Target   = 'Which card to write to'
    Confirm  = 'Last chance to check'
    Flash    = 'Writing the card'
    Done     = 'Finished'
}
$Script:PageBlurbs  = @{
    Hardware = 'Every node on one mesh must be flashed with the same mesh settings. The board type can differ.'
    Prereqs  = 'Anything missing can be installed from here. Nothing is downloaded until you ask for it.'
    Config   = 'These are baked into the card. Saved configurations are shared with the Linux flasher.'
    Scripts  = 'Files in the additional-scripts folder are embedded and run once on the node after setup.'
    Target   = 'Everything on the disk you pick will be destroyed. Check the size and the label.'
    Confirm  = 'Read this back before anything is written.'
    Flash    = 'Leave the card in place until this finishes.'
    Done     = 'Write these down before you close the window. They are not shown again.'
}

# Long jobs never run on the window thread. Each one is a step function the
# timer calls a few times a second; it returns $true while it has more to do.
# That keeps the window painting and the Cancel button alive without a second
# runspace, and every job in here uses the same shape.
$Script:Worker = @{
    Active   = $false
    Step     = $null
    Finish   = $null
    Percent  = 0
    Status   = ''
    Cancel   = $false
}

$Script:FlashCount    = 0
$Script:ScriptReport  = $null
$Script:CandidateList = @()
$Script:R3aTempImage  = ''
$Script:LoadedConfig  = ''

# ============================================================
# Routing the engine's output into the window
# ============================================================
#
# windows.ps1 reports what it is doing with Write-Host, and it is dot-sourced
# into this scope, so a function of that name here wins over the cmdlet and
# every one of those lines lands in the log pane instead of a console nobody
# can see. Read-Host is stubbed to throw for the same reason in reverse: the
# functions this window calls are the non-interactive ones, and if that ever
# stops being true it must fail loudly rather than block on a hidden prompt.

$Script:LogBox = $null
$Script:LogBuf = New-Object System.Collections.ArrayList

function Add-Log {
    param([string]$Line)
    [void]$Script:LogBuf.Add($Line)
    if ($Script:LogBox) {
        $Script:LogBox.AppendText($Line + "`r`n")
        $Script:LogBox.SelectionStart = $Script:LogBox.TextLength
        $Script:LogBox.ScrollToCaret()
    }
}

function Write-Host {
    param(
        [Parameter(Position = 0, ValueFromPipeline = $true)] $Object,
        $ForegroundColor, $BackgroundColor, $Separator,
        [switch]$NoNewline
    )
    $text = if ($null -eq $Object) { "" } else { [string]$Object }
    Add-Log $text
}

function Write-Progress {
    param($Activity, $Status, $PercentComplete, $Id, $CurrentOperation, [switch]$Completed)
    if ($PSBoundParameters.ContainsKey('PercentComplete')) {
        $Script:Worker.Percent = [int]$PercentComplete
    }
    if ($Status) { $Script:Worker.Status = [string]$Status }
}

function Read-Host {
    param($Prompt, [switch]$AsSecureString)
    throw "Read-Host was reached from the window ('$Prompt'). That code path is console-only."
}

# The console behind the window is only noise for the people this is for.
# It is hidden rather than not created, because a .ps1 needs a host process
# and PowerShell always brings one.
function Set-ConsoleWindowVisible {
    param([bool]$Visible)
    try {
        if (-not ('Win32.Con' -as [type])) {
            Add-Type -Namespace Win32 -Name Con -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("kernel32.dll")]
public static extern System.IntPtr GetConsoleWindow();
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool ShowWindow(System.IntPtr hWnd, int nCmdShow);
'@
        }
        $h = [Win32.Con]::GetConsoleWindow()
        if ($h -ne [System.IntPtr]::Zero) { [void][Win32.Con]::ShowWindow($h, $(if ($Visible) { 5 } else { 0 })) }
    } catch { }
}

# ============================================================
# Prerequisites
# ============================================================
#
# The console flow finds these when it needs them and sends the user away to
# install anything missing. Here they are all checked up front and installed
# from inside the window, because "download this from GitHub, find the right
# asset, run it" is the step that loses people.

function Get-PrereqList {
    param([string]$Hardware, [bool]$IsCm4)

    $list = New-Object System.Collections.ArrayList

    if ($Hardware -ne 'r3a') {
        [void]$list.Add([pscustomobject]@{
            Key      = 'rpi-imager'
            Name     = 'Raspberry Pi Imager'
            Why      = 'Writes the operating system to the card.'
            Required = $true
            Source   = 'direct'
            Url      = 'https://downloads.raspberrypi.com/imager/imager_latest.exe'
            Page     = 'https://www.raspberrypi.com/software/'
            File     = 'imager_latest.exe'
            Found    = $false
            Detail   = ''
            Path     = $null
        })
    }

    if ($IsCm4) {
        [void]$list.Add([pscustomobject]@{
            Key      = 'rpiboot'
            Name     = 'rpiboot (usbboot)'
            Why      = 'Makes the CM4 eMMC appear as a disk over USB. Needed for CM4 only.'
            Required = $true
            Source   = 'github'
            Repo     = 'raspberrypi/usbboot'
            Match    = 'setup'
            Page     = 'https://github.com/raspberrypi/usbboot/releases'
            File     = 'rpiboot_setup.exe'
            Found    = $false
            Detail   = ''
            Path     = $null
        })
    }

    if ($Hardware -eq 'r3a') {
        [void]$list.Add([pscustomobject]@{
            Key      = 'ext2fsd'
            # SourceForge, not GitHub: github.com/matt-wu/Ext2Fsd answers 404
            # and has done for a while, so anything pointing there sends people
            # to a dead page. There is no release API here to pick an asset
            # from, so this one is a link rather than an install button.
            Name     = 'Ext2Fsd'
            Why      = 'Lets Windows write to the Linux root partition of the Armbian image. After installing it, open Ext2 Volume Manager and start the service from Tools, Service Management.'
            Required = $true
            Source   = 'page'
            Page     = 'https://sourceforge.net/projects/ext2fsd/'
            Found    = $false
            Detail   = ''
            Path     = $null
        })
        [void]$list.Add([pscustomobject]@{
            Key      = 'xz'
            Name     = '7-Zip'
            Why      = 'Unpacks the .img.xz Armbian download. WSL will do instead if you have it.'
            Required = $true
            Source   = 'page'
            Page     = 'https://www.7-zip.org/download.html'
            Found    = $false
            Detail   = ''
            Path     = $null
        })
        [void]$list.Add([pscustomobject]@{
            Key      = 'openssl'
            Name     = 'openssl or WSL'
            Why      = 'Hashes the radio password. Without it the radio account ships locked and you set the password from the console on first boot.'
            Required = $false
            Source   = 'page'
            Page     = 'https://gitforwindows.org/'
            Found    = $false
            Detail   = ''
            Path     = $null
        })
    }

    return $list.ToArray()
}

function Test-Prereq {
    param($P)

    switch ($P.Key) {
        'rpi-imager' {
            $p1 = Find-InstalledProgram -CommandNames @('rpi-imager-cli.cmd','rpi-imager.exe','rpi-imager') `
                    -RelativePaths @(
                        'Raspberry Pi Ltd\Imager\rpi-imager-cli.cmd',
                        'Raspberry Pi Ltd\Imager\rpi-imager.exe',
                        'Raspberry Pi Imager\rpi-imager.exe',
                        'Raspberry Pi\Imager\rpi-imager.exe') `
                    -CacheKey 'rpi-imager'
            $P.Path = $p1; $P.Found = [bool]$p1
            $P.Detail = if ($p1) { $p1 } else { 'not installed' }
        }
        'rpiboot' {
            $p1 = Find-InstalledProgram -CommandNames @('rpiboot.exe','rpiboot') `
                    -RelativePaths @(
                        'Raspberry Pi\rpiboot.exe',
                        'Raspberry Pi\usbboot\rpiboot.exe',
                        'Raspberry Pi Ltd\rpiboot\rpiboot.exe',
                        'usbboot\rpiboot.exe',
                        'rpiboot\rpiboot.exe',
                        'rpiboot.exe') `
                    -CacheKey 'rpiboot'
            $P.Path = $p1; $P.Found = [bool]$p1
            $P.Detail = if ($p1) { $p1 } else { 'not installed' }
        }
        'ext2fsd' {
            $ok = Test-Ext4Driver
            $P.Found  = $ok
            $P.Detail = if ($ok) { 'service Ext2Srv is running' } else { 'service Ext2Srv not found or stopped' }
        }
        'xz' {
            $sz = @('C:\Program Files\7-Zip\7z.exe','C:\Program Files (x86)\7-Zip\7z.exe') |
                  Where-Object { Test-Path $_ } | Select-Object -First 1
            if (-not $sz) { $sz = (Get-Command 7z -ErrorAction SilentlyContinue).Source }
            if ($sz) {
                $P.Found = $true; $P.Path = $sz; $P.Detail = $sz
            } elseif (Get-Command wsl -ErrorAction SilentlyContinue) {
                $P.Found = $true; $P.Detail = 'WSL is available and will be used'
            } else {
                $P.Found = $false; $P.Detail = 'no 7-Zip and no WSL'
            }
        }
        'openssl' {
            if (Get-Command openssl -ErrorAction SilentlyContinue) {
                $P.Found = $true;  $P.Detail = 'openssl is on PATH'
            } elseif (Get-Command wsl -ErrorAction SilentlyContinue) {
                $P.Found = $true;  $P.Detail = 'WSL is available and will be used'
            } else {
                $P.Found = $false; $P.Detail = 'the radio account will ship with no password set'
            }
        }
    }
    return $P.Found
}

# The search half of Find-ProgramPath with the asking half left out. The window
# asks in its own way, so the console dialogs must not appear behind it.
function Find-InstalledProgram {
    param([string[]]$CommandNames = @(), [string[]]$RelativePaths = @(), [string]$CacheKey)

    if ($CacheKey) {
        $cached = Get-CachedToolPath -Key $CacheKey
        if ($cached) { return $cached }
    }
    foreach ($name in $CommandNames) {
        $onPath = (Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1).Source
        if ($onPath -and (Test-Path -LiteralPath $onPath -PathType Leaf)) { return $onPath }
    }
    foreach ($root in (Get-ProgramSearchRoots)) {
        foreach ($relative in $RelativePaths) {
            $candidate = Join-Path $root $relative
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
        }
    }
    return $null
}

# The download URL for the newest release asset whose name contains $Match.
# Pinning a version would mean editing this file every time upstream releases,
# and an old rpiboot does not know about new silicon revisions.
function Get-GithubAssetUrl {
    param([string]$Repo, [string]$Match)
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
    $headers = @{ 'User-Agent' = 'manet-flasher' }

    # /releases/latest answers 404 on a repo whose newest release is a
    # pre-release, or that has none marked latest at all, so the full list is
    # the fallback rather than an immediate give-up.
    foreach ($url in @("https://api.github.com/repos/$Repo/releases/latest",
                       "https://api.github.com/repos/$Repo/releases")) {
        try {
            $json     = Invoke-RestMethod -Uri $url -Headers $headers -TimeoutSec 20
            $releases = if ($json -is [array]) { $json } else { @($json) }
            foreach ($rel in $releases) {
                $asset = $rel.assets |
                         Where-Object { $_.name -like "*$Match*" -and $_.name -like '*.exe' } |
                         Select-Object -First 1
                if ($asset) { return $asset.browser_download_url }
            }
        } catch { }
    }
    return $null
}

# ============================================================
# The step machine for long jobs
# ============================================================

$Script:ProgressBar   = $null
$Script:ProgressLabel = $null

function Start-Job2 {
    param([scriptblock]$Step, [scriptblock]$Finish, [string]$Status = '')
    $Script:Worker.Step    = $Step
    $Script:Worker.Finish  = $Finish
    $Script:Worker.Status  = $Status
    $Script:Worker.Percent = 0
    $Script:Worker.Cancel  = $false
    $Script:Worker.Active  = $true
    Update-Nav
}

function Stop-Job2 {
    $Script:Worker.Active = $false
    $Script:Worker.Step   = $null
    $Script:Worker.Finish = $null
    Update-Nav
}

function Invoke-WorkerTick {
    if (-not $Script:Worker.Active) { return }

    $more = $false
    try {
        $more = & $Script:Worker.Step
    } catch {
        Add-Log "ERROR: $($_.Exception.Message)"
        $finish = $Script:Worker.Finish
        Stop-Job2
        if ($finish) { & $finish $false "$($_.Exception.Message)" }
        return
    }

    if ($Script:ProgressBar) {
        $p = [math]::Max(0, [math]::Min(100, [int]$Script:Worker.Percent))
        $Script:ProgressBar.Value = $p
    }
    if ($Script:ProgressLabel) { $Script:ProgressLabel.Text = $Script:Worker.Status }

    if (-not $more) {
        $finish = $Script:Worker.Finish
        Stop-Job2
        if ($finish) { & $finish $true '' }
    }
}

# Streams a download a chunk at a time so the window keeps painting and the
# progress bar means something. Invoke-WebRequest would be one line and would
# freeze everything for the length of a 500 MB transfer.
#
# Every job below keeps its state in $Script:Worker rather than in a closure.
# GetNewClosure binds a scriptblock to a new module, where $Script: is that
# module's scope and not this script's, so a step that captured its state that
# way would be writing progress somewhere nothing reads.
function Start-DownloadJob {
    param([string]$Url, [string]$OutFile, [string]$Label, [scriptblock]$OnDone)

    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
    $req  = [System.Net.HttpWebRequest]::Create($Url)
    $req.UserAgent = 'manet-flasher'
    $req.Timeout   = 30000
    $resp = $req.GetResponse()

    $Script:Worker.DlResp   = $resp
    $Script:Worker.DlStream = $resp.GetResponseStream()
    $Script:Worker.DlOut    = [System.IO.File]::Open($OutFile, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write)
    $Script:Worker.DlBuf    = New-Object byte[] (256KB)
    $Script:Worker.DlGot    = 0
    $Script:Worker.DlTotal  = $resp.ContentLength
    $Script:Worker.DlFile   = $OutFile
    $Script:Worker.DlLabel  = $Label

    Add-Log "Downloading $Label"
    Add-Log "  from $Url"

    Start-Job2 -Step $Script:DownloadStep -Finish $OnDone -Status $Label
}

$Script:DownloadStep = {
    $w = $Script:Worker
    if ($w.Cancel) {
        $w.DlOut.Close(); $w.DlStream.Close(); $w.DlResp.Close()
        Remove-Item $w.DlFile -Force -ErrorAction SilentlyContinue
        throw "Cancelled."
    }
    # A handful of chunks per tick: enough throughput to not be the
    # bottleneck, short enough that the window still feels alive.
    for ($i = 0; $i -lt 8; $i++) {
        $read = $w.DlStream.Read($w.DlBuf, 0, $w.DlBuf.Length)
        if ($read -le 0) {
            $w.DlOut.Flush(); $w.DlOut.Close(); $w.DlStream.Close(); $w.DlResp.Close()
            $w.Percent = 100
            Add-Log "  done, $([math]::Round($w.DlGot/1MB,1)) MB"
            return $false
        }
        $w.DlOut.Write($w.DlBuf, 0, $read)
        $w.DlGot += $read
    }
    if ($w.DlTotal -gt 0) {
        $w.Percent = [int](($w.DlGot / $w.DlTotal) * 100)
        $w.Status  = "$($w.DlLabel) - $([math]::Round($w.DlGot/1MB,1)) MB of $([math]::Round($w.DlTotal/1MB,1)) MB"
    } else {
        $w.Status  = "$($w.DlLabel) - $([math]::Round($w.DlGot/1MB,1)) MB"
    }
    return $true
}

# Runs a program and waits without blocking the window. Output is redirected to
# a file and tailed into the log, so rpi-imager's progress is visible here
# rather than in a console that has been hidden.
# Start-Process joins an argument array with plain spaces and quotes nothing,
# so a path with a space in it - C:\Users\Anne Smith\Downloads\provisioning -
# arrives at the child as two arguments. The console flow never hit this
# because the call operator quotes for you.
function Format-ProcessArgument {
    param([string]$Value)
    if ($Value -match '[\s"]') { return '"' + ($Value -replace '"', '\"') + '"' }
    return $Value
}

function Start-ProcessJob {
    param([string]$Path, [string[]]$Arguments = @(), [string]$Label, [scriptblock]$OnDone)

    $Script:Worker.PrOut   = [System.IO.Path]::GetTempFileName()
    $Script:Worker.PrErr   = [System.IO.Path]::GetTempFileName()
    $Script:Worker.PrShown = 0
    $Script:Worker.PrLabel = $Label

    $argLine = (@($Arguments | ForEach-Object { Format-ProcessArgument $_ }) -join ' ')

    # Redirecting output means CreateProcess rather than ShellExecute, and that
    # cannot start a .cmd or .bat on its own. rpi-imager installs as
    # rpi-imager-cli.cmd, so this is the normal case, not an edge one.
    $exe = $Path
    if ([System.IO.Path]::GetExtension($Path) -in @('.cmd', '.bat')) {
        $exe     = $env:ComSpec
        $argLine = "/c " + (Format-ProcessArgument $Path) + $(if ($argLine) { " $argLine" } else { "" })
    }

    Add-Log "Running $Label"
    $splat = @{
        FilePath               = $exe
        NoNewWindow            = $true
        PassThru               = $true
        RedirectStandardOutput = $Script:Worker.PrOut
        RedirectStandardError  = $Script:Worker.PrErr
    }
    if ($argLine) { $splat['ArgumentList'] = $argLine }
    $Script:Worker.PrProc = Start-Process @splat

    Start-Job2 -Step $Script:ProcessStep -Finish $OnDone -Status $Label
}

$Script:ProcessStep = {
    $w = $Script:Worker

    # Tail whatever the child has written since the last tick. Read shared so
    # the running process is not locked out of its own output file.
    $lines = @()
    try { $lines = @(Get-Content -LiteralPath $w.PrOut -ErrorAction SilentlyContinue) } catch { }
    if ($lines.Count -gt $w.PrShown) {
        for ($i = $w.PrShown; $i -lt $lines.Count; $i++) { Add-Log "  $($lines[$i])" }
        $w.PrShown = $lines.Count
    }

    if ($w.PrProc.HasExited) {
        try {
            $tail = @(Get-Content -LiteralPath $w.PrOut -ErrorAction SilentlyContinue)
            for ($i = $w.PrShown; $i -lt $tail.Count; $i++) { Add-Log "  $($tail[$i])" }
            $err = (Get-Content -LiteralPath $w.PrErr -Raw -ErrorAction SilentlyContinue)
            if ($err -and $err.Trim()) { Add-Log "  $($err.Trim())" }
        } catch { }
        Remove-Item $w.PrOut, $w.PrErr -Force -ErrorAction SilentlyContinue
        $w.ExitCode = $w.PrProc.ExitCode
        return $false
    }
    $w.Status = "$($w.PrLabel) is running..."
    return $true
}

# The Rock 3A raw write, one buffer per tick for the same reason.
function Start-RawWriteJob {
    param([string]$ImagePath, [int]$DiskNumber, [scriptblock]$OnDone)

    $Script:Worker.WrSrc = [System.IO.File]::OpenRead($ImagePath)
    $Script:Worker.WrDst = [System.IO.File]::Open("\\.\PhysicalDrive$DiskNumber",
                              [System.IO.FileMode]::Open, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    $Script:Worker.WrBuf     = New-Object byte[] (4MB)
    $Script:Worker.WrWritten = 0
    $Script:Worker.WrTotal   = $Script:Worker.WrSrc.Length

    Start-Job2 -Step $Script:RawWriteStep -Finish $OnDone -Status "Writing the image"
}

$Script:RawWriteStep = {
    $w = $Script:Worker
    if ($w.Cancel) {
        $w.WrSrc.Close(); $w.WrDst.Close()
        throw "Cancelled part way through writing. The card is not usable; flash it again."
    }
    for ($i = 0; $i -lt 4; $i++) {
        $read = $w.WrSrc.Read($w.WrBuf, 0, $w.WrBuf.Length)
        if ($read -le 0) {
            $w.WrDst.Flush(); $w.WrSrc.Close(); $w.WrDst.Close()
            $w.Percent = 100
            return $false
        }
        $w.WrDst.Write($w.WrBuf, 0, $read)
        $w.WrWritten += $read
    }
    $w.Percent = [int](($w.WrWritten / $w.WrTotal) * 100)
    $w.Status  = "Writing - $([math]::Round($w.WrWritten/1GB,2)) GB of $([math]::Round($w.WrTotal/1GB,2)) GB"
    return $true
}

# ============================================================
# Page 1: hardware
# ============================================================

$Script:CONTENT_W = 838
$Script:CONTENT_H = 424

function New-ContentPanel {
    $p           = New-Object System.Windows.Forms.Panel
    $p.Size      = New-Object System.Drawing.Size($Script:CONTENT_W, $Script:CONTENT_H)
    $p.Location  = New-Object System.Drawing.Point(0, 0)
    $p.BackColor = $UI.Panel
    $p.Visible   = $false
    return $p
}

function Build-HardwarePage {
    $p = New-ContentPanel

    $p.Controls.Add((New-Text 'Pick the board you are flashing. This decides which tools are needed and how the card is written.' 24 18 780 20 $UI.Font $UI.Muted))

    $Script:RbCm4  = New-Object System.Windows.Forms.RadioButton
    $Script:RbRpi5 = New-Object System.Windows.Forms.RadioButton
    $Script:RbRpi4 = New-Object System.Windows.Forms.RadioButton
    $Script:RbR3a  = New-Object System.Windows.Forms.RadioButton

    $rows = @(
        @{ Rb = $Script:RbCm4;  T = 'Compute Module 4 (CM4)';  D = 'The current reference board. Written over USB with the module in boot mode.' },
        @{ Rb = $Script:RbRpi5; T = 'Raspberry Pi 5';           D = 'SD card. Supported, but deprioritized: it runs hot in a sealed enclosure.' },
        @{ Rb = $Script:RbRpi4; T = 'Raspberry Pi 4B';          D = 'SD card.' },
        @{ Rb = $Script:RbR3a;  T = 'Radxa Rock 3A';            D = 'SD card. Needs Ext2Fsd and 7-Zip. Not a priority platform.' }
    )

    $y = 58
    foreach ($r in $rows) {
        $rb           = $r.Rb
        $rb.Text      = $r.T
        $rb.Font      = $UI.FontBold
        $rb.ForeColor = $UI.Text
        $rb.Location  = New-Object System.Drawing.Point(28, $y)
        $rb.Size      = New-Object System.Drawing.Size(500, 24)
        $p.Controls.Add($rb)
        $p.Controls.Add((New-Text $r.D 50 ($y + 24) 720 18 $UI.FontSmall $UI.Muted))
        $y += 62
    }

    $Script:RbCm4.Checked = $true

    $note = New-Text ("The mesh settings on the next pages must match on every node, or they will not see each other. " +
                      "Save them once and load the same saved configuration for each card.") 28 ($y + 8) 760 40 $UI.FontSmall $UI.Muted
    $p.Controls.Add($note)

    return $p
}

function Get-SelectedHardware {
    if ($Script:RbR3a.Checked)  { return @{ Model = 'r3a';  IsCm4 = $false } }
    if ($Script:RbRpi5.Checked) { return @{ Model = 'rpi5'; IsCm4 = $false } }
    if ($Script:RbRpi4.Checked) { return @{ Model = 'rpi4'; IsCm4 = $false } }
    return @{ Model = 'rpi4'; IsCm4 = $true }     # CM4 flashes as rpi4, same as the console flow
}

# ============================================================
# Page 2: prerequisites
# ============================================================

function Build-PrereqPage {
    $p = New-ContentPanel

    $Script:PrereqHost            = New-Object System.Windows.Forms.Panel
    $Script:PrereqHost.Location   = New-Object System.Drawing.Point(20, 14)
    $Script:PrereqHost.Size       = New-Object System.Drawing.Size(798, 300)
    $Script:PrereqHost.AutoScroll = $true
    $p.Controls.Add($Script:PrereqHost)

    $Script:PrereqProgress          = New-Object System.Windows.Forms.ProgressBar
    $Script:PrereqProgress.Location = New-Object System.Drawing.Point(20, 328)
    $Script:PrereqProgress.Size     = New-Object System.Drawing.Size(798, 16)
    $Script:PrereqProgress.Visible  = $false
    $p.Controls.Add($Script:PrereqProgress)

    $Script:PrereqStatus = New-Text '' 20 350 640 20 $UI.FontSmall $UI.Muted
    $p.Controls.Add($Script:PrereqStatus)

    $p.Controls.Add((New-Btn 'Check again' 706 346 112 28 { Update-PrereqPage }))

    return $p
}

function Update-PrereqPage {
    $host_ = $Script:PrereqHost
    $host_.Controls.Clear()

    $hw = Get-SelectedHardware
    if (-not $Script:PrereqItems) { $Script:PrereqItems = Get-PrereqList -Hardware $hw.Model -IsCm4 $hw.IsCm4 }

    $y = 6
    foreach ($item in $Script:PrereqItems) {
        [void](Test-Prereq $item)

        $mark = if ($item.Found) { 'OK' } elseif ($item.Required) { 'MISSING' } else { 'optional' }
        $col  = if ($item.Found) { $UI.Good } elseif ($item.Required) { $UI.Bad } else { $UI.Warn }

        $lblMark = New-Text $mark 4 ($y + 2) 80 20 $UI.FontBold $col
        $host_.Controls.Add($lblMark)

        $host_.Controls.Add((New-Text $item.Name 92 ($y + 2) 340 20 $UI.FontBold $UI.Text))
        $host_.Controls.Add((New-Text $item.Why 92 ($y + 22) 560 34 $UI.FontSmall $UI.Muted))
        $host_.Controls.Add((New-Text $item.Detail 92 ($y + 56) 560 18 $UI.FontSmall $col))

        if (-not $item.Found) {
            $it = $item
            # The prerequisite each button acts on rides on the button's Tag.
            # A closure would capture $it into a module scope where $Script:
            # no longer means this script.
            if ($it.Source -eq 'page') {
                $b = New-Btn 'Get it...' 664 ($y + 4) 118 28
                $b.Tag = $it
                $b.Add_Click({ Start-Process $this.Tag.Page })
            } else {
                $b = New-Btn 'Install it' 664 ($y + 4) 118 28
                $b.Tag = $it
                $b.Add_Click({ Install-Prereq $this.Tag })
            }
            $host_.Controls.Add($b)

            $bl = New-Btn 'I have it...' 664 ($y + 36) 118 26
            $bl.Tag  = $it
            $bl.Font = $UI.FontSmall
            $bl.Add_Click({ Locate-Prereq $this.Tag })
            $host_.Controls.Add($bl)
        }

        $sep           = New-Object System.Windows.Forms.Label
        $sep.Location  = New-Object System.Drawing.Point(4, ($y + 82))
        $sep.Size      = New-Object System.Drawing.Size(778, 1)
        $sep.BorderStyle = 'Fixed3D'
        $host_.Controls.Add($sep)

        $y += 96
    }

    Update-Nav
}

function Test-PrereqsSatisfied {
    if (-not $Script:PrereqItems) { return $false }
    foreach ($i in $Script:PrereqItems) {
        if ($i.Required -and -not $i.Found) { return $false }
    }
    return $true
}

# Point at an existing copy. Reuses the console flow's picker and its cache, so
# an answer given here is the answer windows.ps1 finds on its own next time.
function Locate-Prereq {
    param($P)
    $picked = Show-ProgramFilePicker -DisplayName $P.Name `
                -Filter 'Programs (*.exe;*.cmd)|*.exe;*.cmd|All files (*.*)|*.*' `
                -InitialDirectory $env:ProgramFiles
    if (-not $picked) { return }
    if ($P.Key -in @('rpi-imager','rpiboot','xz')) {
        Set-CachedToolPath -Key $P.Key -Path $picked
    }
    $P.Path = $picked; $P.Found = $true; $P.Detail = $picked
    Update-PrereqPage
}

function Install-Prereq {
    param($P)

    $Script:ProgressBar   = $Script:PrereqProgress
    $Script:ProgressLabel = $Script:PrereqStatus
    $Script:PrereqProgress.Visible = $true
    $Script:PendingPrereq = $P

    $url = $P.Url
    if ($P.Source -eq 'github') {
        $Script:PrereqStatus.Text = "Looking up the latest $($P.Name) release..."
        [System.Windows.Forms.Application]::DoEvents()
        $url = Get-GithubAssetUrl -Repo $P.Repo -Match $P.Match
        if (-not $url) {
            $Script:PrereqProgress.Visible = $false
            [System.Windows.Forms.MessageBox]::Show($Script:Form,
                "Could not reach GitHub to find the $($P.Name) installer.`r`n`r`nThe releases page will open instead. Download the installer, run it, then choose 'Check again'.",
                'Could not fetch it', 'OK', 'Warning') | Out-Null
            Start-Process $P.Page
            return
        }
    }

    $Script:PendingInstaller = Join-Path ([System.IO.Path]::GetTempPath()) $P.File

    try {
        Start-DownloadJob -Url $url -OutFile $Script:PendingInstaller `
                          -Label "$($P.Name) installer" -OnDone $Script:AfterPrereqDownload
    } catch {
        $Script:PrereqProgress.Visible = $false
        $Script:PrereqStatus.Text = "Could not start the download: $($_.Exception.Message)"
    }
}

$Script:AfterPrereqDownload = {
    param($ok, $err)
    if (-not $ok) {
        $Script:PrereqProgress.Visible = $false
        $Script:PrereqStatus.Text = "Download failed: $err"
        return
    }
    $Script:PrereqStatus.Text = "Running the $($Script:PendingPrereq.Name) installer. Click through it, then come back here."
    # No silent switches. These are third-party installers whose flags are not
    # ours to assume, and one that silently does the wrong thing is worse than
    # three clicks the user can see.
    Start-ProcessJob -Path $Script:PendingInstaller `
                     -Label "$($Script:PendingPrereq.Name) installer" `
                     -OnDone $Script:AfterPrereqInstall
}

$Script:AfterPrereqInstall = {
    param($ok, $err)
    $Script:PrereqProgress.Visible = $false
    Remove-Item $Script:PendingInstaller -Force -ErrorAction SilentlyContinue
    $name = $Script:PendingPrereq.Name
    Update-PrereqPage
    if ($Script:PendingPrereq.Found) {
        $Script:PrereqStatus.Text = "$name is installed."
        $Script:PrereqStatus.ForeColor = $UI.Good
    } else {
        $Script:PrereqStatus.Text = "$name still is not showing up. Try 'I have it...' and point at it."
        $Script:PrereqStatus.ForeColor = $UI.Warn
    }
}

# ============================================================
# Page 3: mesh settings
# ============================================================

function Build-ConfigPage {
    $p = New-ContentPanel

    # --- saved configurations -------------------------------------------
    $g1 = New-GroupBox 'Saved settings' 16 6 398 70
    $Script:CmbSaved = New-Combo 12 26 250 @()
    $g1.Controls.Add($Script:CmbSaved)
    $g1.Controls.Add((New-Btn 'Load' 270 25 112 26 { Load-SelectedConfig }))
    $p.Controls.Add($g1)

    # --- clients ---------------------------------------------------------
    $g2 = New-GroupBox 'Clients (EUDs)' 16 84 398 160
    $g2.Controls.Add((New-Text 'Connection' 12 27 124 20))
    $Script:CmbEud = New-Combo 140 24 230 @('Wired', 'Wireless', 'Auto')
    $Script:CmbEud.SelectedIndex = 2
    $Script:CmbEud.Add_SelectedIndexChanged({ Update-ConfigEnablement })
    $g2.Controls.Add($Script:CmbEud)

    $g2.Controls.Add((New-Text 'Wi-Fi name' 12 55 124 20))
    $Script:TxtApSsid = New-Input 140 52 230
    $g2.Controls.Add($Script:TxtApSsid)
    $g2.Controls.Add((New-Text 'The last 4 of the wired MAC is added to this, so each node is identifiable.' 12 78 370 16 $UI.FontSmall $UI.Muted))

    $g2.Controls.Add((New-Text 'Wi-Fi password' 12 99 124 20))
    $Script:TxtApKey = New-Input 140 96 144
    $g2.Controls.Add($Script:TxtApKey)
    $g2.Controls.Add((New-Btn 'Generate' 290 95 88 24 {
        $bytes = New-Object byte[] 10
        [Security.Cryptography.RNGCryptoServiceProvider]::Create().GetBytes($bytes)
        $Script:TxtApKey.Text = [Convert]::ToBase64String($bytes)
    }))

    $g2.Controls.Add((New-Text 'Clients per node' 12 127 124 20))
    $Script:NumEuds          = New-Object System.Windows.Forms.NumericUpDown
    $Script:NumEuds.Location = New-Object System.Drawing.Point(140, 124)
    $Script:NumEuds.Size     = New-Object System.Drawing.Size(60, 24)
    $Script:NumEuds.Font     = $UI.Font
    $Script:NumEuds.Minimum  = 1
    $Script:NumEuds.Maximum  = 20
    $Script:NumEuds.Value    = 4
    $Script:NumEuds.Add_ValueChanged({ Update-CapacityLabel })
    $g2.Controls.Add($Script:NumEuds)
    $p.Controls.Add($g2)

    # --- services --------------------------------------------------------
    $g3 = New-GroupBox 'Services on the node' 16 252 398 126
    $Script:ChkMtx    = New-Check 'MediaMTX video server'                12 24 360 $true
    $Script:ChkMumble = New-Check 'Mumble voice server (murmur)'         12 48 360 $true
    $Script:ChkVoice  = New-Check 'Mesh push-to-talk (needs an OpenVLM board)' 12 72 360 $false
    $Script:ChkUpdate = New-Check 'Update MANET tools automatically'     12 96 360 $true
    $g3.Controls.AddRange(@($Script:ChkMtx, $Script:ChkMumble, $Script:ChkVoice, $Script:ChkUpdate))
    $p.Controls.Add($g3)

    # --- mesh ------------------------------------------------------------
    $g4 = New-GroupBox 'The mesh' 424 6 398 150
    $g4.Controls.Add((New-Text 'Mesh name' 12 27 124 20))
    $Script:TxtMeshSsid = New-Input 140 24 230
    $g4.Controls.Add($Script:TxtMeshSsid)

    $g4.Controls.Add((New-Text 'Mesh password' 12 55 124 20))
    $Script:TxtMeshKey = New-Input 140 52 144
    $g4.Controls.Add($Script:TxtMeshKey)
    $g4.Controls.Add((New-Btn 'Generate' 290 51 88 24 {
        $bytes = New-Object byte[] 33
        [Security.Cryptography.RNGCryptoServiceProvider]::Create().GetBytes($bytes)
        $Script:TxtMeshKey.Text = [Convert]::ToBase64String($bytes)
    }))

    $g4.Controls.Add((New-Text 'Country' 12 83 124 20))
    $Script:CmbDomain = New-Combo 140 80 100 (Get-ValidRegulatoryDomains | Sort-Object)
    $Script:CmbDomain.SelectedItem = 'US'
    $Script:CmbDomain.Add_SelectedIndexChanged({ Update-DomainNote })
    $g4.Controls.Add($Script:CmbDomain)
    $Script:LblDomainNote = New-Text '' 248 83 138 20 $UI.FontSmall $UI.Muted
    $g4.Controls.Add($Script:LblDomainNote)

    $Script:ChkAcs = New-Check 'Choose the Wi-Fi channel automatically' 12 110 370 $false
    $g4.Controls.Add($Script:ChkAcs)
    $p.Controls.Add($g4)

    # --- network ---------------------------------------------------------
    $g5 = New-GroupBox 'Addresses' 424 164 398 104
    $g5.Controls.Add((New-Text 'Range' 12 27 124 20))
    $Script:TxtCidr = New-Input 140 24 140
    $Script:TxtCidr.Text = '10.30.2.0/24'
    $Script:TxtCidr.Add_TextChanged({ Update-CapacityLabel })
    $g5.Controls.Add($Script:TxtCidr)
    $Script:LblCapacity = New-Text '' 12 52 372 44 $UI.FontSmall $UI.Muted
    $g5.Controls.Add($Script:LblCapacity)
    $p.Controls.Add($g5)

    # --- passwords -------------------------------------------------------
    $g6 = New-GroupBox 'Passwords' 424 276 398 102
    $g6.Controls.Add((New-Text 'radio login' 12 27 124 20))
    $Script:TxtRadioPw = New-Input 140 24 230
    $Script:TxtRadioPw.Text = 'radio'
    $g6.Controls.Add($Script:TxtRadioPw)

    $g6.Controls.Add((New-Text 'Admin page' 12 55 124 20))
    $Script:TxtAdminPw = New-Input 140 52 144
    $g6.Controls.Add($Script:TxtAdminPw)
    $g6.Controls.Add((New-Btn 'Generate' 290 51 88 24 {
        $Script:TxtAdminPw.Text = Generate-Password -length 10
    }))
    $g6.Controls.Add((New-Text 'Both are shown again on the last page. Write them down there.' 12 78 372 16 $UI.FontSmall $UI.Muted))
    $p.Controls.Add($g6)

    # --- save row --------------------------------------------------------
    $p.Controls.Add((New-Text 'Save these settings as' 16 390 140 20))
    $Script:TxtSaveName = New-Input 160 387 200
    $p.Controls.Add($Script:TxtSaveName)
    $p.Controls.Add((New-Btn 'Save' 368 386 90 26 { Save-ConfigFromPage }))
    $Script:LblSaveNote = New-Text '' 468 390 354 20 $UI.FontSmall $UI.Muted
    $p.Controls.Add($Script:LblSaveNote)

    return $p
}

function Update-DomainNote {
    $d = [string]$Script:CmbDomain.SelectedItem
    if (-not $d) { return }
    $halow = Get-HalowRegulatoryDomain -wifiDomain $d
    $Script:LblDomainNote.Text = if ($halow -ne $d) { "HaLow region: $halow" } else { '' }
}

function Update-ConfigEnablement {
    $wireless = ([string]$Script:CmbEud.SelectedItem) -in @('Wireless', 'Auto')
    foreach ($c in @($Script:TxtApSsid, $Script:TxtApKey, $Script:NumEuds)) { $c.Enabled = $wireless }

    # Automatic channel selection and a client AP cannot both have the radio,
    # which is why the console flow forces it off and says so rather than
    # letting the answer be given and quietly ignored.
    if ($wireless) {
        $Script:ChkAcs.Checked = $false
        $Script:ChkAcs.Enabled = $false
        $Script:ChkAcs.Text    = 'Choose the Wi-Fi channel automatically (not available with a client AP)'
    } else {
        $Script:ChkAcs.Enabled = $true
        $Script:ChkAcs.Text    = 'Choose the Wi-Fi channel automatically'
    }
    Update-CapacityLabel
}

function Update-CapacityLabel {
    $wireless = ([string]$Script:CmbEud.SelectedItem) -in @('Wireless', 'Auto')
    $maxEuds  = if ($wireless) { [int]$Script:NumEuds.Value } else { 0 }
    $cap = Calculate-Capacity -cidr $Script:TxtCidr.Text -maxEuds $maxEuds

    if (-not $cap) {
        $Script:LblCapacity.Text      = 'Not a valid range yet. Use something like 10.30.2.0/24.'
        $Script:LblCapacity.ForeColor = $UI.Muted
        return
    }
    $Script:LblCapacity.Text = ("Room for {0} nodes, {1} client addresses, {2} reserved for services." -f `
                                $cap.MaxNodes, $cap.EudPool, $cap.Services)
    $Script:LblCapacity.ForeColor = if ($cap.MaxNodes -lt 5) { $UI.Warn } else { $UI.Muted }
    if ($cap.MaxNodes -lt 5) {
        $Script:LblCapacity.Text += "`r`nThat is very few nodes. Use a bigger range or fewer clients per node."
    }
}

function Update-SavedConfigList {
    if (-not (Test-Path $CONFIG_DIR)) { New-Item -ItemType Directory -Path $CONFIG_DIR | Out-Null }
    $Script:CmbSaved.Items.Clear()
    foreach ($f in @(Get-ChildItem -Path $CONFIG_DIR -Filter '*.conf' -ErrorAction SilentlyContinue)) {
        [void]$Script:CmbSaved.Items.Add($f.BaseName)
    }
    if ($Script:CmbSaved.Items.Count -gt 0) { $Script:CmbSaved.SelectedIndex = 0 }
}

function Load-SelectedConfig {
    $name = [string]$Script:CmbSaved.SelectedItem
    if (-not $name) { return }
    $file = Join-Path $CONFIG_DIR "$name.conf"
    if (-not (Test-Path $file)) { return }

    Load-Config -ConfigFile $file
    Copy-EngineToPage
    $Script:LoadedConfig = $name
    $Script:TxtSaveName.Text = $name
    $Script:LblSaveNote.Text = "Loaded '$name'."
    $Script:LblSaveNote.ForeColor = $UI.Good
}

function Save-ConfigFromPage {
    $name = $Script:TxtSaveName.Text.Trim()
    if (-not $name) {
        $Script:LblSaveNote.Text = 'Give it a name first.'
        $Script:LblSaveNote.ForeColor = $UI.Bad
        return
    }
    if ($name -match '[\\/:*?"<>|]') {
        $Script:LblSaveNote.Text = 'That name has characters Windows will not allow in a file.'
        $Script:LblSaveNote.ForeColor = $UI.Bad
        return
    }
    $problems = Copy-PageToEngine
    if ($problems.Count -gt 0) {
        $Script:LblSaveNote.Text = 'Fix the settings first: ' + $problems[0]
        $Script:LblSaveNote.ForeColor = $UI.Bad
        return
    }

    if (-not (Test-Path $CONFIG_DIR)) { New-Item -ItemType Directory -Path $CONFIG_DIR | Out-Null }
    $file = Join-Path $CONFIG_DIR "$name.conf"

    # Written by hand rather than through Save-Config, which asks two questions
    # on the console. Same keys, same order, same quoting: linux.sh reads these.
    $content = @"
# Mesh Config: $name
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
    [System.IO.File]::WriteAllText($file, $content.Replace("`r`n", "`n"))
    Update-SavedConfigList
    $Script:CmbSaved.SelectedItem = $name
    $Script:LblSaveNote.Text = "Saved. The Linux flasher reads this too."
    $Script:LblSaveNote.ForeColor = $UI.Good
}

# Page -> engine variables. Returns the list of things that are wrong, so the
# same checks run whether the user pressed Save or Next.
function Copy-PageToEngine {
    $problems = New-Object System.Collections.ArrayList

    $Script:EUD_CONNECTION = ([string]$Script:CmbEud.SelectedItem).ToLower()
    $wireless = $Script:EUD_CONNECTION -in @('wireless', 'auto')

    if ($wireless) {
        $Script:LAN_AP_SSID = $Script:TxtApSsid.Text.Trim()
        $Script:LAN_AP_KEY  = $Script:TxtApKey.Text
        $Script:MAX_EUDS_PER_NODE = [int]$Script:NumEuds.Value
        if (-not $Script:LAN_AP_SSID) { [void]$problems.Add('the client Wi-Fi needs a name') }
        if ($Script:LAN_AP_KEY.Length -lt 8 -or $Script:LAN_AP_KEY.Length -gt 63) {
            [void]$problems.Add('the client Wi-Fi password must be 8 to 63 characters')
        }
    } else {
        $Script:LAN_AP_SSID = ''
        $Script:LAN_AP_KEY  = ''
        $Script:MAX_EUDS_PER_NODE = 0
    }

    $Script:INSTALL_MEDIAMTX = if ($Script:ChkMtx.Checked)    { 'y' } else { 'n' }
    $Script:INSTALL_MUMBLE   = if ($Script:ChkMumble.Checked) { 'y' } else { 'n' }
    $Script:VOICE_ENABLED    = if ($Script:ChkVoice.Checked)  { 'y' } else { 'n' }
    $Script:AUTO_UPDATE      = if ($Script:ChkUpdate.Checked) { 'y' } else { 'n' }
    $Script:AUTO_CHANNEL     = if ($Script:ChkAcs.Checked -and -not $wireless) { 'y' } else { 'n' }

    $Script:MESH_SSID    = $Script:TxtMeshSsid.Text.Trim()
    $Script:MESH_SAE_KEY = $Script:TxtMeshKey.Text
    if (-not $Script:MESH_SSID) { [void]$problems.Add('the mesh needs a name') }
    if ($Script:MESH_SAE_KEY.Length -lt 8 -or $Script:MESH_SAE_KEY.Length -gt 63) {
        [void]$problems.Add('the mesh password must be 8 to 63 characters')
    }

    $domain = Test-RegulatoryDomain -domain ([string]$Script:CmbDomain.SelectedItem)
    if ($domain) {
        $Script:REGULATORY_DOMAIN       = $domain
        $Script:HALOW_REGULATORY_DOMAIN = Get-HalowRegulatoryDomain -wifiDomain $domain
    } else {
        [void]$problems.Add('pick a country')
    }

    # The same rules Ask-LanCidr enforces on the console: private space only,
    # and a prefix that leaves a usable mesh without wasting a /8.
    $cidr = $Script:TxtCidr.Text.Trim()
    if ($cidr -notmatch '^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/(\d{1,2})$') {
        [void]$problems.Add('the address range must look like 10.30.2.0/24')
    } else {
        $prefix = [int]$Matches[1]
        $octets = ($cidr -split '/')[0] -split '\.'
        $o1 = [int]$octets[0]; $o2 = [int]$octets[1]
        $isPrivate = ($o1 -eq 10) -or ($o1 -eq 172 -and $o2 -ge 16 -and $o2 -le 31) -or ($o1 -eq 192 -and $o2 -eq 168)
        if ($prefix -lt 16 -or $prefix -gt 26) {
            [void]$problems.Add('the range prefix must be between /16 and /26')
        } elseif (-not $isPrivate) {
            [void]$problems.Add('the range must be private (10.x, 172.16-31.x or 192.168.x)')
        } elseif (($octets | Where-Object { [int]$_ -gt 255 }).Count -gt 0) {
            [void]$problems.Add('that is not a valid address')
        } else {
            $Script:LAN_CIDR_BLOCK = $cidr
        }
    }

    $Script:RADIO_PW = if ($Script:TxtRadioPw.Text) { $Script:TxtRadioPw.Text } else { 'radio' }
    if (-not $Script:TxtAdminPw.Text) { $Script:TxtAdminPw.Text = Generate-Password -length 10 }
    $Script:ADMIN_PW = $Script:TxtAdminPw.Text

    return $problems.ToArray()
}

# Engine variables -> page, after Load-Config has read a saved file.
function Copy-EngineToPage {
    $Script:CmbEud.SelectedItem = switch ($Script:EUD_CONNECTION) {
        'wired'    { 'Wired' }
        'wireless' { 'Wireless' }
        default    { 'Auto' }
    }
    $Script:TxtApSsid.Text  = $Script:LAN_AP_SSID
    $Script:TxtApKey.Text   = $Script:LAN_AP_KEY
    if ($Script:MAX_EUDS_PER_NODE -ge 1 -and $Script:MAX_EUDS_PER_NODE -le 20) {
        $Script:NumEuds.Value = $Script:MAX_EUDS_PER_NODE
    }
    $Script:ChkMtx.Checked    = ($Script:INSTALL_MEDIAMTX -eq 'y')
    $Script:ChkMumble.Checked = ($Script:INSTALL_MUMBLE   -eq 'y')
    $Script:ChkVoice.Checked  = ($Script:VOICE_ENABLED    -eq 'y')
    $Script:ChkUpdate.Checked = ($Script:AUTO_UPDATE      -ne 'n')
    $Script:TxtMeshSsid.Text  = $Script:MESH_SSID
    $Script:TxtMeshKey.Text   = $Script:MESH_SAE_KEY
    if ($Script:REGULATORY_DOMAIN) { $Script:CmbDomain.SelectedItem = $Script:REGULATORY_DOMAIN }
    if ($Script:LAN_CIDR_BLOCK)    { $Script:TxtCidr.Text = $Script:LAN_CIDR_BLOCK }
    $Script:TxtRadioPw.Text = $Script:RADIO_PW
    $Script:TxtAdminPw.Text = $Script:ADMIN_PW
    Update-ConfigEnablement
    $Script:ChkAcs.Checked = ($Script:AUTO_CHANNEL -eq 'y')
    Update-DomainNote
    Update-CapacityLabel
}

# ============================================================
# Page 4: operator setup scripts
# ============================================================

function Build-ScriptsPage {
    $p = New-ContentPanel

    $Script:LvScripts               = New-Object System.Windows.Forms.ListView
    $Script:LvScripts.Location      = New-Object System.Drawing.Point(20, 14)
    $Script:LvScripts.Size          = New-Object System.Drawing.Size(798, 240)
    $Script:LvScripts.View          = 'Details'
    $Script:LvScripts.FullRowSelect = $true
    $Script:LvScripts.GridLines     = $false
    $Script:LvScripts.Font          = $UI.Font
    [void]$Script:LvScripts.Columns.Add('File', 250)
    [void]$Script:LvScripts.Columns.Add('', 90)
    [void]$Script:LvScripts.Columns.Add('Why', 434)
    $p.Controls.Add($Script:LvScripts)

    $Script:LblScriptsSummary = New-Text '' 20 264 798 56 $UI.Font $UI.Text
    $p.Controls.Add($Script:LblScriptsSummary)

    $warn = New-Text ('These are written into the card unencrypted and are not deleted afterwards. ' +
                      'Anyone who reads the card reads them, so keep private keys and long-lived secrets out.') 20 326 798 36 $UI.FontSmall $UI.Warn
    $p.Controls.Add($warn)

    $p.Controls.Add((New-Btn 'Open the folder' 20 372 140 28 {
        if (-not (Test-Path $ADDITIONAL_SCRIPTS_DIR)) { New-Item -ItemType Directory -Path $ADDITIONAL_SCRIPTS_DIR | Out-Null }
        Start-Process explorer.exe $ADDITIONAL_SCRIPTS_DIR
    }))
    $p.Controls.Add((New-Btn 'Check again' 170 372 140 28 { Update-ScriptsPage }))

    return $p
}

function Update-ScriptsPage {
    $Script:LvScripts.Items.Clear()
    $Script:ScriptReport = Get-AdditionalScriptReport

    foreach ($r in $Script:ScriptReport.Results) {
        $item = New-Object System.Windows.Forms.ListViewItem($r.Name)
        [void]$item.SubItems.Add($(switch ($r.Verdict) { 'OK' { 'will run' } 'SKIP' { 'ignored' } 'FAIL' { 'BROKEN' } }))
        [void]$item.SubItems.Add($r.Reason)
        $item.ForeColor = switch ($r.Verdict) { 'OK' { $UI.Good } 'SKIP' { $UI.Muted } 'FAIL' { $UI.Bad } }
        [void]$Script:LvScripts.Items.Add($item)
    }

    $rep = $Script:ScriptReport
    if ($rep.Results.Count -eq 0) {
        $Script:LblScriptsSummary.Text = "Nothing in additional-scripts, so nothing extra runs on the node. That is the normal case."
        $Script:LblScriptsSummary.ForeColor = $UI.Muted
    } elseif ($rep.Failed) {
        $Script:LblScriptsSummary.Text = "One or more scripts will not run as written. Nothing is flashed until they are fixed or renamed to .disabled."
        $Script:LblScriptsSummary.ForeColor = $UI.Bad
    } elseif ($rep.OverMax) {
        $Script:LblScriptsSummary.Text = "$($rep.TotalBytes) bytes is over the $($rep.MaxBytes)-byte limit. Have a script download the bulk on the node instead; it has internet before these run."
        $Script:LblScriptsSummary.ForeColor = $UI.Bad
    } else {
        $n = $rep.Accepted.Count
        $Script:LblScriptsSummary.Text = "$n script(s) will be embedded and run once as root on each node, after setup finishes."
        if ($rep.OverWarn) { $Script:LblScriptsSummary.Text += "  That is $($rep.TotalBytes) bytes, which is a lot to bake into an image." }
        $Script:LblScriptsSummary.ForeColor = if ($rep.OverWarn) { $UI.Warn } else { $UI.Good }
    }

    # Same variable the console flow sets, and what Add-AdditionalScriptsBlock reads.
    $Script:ADDITIONAL_SCRIPTS = if ($rep.Failed -or $rep.OverMax) { @() } else { $rep.Accepted }
    Update-Nav
}

# ============================================================
# Page 5: target card
# ============================================================

function Build-TargetPage {
    $p = New-ContentPanel

    $Script:GrpBoot = New-GroupBox 'Compute Module 4' 20 10 798 118
    $Script:GrpBoot.Controls.Add((New-Text '1.  Fit the boot jumper (nRPIBOOT to ground) on the carrier board.' 14 24 700 18))
    $Script:GrpBoot.Controls.Add((New-Text '2.  Plug a USB cable from this computer into the carrier board slave port.' 14 44 700 18))
    $Script:GrpBoot.Controls.Add((New-Text '3.  Power the board on, then press the button.' 14 64 700 18))
    $Script:GrpBoot.Controls.Add((New-Btn 'Expose the eMMC' 14 86 150 26 { Invoke-RpiBootFromGui }))
    $Script:LblBootState = New-Text '' 176 90 600 20 $UI.FontSmall $UI.Muted
    $Script:GrpBoot.Controls.Add($Script:LblBootState)
    $p.Controls.Add($Script:GrpBoot)

    $Script:LvDisks               = New-Object System.Windows.Forms.ListView
    $Script:LvDisks.Location      = New-Object System.Drawing.Point(20, 136)
    $Script:LvDisks.Size          = New-Object System.Drawing.Size(798, 194)
    $Script:LvDisks.View          = 'Details'
    $Script:LvDisks.FullRowSelect = $true
    $Script:LvDisks.MultiSelect   = $false
    $Script:LvDisks.HideSelection = $false
    $Script:LvDisks.Font          = $UI.Font
    [void]$Script:LvDisks.Columns.Add('Disk', 50)
    [void]$Script:LvDisks.Columns.Add('Name', 300)
    [void]$Script:LvDisks.Columns.Add('Size', 90)
    [void]$Script:LvDisks.Columns.Add('Connected by', 110)
    [void]$Script:LvDisks.Columns.Add('Contains', 224)
    $Script:LvDisks.Add_SelectedIndexChanged({ Update-DiskWarning; Update-Nav })
    $p.Controls.Add($Script:LvDisks)

    $Script:LblDiskWarn = New-Text '' 20 336 798 40 $UI.Font $UI.Text
    $p.Controls.Add($Script:LblDiskWarn)

    $p.Controls.Add((New-Btn 'Look again' 20 384 140 28 { Update-TargetPage }))
    $p.Controls.Add((New-Text 'The disk you are booted from is never listed.' 172 390 500 18 $UI.FontSmall $UI.Muted))

    return $p
}

function Update-TargetPage {
    $hw = Get-SelectedHardware
    $Script:GrpBoot.Visible = $hw.IsCm4

    $sel = $null
    if ($Script:LvDisks.SelectedItems.Count -gt 0) { $sel = $Script:LvDisks.SelectedItems[0].Text }

    $Script:LvDisks.Items.Clear()
    $Script:CandidateList = @(Get-CandidateDisks)

    foreach ($d in $Script:CandidateList) {
        $item = New-Object System.Windows.Forms.ListViewItem([string]$d.Number)
        [void]$item.SubItems.Add($d.FriendlyName)
        [void]$item.SubItems.Add("$($d.SizeGB) GB")
        [void]$item.SubItems.Add($d.BusType)
        [void]$item.SubItems.Add($(if ($d.Volumes.Count) { $d.Volumes -join ', ' } else { '' }))
        $item.Tag = $d
        if (-not $d.IsRemovable) { $item.ForeColor = $UI.Bad }
        [void]$Script:LvDisks.Items.Add($item)
        if ($sel -and $item.Text -eq $sel) { $item.Selected = $true }
    }

    if ($Script:LvDisks.Items.Count -eq 0) {
        $Script:LblDiskWarn.Text = 'No card found. Plug in the card reader, or for a CM4 use the button above, then choose Look again.'
        $Script:LblDiskWarn.ForeColor = $UI.Warn
    } else {
        Update-DiskWarning
    }
    Update-Nav
}

function Get-SelectedDisk {
    if ($Script:LvDisks.SelectedItems.Count -eq 0) { return $null }
    return $Script:LvDisks.SelectedItems[0].Tag
}

function Update-DiskWarning {
    $d = Get-SelectedDisk
    if (-not $d) {
        $Script:LblDiskWarn.Text = 'Pick the card from the list.'
        $Script:LblDiskWarn.ForeColor = $UI.Muted
        return
    }
    if ($d.IsRemovable) {
        $Script:LblDiskWarn.Text = "Disk $($d.Number), $($d.SizeGB) GB. Everything on it will be destroyed."
        $Script:LblDiskWarn.ForeColor = $UI.Text
    } else {
        $Script:LblDiskWarn.Text = ("Disk $($d.Number) is connected by $($d.BusType), which is how a hard drive is connected, not a card reader.`r`n" +
                                    "If this is not the card you meant, pick another one. Everything on it will be destroyed.")
        $Script:LblDiskWarn.ForeColor = $UI.Bad
    }
}

# The console flow's Invoke-RpiBoot asks the user to press Enter twice, which a
# window cannot do. Same two steps, driven by the button instead.
function Invoke-RpiBootFromGui {
    $item = $Script:PrereqItems | Where-Object { $_.Key -eq 'rpiboot' } | Select-Object -First 1
    if (-not $item -or -not $item.Path) {
        $Script:LblBootState.Text = 'rpiboot is not installed. Go back to the prerequisites page.'
        $Script:LblBootState.ForeColor = $UI.Bad
        return
    }

    $Script:RPIBOOT_PATH = $item.Path
    $Script:DisksBefore  = @(Get-Disk | Select-Object -ExpandProperty Number)
    $Script:LblBootState.Text = 'Running rpiboot...'
    $Script:LblBootState.ForeColor = $UI.Muted
    $Script:ProgressBar   = $null
    $Script:ProgressLabel = $Script:LblBootState

    Start-ProcessJob -Path $item.Path -Label 'rpiboot' -OnDone {
        param($ok, $err)
        # rpiboot exits non-zero when no module answers, so its code is not
        # evidence on its own. A new disk appearing is. The wait is broken up
        # rather than one Start-Sleep, which would freeze the window for eight
        # seconds at the moment the user is watching it hardest.
        for ($t = $DEVICE_WAIT; $t -gt 0; $t--) {
            $Script:LblBootState.Text = "Waiting for the eMMC to appear ($t)..."
            [System.Windows.Forms.Application]::DoEvents()
            Start-Sleep -Seconds 1
        }
        $after = @(Get-Disk | Select-Object -ExpandProperty Number)
        $new   = @($after | Where-Object { $Script:DisksBefore -notcontains $_ })
        if ($new.Count -gt 0) {
            $Script:LblBootState.Text = "The eMMC appeared as disk $($new -join ', '). Pick it below."
            $Script:LblBootState.ForeColor = $UI.Good
        } else {
            $Script:LblBootState.Text = 'No new disk appeared. Check the boot jumper and the USB cable, then try again.'
            $Script:LblBootState.ForeColor = $UI.Warn
        }
        Update-TargetPage
    }
}

# ============================================================
# Page 6: confirmation
# ============================================================

function Build-ConfirmPage {
    $p = New-ContentPanel

    $Script:TxtSummary            = New-Object System.Windows.Forms.TextBox
    $Script:TxtSummary.Location   = New-Object System.Drawing.Point(20, 14)
    $Script:TxtSummary.Size       = New-Object System.Drawing.Size(798, 310)
    $Script:TxtSummary.Multiline  = $true
    $Script:TxtSummary.ReadOnly   = $true
    $Script:TxtSummary.ScrollBars = 'Vertical'
    $Script:TxtSummary.Font       = $UI.FontMono
    $Script:TxtSummary.BackColor  = [System.Drawing.Color]::FromArgb(248, 248, 248)
    $p.Controls.Add($Script:TxtSummary)

    $Script:LblEraseWarn = New-Text '' 20 334 798 26 $UI.FontBold $UI.Bad
    $p.Controls.Add($Script:LblEraseWarn)

    $Script:ChkErase = New-Check '' 20 366 798 $false
    $Script:ChkErase.Font = $UI.FontBold
    $Script:ChkErase.Add_CheckedChanged({ Update-Nav })
    $p.Controls.Add($Script:ChkErase)

    return $p
}

function Update-ConfirmPage {
    $d  = Get-SelectedDisk
    $hw = Get-SelectedHardware

    # Reachable by going Back to the card list and clearing the selection.
    if (-not $d) { Go-Page 'Target'; return }

    $boardName = if ($hw.IsCm4) { 'Compute Module 4' }
                 elseif ($hw.Model -eq 'r3a')  { 'Radxa Rock 3A' }
                 elseif ($hw.Model -eq 'rpi5') { 'Raspberry Pi 5' }
                 else { 'Raspberry Pi 4B' }

    $scriptCount = if ($Script:ADDITIONAL_SCRIPTS) { $Script:ADDITIONAL_SCRIPTS.Count } else { 0 }

    $lines = @(
        "  Board            $boardName"
        "  Writing to       Disk $($d.Number), $($d.FriendlyName), $($d.SizeGB) GB, over $($d.BusType)"
        ""
        "  Mesh name        $($Script:MESH_SSID)"
        "  Mesh password    $($Script:MESH_SAE_KEY)"
        "  Address range    $($Script:LAN_CIDR_BLOCK)"
        "  Country          $($Script:REGULATORY_DOMAIN)   (HaLow region $($Script:HALOW_REGULATORY_DOMAIN))"
        "  Auto channel     $($Script:AUTO_CHANNEL)"
        ""
        "  Clients          $($Script:EUD_CONNECTION)"
    )
    if ($Script:EUD_CONNECTION -in @('wireless','auto')) {
        $lines += "  Client Wi-Fi     $($Script:LAN_AP_SSID)  (the last 4 of the wired MAC is appended)"
        $lines += "  Client password  $($Script:LAN_AP_KEY)"
        $lines += "  Clients per node $($Script:MAX_EUDS_PER_NODE)"
    }
    $lines += @(
        ""
        "  MediaMTX         $($Script:INSTALL_MEDIAMTX)"
        "  Mumble           $($Script:INSTALL_MUMBLE)"
        "  Push-to-talk     $($Script:VOICE_ENABLED)"
        "  Auto update      $($Script:AUTO_UPDATE)"
        ""
        "  radio password   $($Script:RADIO_PW)"
        "  admin password   $($Script:ADMIN_PW)"
        ""
        "  Setup scripts    $scriptCount to run once on the node"
    )

    $Script:TxtSummary.Text = ($lines -join "`r`n")
    $Script:LblEraseWarn.Text = "Everything on disk $($d.Number) will be destroyed. This cannot be undone."
    $Script:ChkErase.Text     = "Yes, erase disk $($d.Number) ($($d.FriendlyName), $($d.SizeGB) GB)"
    $Script:ChkErase.Checked  = $false
    Update-Nav
}

# ============================================================
# Page 7: flashing
# ============================================================

function Build-FlashPage {
    $p = New-ContentPanel

    $Script:FlashProgress          = New-Object System.Windows.Forms.ProgressBar
    $Script:FlashProgress.Location = New-Object System.Drawing.Point(20, 16)
    $Script:FlashProgress.Size     = New-Object System.Drawing.Size(798, 22)
    $p.Controls.Add($Script:FlashProgress)

    $Script:LblFlashStatus = New-Text 'Getting ready...' 20 44 798 20 $UI.Font $UI.Text
    $p.Controls.Add($Script:LblFlashStatus)

    $Script:LogBox            = New-Object System.Windows.Forms.TextBox
    $Script:LogBox.Location   = New-Object System.Drawing.Point(20, 70)
    $Script:LogBox.Size       = New-Object System.Drawing.Size(798, 338)
    $Script:LogBox.Multiline  = $true
    $Script:LogBox.ReadOnly   = $true
    $Script:LogBox.ScrollBars = 'Vertical'
    $Script:LogBox.Font       = $UI.FontMono
    $Script:LogBox.BackColor  = [System.Drawing.Color]::FromArgb(24, 24, 24)
    $Script:LogBox.ForeColor  = [System.Drawing.Color]::FromArgb(220, 220, 220)
    $Script:LogBox.WordWrap   = $false
    $p.Controls.Add($Script:LogBox)

    # Anything already logged before the box existed, so nothing is lost.
    foreach ($l in $Script:LogBuf) { $Script:LogBox.AppendText($l + "`r`n") }

    return $p
}

function Start-Flash {
    $Script:ProgressBar   = $Script:FlashProgress
    $Script:ProgressLabel = $Script:LblFlashStatus

    $d  = Get-SelectedDisk
    $hw = Get-SelectedHardware
    $Script:HARDWARE_MODEL = $hw.Model
    $Script:TARGET_DEVICE  = $d.Number

    Add-Log "=============================================="
    Add-Log " Writing disk $($d.Number): $($d.FriendlyName)"
    Add-Log "=============================================="

    if ($hw.Model -eq 'r3a') { Start-Rock3aFlash } else { Start-RpiFlash }
}

function Start-RpiFlash {
    try {
        $content    = Build-ProvisioningScript -TemplatePath $TEMPLATE_FILE
        $tempScript = Join-Path $ScriptDir 'firstrun.sh'
        [System.IO.File]::WriteAllText($tempScript, $content.Replace("`r`n", "`n"))
        Add-Log "First-boot script written to $tempScript"
    } catch {
        Complete-Flash $false $_.Exception.Message
        return
    }

    $imager = ($Script:PrereqItems | Where-Object { $_.Key -eq 'rpi-imager' } | Select-Object -First 1).Path
    $Script:RPI_IMAGER_PATH = $imager

    $target = "\\.\PhysicalDrive$($Script:TARGET_DEVICE)"
    $Script:LblFlashStatus.Text = 'Raspberry Pi Imager is downloading and writing the image. This takes a few minutes.'
    $Script:FlashProgress.Style = 'Marquee'

    Start-ProcessJob -Path $imager `
        -Arguments @('--cli', $OS_IMAGE_URL, $target, '--first-run-script', $tempScript) `
        -Label 'Raspberry Pi Imager' `
        -OnDone {
            param($ok, $err)
            $Script:FlashProgress.Style = 'Blocks'
            Remove-Item (Join-Path $ScriptDir 'firstrun.sh') -Force -ErrorAction SilentlyContinue
            if (-not $ok) { Complete-Flash $false $err; return }
            if ($Script:Worker.ExitCode -ne 0) {
                # A remembered path that does not flash must not be replayed on
                # every future run, here or on the console.
                Remove-CachedToolPath -Key 'rpi-imager' -Reason "it did not flash the card"
                Complete-Flash $false "Raspberry Pi Imager exited with code $($Script:Worker.ExitCode)."
                return
            }
            Complete-Flash $true ''
        }
}

function Start-Rock3aFlash {
    if (-not (Test-Ext4Driver)) {
        Complete-Flash $false 'The Ext2Fsd service is not running. Go back to the prerequisites page.'
        return
    }
    try {
        if (-not $Script:ARMBIAN_IMAGE -or -not (Test-Path $Script:ARMBIAN_IMAGE)) {
            $Script:LblFlashStatus.Text = 'Fetching the Armbian image...'
            [System.Windows.Forms.Application]::DoEvents()
            if (-not (Get-ArmbianImageForGui)) {
                Complete-Flash $false 'Could not obtain the Armbian image.'
                return
            }
        }
        if (-not $Script:R3aTempImage) {
            $Script:LblFlashStatus.Text = 'Preparing the image (this mounts it and writes the config into it)...'
            [System.Windows.Forms.Application]::DoEvents()
            $Script:R3aTempImage = New-Rock3aCustomImage
        }
    } catch {
        Complete-Flash $false $_.Exception.Message
        return
    }

    Add-Log "Wiping disk $($Script:TARGET_DEVICE)..."
    Clear-Disk -Number $Script:TARGET_DEVICE -RemoveData -Confirm:$false -ErrorAction SilentlyContinue

    Start-RawWriteJob -ImagePath $Script:R3aTempImage -DiskNumber $Script:TARGET_DEVICE -OnDone {
        param($ok, $err)
        if ($ok) { Complete-Flash $true '' } else { Complete-Flash $false $err }
    }
}

# The console flow asks which of two ways to get the image. Here the download
# is the answer unless a copy is already sitting next to the script, because
# the alternative is a file-path question this audience should not be asked.
function Get-ArmbianImageForGui {
    $localImage      = Join-Path $ScriptDir $ARMBIAN_IMAGE_FILENAME
    $localCompressed = Join-Path $ScriptDir "${ARMBIAN_IMAGE_FILENAME}.xz"

    if (Test-Path $localImage) { $Script:ARMBIAN_IMAGE = $localImage; return $true }

    if (-not (Test-Path $localCompressed)) {
        Add-Log "Downloading the Armbian image. This is a large file."
        try {
            [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri $ARMBIAN_IMAGE_URL -OutFile $localCompressed -UseBasicParsing
        } catch {
            Add-Log "Download failed: $($_.Exception.Message)"
            return $false
        }
    }
    if (-not (Expand-XzFile -CompressedPath $localCompressed -OutputPath $localImage)) { return $false }
    $Script:ARMBIAN_IMAGE = $localImage
    return $true
}

function Complete-Flash {
    param([bool]$Ok, [string]$Reason)

    $Script:FlashProgress.Style = 'Blocks'
    if ($Ok) {
        $Script:FlashCount++
        $Script:FlashProgress.Value = 100
        $Script:LblFlashStatus.Text = 'Done. The card is written.'
        $Script:LblFlashStatus.ForeColor = $UI.Good
        Add-Log ""
        Add-Log "Flash complete."
        Go-Page 'Done'
    } else {
        $Script:LblFlashStatus.Text = "It did not work: $Reason"
        $Script:LblFlashStatus.ForeColor = $UI.Bad
        Add-Log ""
        Add-Log "FAILED: $Reason"
        $Script:FlashFailed = $true
        Update-Nav
    }
}

# ============================================================
# Page 8: what to write down
# ============================================================

function Build-DonePage {
    $p = New-ContentPanel

    $p.Controls.Add((New-Text 'The card is ready. Put it in the board and connect Ethernet with internet access.' 20 12 798 20 $UI.FontBold $UI.Good))
    $p.Controls.Add((New-Text ('The node sets itself up on first boot and reboots several times. It takes about ten minutes. ' +
                               'Leave it alone until it settles.') 20 34 798 34 $UI.Font $UI.Text))

    $Script:TxtReceipt            = New-Object System.Windows.Forms.TextBox
    $Script:TxtReceipt.Location   = New-Object System.Drawing.Point(20, 76)
    $Script:TxtReceipt.Size       = New-Object System.Drawing.Size(798, 262)
    $Script:TxtReceipt.Multiline  = $true
    $Script:TxtReceipt.ReadOnly   = $true
    $Script:TxtReceipt.ScrollBars = 'Vertical'
    $Script:TxtReceipt.Font       = $UI.FontMono
    $Script:TxtReceipt.BackColor  = [System.Drawing.Color]::FromArgb(248, 248, 248)
    $p.Controls.Add($Script:TxtReceipt)

    $p.Controls.Add((New-Btn 'Save to a file...' 20 350 150 30 { Save-Receipt }))
    $p.Controls.Add((New-Btn 'Copy' 178 350 100 30 {
        try { [System.Windows.Forms.Clipboard]::SetText($Script:TxtReceipt.Text) } catch { }
    }))
    $Script:LblReceiptNote = New-Text '' 288 356 530 20 $UI.FontSmall $UI.Muted
    $p.Controls.Add($Script:LblReceiptNote)

    $p.Controls.Add((New-Text 'Keep this somewhere safe. The passwords are not recoverable from the card by anyone who does not already have it.' 20 388 798 20 $UI.FontSmall $UI.Muted))

    return $p
}

function Get-ReceiptText {
    $lines = @(
        "MANET node settings"
        "Written $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
        ""
        "Mesh name (SSID)      $($Script:MESH_SSID)"
        "Mesh password         $($Script:MESH_SAE_KEY)"
        "Address range         $($Script:LAN_CIDR_BLOCK)"
        "Country               $($Script:REGULATORY_DOMAIN)"
        "HaLow region          $($Script:HALOW_REGULATORY_DOMAIN)"
        ""
    )
    if ($Script:EUD_CONNECTION -in @('wireless','auto')) {
        $lines += @(
            "Client Wi-Fi name     $($Script:LAN_AP_SSID)-xxxx   (xxxx is the last 4 of the wired MAC)"
            "Client Wi-Fi password $($Script:LAN_AP_KEY)"
            "Clients per node      $($Script:MAX_EUDS_PER_NODE)"
            ""
        )
    }
    $lines += @(
        "SSH login             radio / $($Script:RADIO_PW)"
        "Admin page password   $($Script:ADMIN_PW)"
        ""
        "Every node on this mesh must be flashed with the same mesh name and"
        "mesh password, or they will not see each other. Load the saved"
        "settings of the same name for each card."
    )
    if ($Script:HARDWARE_MODEL -eq 'r3a') {
        $lines += @(
            ""
            "Rock 3A: the root password is the Armbian default, 1234. Change it."
        )
        if ($Script:R3A_RADIO_HASH -eq '!') {
            $lines += "The radio account shipped locked: log in as root and run 'passwd radio'."
        }
    }
    return ($lines -join "`r`n")
}

function Update-DonePage {
    $Script:TxtReceipt.Text = Get-ReceiptText
    $Script:LblReceiptNote.Text = "$($Script:FlashCount) card(s) written this session."
    Update-Nav
}

function Save-Receipt {
    # The mesh name is passed in rather than read from $Script:. On a host that
    # is not already STA this scriptblock runs in a separate runspace, which
    # carries none of this script's variables with it.
    $dialog = {
        param($MeshName)
        Add-Type -AssemblyName System.Windows.Forms
        $sfd          = New-Object System.Windows.Forms.SaveFileDialog
        $sfd.Title    = 'Save the node settings'
        $sfd.Filter   = 'Text files (*.txt)|*.txt|All files (*.*)|*.*'
        $sfd.FileName = "manet-$MeshName-settings.txt"
        $owner        = New-Object System.Windows.Forms.Form
        $owner.TopMost= $true
        if ($sfd.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) { $sfd.FileName } else { '' }
    }
    try {
        $path = Invoke-OnStaThread -Action $dialog -Arguments @($Script:MESH_SSID)
        if ($path) {
            [System.IO.File]::WriteAllText($path, $Script:TxtReceipt.Text)
            $Script:LblReceiptNote.Text = "Saved to $path"
            $Script:LblReceiptNote.ForeColor = $UI.Good
        }
    } catch {
        $Script:LblReceiptNote.Text = "Could not save: $($_.Exception.Message)"
        $Script:LblReceiptNote.ForeColor = $UI.Bad
    }
}

# ============================================================
# Navigation
# ============================================================

function Go-Page {
    param([string]$Name)

    foreach ($k in $Script:Pages.Keys) { $Script:Pages[$k].Visible = $false }
    $Script:PageIndex = [array]::IndexOf($Script:PageOrder, $Name)
    $Script:Pages[$Name].Visible = $true

    $Script:LblTitle.Text = $Script:PageTitles[$Name]
    $Script:LblBlurb.Text = $Script:PageBlurbs[$Name]

    switch ($Name) {
        'Prereqs' { Update-PrereqPage }
        'Config'  { Update-SavedConfigList; Update-ConfigEnablement; Update-DomainNote; Update-CapacityLabel }
        'Scripts' { Update-ScriptsPage }
        'Target'  { Update-TargetPage }
        'Confirm' { Update-ConfirmPage }
        'Flash'   { $Script:FlashFailed = $false; Start-Flash }
        'Done'    { Update-DonePage }
    }
    Update-Nav
}

function Update-Nav {
    if (-not $Script:BtnNext) { return }
    $name = $Script:PageOrder[$Script:PageIndex]
    $busy = $Script:Worker.Active

    $Script:BtnBack.Visible = $name -notin @('Hardware', 'Flash', 'Done')
    $Script:BtnBack.Enabled = -not $busy

    switch ($name) {
        'Hardware' { $Script:BtnNext.Text = 'Next';  $Script:BtnNext.Enabled = $true }
        'Prereqs'  { $Script:BtnNext.Text = 'Next';  $Script:BtnNext.Enabled = ((Test-PrereqsSatisfied) -and -not $busy) }
        'Config'   { $Script:BtnNext.Text = 'Next';  $Script:BtnNext.Enabled = $true }
        'Scripts'  {
            $rep = $Script:ScriptReport
            $Script:BtnNext.Text = 'Next'
            $Script:BtnNext.Enabled = (-not $rep) -or (-not $rep.Failed -and -not $rep.OverMax)
        }
        'Target'   { $Script:BtnNext.Text = 'Next';  $Script:BtnNext.Enabled = ((Get-SelectedDisk) -ne $null -and -not $busy) }
        'Confirm'  {
            $d = Get-SelectedDisk
            $Script:BtnNext.Text = if ($d) { "Erase disk $($d.Number) and write the card" } else { 'Write the card' }
            $Script:BtnNext.Enabled = $Script:ChkErase.Checked
        }
        'Flash'    {
            $Script:BtnNext.Text = if ($Script:FlashFailed) { 'Back to the card list' } else { 'Working...' }
            $Script:BtnNext.Enabled = [bool]$Script:FlashFailed
        }
        'Done'     { $Script:BtnNext.Text = 'Flash another card'; $Script:BtnNext.Enabled = $true }
    }

    $Script:BtnCancel.Text = if ($name -eq 'Done') { 'Close' } else { 'Quit' }

    # The confirm button spells out which disk it is about to erase, so it is
    # wider than the others and Back has to move out of its way.
    $Script:BtnNext.Width = if ($name -eq 'Confirm') { 280 } else { 150 }
    $Script:BtnNext.Left  = $Script:Form.ClientSize.Width - $Script:BtnNext.Width - 20
    $Script:BtnBack.Left  = $Script:BtnNext.Left - $Script:BtnBack.Width - 10
}

function Invoke-Next {
    $name = $Script:PageOrder[$Script:PageIndex]

    switch ($name) {
        'Hardware' {
            # The list depends on the board, so it is rebuilt rather than reused.
            $Script:PrereqItems = $null
            Go-Page 'Prereqs'
        }
        'Prereqs' { Go-Page 'Config' }
        'Config'  {
            $problems = Copy-PageToEngine
            if ($problems.Count -gt 0) {
                [System.Windows.Forms.MessageBox]::Show($Script:Form,
                    ("These need fixing first:`r`n`r`n  - " + ($problems -join "`r`n  - ")),
                    'Not quite ready', 'OK', 'Warning') | Out-Null
                return
            }
            if (-not $Script:LoadedConfig -and -not $Script:TxtSaveName.Text.Trim()) {
                $r = [System.Windows.Forms.MessageBox]::Show($Script:Form,
                    ("These settings have not been saved.`r`n`r`nEvery other node on this mesh has to be flashed with the same ones, " +
                     "so saving them now is worth it. Carry on without saving?"),
                    'Not saved', 'YesNo', 'Question')
                if ($r -ne [System.Windows.Forms.DialogResult]::Yes) { return }
            }
            Go-Page 'Scripts'
        }
        'Scripts' { Go-Page 'Target' }
        'Target'  { Go-Page 'Confirm' }
        'Confirm' { Go-Page 'Flash' }
        'Flash'   { if ($Script:FlashFailed) { Go-Page 'Target' } }
        'Done'    {
            [System.Windows.Forms.MessageBox]::Show($Script:Form,
                "Take the card out and put the next one in, then choose it from the list.",
                'Next card', 'OK', 'Information') | Out-Null
            Go-Page 'Target'
        }
    }
}

function Invoke-Back {
    $name = $Script:PageOrder[$Script:PageIndex]
    switch ($name) {
        'Prereqs' { Go-Page 'Hardware' }
        'Config'  { Go-Page 'Prereqs' }
        'Scripts' { Go-Page 'Config' }
        'Target'  { Go-Page 'Scripts' }
        'Confirm' { Go-Page 'Target' }
    }
}

# ============================================================
# The window
# ============================================================

function Build-Window {
    $form                 = New-Object System.Windows.Forms.Form
    $form.Text            = 'MANET radio flasher'
    $form.ClientSize      = New-Object System.Drawing.Size(838, 610)
    $form.StartPosition   = 'CenterScreen'
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox     = $false
    $form.BackColor       = $UI.Bg
    $form.Font            = $UI.Font
    $Script:Form          = $form

    # --- header ---
    $header           = New-Object System.Windows.Forms.Panel
    $header.Location  = New-Object System.Drawing.Point(0, 0)
    $header.Size      = New-Object System.Drawing.Size(838, 76)
    $header.BackColor = $UI.Header
    $form.Controls.Add($header)

    $Script:LblTitle = New-Text '' 20 14 798 28 $UI.FontTitle $UI.HeaderTxt
    $header.Controls.Add($Script:LblTitle)
    $Script:LblBlurb = New-Text '' 22 46 798 20 $UI.FontSmall ([System.Drawing.Color]::FromArgb(190, 205, 220))
    $header.Controls.Add($Script:LblBlurb)

    # --- content host ---
    $contentHost           = New-Object System.Windows.Forms.Panel
    $contentHost.Location  = New-Object System.Drawing.Point(0, 76)
    $contentHost.Size      = New-Object System.Drawing.Size($Script:CONTENT_W, $Script:CONTENT_H)
    $contentHost.BackColor = $UI.Panel
    $form.Controls.Add($contentHost)

    $Script:Pages = @{
        Hardware = Build-HardwarePage
        Prereqs  = Build-PrereqPage
        Config   = Build-ConfigPage
        Scripts  = Build-ScriptsPage
        Target   = Build-TargetPage
        Confirm  = Build-ConfirmPage
        Flash    = Build-FlashPage
        Done     = Build-DonePage
    }
    foreach ($k in $Script:Pages.Keys) { $contentHost.Controls.Add($Script:Pages[$k]) }

    # --- footer ---
    $footer           = New-Object System.Windows.Forms.Panel
    $footer.Location  = New-Object System.Drawing.Point(0, 508)
    $footer.Size      = New-Object System.Drawing.Size(838, 102)
    $footer.BackColor = $UI.Bg
    $form.Controls.Add($footer)

    $rule             = New-Object System.Windows.Forms.Label
    $rule.Location    = New-Object System.Drawing.Point(0, 0)
    $rule.Size        = New-Object System.Drawing.Size(838, 1)
    $rule.BorderStyle = 'Fixed3D'
    $footer.Controls.Add($rule)

    $Script:BtnCancel = New-Btn 'Quit' 20 30 110 34 {
        if ($Script:Worker.Active) {
            $r = [System.Windows.Forms.MessageBox]::Show($Script:Form,
                "Something is still running. Stopping now can leave the card half written.`r`n`r`nQuit anyway?",
                'Still working', 'YesNo', 'Warning')
            if ($r -ne [System.Windows.Forms.DialogResult]::Yes) { return }
            $Script:Worker.Cancel = $true
        }
        $Script:Form.Close()
    }
    $footer.Controls.Add($Script:BtnCancel)

    $Script:BtnBack = New-Btn 'Back' 528 30 130 34 { Invoke-Back }
    $footer.Controls.Add($Script:BtnBack)

    $Script:BtnNext = New-Btn 'Next' 668 30 150 34 { Invoke-Next }
    $Script:BtnNext.Font = $UI.FontBold
    $footer.Controls.Add($Script:BtnNext)

    # The launcher may well have moved itself into a folder the user has not
    # seen, and that folder is where their saved settings and their own setup
    # scripts live. Naming it, and opening it on a click, is the answer to
    # "where did my file go".
    $Script:LnkFolder             = New-Object System.Windows.Forms.LinkLabel
    $Script:LnkFolder.Location    = New-Object System.Drawing.Point(20, 72)
    $Script:LnkFolder.Size        = New-Object System.Drawing.Size(798, 18)
    $Script:LnkFolder.Font        = $UI.FontSmall
    $Script:LnkFolder.LinkColor   = $UI.Header
    $Script:LnkFolder.AutoEllipsis = $true
    $Script:LnkFolder.Text        = "Settings and setup scripts live in  $ScriptDir"
    $Script:LnkFolder.LinkArea    = New-Object System.Windows.Forms.LinkArea(36, $ScriptDir.Length)
    $Script:LnkFolder.Add_LinkClicked({
        try { Start-Process explorer.exe $ScriptDir } catch { }
    })
    $footer.Controls.Add($Script:LnkFolder)

    # The heartbeat for every long job. 90 ms is short enough that a progress
    # bar looks continuous and long enough that the stepping itself is not the
    # bottleneck.
    $Script:Timer          = New-Object System.Windows.Forms.Timer
    $Script:Timer.Interval = 90
    $Script:Timer.Add_Tick({ Invoke-WorkerTick })
    $Script:Timer.Start()

    $form.Add_FormClosing({
        if ($Script:Worker.Active -and -not $Script:Worker.Cancel) {
            $r = [System.Windows.Forms.MessageBox]::Show($Script:Form,
                "Something is still running. Closing now can leave the card half written.`r`n`r`nClose anyway?",
                'Still working', 'YesNo', 'Warning')
            if ($r -ne [System.Windows.Forms.DialogResult]::Yes) { $_.Cancel = $true; return }
            $Script:Worker.Cancel = $true
        }
        $Script:Timer.Stop()
        if ($Script:R3aTempImage -and (Test-Path $Script:R3aTempImage)) {
            Remove-Item $Script:R3aTempImage -Force -ErrorAction SilentlyContinue
        }
        Remove-Item (Join-Path $ScriptDir 'firstrun.sh') -Force -ErrorAction SilentlyContinue
    })

    return $form
}

# ============================================================
# Start
# ============================================================

function Show-StartupProblem {
    param([string]$Text)
    [System.Windows.Forms.MessageBox]::Show($Text, 'Cannot start', 'OK', 'Error') | Out-Null
}

if (-not $Preview) {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Show-StartupProblem ("This has to run as Administrator, because writing to a card means writing to a disk.`r`n`r`n" +
                             "Close this and double-click 'Flash a Radio.cmd' instead. It asks Windows for permission first.")
        exit 1
    }
    if (-not (Test-Path $TEMPLATE_FILE)) {
        Show-StartupProblem ("firstrun.sh.template is missing from:`r`n$ScriptDir`r`n`r`n" +
                             "Copy the whole provisioning folder, not just some of the files in it.")
        exit 1
    }
}

Set-ConsoleWindowVisible $false

$Script:Worker.ExitCode = 0
$Script:FlashFailed     = $false

# Past this point the console is hidden, so an unhandled error would look like
# the program vanishing. Anything that gets here says so in a window and puts
# the console back, where the stack trace still is.
try {
    $form = Build-Window
    Go-Page 'Hardware'
    [void]$form.ShowDialog()
    $form.Dispose()
} catch {
    Set-ConsoleWindowVisible $true
    Show-StartupProblem ("The flasher stopped with an error:`r`n`r`n$($_.Exception.Message)`r`n`r`n" +
                         "Nothing further has been written to any card. The console window behind this has the details.")
    Write-Output $_.ScriptStackTrace
    exit 1
}
