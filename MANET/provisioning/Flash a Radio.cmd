@echo off
setlocal
title MANET radio flasher

rem Double-click this. It is the only file you need.
rem
rem On its own it makes itself a folder, moves in, and fetches what it needs
rem from GitHub. Settings and your own setup scripts live in that folder too,
rem so everything to do with flashing stays in one place.
rem
rem Sitting in a checkout, next to windows.ps1, it uses the folder as it stands
rem and downloads nothing.
rem
rem Two things stop a .ps1 from just running when someone double-clicks it:
rem Windows will not run one without asking, and writing to a card needs
rem Administrator. This deals with both, then opens the window.

set "REPO=very-srs/MANET"
set "BRANCH=main"
set "SUBDIR=MANET/provisioning"
set "FILES=windows.ps1|manet-flasher.ps1|firstrun.sh.template|rock3a-provision.sh.template|additional-scripts/README.md"
set "HERE=%~dp0"
set "SELF=%~f0"
set "MYNAME=%~nx0"
set "FOLDER=MANET Flasher"
set "HOMEMARK=.manet-flasher-home"
set "MOVEDFROM="

rem No path built here ever ends in a backslash before a closing quote. A
rem folder name with a space in it, which "MANET Flasher" has, comes apart at
rem the space when a trailing \" confuses whichever command receives it.
set "WORK="

rem Running straight out of a zip gives a temporary folder that Windows throws
rem away, so anything downloaded into it is lost and anything extracted beside
rem it is not there. Say so rather than failing halfway through.
echo "%HERE%" | find /i ".zip" >nul
if not errorlevel 1 goto :in_a_zip

rem fltmc needs Administrator and, unlike "net session", does not depend on a
rem Windows service that plenty of machines have turned off. If it is missing
rem altogether the sentinel below still keeps this to one relaunch.
fltmc >nul 2>&1
if not errorlevel 1 goto :elevated

rem Already been round once? Then the check above is wrong about this machine.
rem Carry on and let the flasher itself say so, because a detection that is
rem wrong must not turn into an endless spawn of new windows.
if /i "%~1"=="--elevated" goto :elevated

rem Ask Windows for permission and start again. The prompt that appears is
rem Windows asking, not this script. Paths go through the environment because
rem a quoted argument would come apart on a folder like C:\Users\O'Brien.
set "MANET_SELF=%SELF%"
set "MANET_HERE=%HERE%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "Start-Process -FilePath $env:MANET_SELF -ArgumentList '--elevated' -WorkingDirectory $env:MANET_HERE -Verb RunAs" >nul 2>&1
if errorlevel 1 goto :no_permission
exit /b


:elevated
rem Elevating goes through the Windows elevation service, which starts us in
rem C:\Windows\System32 whatever -WorkingDirectory said. Nothing below depends
rem on the current folder, but leaving a user sitting in System32 after a
rem failure is its own small horror.
cd /d "%HERE%" 2>nul

rem Our own folder, from a previous run, so refresh what is in it. This is
rem tested BEFORE the checkout test below and that order matters: the first run
rem downloads windows.ps1 into this folder, so a checkout test that ran first
rem would match from the second run onwards and the files would never be
rem refreshed again. The marker is only ever written by us. The folder name is
rem checked too, so deleting the marker does not nest a folder inside a folder.
if exist "%HERE%%HOMEMARK%" goto :work_here
for %%I in ("%HERE:~0,-1%") do set "MYDIR=%%~nxI"
if /i "%MYDIR%"=="%FOLDER%" goto :work_here

rem A checkout: windows.ps1 is right there and no marker of ours beside it. Use
rem the folder as it stands, download nothing, overwrite nothing. Somebody's
rem work in progress may be in it.
if exist "%HERE%windows.ps1" goto :run_here

rem Nothing here but us: make a home and move into it. There is no relaunch.
rem This process simply carries on with the new folder as its working folder,
rem and the copy left behind is deleted at the very end.
set "TARGET=%HERE%%FOLDER%"
mkdir "%TARGET%" 2>nul
copy /y "%SELF%" "%TARGET%\%MYNAME%" >nul 2>&1
if not errorlevel 1 goto :relocated

rem Could not write beside ourselves: a read-only share, Program Files, a
rem locked-down Downloads folder. The user's own app data always works.
set "TARGET=%LOCALAPPDATA%\%FOLDER%"
mkdir "%TARGET%" 2>nul
copy /y "%SELF%" "%TARGET%\%MYNAME%" >nul 2>&1
if errorlevel 1 goto :relocate_failed

:relocated
type nul > "%TARGET%\%HOMEMARK%"
set "WORK=%TARGET%"
set "MOVEDFROM=%SELF%"
echo.
echo   Everything for the flasher now lives in
echo     %TARGET%
echo   including your saved settings. Run it from there next time.
goto :fetch_and_run

