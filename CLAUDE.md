# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

MANET radios — transforms SBCs (CM4, RPi5, Rock 3A) into self-forming mesh nodes using **batman-adv** (Layer 2) and **802.11ah HaLow** (Morse Micro mm610x chipset). Key mesh features: zero-conf IPv4/IPv6, batman-adv BATMAN V, auto-channel selection, tourguide partition healing, decentralized service elections (MediaMTX, NTP).

## Repository layout

```
MANET/
  node_tools/        # Runtime scripts deployed to /usr/local/bin/
  systemd/           # Service units → /etc/systemd/system/
  systemd-network/   # networkd units → /etc/systemd/network/
  networkd-dispatcher/  # carrier/off hooks
  udev/rules.d/      # udev rules → /etc/udev/rules.d/
  etc/               # Small configs (sudoers.d, avahi, sysctl fragments)
  share/manet/       # Shared assets (Avahi templates etc.)
  root/              # regulatory.db and networkd-dispatcher scripts
  binaries_arm64/    # Pre-built: alfred, batctl, wpa_supplicant_s1g, wpa_cli_s1g
  provisioning/      # firstrun.sh.template, linux.sh flasher, rock3a-provision.sh.template
  packaging/         # build-rpi5-tarball.sh (modern packaging script)
  install_packages/  # Output tarballs: rpi5-install.tar.gz, r3a-install.tar.gz, etc.
kernel-work/         # NOT in git — kernel build infrastructure
  real_work/         # Active build scripts (build-cm4.sh, build-r3a.sh, build-rpi5.sh, package-all.sh)
  morse-kernel-staging-cm4/  # OLD manual CM4 staging dir (stale)
  morse-kernel-staging-rpi5/ # OLD RPi5 staging dir (stale)
  packages/          # Build output: cm4-kernel.tar.gz, rpi5-kernel.tar.gz, r3a-kernel.tar.gz
stage-node-tools.sh  # STALE — targets old staging dirs, do not use
```

## Supported hardware

| Target | SoC | OS | Kernel tree | HaLow bus |
|--------|-----|----|-------------|-----------|
| CM4 | BCM2711 | RPi OS Trixie | `linux-rpi-6.18` (RPi fork) | SPI |
| RPi5 | BCM2712 | RPi OS Trixie | `linux-rpi-6.18` (RPi fork) | SPI |
| Rock 3A | RK3568 | Armbian Trixie | `linux-6.18` (mainline) | USB |

Trixie = Debian 13, fully merged-usr (`/lib` → `usr/lib` symlink). Modules go to `/usr/lib/modules/`.

## Packaging architecture (two-tarball model)

**RPi5 (modern / CI-driven):**
1. **SBC overlay** (`rpi5-sbc-overlay.tar.gz`) — kernel image, DTBs, modules, Morse firmware blobs. Released as GitHub release tag `rpi5-sbc-overlay-current`. Built separately from `kernel-work/real_work/build-rpi5.sh`.
2. **Install tarball** (`rpi5-install.tar.gz`) — universal node tools + SBC overlay merged. Built by `MANET/packaging/build-rpi5-tarball.sh`. CI builds and releases on every push via `.github/workflows/`.

**CM4 (transitional / not yet CI-driven):**
- `kernel-work/real_work/build-cm4.sh` — builds kernel + Morse driver into `build-cm4/staging/`
- `kernel-work/real_work/package-all.sh` — assembles `cm4-kernel.tar.gz` (kernel + modules + tools). The `etc/`, `usr/local/`, `root/` staging content is manually staged by Mike after each build — it is NOT auto-populated.
- **No CM4 equivalent of `build-rpi5-tarball.sh` yet** — this is a known gap.
- Morse firmware blobs (`usr/lib/firmware/morse/`) are in staging but **not packaged** by `package-all.sh` — gap that needs fixing.
- `firstrun.sh` CM4 branch downloads from `https://www.colorado-governor.com/manet/cm4-install.tar.gz` — this is the intended distribution point; Mike manually uploads fresh tarballs there.

**Rock 3A:**
- Armbian base image + dpkg `.deb` kernel install (not tarball-based).
- `r3a-install.tar.gz` in `install_packages/` contains node tools only.

## Build commands (kernel-work/real_work/)

```bash
./build-cm4.sh      # BCM2711: builds kernel, modules, Morse SPI driver → build-cm4/staging/
./build-rpi5.sh     # BCM2712: builds kernel, modules, Morse SPI driver → build-rpi5/staging/
./build-r3a.sh      # RK3568: builds kernel, modules, Morse USB driver → build-r3a/staging/
./package-all.sh    # Assembles kernel-work/packages/{cm4,rpi5,r3a}-kernel.tar.gz
```

