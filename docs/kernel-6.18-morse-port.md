# Kernel 6.18 + Morse HaLow driver port — complete change record

This documents every kernel and driver change made to bring the MANET nodes up on
Linux 6.18 with the Morse Micro HaLow driver (morse_driver 1.16.4), across all three
hardware targets. It is self-contained on purpose: the build workspace
(`kernel-work/`) is **not** in git, so this file is the durable record of what was
changed and why.

Companion documents:
- `kernel-work/real_work/PORTING-MORSE-DRIVER.md` — deep diagnostic guide for the
  CM4 SPI debugging (symptom → cause tables, wire-level analysis). Read it before
  re-porting to a newer kernel.
- `docs/armbian_6.18_morse_build.txt` — **historical**: the original Rock 3A
  approach (Armbian stock kernel + headers + container build + manual sed patches).
  Superseded by the Gateworks driver branch + `build-r3a.sh` described below.

**Status:** working on bench hardware as of 2026-06. CM4 nodes (`pi`, `pi2`) run
`6.18.33-manet` with the HaLow link carrying ~32.5 Mbps batman throughput.

---

## 1. Targets and component versions

| Target | SoC | Kernel tree | Kernel version | Morse bus | Module |
|--------|-----|-------------|----------------|-----------|--------|
| CM4 | BCM2711 | `linux-rpi-6.18` (RPi fork) | 6.18.33-manet | SPI (spi0) | mm6108, FGH100M-AA-MD (Molex) |
| RPi5 | BCM2712 | `linux-rpi-6.18` (RPi fork) | 6.18.33-manet | SPI | mm6108 |
| Rock 3A | RK3568 | `linux-6.18` (mainline) | 6.18.0-manet | USB (M.2 E-key) | MM8108B2, Gateworks GW16167 |

Driver: **morse_driver 1.16.4**, built from the **Gateworks fork**
(`https://github.com/Gateworks/morse_driver.git`, branch `1.16.4-gateworks`),
checked out at `kernel-work/real_work/morse-driver-1.16/`. Morse Micro's own
release only targets kernels ≤ 6.12.

There are three layers of change, summarized:

| Layer | What | Applies to |
|-------|------|-----------|
| Driver: kernel-API compat | Gateworks branch commits (6.13→6.18 API churn) | all targets |
| Driver: mm6108 SPI fixes | local uncommitted diff in `spi.c`/`firmware.c`/`bus.h` | CM4/RPi5 only |
| Kernel trees | `spi-bcm2835.c` CS handling; `mt7915` txpower patch | per-tree (see §4) |

Plus the DTS overlay (§5), config fragments (§6), and a post-build binary patch on
`dot11ah.ko` (§7).

---

## 2. Driver layer 1 — kernel 6.13→6.18 API compatibility (Gateworks branch)

The original Rock 3A port (Feb 2026) patched these by hand with sed (see the
historical txt guide). That approach is dead: the Gateworks branch
`1.16.4-gateworks` carries the same fixes as proper commits, authored by Tim
Harvey. On top of the 1.16.4 release the branch adds:

