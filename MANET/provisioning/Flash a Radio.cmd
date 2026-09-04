@echo off
setlocal
title MANET radio flasher

rem Double-click this. It is the only file you need.
rem
rem If the rest of the provisioning folder is sitting next to it, it uses that.
rem If this file is on its own, it fetches what it needs from GitHub into a
rem MANET-flasher folder beside itself and runs from there.
rem
rem Two things stop a .ps1 from just running when someone double-clicks it:
rem Windows will not run one without asking, and writing to a card needs
rem Administrator. This deals with both, then opens the window.

set "SRC=https://raw.githubusercontent.com/very-srs/MANET/main/MANET/provisioning"
set "FILES=windows.ps1|manet-flasher.ps1|firstrun.sh.template|rock3a-provision.sh.template|additional-scripts/README.md"
set "HERE=%~dp0"

rem Running straight out of a zip gives a temporary folder that Windows throws
rem away, so anything downloaded into it is lost and anything extracted beside
rem it is not there. Say so rather than failing halfway through.
echo "%HERE%" | find /i ".zip" >nul
if not errorlevel 1 goto :in_a_zip

rem Already Administrator? net session only succeeds when elevated.
net session >nul 2>&1
if not errorlevel 1 goto :elevated

rem Ask Windows for permission and start again. The prompt that appears is
rem Windows asking, not this script.
set "MANET_SELF=%~f0"
set "MANET_HERE=%HERE%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "Start-Process -FilePath $env:MANET_SELF -WorkingDirectory $env:MANET_HERE -Verb RunAs" >nul 2>&1
if errorlevel 1 goto :no_permission
exit /b

:elevated
rem Beside a checkout, use it as it stands and never overwrite anything: those
rem files may be someone's work in progress. On its own, keep our copy in a
rem subfolder so a desktop does not end up strewn with them.
if exist "%HERE%windows.ps1" goto :use_local

set "WORK=%HERE%MANET-flasher\"
call :fetch
if errorlevel 1 goto :fetch_failed
goto :launch

:use_local
set "WORK=%HERE%"
goto :launch

:launch
if not exist "%WORK%manet-flasher.ps1" goto :missing_gui

rem Windows PowerShell 5.1 ships with Windows, so nothing has to be installed.
rem -STA is what the window needs, -ExecutionPolicy Bypass gets past the block
rem on scripts that came from the internet, and -NoProfile keeps someone else's
rem profile out of it.
powershell.exe -NoProfile -ExecutionPolicy Bypass -STA -File "%WORK%manet-flasher.ps1"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :bad_exit
exit /b 0


rem ============================================================
rem Fetching our own copy
rem ============================================================
rem Everything is passed through environment variables rather than %VARS%
rem inside the quoted command, so nothing here depends on batch quoting rules.
rem Files are refreshed on every run, because a launcher-managed copy that
rem silently went stale would be worse than a download nobody notices. If the
rem network is not there, whatever was fetched last time is used instead.

:fetch
echo.
echo   Getting what the flasher needs...
echo.
set "MANET_SRC=%SRC%"
set "MANET_FILES=%FILES%"
set "MANET_WORK=%WORK%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $bad=0; foreach($f in $env:MANET_FILES.Split('|')){ $dest=Join-Path $env:MANET_WORK ($f -replace '/','\'); $dir=Split-Path $dest; if(-not (Test-Path $dir)){ New-Item -ItemType Directory -Path $dir -Force | Out-Null }; $tmp=$dest+'.part'; try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 60 -Uri ($env:MANET_SRC+'/'+$f) -OutFile $tmp; $head=(Get-Content -LiteralPath $tmp -TotalCount 1 -ErrorAction SilentlyContinue); if((Get-Item $tmp).Length -lt 400 -or ($head -and $head.TrimStart().StartsWith('<'))){ throw 'that is not the file, it looks like a sign-in or error page' }; Move-Item -LiteralPath $tmp -Destination $dest -Force; '   ok      '+$f } catch { Remove-Item $tmp -Force -ErrorAction SilentlyContinue; if(Test-Path $dest){ '   cached  '+$f+'   ('+$_.Exception.Message+')' } else { '   FAILED  '+$f; '           '+$_.Exception.Message; $bad++ } } }; if($bad -gt 0){ exit 1 }"
if errorlevel 1 exit /b 1
echo.
exit /b 0


rem ============================================================
rem Things that went wrong
rem ============================================================

:in_a_zip
echo.
echo   This is running from inside a zip file.
echo.
echo   Right-click the zip, choose "Extract All", and run this again from the
echo   folder it makes. Windows deletes the folder a zip is previewed in, so
echo   nothing the flasher needs would survive.
echo.
pause
exit /b 1

:no_permission
echo.
echo   Windows would not grant Administrator, so the card cannot be written.
echo   Try again and choose Yes, or ask whoever administers this computer.
echo.
pause
exit /b 1

:fetch_failed
echo.
echo   Could not download the files the flasher needs, and there is no copy
echo   here from a previous run.
echo.
echo   Check this computer is on the internet and try again. If it is behind a
echo   proxy or a sign-in page, open a browser first and get through it.
echo.
echo   Failing that, download the whole provisioning folder from
echo   https://github.com/very-srs/MANET and put this file back in it.
echo.
pause
exit /b 1

:missing_gui
echo.
echo   manet-flasher.ps1 is not in:
echo     %WORK%
echo.
echo   Delete that folder and run this again, and it will fetch a fresh copy.
echo.
pause
exit /b 1

:bad_exit
echo.
echo   The flasher stopped with code %RC%.
echo.
pause
exit /b %RC%