:work_here
set "WORK=%HERE:~0,-1%"
goto :fetch_and_run

:run_here
set "WORK=%HERE:~0,-1%"
goto :launch

:fetch_and_run
call :fetch
if errorlevel 1 goto :fetch_failed
goto :launch

:launch
if not exist "%WORK%\manet-flasher.ps1" goto :missing_gui
cd /d "%WORK%" 2>nul

rem Windows PowerShell 5.1 ships with Windows, so nothing has to be installed.
rem -STA is what the window needs, -ExecutionPolicy Bypass gets past the block
rem on scripts that came from the internet, and -NoProfile keeps someone else's
rem profile out of it.
powershell.exe -NoProfile -ExecutionPolicy Bypass -STA -File "%WORK%\manet-flasher.ps1"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :bad_exit

if not defined MOVEDFROM exit /b 0
if not exist "%MOVEDFROM%" exit /b 0
rem Tidy away the copy that was downloaded, now that everything is in the new
rem folder. (goto) makes cmd let go of this file so it can delete itself.
rem Nothing after this line is ever read, which is what makes it safe.
(goto) 2>nul & del /f /q "%MOVEDFROM%" 2>nul


rem ============================================================
rem Fetching our own copy
rem ============================================================
rem Everything is passed through environment variables rather than %VARS%
rem inside the quoted command, so nothing here depends on batch quoting rules.
rem Files are refreshed on every run, because a launcher-managed copy that
rem silently went stale would be worse than a download nobody notices. If the
rem network is not there, whatever was fetched last time is used instead.
rem
rem The branch is resolved to a commit first, and the files are then pulled
rem from URLs pinned to that commit. raw.githubusercontent caches a branch URL
rem for several minutes, so fetching .../main/... shortly after a change hands
rem back the previous file and the flasher appears not to have changed at all.
rem A query string does not help; that cache ignores it. A commit URL is a
rem different URL whenever the content differs, so it cannot be stale.

:fetch
echo.
echo   Getting what the flasher needs...
echo.
set "MANET_REPO=%REPO%"
set "MANET_BRANCH=%BRANCH%"
set "MANET_SUBDIR=%SUBDIR%"
set "MANET_FILES=%FILES%"
set "MANET_WORK=%WORK%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $hdr=@{'User-Agent'='manet-flasher'}; $ref=$env:MANET_BRANCH; try { $sha=Invoke-RestMethod -TimeoutSec 30 -Headers ($hdr+@{'Accept'='application/vnd.github.sha'}) -Uri ('https://api.github.com/repos/'+$env:MANET_REPO+'/commits/'+$env:MANET_BRANCH); if($sha -match '^[0-9a-f]{40}$'){ $ref=$sha } } catch { }; $base='https://raw.githubusercontent.com/'+$env:MANET_REPO+'/'+$ref+'/'+$env:MANET_SUBDIR; if($ref -eq $env:MANET_BRANCH){ '   could not resolve the commit; this copy may be a few minutes behind' } else { '   from commit '+$ref.Substring(0,12) }; $bad=0; foreach($f in $env:MANET_FILES.Split('|')){ $dest=Join-Path $env:MANET_WORK ($f -replace '/','\'); $dir=Split-Path $dest; if(-not (Test-Path $dir)){ New-Item -ItemType Directory -Path $dir -Force | Out-Null }; $tmp=$dest+'.part'; try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 60 -Headers $hdr -Uri ($base+'/'+$f) -OutFile $tmp; $head=(Get-Content -LiteralPath $tmp -TotalCount 1 -ErrorAction SilentlyContinue); if((Get-Item $tmp).Length -lt 400 -or ($head -and $head.TrimStart().StartsWith('<'))){ throw 'that is not the file, it looks like a sign-in or error page' }; $sz=(Get-Item $tmp).Length; Move-Item -LiteralPath $tmp -Destination $dest -Force; '   ok      '+$f.PadRight(30)+$sz+' bytes' } catch { Remove-Item $tmp -Force -ErrorAction SilentlyContinue; if(Test-Path $dest){ '   cached  '+$f+'   ('+$_.Exception.Message+')' } else { '   FAILED  '+$f; '           '+$_.Exception.Message; $bad++ } } }; if($bad -gt 0){ exit 1 }"
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

:relocate_failed
echo.
echo   Could not make a folder to work in, either next to this file or in
echo     %LOCALAPPDATA%
echo.
echo   Copy this file somewhere you can write to, such as your Desktop or
echo   Documents, and run it again.
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
echo   manet-flasher.ps1 is not in
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