| Commit | Fix |
|--------|-----|
| `58be96b` | handle kernels without `IEEE80211_CHAN_IGNORE` (a MorseMicro out-of-tree kernel flag; vanilla/RPi kernels don't have it) |
| `0654bbc` | 6.15+ timer API (`from_timer`→`timer_container_of`, `del_timer_sync`→`timer_delete_sync`, hrtimer setup) — `compat.h`, `watchdog.c` |
| `e1e22f5` | 6.14+ `ieee80211_ops` signature changes (`radio_idx` / `link_id` params on `.config`, `.get_txpower`, `.set_frag_threshold`, `.set_rts_threshold`; `set_wiphy_params` in `wiphy.c`) |
| `d2d3045` | 6.15+ removed `crc7_be_byte` export — switched to `crc7_be` (`yaps-hw.c`) |
| `16f104f` | 6.18+ removed S1G channel flags — local copies in `dot11ah/s1g_ieee80211.h` |
| `7239876` | 6.16+ `ieee80211_is_s1g_short_beacon` signature / S1G beacon parsing changes — local helpers, also covers backports to 6.12.39 / 6.15.7 |
| `dec5bc2` | misc compile errors (`debug.h`, `firmware.h`, `pageset.c`) |

The API-change cheat sheet (6.12 → 6.18) from the original port still applies and
lives in `docs/armbian_6.18_morse_build.txt` ("Summary of API Changes").

**When updating the driver:** pull/rebase the Gateworks branch rather than
re-deriving these. Our local SPI work (§3) sits as an uncommitted diff on top of
`dec5bc2` — consider committing it to a local branch so a `git pull` doesn't eat it.

---

## 3. Driver layer 2 — mm6108 SPI fixes (CM4/RPi5)

These are the result of the June 2026 SPI debug sessions on the CM4. They live as
an **uncommitted working-tree diff** in `morse-driver-1.16/` touching `spi.c`,
`firmware.c`, `bus.h` (~90 insertions). None of them are kernel-version-specific
except 3.1 — the rest are mm6108 hardware-behavior fixes that older kernels' looser
SPI timing happened to mask. Full diagnostic narrative: `PORTING-MORSE-DRIVER.md`.

### 3.1 `morse_spi_initsequence`: `cs_off=1` instead of `SPI_CS_HIGH` (`spi.c`)

The SD power-on sequence (74+ clocks with CS deasserted) was implemented by
flipping `SPI_CS_HIGH` in the device mode around the transfer. On 6.1+ kernels
with GPIO-descriptor CS, `SPI_CS_HIGH` no longer drives the physical pin. Replaced
with the `spi_transfer.cs_off` flag, which works for both native and GPIO CS:

```c
memset(mspi->data, 0xFF, MM610X_BUF_SIZE);
mspi->t.cs_off = 1;
morse_spi_xfer(mspi, 18);
mspi->t.cs_off = 0;
```

### 3.2 2-bit MISO deshift in `morse_spi_find_response` (`spi.c`)

The mm6108 drives MISO with a consistent **2-bit shift** relative to the byte
boundary — an intrinsic hardware property, exposed by 6.18's tighter SPI timing.
R1 = 0x00 arrives on the wire as `0xC0 0x3F`. Detection: first non-idle byte has
bits[7:5] = `0b110` (`(*cp & 0xE0) == 0xC0`). Fix: in-place decode of the whole
buffer from R1 to the end, **before** the R1 value check:

```c
if ((*cp & 0xE0) == 0xC0) {
    u8 *p;
    for (p = cp; p < end - 1; p++)
        *p = (u8)((*p << 2) | (*(p + 1) >> 6));
    *p = (u8)(*p << 2);
}
```

Without this, CMD63 (`SD_IO_MORSE_INIT`, the vendor command that switches the chip
into SPI mode) looks like it gets no response at all, and probe fails.

### 3.3 Data-ACK scan fixes in `morse_spi_find_data_ack` (`spi.c`)

The in-place deshift creates two transition artifacts in the write-ACK search
window, and neither is a valid SPI data-response code:

- `0xFC` — decoded `0xFF→0x00` boundary byte (chip asserting busy)
- `0x03` — decoded `0x00→0xC1` boundary byte (one byte before the ACK token)

The skip loop now passes over idle, busy, and both artifacts:

```c
while (cp < end && (*cp == 0xff || *cp == 0x00 || *cp == 0xfc || *cp == 0x03))
    cp++;
```

Plus a conditional single-byte deshift for the case where `find_response` did
*not* deshift (R1 came back clean but the later ACK token is shifted — appears as
`0xC1` instead of `0x05`). A successful ACK after the full-buffer deshift appears
as `0xE5` (`0xE5 & 0x1F = 0x05 = SPI_RESPONSE_ACCEPTED`) — that is success, not an
error.

### 3.4 CMD53 write ACK timing + byte-mode tolerance (`spi.c`, `morse_spi_cmd53_write`)

Three related changes:

- **Byte-mode writes get the full inter-block delay.** The ACK-window padding was
  `block ? inter_block_delay_bytes : 4`; the chip's write-ACK token does not
  reliably appear within 4 bytes. Now always `inter_block_delay_bytes`.
- **ACK search starts at the beginning of each block's transmission** (the old
  code skipped `TOKEN + 512 + CRC` ahead as an optimization). The mm6108 can ACK
  *early*, before data+CRC has fully clocked out; skipping ahead missed those ACKs.
