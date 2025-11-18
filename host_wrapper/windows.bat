@echo off
setlocal enabledelayedexpansion

:: --- Configuration ---
set "TEMPLATE_FILE=firstrun.sh.template"
set "TEMP_SCRIPT_FILE=%TEMP%\firstrun-temp-%RANDOM%.sh"
set "CONFIG_DIR=pi-configs"
set "OS_IMAGE_URL=https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2025-10-02/2025-10-01-raspios-trixie-arm64-lite.img.xz"

:: --- 1. Check Dependencies ---
if not exist "%TEMPLATE_FILE%" (
	echo ERROR: Template file '%TEMPLATE_FILE%' not found.
	pause
	exit /b
)
if exist "%ProgramFiles(x86)%\Raspberry Pi Imager\rpi-imager.exe" (
	set "IMAGER_PATH=%ProgramFiles(x86)%\Raspberry Pi Imager\rpi-imager.exe"
) else if exist "%ProgramFiles%\Raspberry Pi Imager\rpi-imager.exe" (
	set "IMAGER_PATH=%ProgramFiles%\Raspberry Pi Imager\rpi-imager.exe"
) else (
	echo ERROR: Cannot find rpi-imager.exe
	pause
	exit /b
)
echo Found rpi-imager at %IMAGER_PATH%
echo.

:: Find rpiboot.exe
set "RPIBOOT_PATH="
if exist "%ProgramFiles(x86)%\Raspberry Pi\rpiboot.exe" (
	set "RPIBOOT_PATH=%ProgramFiles(x86)%\Raspberry Pi\rpiboot.exe"
) else if exist "%ProgramFiles%\Raspberry Pi\rpiboot.exe" (
	set "RPIBOOT_PATH=%ProgramFiles%\Raspberry Pi\rpiboot.exe"
) else if exist "rpiboot.exe" (
	set "RPIBOOT_PATH=.\rpiboot.exe"
)

:: Ensure config directory exists
if not exist "%CONFIG_DIR%\" mkdir "%CONFIG_DIR%"

:: --- 2. Load or Create Config ---
set "CONFIG_FOUND="
for %%f in (%CONFIG_DIR%\*.bat) do set "CONFIG_FOUND=true"

if defined CONFIG_FOUND (
	echo Found saved configuration(s).
	echo 1. Load a saved configuration
	echo 2. Create a new configuration
	choice /c 12 /m "Select an option:"
	if errorlevel 2 goto :new_config
	if errorlevel 1 goto :load_menu
) else (
	echo No saved configs found. Starting new setup.
	goto :new_config
)

:load_menu
cls
echo Please select a configuration to load:
set /a "count=0"
for /f "delims=" %%f in ('dir /b "%CONFIG_DIR%\*.bat"') do (
	set /a "count+=1"
	set "config_!count!=%%f"
	echo !count!. %%~nf
)
set "config_count=%count%"
echo.
set /p "choice_num=Enter number (or 'c' to cancel): "

if /i "%choice_num%"=="c" (
	echo Aborting.
	pause
	exit /b
)

:: Validate choice
if %choice_num% GTR 0 if %choice_num% LEQ %config_count% (
	call :load_config "%%~n!config_%choice_num%!%%"
	goto :post_config
) else (
	echo Invalid selection.
	pause
	goto :load_menu
)

:new_config
call :ask_questions
call :save_config
goto :post_config


:post_config
:: --- 3. Select Hardware (moved here from ask_questions) ---
echo.
echo --- Hardware Selection ---
call :ask_hardware_model

:: --- 4. Get Image & Device ---
echo.
echo --- Image ^& Device ---
echo Using image: %OS_IMAGE_URL%
echo rpi-imager will download/cache this image if needed.
echo.
echo Identifying available drives...
echo ========================================================
wmic diskdrive get DeviceID,Model,Size
echo ========================================================
echo.
echo WARNING: Identify your BOOT DRIVE (e.g., C:\) from the list above and DO NOT select it.
echo		  Common target drives are removable media.
set /p "TARGET_DEVICE=Enter target device (e.g., \\.\PHYSICALDRIVE2): "

echo.
echo WARNING: This will ERASE ALL DATA on %TARGET_DEVICE%.
set /p "CONFIRM=Are you sure? (yes/no): "
if not /i "%CONFIRM%"=="yes" (
	echo Aborting.
	pause
	exit /b
)

