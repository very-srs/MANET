# Provisioning Guide

This directory contains the scripts and templates needed to flash a new mesh radio node.

---

## How It Works

The provisioning process has two phases:

**Phase 1 – Flashing (on your computer):** You run `linux.sh` or `windows.ps1` on your host machine. The script walks you through selecting hardware, loading or creating a configuration, then prepares and flashes the OS image to your target storage device. All your mesh settings are baked into the image during this step.

**Phase 2 – First Boot (on the node):** You insert the storage, connect Ethernet, and power on the node. A systemd service embedded in the image runs automatically once the network is available, downloads packages, configures the radio interfaces, and reboots into a fully functional mesh node.

---

## PREREQUISITES

You will need:
- A supported SBC (see main README for hardware support table)
- A Linux or Windows computer to flash from
- An SD card or eMMC adapter, appropriate for your hardware
- Ethernet internet access on the node during its first boot

### Required tools (Linux host)

| Tool | Purpose | Install |
|------|---------|---------|
| `rpi-imager` | Flashing Raspberry Pi boards | `sudo apt install rpi-imager` |
| `rpiboot` | Mounting CM4 eMMC | `sudo apt install rpiboot` |
| `losetup` | Mounting Armbian image for Rock 3A | included in `util-linux` |
| `xz` | Decompressing downloaded Armbian images | `sudo apt install xz-utils` |
| `bc` | Network CIDR calculations | `sudo apt install bc` |
| `openssl` | Generating SAE keys | usually pre-installed |

> **Rock 3A on Linux:** `rpi-imager` is not needed. `rpiboot` is not needed. You do need `losetup`, `xz`, `bc`, and `openssl`.

> **Raspberry Pi on Linux:** `rpi-imager` is required. `losetup` and `xz` are not required.

> **CM4 on Linux:** `rpi-imager` and `rpiboot` are both required.

### Required tools (Windows host)