- **Byte-mode writes tolerate a missing data-ACK.** Some register writes (e.g.
  `CLK_CTRL`, MSI) trigger a chip-side reset, so no ACK ever comes; the data was
  nevertheless transmitted. Block-mode writes still hard-fail without an ACK.

### 3.5 Address-window cache invalidation (`spi.c`)

`morse_spi_mem_read`/`morse_spi_mem_write` error exits now call
`morse_spi_reset_base_address(mspi)`. After a chip-side reset, the chip's CMD52
address-window registers are cleared but the driver's cache still claimed they
were set, so retries read/wrote through a stale window.

### 3.6 Skip `digital_reset` on first firmware load (`firmware.c`)

`morse_firmware_init_preloaded` now sets `mors->chip_was_reset = true` before the
retry loop. After probe's CMD63 the chip is in a known-good SPI state; the old
behavior reset the RISC-V CPU immediately and the SPI peripheral sometimes ended
up in a subtly different state that broke CMD53 multi-block writes. First attempt
now uses the probe state directly; the retry path still does `digital_reset`.

Note: on real mm6108 hardware, `digital_reset` (write 0xDEAD to RESET) only resets
the RISC-V CPU, **not** the SPI peripheral.

### 3.7 `spi_reinit` infrastructure (`bus.h`, `spi.c`) — present but deliberately unused

A `spi_reinit` bus-ops callback (`morse_spi_reinit`: CMD63 retry loop with CMD0
fallback) was added during debugging on the theory that `digital_reset` knocked
the chip back to native SDIO mode. **That theory was wrong** — and calling CMD63
on a chip already in SPI mode breaks CMD53 entirely. The infrastructure stays in
the tree for edge cases but must **not** be called in the normal
`digital_reset → firmware load` path.

Likewise: do **not** send CMD0 before CMD63 in probe. CMD0 puts the chip in SD SPI
idle state, after which CMD63 fails with R1=0x04 (function unsupported). The probe
loop tries CMD63 first and only issues CMD0 as a retry fallback.

---

## 4. Kernel tree patches

Both trees also carry untracked junk; the *real* diffs are exactly these.

### 4.1 `linux-rpi-6.18`: `drivers/spi/spi-bcm2835.c` — GPIO-descriptor CS (CM4/RPi5)

Two hunks, working in tandem with the DTS overlay (§5):

```c
/* in bcm2835_spi_probe(): */
/* Use GPIO descriptor CS so the SPI framework toggles GPIO8 HIGH
 * between messages.  This gives the mm6108 the CS deassert pulse
 * it requires after CMD53 byte-mode writes. */
ctlr->use_gpio_descriptors = true;
```

`use_gpio_descriptors = true` is the upstream default in 6.18 — we had disabled it
during an earlier (wrong) debugging approach; the patch pins it back on with a
comment so it can't silently regress. The second hunk is a comment-only
clarification on the existing empty-`cs-gpios` early return in
`bcm2835_spi_setup()` (a leftover guard from the old approach; harmless and never
taken now that `cs-gpios` is non-empty).

