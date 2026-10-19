# Kernel 6.18 and Morse HaLow driver port

This records the kernel and driver changes for Linux 6.18 with the Morse Micro
HaLow driver (morse_driver 1.16.4) on all three hardware targets. The build
workspace (`kernel-work/`) contains downloaded inputs and build outputs.
The [build recipes](../kernel/build/README.md), configuration fragments, and
local patches are tracked in the repository.

Companion documents:
- [Kernel build workflow](../kernel/build/README.md): source pins, dependencies,
  commands, configuration, and overlay assembly. The SPI mechanisms and
  failure signatures below explain the changes to preserve when porting.
- `docs/armbian_6.18_morse_build.txt`: **historical**: the original Rock 3A
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

Changes fall into three groups:

| Layer | What | Applies to |
|-------|------|-----------|
| Driver: kernel-API compat | Gateworks branch commits (6.13→6.18 API churn) | all targets |
| Driver: mm6108 SPI fixes | tracked `kernel/patches/morse-driver/0001-spi-and-power.patch` | CM4/RPi5 only |
| Kernel trees | `spi-bcm2835.c` CS handling; `mt7915` txpower patch | per-tree (see §4) |
| Kernel trees | cfg80211 background CAC fix (upstream backport) | all targets (§4.5) |
| Kernel trees | MT7916 third-chain reservation and observer | CM4/Rock 3A (§4.6) |

Plus the DTS overlay (§5), config fragments (§6), and a post-build binary patch on
`dot11ah.ko` (§7).

---

## 2. Driver layer 1: kernel 6.13→6.18 API compatibility (Gateworks branch)

The original Rock 3A port (Feb 2026) patched these by hand with sed (see the
historical txt guide). The Gateworks branch `1.16.4-gateworks` now carries
these fixes as commits authored by Tim Harvey. On top of the 1.16.4 release
the branch adds:

| Commit | Fix |
|--------|-----|
| `58be96b` | handle kernels without `IEEE80211_CHAN_IGNORE` (a MorseMicro out-of-tree kernel flag; vanilla/RPi kernels don't have it) |
| `0654bbc` | 6.15+ timer API (`from_timer`→`timer_container_of`, `del_timer_sync`→`timer_delete_sync`, hrtimer setup): `compat.h`, `watchdog.c` |
| `e1e22f5` | 6.14+ `ieee80211_ops` signature changes (`radio_idx` / `link_id` params on `.config`, `.get_txpower`, `.set_frag_threshold`, `.set_rts_threshold`; `set_wiphy_params` in `wiphy.c`) |
| `d2d3045` | 6.15+ removed `crc7_be_byte` export: switched to `crc7_be` (`yaps-hw.c`) |
| `16f104f` | 6.18+ removed S1G channel flags: local copies in `dot11ah/s1g_ieee80211.h` |
| `7239876` | 6.16+ `ieee80211_is_s1g_short_beacon` signature / S1G beacon parsing changes: local helpers, also covers backports to 6.12.39 / 6.15.7 |
| `dec5bc2` | misc compile errors (`debug.h`, `firmware.h`, `pageset.c`) |

The API-change cheat sheet (6.12 → 6.18) from the original port still applies and
lives in `docs/armbian_6.18_morse_build.txt` ("Summary of API Changes").

**When updating the driver:** use the Gateworks branch for the API fixes.
The local SPI work (§3) is an uncommitted diff on top of `dec5bc2`; preserve
it on a local branch before pulling or rebasing.

---

## 3. Driver layer 2: mm6108 SPI fixes (CM4/RPi5)

These are the result of the June 2026 SPI debug sessions on the CM4. They live as
an **uncommitted working-tree diff** in `morse-driver-1.16/` touching `spi.c`,
`firmware.c`, `bus.h` (~90 insertions). None of them are kernel-version-specific
except 3.1: the rest are mm6108 hardware-behavior fixes that older kernels' looser
SPI timing happened to mask. The failure mechanisms and signatures follow.

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
boundary: an intrinsic hardware property, exposed by 6.18's tighter SPI timing.
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

- `0xFC`: decoded `0xFF→0x00` boundary byte (chip asserting busy)
- `0x03`: decoded `0x00→0xC1` boundary byte (one byte before the ACK token)

The skip loop now passes over idle, busy, and both artifacts:

```c
while (cp < end && (*cp == 0xff || *cp == 0x00 || *cp == 0xfc || *cp == 0x03))
    cp++;
```

Plus a conditional single-byte deshift for the case where `find_response` did
*not* deshift (R1 came back clean but the later ACK token is shifted: appears as
`0xC1` instead of `0x05`). A successful ACK after the full-buffer deshift appears
as `0xE5` (`0xE5 & 0x1F = 0x05 = SPI_RESPONSE_ACCEPTED`): that is success, not an
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

### 3.7 `spi_reinit` infrastructure (`bus.h`, `spi.c`): present but deliberately unused

A `spi_reinit` bus-ops callback (`morse_spi_reinit`: CMD63 retry loop with CMD0
fallback) was added during debugging on the theory that `digital_reset` knocked
the chip back to native SDIO mode. Testing ruled this out. Calling CMD63
on a chip already in SPI mode breaks CMD53 entirely. The infrastructure stays in
the tree for edge cases but must **not** be called in the normal
`digital_reset → firmware load` path.

Likewise: do **not** send CMD0 before CMD63 in probe. CMD0 puts the chip in SD SPI
idle state, after which CMD63 fails with R1=0x04 (function unsupported). The probe
loop tries CMD63 first and only issues CMD0 as a retry fallback.

---

## 4. Kernel tree patches

The tracked recipes in `kernel/build/` apply the project patch series.
The Morse source preparation step applies its separate driver patch.

### 4.1 GPIO-descriptor CS in `linux-rpi-6.18` (CM4/RPi5)

File: `drivers/spi/spi-bcm2835.c`.

The pinned kernel already enables GPIO descriptor chip select, working
with the DTS overlay (§5):

```c
/* in bcm2835_spi_probe(): */
/* Use GPIO descriptor CS so the SPI framework toggles GPIO8 HIGH
 * between messages.  This gives the mm6108 the CS deassert pulse
 * it requires after CMD53 byte-mode writes. */
ctlr->use_gpio_descriptors = true;
```

`use_gpio_descriptors = true` is the upstream default in the pinned 6.18
source. No functional patch is needed there. Preserve it when porting.
The existing empty-`cs-gpios` early return in `bcm2835_spi_setup()` is not
taken with this overlay because `cs-gpios` is non-empty.

The mm6108 uses SDIO byte-mode CMD53 for small
register writes. After a byte-mode write the chip's SPI state machine needs a
**CS deassert pulse** to return to idle (block-mode writes don't, because the data
response token ends the transaction). With CS held permanently low, the chip
interprets the next command as continuation data and every subsequent CMD52
returns `-ENODATA`. Signature in dmesg: firmware loads, then `dm_write failed -95`.

### 4.2 MT7916 txpower cap removal in both kernel trees

File: `drivers/net/wireless/mediatek/mt76/mt7915/init.c`.

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

At runtime `manet-mesh-power.service` requests 30 dBm on every mesh radio, so
regulatory (30 dBm in every country in MANET's `regulatory.db`) and the
card's own hardware are the only limits.

The change is recorded in
`kernel/patches/mt76-txpower/0001-mt7915-channel-power.patch`.
`build-r3a.sh` applies it with `git apply` after the PCIe patch and skips it
when the reverse check succeeds. CM4 already carries the identical edit;
`build-cm4.sh` still relies on that working-tree change and does not apply it
automatically. A clean CM4 tree needs this patch to retain the same power
policy. Re-apply the txpower patch and the separate SPI changes after a tree
update as needed.

---

### 4.3 MT7916 timing controls (CM4 and Rock 3A)

The [MT7916 patch series](../kernel/patches/mt76-ftm/README.md) adds opt-in
report capture, marked probe selection, and ACK spatial-extension controls.
The same patches apply to Raspberry Pi kernel commit
`95b85bebbedcaedfa7ca79116ed38b7376fba412` and mainline Rock 3A commit
`7d0a66e4bb9081d75c82ec4957c50034cb0ea449`. The helper selects by exact HEAD
and rejects other bases. `build-cm4.sh` and `build-r3a.sh` apply the series
idempotently before configuring/building the kernel; partial or conflicting
applications fail before timing source changes. The existing txpower and SPI
patches remain separate. No debugfs writes leaves timing selection and register
changes inactive. Both builds carry `MODULE_INFO(manet_timing, "1")` for the
MT7916 firmware selector, which defaults to patched WM when this tag is present
regardless of `positioning=`; this tag is not a hardware validation result.
The Rock 3A fragment enables `CONFIG_FTRACE=y` and
`CONFIG_ENABLE_DEFAULT_TRACERS=y` for the same trace event format as CM4.
Install the rebuilt Image and complete matching module tree together;
the unchanged `6.18.0-manet` release string does not imply module compatibility
with the earlier kernel that lacked tracing. Hardware validation is pending.

### 4.4 Rock 3A PCIe slot power delay

`kernel/patches/r3a-pcie/0001-rock-3a-pcie-startup-delay.patch` adds
`startup-delay-us = <50000>` to `vcc3v3_pcie` in `rk3568-rock-3a.dts`.
`pcie-dw-rockchip.c` enables that rail and initializes the PCIe3 PHY at once,
and `phy-rockchip-snps-pcie3.c` gives `SRAM_INIT_DONE` only 500 us. Without
the delay the poll intermittently times out (`lock failed 0x..., check
input refclk and power supply` in dmesg) and the M.2 Wi-Fi card on the PCIe3
slot never appears. The DT is the same in 6.6 through 6.19, so the failure
is not tied to a kernel version. `build-r3a.sh` applies the patch with
`git apply` and skips it when it is already applied.

A board running an older DTB can get the same fix from an Armbian user
overlay: compile a fragment targeting `/regulator-vcc3v3-pcie` with
`dtc -@` into `/boot/overlay-user/pcie-delay.dtbo` and add
`user_overlays=pcie-delay` to `/boot/armbianEnv.txt`.

### 4.5 cfg80211 background CAC fix (all trees)

`kernel/patches/cfg80211-bgcac/` holds two upstream commits by Janusz
Dziedzic that 6.18 stable does not carry: 92fecd2 "wifi: cfg80211: fix
background CAC" and 68b908b "wifi: cfg80211: events, report background
radar". In stock 6.18, stopping a background CAC clears the owning interface
before the abort is handled. No abort event is sent, and the pending
completion survives, so a replacement CAC on another channel can be reported
finished at the old deadline, before its own 60 seconds. 92fecd2 fixes the
lifecycle. 68b908b is required with it, because 92fecd2 alone releases the
owner before the FINISHED event and that event then loses its interface index.

0002 depends on 0001. `build-r3a.sh` and `build-cm4.sh` apply the pair with
`git apply` unless 0002 already reverse-applies, and fail if only 0001 is
present. Both apply cleanly to mainline 6.18 and to `linux-rpi-6.18` at
6.18.33. RPi5 builds from the same tree. On a Rock 3A with the dedicated
MT7916 receiver, the abort is reported and a replacement CAC runs its full
time (61.8 s measured).

### 4.6 MT7916 third-chain observer (CM4 and Rock 3A)

[`kernel/patches/mt76-bgobs/`](../kernel/patches/mt76-bgobs/README.md) carries
an opt-in 2T2R reservation and the production cold-start observer. Both board
builders apply the txpower patch, then invoke:

```sh
python3 "$KWORK/../kernel/patches/mt76-bgobs/apply.py" "$KDIR"
```

This replaces the standalone timing hook. The helper applies or verifies the
complete timing prerequisite plus observer series transactionally on either
pinned kernel HEAD. It preserves other local patches and refuses partial or
conflicting series. The cfg80211 CAC fixes remain a separate build step.

The EEPROM guard accepts the cards' selectable 5/6 GHz byte `0xdb` only when
operating as 5 GHz. Reservation changes the RAM image and host chain masks;
it never writes efuse. The main band loses its third receive chain. The
read-only module parameter defaults off; the node firmware/config service
prepares it from `mt7916_background_rx` before coldplug.

The reservation initially prevented the main 5 GHz radio from peering on
two Rock 3A nodes. Patch 0003 now parses band 1 before the early firmware
upload, so a selectable card uploads `0x52` rather than `0xd2`, and aligns
the legacy OFDM TX-path count with two-path operation. Host tests capture the
actual uploaded image and reject the old initialization order. A three-node
5180 MHz HT20 bench with the observer active passed on both Rock 3A boards:
both peers stay established and traffic flows in both directions, with
receive throughput reduced by up to a third by the reserved chain. The
reservation stays off by default until runtime chain policy exists. Host mask `0x0c`
encodes band-local RX `0x3`, not global RF pin numbers.

Production observation uses the validated frontend/detector configuration
with interrupts masked, pulse writer disabled, and no CAC buffer dependency.
It exposes a root-only, versioned debugfs JSON interface. The kernel preserves
raw bins and matched main-band survey TX counters; userspace corrects own-TX
blanking and rejects uncertain samples. No diagnostic probe modes ship.
The series README specifies commands, fields, locking, and restoration.

The cold-start configuration has Rock 3A RF evidence on the pinned 2024 WM.
Both production modules compile offline and the C lifecycle tests run against
both trees. The reservation-v2 Rock 3A bench found that heavy local TX biases
the corrected estimate even on a far quiet channel. Userspace now omits
windows with at least 10% local TX, preserving the low-TX controls while
limiting amplification of accounting error. This provisional guard does not
qualify intermediate loads or the 250 ms production dwell. CM4 remains the
verification target; its cold boot, reservation,
RF response, cancellation, and traffic behavior have not yet been benched.

## 5. DTS overlay: `dts-overlays/cm4-morse-spi.dts` (CM4 only)

Compiled to `mm610x-spi.dtbo` by `build-cm4.sh` (`dtc -@`). Wires spi0 to the
mm6108: pins 9/10/11 as ALT0 (MISO/MOSI/SCLK), GPIO8 as **function 1 = OUTPUT** for
CS, `spi-max-frequency = 50000000`, plus control GPIOs (reset=17, power=3+7,
spi-irq=5, all active-high), and disabled `spidev@0/1`.

The critical line, paired with §4.1:

```dts
cs-gpios = <&gpio 8 1>;  /* 1 = GPIO_ACTIVE_LOW */
```

**Never set `cs-gpios = <>;` (empty).** That was the old broken approach: the
BCM2835 driver then sets CS=3 (hardware CE disabled) and GPIO8 sits permanently
low (asserted), triggering the byte-mode CMD53 lockup described in
§4.1. With a real GPIO descriptor, the SPI framework drives GPIO8 high between
messages and low during them, and `cs_off=1` (§3.1) still works for the power-on
clocks. The RPi base DT has always had `cs-gpios = <&gpio 8 1>` for spi0; the
overlay just must not override it.

Rock 3A needs no overlay (USB enumeration; MorseMicro VID `325b`).

---

## 6. Kernel config and build system

Config is assembled per target as: platform defconfig + `manet-common.config` +
platform fragment, merged with `merge_config.sh` then `olddefconfig`.
Fragments live in `kernel/build/config-fragments/`.

Relevant `manet-common.config` entries for all targets:

- `CONFIG_TRIM_UNUSED_KSYMS=n` and `CONFIG_DEBUG_INFO_BTF=n`: **required** for
  the out-of-tree Morse modules to link/load.
- `CONFIG_CFG80211_CERTIFICATION_ONUS=y`, `CONFIG_CFG80211_REQUIRE_SIGNED_REGDB=n`,
  `CONFIG_CFG80211_USE_KERNEL_REGDB_KEYS=n`: allows our unsigned custom
  `regulatory.db` (S1G/HaLow rules; deployed from `MANET/root/`).
- batman-adv `=m` with DAT/NC/MCAST/BLA; cfg80211/mac80211/CRC7 `=m` (morse deps);
  `CONFIG_MT7915E=m` (MT7916 card); ebtables; SPI core `=y`; cpufreq + thermal
  governors; `CONFIG_LOCALVERSION="-manet"`, `LOCALVERSION_AUTO=n`.

Platform fragments: `manet-rpi.config` (bcm2835 thermal, RPi firmware/cpufreq,
`CONFIG_MFD_RP1=y`: needed for RPi5, harmless on CM4) and `manet-r3a.config`
(Rockchip thermal/cpufreq/PM domains).

Build scripts (`kernel/build/build-{cm4,rpi5,r3a}.sh`) each: configure,
build `Image[.gz] modules dtbs`, `modules_install` into `build-*/staging/`, build
the Morse driver out-of-tree against the build dir, stage `morse.ko` +
`dot11ah/dot11ah.ko` into `extra/morse/`, apply the power patch (§7), and
xz-compress all modules. Per-target notes:

- **CM4** (`bcm2711_defconfig`): also compiles the DTS overlay (§5).
  Driver flags: `CONFIG_MORSE_SPI=y CONFIG_MORSE_USB=y CONFIG_MORSE_VENDOR_COMMAND=y
  CONFIG_MORSE_USER_ACCESS=y`. **Both buses are built in**: CM4 boards ship
  either with the SPI HaLow hat (mm6108) or with a USB MM8108 card, and one
  module serves both. `CONFIG_MORSE_SPI` and `CONFIG_MORSE_USB` are independent
  `#ifdef` blocks in `init.c`/`morse.h` and coexist fine; the unused bus driver
  just registers and never probes. See §6.1.
- **RPi5** (`bcm2712_defconfig`): same tree and flags as CM4 (currently without
  `CONFIG_MORSE_VENDOR_COMMAND`: add it if vendor commands are needed there).
  Known latent bug (rpi5 work is deferred, unfixed as of 2026-06): `build-rpi5.sh`
  references `$KWORK` for the power patch (§7) but never defines it: the script
  will abort at that step. Copy the `KWORK=` line from `build-cm4.sh` when rpi5
  work resumes.
- **Rock 3A**: the mainline tree uses arm64 `defconfig` because
  **`rockchip_defconfig` no longer exists in 6.18**.
  Driver flag `CONFIG_MORSE_USB=y` instead of SPI.
  Before configuration, applies the PCIe slot power patch (§4.4), the recorded
  txpower patch (§4.2), then timing and observer support via
  `python3 "$KWORK/../kernel/patches/mt76-bgobs/apply.py" "$KDIR"` (§4.6), then
  the cfg80211 background CAC pair (§4.5). `build-cm4.sh` applies txpower,
  the combined series, and the cfg80211 pair the same way.
  `manet-r3a.config` enables `CONFIG_FTRACE=y` (the tracing menu) and
  `CONFIG_ENABLE_DEFAULT_TRACERS=y` (static events without function tracing).
  Kconfig selects `TRACING`, `EVENT_TRACING`, `TRACEPOINTS`, `RING_BUFFER`, and
  `NOP_TRACER`, which provide tracefs instances, event enable/filter files,
  `trace`, `trace_pipe`, `set_event`, clocks, and per-CPU loss statistics.
  The fragment disables `UPROBE_EVENTS`, `EPROBE_EVENTS`, and
  `IWLWIFI_DEVICE_TRACING`, which otherwise default on with tracing but are
  unnecessary for MT7916 capture. `FUNCTION_TRACER`, `STACK_TRACER`,
  `SCHED_TRACER`, and `FTRACE_SYSCALLS` remain unset. Context-switch event
  support is selected by `EVENT_TRACING`, with recording opt-in.
  CM4's fragment and defconfig are unchanged.
  This replaces the entire old Armbian headers/podman/GCC-13 workflow: the kernel
  is now built from source, so the driver compiles in the same environment with
  the same toolchain and `KBUILD_MODPOST_WARN` hacks are unnecessary.

All targets cross-compile with `aarch64-linux-gnu-`. All Morse builds pass
`KERNEL_SRC="$BUILD"` and `MORSE_TRACE_PATH="$(pwd)"`.

Build and stage the Rock 3A overlay from the repository root:

```sh
bash kernel/build/build-r3a.sh
bash kernel/build/build-r3a-sbc-overlay.sh
```

The first script builds `6.18.0-manet` and installs modules under
`kernel-work/real_work/build-r3a/staging/lib/modules/`. The second copies them
into `kernel-work/packages/r3a-sbc-overlay/usr/lib/modules/`, with the kernel
Image, RK3568 DTBs, and Morse firmware, and regenerates module dependencies.
Check the staged `mt7915e.ko.xz` with `modinfo -F manet_timing` (expect `1`)
and `modinfo -F vermagic` (expect `6.18.0-manet SMP preempt mod_unload aarch64`).
Hardware validation of the Rock 3A timing build is still pending.
Also check `__tracepoint_mt7915_rx_tmr` and the `_ftrace_events` section in
the built `mt7915e.ko`. A module tag by itself does not prove tracing was built.

### 6.1 USB HaLow cards on CM4 (added 2026-08-11)

CM4 boards without the SPI hat use a USB **MM8108** card (`lsusb`:
`325b:8100 Morse Micro MM81xx Wi-Fi HaLow 802.11ah Transceiver`). The kernel
side needs nothing special: `bcm2711_defconfig` already has `CONFIG_USB=y` +
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
with no hat attached: it is not evidence of a USB problem. Quick check for
whether the module has USB support at all:

```bash
modinfo morse | grep alias    # want: usb:v325Bp8100d*dc*dsc*dp*ic*isc*ip*in*
```

With `CONFIG_MORSE_USB=y` the driver binds as `morse_usb`, auto-selects
`morse/mm8108b2-rl.bin` + a BCF from the chip's board type (bench card:
`bcf_boardtype_0807.bin`), and creates the HaLow netdev. `FW manifest pointer
not set (ret:-5)` right after firmware load is benign on this card: the mesh
comes up normally afterwards.

Provisioning: `radio-setup.sh` skips the SPI overlay / GPIO 3+7+17 config.txt
entries when `has_usb_morse_device` sees a USB card (it was already USB-aware
for the BCF/spi_clock modparams). An already-provisioned board keeps whatever
config.txt it has; drop `dtoverlay=mm610x-spi.dtbo`, `dtparam=spi=on`, and the
three `gpio=` lines by hand to silence the phantom SPI probe.

**Module.symvers gotcha:** the Morse make consumes the kernel build dir's
`Module.symvers`. After a `make clean` in the kernel tree, rebuild kernel modules
first, then re-copy `Module.symvers` into the driver dir: otherwise you deploy a
.ko built against stale symbols (classic symptom: dmesg shows old debug prints
after deploying a "new" module).

**modules.dep gotcha:** `modules_install` runs before the Morse modules are added
to `extra/morse/`, so the generated `modules.dep` does not know them. Run
`depmod -a` on the target after install (or `sudo depmod -b <rootfs> <kver>` from
the dev machine).

---

## 7. HaLow transmit power: no software ceilings

Three layers would otherwise cap HaLow below what the hardware can do. All
three are raised to 30 dBm for every region. What remains is the firmware's own
per-bandwidth limit and the hardware; nothing here measures radiated output.

**dot11ah.ko regulatory rules.** `kernel/build/morse-dot11ah-power-patch.py`
rewrites `max_eirp` in **every** HaLow regulatory rule baked into `dot11ah.ko`,
run by all three build scripts with `--power-dbm 30`. It scans the binary for
plausible `ieee80211_reg_rule` structs (start_freq 700-960 MHz) instead of
using hardcoded offsets, so it survives kernel/driver rebuilds. (`--min-only`
raises only rules below the target, and doubles as a check: on the deployed
6.18.33 CM4 module it finds 33 rules, none below 30.)

**BCF per-region power tables.** Each BCF carries a TX power TLV per channel
in each `.regdom_XX` section, and the firmware will not exceed it. Morse Micro
licenses the BCFs for distribution complete and unmodified, so the overlays and
every archive carry them as published and `stage-kernel-payload.py` refuses an
overlay whose BCFs are already patched. The node raises the limits itself:
`manet-morse-bcf.service` runs `manet-morse-bcf.py` at every boot, before udev
probes the radios, and sets every TX power TLV to 30 dBm in **every**
`bcf_*.bin` and regenerates the CRCs. It patches the directory the kernel reads
firmware from, which is the trial slot's `firmware_class.path` while a kernel
update is running and `/usr/lib/firmware/morse` otherwise. The patch is
idempotent, needs no `pyelftools`, and was checked byte for byte against the
build-time tool it replaced. Every BCF is covered because a USB MM8108
picks its BCF by OTP board type (`bcf_boardtype_XXXX.bin`) and SPI modules
name theirs in a module option. A region with no power TLVs is left as is;
`bcf_boardtype_0802`, `bcf_fgh100maamd`, and `bcf_mf08651_jp` have none at
all and are skipped. Regression this replaced: the CM4 overlay used to patch
only `bcf_fgh100mhaamd.bin` (US), so the USB card's stock
`bcf_boardtype_0807.bin` held the bench nodes at 21.25 dBm (908 MHz, 8 MHz).

**Driver clamp to the firmware maximum.** `morse_mac_set_txpower()` in
`morse-driver-1.16/mac.c` clamped every request to the firmware's reported
maximum (2475 mBm on the bench MM8108), and `CHAN5GHZ()` advertised a 22 dBm
`max_power` on the 5 GHz-mapped channels. The working-tree patch removes the
clamp (the firmware maximum is still queried and logged) and sets the channel
`max_power` to 30. The firmware then applies its own limit and reports the
result, which is what `iw` shows. On the 6.6 kernel this was a binary patch,
`kernel-work/morse-force-txpower.py`; it was not carried into the 6.18 port.
`git diff mac.c` in `morse-driver-1.16` is the source of truth.

**Channel table.** `dot11ah/s1g_channels_rules.c` gives each S1G channel its
own `max_reg_power`, which the driver requests at every channel set. US is
36 dBm, but other regions run as low as 4.77 dBm (EU 16). The working-tree
patch wraps the table macro in `MANET_S1G_POWER()`, raising anything below
30 dBm to 30.

**What remains is the firmware.** With all of the above in place the driver
requests 30 to 36 dBm and the MM8108 firmware answers with its own
per-bandwidth limit. These are firmware/driver reports, not RF measurements,
and they do not show whether a PA protection circuit or the firmware is the
limiting stage. Measured on CM4 with an MM8108 (`bcf_boardtype_0807`):
24.00 dBm at 2 MHz, 22.25 at 4 MHz, 22.00 at 8 MHz. The firmware
reports 24.75 dBm as its maximum. `iw dev wlan2 set txpower` does not reach
the firmware on this driver; the power is fixed at each channel set.
A likely cause (source reading, not yet instrumented): on 6.18, mac80211
delivers a user power change as `BSS_CHANGED_TXPOWER` to
`bss_info_changed`, which `morse_mac_ops_bss_info_changed()` does not handle;
the driver only handles `IEEE80211_CONF_CHANGE_POWER` in `.config`. The
runtime therefore treats HaLow power as fixed until reboot and the UI shows
it read-only. Handling the callback is a possible future driver fix.

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

Do not use manual tar extraction onto a live CM4's mounted FAT32 boot
partition. The offline procedure below is for bench work and physical
recovery: power down and mount the eMMC on the dev machine. Fielded radios
may be sealed, so normal kernel maintenance needs a validated in-place update
path. Kernel payloads are permitted in update archives; follow the
[field update policy](node-tools-internals.md#field-update-policy) when
implementing their activation and boot recovery.
First-boot provisioning is a separate, existing path: both templates extract
the verified install archive onto the live, newly imaged system, then reboot
into MANET. That initial setup is not an atomic boot-partition update and must
not be interrupted. It does not make live extraction safe for an established
node. For a kernel-only tarball onto offline mounted partitions:

```bash
# $BOOTFS = FAT boot partition, $ROOTFS = ext4 root, $KVER e.g. 6.18.33-manet
sudo tar xzf cm4-kernel.tar.gz -C "$BOOTFS" --strip-components=2 ./boot/firmware/
sudo tar xzf cm4-kernel.tar.gz -C "$ROOTFS" ./usr/lib/modules/ ./etc/ ./usr/local/ ./root/
sudo depmod -b "$ROOTFS" "$KVER"
```

`--strip-components=2` on boot is required: the archive stores firmware under
`./boot/firmware/`, and the mounted FAT volume *is* that firmware directory.
If the tarball has no `usr/lib/firmware/morse/`, copy those blobs from the
build staging dir or the SBC overlay onto `$ROOTFS/usr/lib/firmware/morse/`.

Success check after boot:

```bash
dmesg | grep -E 'morse|wlan2'
# expect: Morse Micro Dot11ah driver registration ... → ~4 s pause (chip ID +
# SPI setup: normal) → Loaded firmware → batman_adv: bat0: Adding interface: wlan2
```

For SPI failures, compare the observed response/ACK bytes and chip-select
behavior against sections 3 and 4.1. For a USB card, check the compiled bus
support and module alias described in section 6.1.

### 8.1 APT owns userland; MANET releases own the kernel

Only a matched kernel, DTBs, overlays, and modules from the board's MANET
release belong in its boot path, whether delivered by provisioning or a
validated field-update procedure. `/etc/apt/preferences.d/00-manet-kernel`
assigns priority -1 to distribution kernel images, headers, per-flavour base
packages, DTBs, Armbian U-Boot/BSP packages, and alternate boot writers.
`raspi-firmware` is also pinned: its own upgrade invokes the kernel-copy hook.
The helper `manet-kernel-protect.py` holds installed boot packages to prevent
automatic removal as well as upgrades. Headers stay removable for OS cleanup.
Pins also cover future versioned package names, which holds alone cannot do.

Pins do not stop already installed hooks. The helper sets `KERNEL=none` and
`INITRAMFS=none` in `/etc/default/raspi-firmware` on Pi boards, disables
`flash-kernel` with `/etc/flash-kernel/machine`, and uses local dpkg diversions
to replace `update-initramfs` and Rock 3A's `99-uboot` initramfs hook with
successful no-ops. Original package files remain at `.manet-distrib` paths.
This prevents userland triggers from regenerating a stock initramfs or
retargeting `/boot/uInitrd`. A chmod on one kernel postinst hook is insufficient.
The initramfs tools can still receive updates as userland dependencies.

Both templates apply protection before their first APT transaction. Install
and tools archives carry the policy and helper; tools updates apply protection
before dependency installation and reapply it during installation/recovery.
APT transactions already running must finish before the helper changes hooks.

`apt upgrade` and ordinary tool installs remain available. In particular,
`build-essential` needs `libc6-dev`, which needs `linux-libc-dev`; those are
userland headers, not `linux-headers-*`, and remain unpinned. `libssl-dev` does
not require a kernel image. EEPROM updates (`rpi-eeprom`) and device firmware
(`armbian-firmware`, `firmware-mediatek`) remain available because they do not
replace these boot kernel files. A package requiring a new stock kernel is
intentionally unsatisfiable; select a compatible userland package or update
through a MANET release instead of overriding the policy.

Kernel replacement covers provisioning, offline bench maintenance, and CM4
field trials. A matched CM4 tools payload starts a one-shot tryboot trial by
default, using a profile derived from that node's boot files and hardware.
Older CM4 images with `auto_initramfs=1` keep their original boot selector and
initramfs for fallback. The trial disables automatic initramfs loading only
in its candidate selector, then commits that setting with the new slot after
health confirmation. Custom initramfs and roots needing early userspace are
refused with an explanation. A new tools wrapper can deliver this fix to
v0.574 installations without reflashing or manually changing boot settings.
The firmware kernel watchdog, bounded userspace handoff, PID 1 watchdog, and
confirmation guard provide the benched fallback mechanism. Generic field
health checks have confirmed real CM4 kernel updates through the updater. Operators can
set `enabled=n` in `/etc/manet-kernel-update.conf` to keep tools-only updates.
Rock 3A kernel activation remains disabled. See the
[kernel activation protocol](node-tools-internals.md#kernel-payload-and-activation-protocol)
for boot-format restrictions, confirmation, and retained failed candidates.
Physical access or reflashing must not become the normal update requirement for sealed radios.
Leave the APT protections enabled when installing a MANET kernel. Kernel
security fixes require a new MANET kernel release as well as ordinary
userland security updates.

The package behavior is documented in the upstream
[Pi kernel-copy hook](https://github.com/RPi-Distro/firmware/blob/pios/trixie/debian/kernel/postinst.d/z50-raspi-firmware),
[firmware postinst](https://github.com/RPi-Distro/firmware/blob/pios/trixie/debian/raspi-firmware.postinst),
[Armbian initramfs hook](https://github.com/armbian/build/blob/main/packages/bsp/common/etc/initramfs/post-update.d/99-uboot),
and Debian's [APT preferences](https://manpages.debian.org/trixie/apt/apt_preferences.5.en.html)
and [flash-kernel](https://manpages.debian.org/trixie/flash-kernel/flash-kernel.8.en.html)
manuals. Verify installed package owners and hooks when changing base images.

### 8.2 Recovering a node overwritten by APT

Installing protection prevents another overwrite; it does not restore the
running kernel or boot files. If `uname -r` reports a stock `rpt-rpi` kernel
and the MANET radios disappeared, obtain the matching board and MANET
release's install archive and verify its release checksum. Preserve mesh
configuration. For a CM4, power down, expose the eMMC using rpiboot, and mount
its FAT boot and ext4 root partitions on the dev machine. Follow the offline
partition layout in section 8, using that install archive as the source:

```bash
# Inspect the archive first and select its exact module version, not uname -r.
tar tzf cm4-install.tar.gz | less
sudo tar xzf cm4-install.tar.gz -C "$BOOTFS" --strip-components=2 \
    ./boot/firmware/
sudo tar xzf cm4-install.tar.gz -C "$ROOTFS" \
    ./usr/lib/modules/ ./usr/lib/firmware/morse/
sudo depmod -b "$ROOTFS" "$KVER"
sync
```

Use the matching release's boot configuration; preserve any required local
boot settings when reviewing the restored files. Unmount both partitions
cleanly before powering on. RPi5 and Rock 3A likewise need their own matching
boot files, DTBs, modules, and firmware; Rock 3A uses `/boot/Image` and
`/boot/dtb/rockchip/` on its root filesystem. Do not apply Pi paths to Rock 3A.
Install the protection from the corrected release before another APT action.
After boot, verify `uname -r`, the matching `/usr/lib/modules` directory,
Morse/MT7916 driver loading, radio enumeration, and mesh peers. Do not purge
stock kernels as a recovery step: their removal hooks can also change boot
files.

### 8.3 Rock 3A tracing kernel replacement

This build keeps `6.18.0-manet` while adding kernel tracing. Replace the Image
and **all** modules together, including Morse; mixing modules from the two
configurations is not supported even when vermagic matches. Compare the board's
active DTB with the overlay and replace it if different. Preserve user overlays
and boot arguments. If the boot path loads an initramfs, rebuild it against the
new module tree. The overlay builder does not produce an initramfs.

Confirm the node's actual boot path before changing it. The expected paths are
`/boot/Image`, `/boot/dtb/rockchip/`, and `/usr/lib/modules/6.18.0-manet/`, with
`/boot` on the ext4 root filesystem. The local Armbian Rockchip boot-script
sources load `Image`, `uInitrd`, and `dtb/${fdtfile}` after importing
`armbianEnv.txt`. This does not prove the installed node uses that script:
inspect the loaded `boot.scr`, any `extlinux/extlinux.conf`, symlink targets,
`/proc/cmdline`, mounts, and the installed U-Boot configuration. The presence of
both files does not establish which one U-Boot selects.

Before a live replacement, save the entire boot directory, a detached copy of
the old Image and initramfs, and the complete old module tree. Stage and verify
the new files before replacing active paths. Use temporary files on the same
filesystem and rename them into place; this avoids partial individual files
but does not make the whole update atomic. With no serial console, arrange
tested recovery media or offline access to the root filesystem before reboot.
Do not reload modules from the new tree into the old running kernel.

Leave the APT protection diversions enabled. For an intentional update, use
`mkinitramfs` with the exact release and new module tree, and explicitly wrap
the result with `mkimage` if the verified boot script loads a U-Boot `uInitrd`.
Use the installed boot path's architecture, compression, and filenames. Calling
the diverted `update-initramfs` or `99-uboot` hook will not do this work.

An extlinux entry can name a saved Image and initramfs, but an old kernel also
needs its old root module tree. A second entry pointing at the newly replaced
root is not a complete fallback with this shared release string. Use a tested
separate recovery root, or restore the old module tree before selecting the
saved Image. A menu entry cannot recover a hung kernel by itself; unattended
recovery needs verified boot-count support, an alternate boot command, and a
way to reset the board. Do not assume those are enabled on an installed node.
See the U-Boot [extlinux documentation](https://docs.u-boot.org/en/latest/develop/bootstd/extlinux.html)
and [boot-count requirements](https://docs.u-boot.org/en/latest/api/bootcount.html).

After boot, verify the Image identity as well as `uname -r`, module loading,
Ethernet and mesh access, and `modinfo -F manet_timing mt7915e`. Mount tracefs
at `/sys/kernel/tracing` if needed and check
`events/mt7915/mt7915_rx_tmr/{enable,filter,format}`. Exercise a temporary
tracefs instance using the `nop` tracer and `mono` clock, then disable its
event and remove it. Seeing the event proves registration, not ranging or
stock-equivalent radio behavior; those still require hardware checks.

---

## 9. Known runtime hardware gotcha (context for driver work)

The mm6108 firmware can wedge if a mesh plink teardown sends
`morse_cmd_disable_key` at the wrong moment, causing ENODEV (-19) errors and
empty scans. Recovery requires a full power cycle; reloading the module leaves
CMD63 failing. Runtime mitigations (`mesh_plink_timeout=0`
enforced by batman-enslave-watch, `group_rekey=0`) prevent the trigger; a driver
hardening fix (tolerate disable_key ENODEV without wedging) remains on the
wishlist.