All cross-compiled with `aarch64-linux-gnu-`. Kernel config = platform defconfig + `config-fragments/manet-common.config` + platform fragment.

After a `build-cm4.sh` run, the staging dir is **wiped and rebuilt**. Any manually staged content in `build-cm4/staging/etc`, `usr/local/`, `root/` must be re-staged before running `package-all.sh`.

**Packaging the tools tarball (RPi5):**
```bash
SBC_OVERLAY_DIR=/path/to/extracted/overlay \
  bash MANET/packaging/build-rpi5-tarball.sh rpi5-install.tar.gz
```

## Provisioning flow

1. Flash OS image via `MANET/provisioning/linux.sh` (uses rpi-imager or dd). For Rock 3A: `rock3a-provision.sh.template`.
2. `firstrun.sh` runs on first boot: creates `radio` user, installs SSH, writes `provision-mesh.sh`, enables `mesh-provision.service`.
3. `mesh-provision.service` runs `provision-mesh.sh` once network is up: `apt install` dependencies, downloads install tarball, extracts to `/`, installs Morse firmware from `/root/morse-firmware/`, sets up networkd/nftables/radvd config.
4. `radio-setup-run-once.service` runs `radio-setup.sh` at next boot: final radio and mesh interface bring-up.

Templates use `__PLACEHOLDER__` substitution (mesh key, SSID, regulatory domain, hardware model, etc.).

## Key runtime services (deployed to /etc/systemd/system/)

- `batman-enslave.service` / `batman-enslave-watch.service` — enslave wireless interface to batman-adv
- `node-manager.service` — core mesh orchestrator (static or ACS variant)
- `ethernet-autodetect.service` — EUD mode switching (wired/wireless/auto)
- `manet-txpower.service` — TX power enforcement
- `sae-watchdog.service` — monitors HaLow association health
- `mesh-default-route-fix.service` — repairs default route on mesh changes
- `ebtables-restore.service` — restores bridge filtering rules at boot
- `radio-setup-run-once.service` — one-shot first-boot radio configuration

## Kernel config fragments (kernel-work/real_work/config-fragments/)

- `manet-common.config` — batman-adv, cfg80211/mac80211, SPI, ebtables, thermal governors, `CONFIG_TRIM_UNUSED_KSYMS=n`, `CONFIG_DEBUG_INFO_BTF=n` (both required for out-of-tree Morse driver), `-manet` localversion
- `manet-rpi.config` — BCM2835/RPi thermal, RPi cpufreq, RP1 MFD (shared by CM4 + RPi5)
- `manet-r3a.config` — Rockchip thermal, cpufreq, PM domains

## Morse driver notes

Out-of-tree driver (`morse-driver-1.16/`), built per-platform against the kernel build dir:
- CM4/RPi5: `CONFIG_MORSE_SPI=y` — SPI DTS overlay (`dts-overlays/cm4-morse-spi.dts`) compiled to `mm610x-spi.dtbo`
- Rock 3A: `CONFIG_MORSE_USB=y`
- All: `CONFIG_WLAN_VENDOR_MORSE=m CONFIG_MORSE_VENDOR_COMMAND=y CONFIG_MORSE_USER_ACCESS=y`

Produces `morse.ko` + `dot11ah/dot11ah.ko`. The `modules.dep` generated at build time does NOT include the Morse extras (they live in `extra/morse/` added after `make modules_install`). Run `depmod -a` on target after install, or from dev machine: `sudo depmod -b /mnt/rootfs <kver>`.

## Installing a kernel tarball safely

Do NOT extract directly to a running CM4's mounted filesystems — FAT32 can corrupt mid-write. Instead:
1. Power down, mount eMMC on dev machine
2. Extract boot content: `sudo tar xzf cm4-kernel.tar.gz -C /media/mike/bootfs --strip-components=2 ./boot/firmware/`
3. Extract rootfs content: `sudo tar xzf cm4-kernel.tar.gz -C /media/mike/rootfs ./usr/lib/modules/ ./etc/ ./usr/local/ ./root/`
4. Run depmod: `sudo depmod -b /media/mike/rootfs 6.18.33-manet`
5. Copy Morse firmware blobs separately (missing from tarball): `sudo cp kernel-work/real_work/build-cm4/staging/usr/lib/firmware/morse/* /media/mike/rootfs/usr/lib/firmware/morse/`