**Why this matters (the big one):** the mm6108 uses SDIO byte-mode CMD53 for small
register writes. After a byte-mode write the chip's SPI state machine needs a
**CS deassert pulse** to return to idle (block-mode writes don't, because the data
response token ends the transaction). With CS held permanently low, the chip
interprets the next command as continuation data and every subsequent CMD52
returns `-ENODATA`. Signature in dmesg: firmware loads, then `dm_write failed -95`.

### 4.2 Both trees: `drivers/net/wireless/mediatek/mt76/mt7915/init.c` — txpower cap removal

Applied identically to `linux-rpi-6.18` and `linux-6.18`. The MT7916 (2.4 GHz mesh
radio, `mt7915e` driver) clamps each channel's `max_power` to the EEPROM target
power; our M.2 cards' EEPROM values are conservative. The patch lets regulatory be
the only cap:

```diff
-		chan->max_power = min_t(int, chan->max_reg_power,
-					target_power);
+		chan->max_power = chan->max_reg_power; /* mt7916 txpower patch */
 		chan->orig_mpwr = target_power;
```

(Actual TX power is then enforced at runtime by `manet-txpower.service`.)

**When porting:** both patches are uncommitted working-tree diffs in the kernel
trees. Re-apply after any tree update; `git diff` in each tree is the source of
truth.

---

## 5. DTS overlay — `dts-overlays/cm4-morse-spi.dts` (CM4 only)

Compiled to `mm610x-spi.dtbo` by `build-cm4.sh` (`dtc -@`). Wires spi0 to the
mm6108: pins 9/10/11 as ALT0 (MISO/MOSI/SCLK), GPIO8 as **function 1 = OUTPUT** for
CS, `spi-max-frequency = 50000000`, plus control GPIOs (reset=17, power=3+7,
spi-irq=5, all active-high) and disabled `spidev@0/1`.

The critical line, paired with §4.1:

```dts
cs-gpios = <&gpio 8 1>;  /* 1 = GPIO_ACTIVE_LOW */
```

**Never set `cs-gpios = <>;` (empty).** That was the old broken approach: the
BCM2835 driver then sets CS=3 (hardware CE disabled) and GPIO8 sits permanently
low — always asserted — which triggers the byte-mode CMD53 lockup described in
§4.1. With a real GPIO descriptor, the SPI framework drives GPIO8 high between
messages and low during them, and `cs_off=1` (§3.1) still works for the power-on
clocks. The RPi base DT has always had `cs-gpios = <&gpio 8 1>` for spi0; the
overlay just must not override it.

Rock 3A needs no overlay (USB enumeration; MorseMicro VID `325b`).

---

## 6. Kernel config and build system

Config is assembled per target as: platform defconfig + `manet-common.config` +
platform fragment, merged with `merge_config.sh` then `olddefconfig`.
Fragments live in `kernel-work/real_work/config-fragments/`.

`manet-common.config` (all targets) — the non-obvious entries:

- `CONFIG_TRIM_UNUSED_KSYMS=n` and `CONFIG_DEBUG_INFO_BTF=n` — **required** for
  the out-of-tree Morse modules to link/load.
- `CONFIG_CFG80211_CERTIFICATION_ONUS=y`, `CONFIG_CFG80211_REQUIRE_SIGNED_REGDB=n`,
  `CONFIG_CFG80211_USE_KERNEL_REGDB_KEYS=n` — allows our unsigned custom
  `regulatory.db` (S1G/HaLow rules; deployed from `MANET/root/`).
- batman-adv `=m` with DAT/NC/MCAST/BLA; cfg80211/mac80211/CRC7 `=m` (morse deps);
  `CONFIG_MT7915E=m` (MT7916 card); ebtables; SPI core `=y`; cpufreq + thermal
  governors; `CONFIG_LOCALVERSION="-manet"`, `LOCALVERSION_AUTO=n`.

Platform fragments: `manet-rpi.config` (bcm2835 thermal, RPi firmware/cpufreq,
`CONFIG_MFD_RP1=y` — needed for RPi5, harmless on CM4) and `manet-r3a.config`
(Rockchip thermal/cpufreq/PM domains).