| Tool | Purpose | Install |
|------|---------|---------|
| `rpi-imager` | Flashing Raspberry Pi boards | [Download Installer](https://downloads.raspberrypi.com/imager/imager_latest.exe) |
| Ext2Fsd | Mounting ext4 partitions for Rock 3A | Required for Rock 3A provisioning |

> **CM4 on Windows:** You must manually run `rpiboot` before running `windows.ps1` to mount the eMMC. The script will not do this for you. On Linux, the script handles this interactively.

> **Rock 3A on Windows — password hashing:** The script pre-creates the `radio` user by writing directly to `/etc/shadow`, which requires generating a Linux SHA-512 password hash on your Windows machine. The script tries `openssl` (available if Git for Windows is installed), then WSL, then Python. If none of these are available the `radio` account will be created with a locked password — you can still log in as `root` (password `1234`) and run `passwd radio` to set it manually. Having Git for Windows installed is the easiest way to satisfy this.

### Files needed from this directory

Clone or download the entire `provisioning/` directory to your working folder. The scripts require these files to be present alongside them:

- `linux.sh` — flashing script for Linux hosts
- `windows.ps1` — flashing script for Windows hosts
- `firstrun.sh.template` — Raspberry Pi first-boot script template
- `rock3a-provision.sh.template` — Rock 3A first-boot provisioning script template

### OS Images

**You do not need to download OS images manually.** The scripts handle this automatically:

- **Raspberry Pi (all models):** `rpi-imager` downloads the correct Raspberry Pi OS Lite image directly from the Raspberry Pi Foundation's servers and caches it locally.

- **Rock 3A:** The script will offer to download the correct Armbian image automatically. If you already have an Armbian `.img` or `.img.xz` file locally, you can point the script to it instead. The expected image is Armbian Trixie (Debian 13) minimal for the Rock 3A — do not use a generic ARM64 image, it must be the board-specific build.

---

## FLASHING

From the `provisioning/` directory, run the script matching your host OS:

```bash
# Linux
bash linux.sh

# Windows (PowerShell, run as Administrator)
.\windows.ps1
```

The script will:
1. Ask you to select your hardware platform (Rock 3A, Pi 5, Pi 4B, or CM4)
2. Offer to load a saved configuration or create a new one
3. Acquire the OS image (download automatically or use a local file)
4. Ask you to select the target device
5. Show a final confirmation before writing — **all data on the target will be erased**
6. Flash and configure the image

> **Saved configs:** The script saves configurations to a `.mesh-configs/` directory so you can re-flash nodes with the same settings quickly.

> **CM4 on Linux:** When you select CM4, the script will prompt you to connect the module in USB-boot mode, then run `rpiboot` automatically and detect the newly mounted eMMC device.

---

## SETUP OPTIONS

The script will ask the following questions. These can also be loaded from a saved config file.

### 1. EUD (End User Device) Connection Type

How EUDs connect to this mesh node:

- **Wired** — EUDs connect via Ethernet (or Ethernet-to-USB adapter). No Wi-Fi AP is broadcast. Both 2.4 and 5 GHz radios join the MANET mesh. Connecting the node to an internet-enabled network makes it a mesh gateway.
- **Wireless** — The node broadcasts a 5 GHz 802.11ax access point at low power (5 dBm) for EUDs to join. Connecting to an upstream network enables gateway mode. Wired EUDs also work.
- **Auto** — Behaves as Wireless until a wired EUD is detected, then stops broadcasting the AP.

### 2. Install MediaMTX Server?

If yes, a MediaMTX streaming server will be elected to run somewhere on the mesh. Its reserved address ends in `.2` of your chosen network range (e.g. `10.30.1.2` on a `10.30.1.0/24` network).

### 3. Install Mumble Server?

If yes, a Mumble voice server will be available on the mesh at the address ending in `.3` (e.g. `10.30.1.3`). Note: Mumble integration is currently untested.

### 4. Mesh SSID

The SSID all nodes use to form the MANET backhaul mesh.

### 5. Mesh SAE Key

The WPA3-SAE encryption key for the mesh. A key will be generated automatically if you leave this blank.

### 6. Network CIDR Block

The IPv4 network range for the mesh (e.g. `10.30.1.0/24`). Node addresses, EUD DHCP ranges, and service addresses are all allocated from this block.

### 7. Max EUDs per Node

The maximum number of end-user devices each node will serve. This controls DHCP pool sizing.

### 8. Regulatory Domain

Your country code for Wi-Fi regulatory compliance (e.g. `US`, `GB`, `AU`).

### 9. Auto Channel Selection

If enabled, nodes negotiate channel selection automatically. If disabled, a fixed channel is used.

### 10. Passwords

- **Radio user password** — SSH/login password for the `radio` account on the node.
- **Admin password** — Used for web interface access where applicable.

---

## FIRST BOOT

Insert the storage media, connect Ethernet, and power on the node. What happens next depends on the hardware:

### Raspberry Pi (all models including CM4)

The `firstrun.sh` script runs on the very first boot (injected by `rpi-imager`). It:

1. Disables the default Raspberry Pi setup wizard
2. Creates the `radio` user account
3. Enables SSH
4. Creates and enables a `mesh-provision` systemd service for the next stage

After a reboot, `provision-mesh.sh` runs once network is available. It:

1. Waits for internet connectivity (up to 5 minutes)
2. Sets the Wi-Fi regulatory domain
3. Calculates a unique hostname from the node's MAC address
4. Installs required packages (`batctl`, `wpa_supplicant`, etc.)
5. Configures network interfaces, mesh settings, and DHCP
6. Disables itself and reboots into the final mesh configuration

The full process takes a few minutes and involves two reboots.

### Rock 3A

The provisioning script and all configuration are embedded directly into the Armbian image during flashing. No `rpi-imager` first-run injection is involved.

On first boot, a `mesh-provision` systemd service (triggered by the presence of a `/root/.mesh-not-provisioned` flag file) runs `provision-mesh.sh`. The `radio` user account is pre-created during image preparation, so there is no interactive setup wizard to bypass.

The provisioning script:

1. Waits for internet connectivity
2. Installs required packages
3. Configures interfaces and mesh settings
4. Removes the trigger flag file and reboots

After the reboot the node is fully operational.

---

## DEFAULT CREDENTIALS

| Account | Username | Default Password |
|---------|----------|-----------------|
| SSH / radio user | `radio` | Set during provisioning |
| Armbian root (Rock 3A) | `root` | `1234` (Armbian default — change this) |

---

## TROUBLESHOOTING

**Provisioning logs (Raspberry Pi):**
- Phase 1 (firstrun): `/boot/firmware/firstrun.log`
- Phase 2 (mesh provisioning): `/boot/firmware/provision.log`

**Provisioning logs (Rock 3A):**
- `/var/log/mesh-provision.log`

**Node hasn't provisioned after 10 minutes:** Check that Ethernet is connected and has a working internet connection. The provisioning script waits up to 5 minutes for connectivity before timing out.
