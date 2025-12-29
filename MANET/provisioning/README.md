#### PREREQUISITES

Create a new directory on your computer to work out of.

For Rock 3A boards, download this OS image:
[https://dl.armbian.com/uefi-arm64/Trixie_current_minimal](https://dl.armbian.com/uefi-arm64/Trixie_current_minimal).
Place this image in the directory you'll be working from.

For RPi boards, you need `rpi-imager`.
- Windows: [Download Installer](https://downloads.raspberrypi.com/imager/imager_latest.exe)
- Linux: `sudo apt install rpi-imager`

For CM4 Boards, in addition to `rpi-imager` you need `rpiboot`.
- Windows: [Download Installer](https://github.com/raspberrypi/usbboot/blob/master/win32/rpiboot_setup.exe)
- Linux: `sudo apt install rpiboot`

Download the files from this repo's directory to the directory you'll be working from:
- `ddrelease64.exe` (Windows only - dd for Windows)
- `firstrun.sh.template` (Script runs when the node first boots)
- `linux.sh` (The flashing script to run on Linux)
- `windows.ps1` (The flashing script to run on Windows)

---

### FLASHING

Once you have your tools installed and scripts collected into a directory, run the flashing script that corresponds to the OS your computer uses. Answer the questions, and the flashing script will use either `rpi-imager` or `dd` to flash the device, depending on which OS you're installing.

> Note for CM4 on Windows: You must manually run `rpiboot` to mount your eMMC storage before you run the Windows flashing script. Linux will handle this automatically.

---

### SETUP OPTIONS

1. Select EUD (client) connection type
   You are choosing how EUDs will connect to this mesh node.

   Wired - You'll use an Ethernet to USB adapter for a phone, or plain Ethernet for a computer. There will be no WiFi AP broadcast from the mesh node; both 2.4 and 5 GHz radios will be included in the MANET mesh. Plugging the node into an external, internet-enabled network will turn the node into a mesh gateway.

   Wireless - This mesh node will broadcast a low power (5db) 5GHz 802.11ax access point for your EUD to connect to. Plugging the node into an external, internet-enabled network will turn the node into a mesh gateway. Plugging in an EUD will also work the same as with the wired option.

   Auto - This mode will function as wireless in the absence of a plugged-in EUD. If an EUD is plugged in, the node will not broadcast an AP.

2. Install MediaMTX Server?

   A MediaMTX server will be available on this network. It will be assigned the address ending in `.2` of whichever network range you select.
   Ex: `10.30.1.2` on a `10.30.1.0/24` network.

3. Install Mumble Server?

   A Mumble server will be available on this network. It will be assigned the address ending in `.3` of whichever network range you select.
   Ex: `10.30.1.3` on a `10.30.1.0/24` network.

4. Enter MESH SSID Name

   This is the SSID the nodes will use for the MANET mesh.

5. Enter MESH SAE Key

   This is the WPA3-SAE encryption key the nodes will use. It is likely you will want to use the automatically generated one here (just hit enter) unless you have a specific reason to use a pre-existing one. The automatic one will be 58 characters and be created with your system's random number generator/SSL library.

6. Enter a password for the radio user

   Each node has a user called `radio`, for use if you want to SSH into the node. Here you are setting your own password for this user. Leaving this blank sets the password to the default: `radio`.

7. Use default LAN network

   Saying yes to this sets your network to `10.30.1.0/24`. Saying no here allows you to select the network addressing you would prefer. A custom address space must be provided in CIDR notation and must be between `/16` and `/26`.

8. Use Automatic WiFi Channel Selection

   A no answer here sets all nodes to a default and static 802.11ax channel. With automatic channel selection enabled, the radios will periodically scan for and switch to a clearer WiFi channel. There is a minor network disruption for this to work (one of the two WiFi radios will disconnect briefly). This is not an option when using a wireless EUD.

---

### HARDWARE STATUS

After answering these questions, you may (and should) save this config. It will allow you to load it on the next run of the script and flash additional radios without going through the questions again.

When finished with the questions, or after loading an existing config, you must select the type of hardware you'll be flashing.

| Hardware | Status | Notes |
| :--- | :--- | :--- |
| Radxa Rock 3A | Pre-release | Full support expected Feb 2026 |
| Raspberry Pi 5 | Pre-release | No HaLow support |
| Raspberry Pi 4B | Untested | Support expected soon |
| Compute Module 4 (CM4) | Functional | Fully functional, 802.11ax + HaLow |
