@echo off
setlocal enabledelayedexpansion
title MANET radio flasher

rem Double-click this. It is the only file you need to start.
rem
rem Two things stop a .ps1 from just running when someone double-clicks it:
rem Windows will not run one without asking, and writing to a card needs
rem Administrator. This deals with both, then opens the window.

set "HERE=%~dp0"
set "GUI=%HERE%manet-flasher.ps1"

rem Running straight out of a zip gives a temporary folder that holds only the
rem file that was double-clicked, so the templates the flasher needs are not
rem beside it. Say so rather than failing halfway through.
echo %HERE% | find /i ".zip\" >nul
if not errorlevel 1 (
    echo.
    echo   This is running from inside a zip file.
    echo.
    echo   Right-click the zip, choose "Extract All", and run this again from
    echo   the folder it makes. The flasher needs the other files next to it.
    echo.
    pause
    exit /b 1
)

if not exist "%GUI%" (
    echo.
    echo   manet-flasher.ps1 is not in this folder:
    echo     %HERE%
    echo.
    echo   Copy the whole provisioning folder, not just this one file.
    echo.
    pause
    exit /b 1
)

rem Already Administrator? net session only succeeds when elevated.
net session >nul 2>&1
if not errorlevel 1 goto :run

rem Ask Windows for permission and start again. The prompt that appears is
rem Windows asking, not this script.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "Start-Process -FilePath '%~f0' -WorkingDirectory '%HERE%' -Verb RunAs" >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Windows would not grant Administrator, so the card cannot be written.
    echo   Try again and choose Yes, or ask whoever administers this computer.
    echo.
    pause
)
exit /b

:run
rem Windows PowerShell 5.1 ships with Windows, so nothing has to be installed.
rem -STA is what the window needs, -ExecutionPolicy Bypass gets past the block
rem on scripts that came from the internet, and -NoProfile keeps someone else's
rem profile out of it.
powershell.exe -NoProfile -ExecutionPolicy Bypass -STA -File "%GUI%"
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo   The flasher stopped with code %RC%.
    echo.
    pause
)
exit /b %RC%