Build scripts (`kernel-work/real_work/build-{cm4,rpi5,r3a}.sh`) each: configure,
build `Image[.gz] modules dtbs`, `modules_install` into `build-*/staging/`, build
the Morse driver out-of-tree against the build dir, stage `morse.ko` +
`dot11ah/dot11ah.ko` into `extra/morse/`, apply the power patch (§7), and
xz-compress all modules. Per-target notes:

- **CM4** (`bcm2711_defconfig`): also compiles the DTS overlay (§5).
  Driver flags: `CONFIG_MORSE_SPI=y CONFIG_MORSE_USB=y CONFIG_MORSE_VENDOR_COMMAND=y
  CONFIG_MORSE_USER_ACCESS=y`. **Both buses are built in** — CM4 boards ship
  either with the SPI HaLow hat (mm6108) or with a USB MM8108 card, and one
  module serves both. `CONFIG_MORSE_SPI` and `CONFIG_MORSE_USB` are independent
  `#ifdef` blocks in `init.c`/`morse.h` and coexist fine; the unused bus driver
  just registers and never probes. See §6.1.
- **RPi5** (`bcm2712_defconfig`): same tree and flags as CM4 (currently without
  `CONFIG_MORSE_VENDOR_COMMAND` — add it if vendor commands are needed there).
  Known latent bug (rpi5 work is deferred, unfixed as of 2026-06): `build-rpi5.sh`
  references `$KWORK` for the power patch (§7) but never defines it — the script
  will abort at that step. Copy the `KWORK=` line from `build-cm4.sh` when rpi5
  work resumes.
- **Rock 3A**: mainline tree; **`rockchip_defconfig` no longer exists in 6.18** —
  starts from arm64 `defconfig`. Driver flag `CONFIG_MORSE_USB=y` instead of SPI.
  This replaces the entire old Armbian headers/podman/GCC-13 workflow: the kernel
  is now built from source, so the driver compiles in the same environment with
  the same toolchain and `KBUILD_MODPOST_WARN` hacks are unnecessary.

All targets cross-compile with `aarch64-linux-gnu-`. All Morse builds pass
`KERNEL_SRC="$BUILD"` and `MORSE_TRACE_PATH="$(pwd)"`.

### 6.1 USB HaLow cards on CM4 (added 2026-08-11)

CM4 boards without the SPI hat use a USB **MM8108** card (`lsusb`:
`325b:8100 Morse Micro MM81xx Wi-Fi HaLow 802.11ah Transceiver`). The kernel
side needs nothing special — `bcm2711_defconfig` already has `CONFIG_USB=y` +
xHCI, and the card enumerates on the CM4's `fe9c0000.xhci` bus. What was missing
was purely a **driver build flag**: `build-cm4.sh` passed only
`CONFIG_MORSE_SPI=y`, so `usb.o` was never compiled, `morse.ko` carried no
`usb:v325Bp8100*` alias, and the USB device sat with `Driver=[none]` while the
only visible dmesg line was the (unrelated, harmless) SPI probe failure:

```
morse_spi spi0.0: morse_spi_probe: failed to init SPI with CMD63 (ret:-61)
morse_spi_probe failed. The driver has not been loaded!
```

That message appears on **any** CM4 whose config.txt loads `mm610x-spi.dtbo`
with no hat attached — it is not evidence of a USB problem. Quick check for
whether the module has USB support at all:

```bash
modinfo morse | grep alias    # want: usb:v325Bp8100d*dc*dsc*dp*ic*isc*ip*in*
```

With `CONFIG_MORSE_USB=y` the driver binds as `morse_usb`, auto-selects
`morse/mm8108b2-rl.bin` + a BCF from the chip's board type (bench card:
`bcf_boardtype_0807.bin`), and creates the HaLow netdev. `FW manifest pointer
not set (ret:-5)` right after firmware load is benign on this card — the mesh
comes up normally afterwards.