:: --- 5. Create Temporary Script ---
echo "Generating temporary firstrun script..."
(for /f "delims=" %%i in (%TEMPLATE_FILE%) do (
	set "line=%%i"
	set "line=!line:__HARDWARE_MODEL__=%HARDWARE_MODEL%!"
	set "line=!line:__EUD_CONNECTION__=%EUD_CONNECTION%!"
	set "line=!line:__INSTALL_MEDIAMTX__=%INSTALL_MEDIAMTX%!"
	set "line=!line:__INSTALL_MUMBLE__=%INSTALL_MUMBLE%!"
	set "line=!line:__LAN_SSID__=%LAN_SSID%!"
	set "line=!line:__LAN_SAE_KEY__=%LAN_SAE_KEY%!"
	set "line=!line:__LAN_CIDR_BLOCK__=%LAN_CIDR_BLOCK%!"
	set "line=!line:__AUTO_CHANNEL__=%AUTO_CHANNEL%!"
	echo(!line!
)) > "%TEMP_SCRIPT_FILE%"

:: --- 6. Run rpi-imager ---
echo Starting rpi-imager...
"%IMAGER_PATH%" --cli "%OS_IMAGE_URL%" "%TARGET_DEVICE%" --first-run-script "%TEMP_SCRIPT_FILE%"

:: --- 7. Cleanup ---
del "%TEMP_SCRIPT_FILE%"
echo.
echo Done! Flashing complete. The Pi will configure itself on first boot.
pause
goto :eof

:: =================================
:: FUNCTIONS (GOTO Labels)
:: =================================

:ask_questions
echo --- Starting New Configuration ---

:: Hardware selection is now done in :post_config

:ask_eud_connection
echo Select EUD (client) connection type:
echo 1. Wired
echo 2. Wireless
choice /c 12 /m "Select an option:"
if errorlevel 2 set "EUD_CONNECTION=wireless"
if errorlevel 1 set "EUD_CONNECTION=wired"
echo Selected: %EUD_CONNECTION%

:ask_mediamtx
set "INSTALL_MEDIAMTX="
set /p "INSTALL_MEDIAMTX=Install MediaMTX Server? (Y/n) [y]: "
if not defined INSTALL_MEDIAMTX set "INSTALL_MEDIAMTX=y"
if /i "%INSTALL_MEDIAMTX%"=="y" (set "INSTALL_MEDIAMTX=y") else (set "INSTALL_MEDIAMTX=n")

:ask_mumble
set "INSTALL_MUMBLE="
set /p "INSTALL_MUMBLE=Install Mumble Server (murmur)? (Y/n) [y]: "
if not defined INSTALL_MUMBLE set "INSTALL_MUMBLE=y"
if /i "%INSTALL_MUMBLE%"=="y" (set "INSTALL_MUMBLE=y") else (set "INSTALL_MUMBLE=n")

:ask_lan_ssid
set /p "LAN_SSID=Enter LAN SSID Name: "

:ask_lan_sae
set "LAN_SAE_KEY="
set /p "LAN_SAE_KEY_INPUT=Enter LAN SAE Key (WPA3 password, 8-63 chars) [or press Enter to generate]: "
if "%LAN_SAE_KEY_INPUT%"=="" (
	set "LAN_SAE_KEY=P%RANDOM%a%RANDOM%s%RANDOM%s"
	echo Generated (weak) SAE Key: %LAN_SAE_KEY%
) else (
	set "LAN_SAE_KEY=%LAN_SAE_KEY_INPUT%"
)
:: Validate length
set "test_key=%LAN_SAE_KEY%"
set "key_len=0"
:len_loop
if defined test_key (
	set "test_key=%test_key:~1%"
	set /a "key_len+=1"
	goto :len_loop
)
if %key_len% LSS 8 (
	echo ERROR: Key must be at least 8 characters. You entered %key_len%.
	goto :ask_lan_sae
)
if %key_len% GTR 63 (
	echo ERROR: Key must be 63 characters or less. You entered %key_len%.
	goto :ask_lan_sae
)

:: Call the new CIDR function
call :ask_lan_cidr

:ask_auto_channel
set "AUTO_CHANNEL="
set /p "AUTO_CHANNEL=Use Automatic Channel Selection? (Y/n) [y]: "
if not defined AUTO_CHANNEL set "AUTO_CHANNEL=y"
if /i "%AUTO_CHANNEL%"=="y" (set "AUTO_CHANNEL=y") else (set "AUTO_CHANNEL=n")

echo ----------------------------------
goto :eof


:ask_hardware_model
echo Select Raspberry Pi Model:
echo 1. Raspberry Pi 5
echo 2. Raspberry Pi 4B
echo 3. Compute Module 4 (CM4)
choice /c 123 /m "Select an option:"
if errorlevel 3 goto :hw_cm4
if errorlevel 2 (
	set "HARDWARE_MODEL=rpi4"
	echo Selected: Raspberry Pi 4B
	goto :eof
)
if errorlevel 1 (
	set "HARDWARE_MODEL=rpi5"
	echo Selected: Raspberry Pi 5
	goto :eof
)

:hw_cm4
set "HARDWARE_MODEL=rpi4"
echo Selected: Compute Module 4 (CM4)
if not defined RPIBOOT_PATH (
	echo ERROR: 'rpiboot.exe' not found.
	echo Please install Raspberry Pi drivers or place rpiboot.exe in this directory.
	pause
	exit /b
)
echo Please connect your CM4 to this computer in USB-boot mode.
echo The script will run rpiboot.exe to mount the eMMC.
pause
start "RPiBoot" /wait "%RPIBOOT_PATH%"
echo "'rpiboot' finished. The eMMC should now be visible as a drive."
goto :eof


:ask_lan_cidr
set "DEFAULT_CIDR=10.30.2.0/24"
set "confirm_default="
set /p "confirm_default=Use default LAN network %DEFAULT_CIDR%? (Y/n) [y]: "
if not defined confirm_default set "confirm_default=y"

if /i "%confirm_default%"=="y" (
	set "LAN_CIDR_BLOCK=%DEFAULT_CIDR%"
	echo Using default network: %LAN_CIDR_BLOCK%
	goto :eof
)

:manual_cidr_loop
set "custom_cidr="
set /p "custom_cidr=Enter custom LAN CIDR block (e.g., 10.10.0.0/16): "

set "ip_part="
set "prefix_part="
for /f "tokens=1,2 delims=/" %%a in ("%custom_cidr%") do (
	set "ip_part=%%a"
	set "prefix_part=%%b"
)

if not defined ip_part (
	echo ERROR: Invalid format. Must be x.x.x.x/yy
	goto :manual_cidr_loop
)
if not defined prefix_part (
	echo ERROR: Invalid format. Missing prefix /yy
	goto :manual_cidr_loop
)

:: Check if prefix is a number
set /a "check_prefix=prefix_part + 0" 2>nul
if not "%check_prefix%"=="%prefix_part%" (
	 echo ERROR: Prefix '/%prefix_part%' is not a valid number.
	 goto :manual_cidr_loop
)
if %prefix_part% LSS 16 (
	echo ERROR: Prefix /%prefix_part% is too small. Must be 16-30.
	goto :manual_cidr_loop
)
if %prefix_part% GTR 30 (
	echo ERROR: Prefix /%prefix_part% is too large. Must be 16-30.
	goto :manual_cidr_loop
)

:: Validate IP as a private range
set "o1="
set "o2="
for /f "tokens=1,2 delims=." %%i in ("%ip_part%") do (
	set "o1=%%i"
	set "o2=%%j"
)

set "is_private=false"
if "%o1%"=="10" (
	set "is_private=true"
)
if "%o1%"=="172" (
	set /a "check_o2=o2 + 0" 2>nul
	if "%check_o2%"=="%o2%" (
		if %o2% GEQ 16 if %o2% LEQ 31 (
			set "is_private=true"
		)
	)
)
if "%o1%"=="192" (
	if "%o2%"=="168" (
		set "is_private=true"
	)
)

if "%is_private%"=="false" (
	echo ERROR: IP %ip_part% is not in a private range.
	echo Must be in 10.0.0.0/8, 172.16.0.0/12, or 192.168.0.0/16.
	goto :manual_cidr_loop
)

:: All checks passed
set "LAN_CIDR_BLOCK=%custom_cidr%"
echo "Using custom network: %LAN_CIDR_BLOCK%"
goto :eof


:save_config
echo.
set "save_choice="
set /p "save_choice=Save this configuration? (Y/n) [y]: "
if not defined save_choice set "save_choice=y"
if /i not "%save_choice%"=="y" goto :eof

set /p "config_name=Enter a name for this config (e.g., media-server): "
if "%config_name%"=="" (
	echo Invalid name, skipping save.
	goto :eof
)
set "CONFIG_FILE=%CONFIG_DIR%\%config_name%.bat"
(
	echo @echo off
	echo rem Pi Imager Config: %config_name%
	echo rem Hardware model is selected at runtime, not saved
	echo set "EUD_CONNECTION=%EUD_CONNECTION%"
	echo set "INSTALL_MEDIAMTX=%INSTALL_MEDIAMTX%"
	echo set "INSTALL_MUMBLE=%INSTALL_MUMBLE%"
	echo set "LAN_SSID=%LAN_SSID%"
	echo set "LAN_SAE_KEY=%LAN_SAE_KEY%"
	echo set "LAN_CIDR_BLOCK=%LAN_CIDR_BLOCK%"
	echo set "AUTO_CHANNEL=%AUTO_CHANNEL%"
) > "%CONFIG_FILE%"
echo Configuration saved to %CONFIG_FILE%
goto :eof


:load_config
set "CONFIG_FILE=%CONFIG_DIR%\%~1.bat"
echo Loading config from %CONFIG_FILE%...
call "%CONFIG_FILE%"
echo --- Loaded Configuration ---
echo   EUD Connection: %EUD_CONNECTION%
echo   Install MediaMTX: %INSTALL_MEDIAMTX%
echo   Install Mumble: %INSTALL_MUMBLE%
echo   LAN SSID: %LAN_SSID%
echo   LAN SAE Key: %LAN_SAE_KEY%
echo   LAN CIDR Block: %LAN_CIDR_BLOCK%
echo   Auto Channel: %AUTO_CHANNEL%
echo ----------------------------
echo (Hardware will be selected next)
goto :eof