Provisioning: `radio-setup.sh` skips the SPI overlay / GPIO 3+7+17 config.txt
entries when `has_usb_morse_device` sees a USB card (it was already USB-aware
for the BCF/spi_clock modparams). An already-provisioned board keeps whatever
config.txt it has; drop `dtoverlay=mm610x-spi.dtbo`, `dtparam=spi=on` and the
three `gpio=` lines by hand to silence the phantom SPI probe.

**Module.symvers gotcha:** the Morse make consumes the kernel build dir's
`Module.symvers`. After a `make clean` in the kernel tree, rebuild kernel modules
first, then re-copy `Module.symvers` into the driver dir — otherwise you deploy a
.ko built against stale symbols (classic symptom: dmesg shows old debug prints
after deploying a "new" module).

**modules.dep gotcha:** `modules_install` runs before the Morse modules are added
to `extra/morse/`, so the generated `modules.dep` does not know them. Run
`depmod -a` on the target after install (or `sudo depmod -b <rootfs> <kver>` from
the dev machine).

---

## 7. dot11ah.ko regulatory power patch (post-build, all targets)

`kernel-work/morse-dot11ah-power-patch.py` rewrites `max_eirp` in **every**
HaLow regulatory rule baked into `dot11ah.ko`, run by all three build scripts with
`--power-dbm 30`. It scans the binary for plausible `ieee80211_reg_rule` structs
(start_freq 700–960 MHz) instead of using hardcoded offsets, so it survives
kernel/driver rebuilds. The driver's built-in rules are otherwise conservative and
would cap TX power below what the hardware/region allows. (`--min-only` raises
only rules below the target.)

---

## 8. Build → package → deploy chain

1. `build-{cm4,r3a,rpi5}.sh` → kernel + modules + Morse driver in `build-*/staging/`.
2. `build-{cm4,r3a}-sbc-overlay.sh` → `kernel-work/packages/{cm4,r3a}-sbc-overlay/`
   (boot content, modules, **Morse firmware blobs** from `kernel-work/morse-firmware/`).
3. `MANET/packaging/build-{cm4,r3a,rpi5}-tarball.sh` → install tarballs in
   `MANET/install_packages/` (node tools + SBC overlay).

Firmware blobs: CM4/RPi5 use mm6108 firmware + `bcf_fgh100mhaamd.bin` (Molex
FGH100M-AA-MD, US frequency plan); Rock 3A uses `mm8108*.bin` + `bcf_mf15457.bin`
(Gateworks GW16167). The driver logs the BCF filename it wants in dmesg if the
right one is missing.

Never extract a kernel tarball onto a live CM4's mounted FAT32 boot partition —
power down and extract on the dev machine (sequence in the top-level CLAUDE.md).

Success check after boot:

```bash
dmesg | grep -E 'morse|wlan2'
# expect: Morse Micro Dot11ah driver registration ... → ~4 s pause (chip ID +
# SPI setup — normal) → Loaded firmware → batman_adv: bat0: Adding interface: wlan2
```

If it breaks, work through the symptom table in `PORTING-MORSE-DRIVER.md`
("Diagnostic guide: what to check if it breaks again").

---

## 9. Known runtime hardware gotcha (context for driver work)

The mm6108 firmware can wedge if a mesh plink teardown sends
`morse_cmd_disable_key` at the wrong moment: ENODEV (-19) errors, empty scans, and
**only a full power cycle recovers it** — module reload doesn't (CMD63 fails
without a hardware power cycle). Runtime mitigations (`mesh_plink_timeout=0`
enforced by batman-enslave-watch, `group_rekey=0`) prevent the trigger; a driver
hardening fix (tolerate disable_key ENODEV without wedging) remains on the
wishlist. Details in `handoff` and the top-level CLAUDE.md.
