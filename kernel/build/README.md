# Kernel and Morse builds

These are the kernel, Morse module and SBC overlay recipes used by the
package builders. Source recipes, configuration fragments and local patches
belong in Git. Downloaded trees, modules, firmware blobs and completed
overlays remain under the ignored `kernel-work/` directory.

CM4 is the verification target. Rock 3A and RPi5 have build recipes, but a
successful build does not establish hardware support for a new change.
The [kernel port record](../../docs/kernel-6.18-morse-port.md) explains the
driver changes, board differences and hardware failure modes.

## Host setup

Use a Linux build host with an ARM64 cross compiler. On Debian or Ubuntu:

```bash
sudo apt install build-essential bc bison flex git curl xz-utils kmod \
  libssl-dev libelf-dev device-tree-compiler \
  gcc-aarch64-linux-gnu g++-aarch64-linux-gnu
bash MANET/packaging/setup-dev-env.sh
source ~/.venvs/manet/bin/activate
```

Run commands from the repository root. The development environment supplies
`pyelftools` for BCF parsing. Allow several gigabytes for each kernel source
and build tree. `JOBS=2` limits compilation concurrency when memory is tight.
Preparation needs network access. Compilation uses the downloaded inputs.

## Fetch the exact inputs

```bash
python3 kernel/build/prepare-sources.py --board cm4
python3 kernel/build/prepare-sources.py --board cm4 --check
```

Use `--board r3a`, `--board rpi5`, or `--board all` for the other targets.
[sources.json](sources.json) pins the kernel, Gateworks Morse driver and
Morse firmware revisions. Preparation checks out those exact commits and
applies the [Morse patch](../patches/morse-driver/0001-spi-and-power.patch).
It can resume an interrupted fetch. It refuses a checkout at a different
revision and never resets an existing tree. `--check` performs no downloads
or source changes. It checks revisions and the required Morse patch, not
whether a developer has made additional kernel edits.

The default layout is:

```text
kernel-work/
  real_work/linux-rpi-6.18/       CM4 and RPi5 source
  real_work/linux-6.18/           Rock 3A source
  real_work/morse-driver-1.16/    patched out-of-tree driver
  real_work/build-<board>/       compiler output and module staging
  morse-firmware/                pinned upstream firmware and BCFs
  packages/<board>-sbc-overlay/  assembled board input for packaging
```

Set `MANET_KERNEL_WORK` to an **absolute path** to use another workspace.
Export it before preparation, compilation and overlay assembly. The package
builders accept the resulting overlay through `SBC_OVERLAY_DIR`.

## Build a board

```bash
bash kernel/build/build-cm4.sh
ALL_OVERLAYS=1 bash kernel/build/build-cm4-sbc-overlay.sh
bash MANET/packaging/build-cm4-tarball.sh /tmp/manet/cm4-install.tar.gz
```

For Rock 3A or RPi5, replace `cm4` with `r3a` or `rpi5`. The RPi5 install
builder requires an explicit overlay:

```bash
SBC_OVERLAY_DIR="$PWD/kernel-work/packages/rpi5-sbc-overlay" \
  bash MANET/packaging/build-rpi5-tarball.sh /tmp/manet/rpi5-install.tar.gz
```

The kernel builders merge the platform defconfig with
[common](config-fragments/manet-common.config) and platform fragments, run
`olddefconfig`, then build the image, device trees and modules. They clean
the Morse build before rebuilding against the same kernel. Morse's build
does not reliably notice changed generated kernel configuration on its own.
Do not build multiple boards concurrently against the shared Morse source.

CM4 and Rock 3A apply the committed MT7916 timing/observer series as well as
the channel-power and background-CAC patches. The observer remains disabled
by default. Its recorded 2T2R reservation defect is not resolved by restoring
these build scripts. RPi5 applies the channel-power and background-CAC
patches but does not add the CM4/Rock 3A observer series itself. A shared Pi
tree previously built for CM4 retains those patches. Use a separate
workspace when comparing exact per-board patch sets.

The pinned Pi source already enables GPIO descriptor chip select. The
Morse patch supplies the SPI initialization, response/ACK handling and
power-policy changes described in the port record. The build also patches
the compiled `dot11ah.ko` rules, and overlay assembly patches BCF power
tables and their CRCs. These are separate layers of the existing radio
power policy. Neither a configured limit nor a successful build measures
the RF power delivered by a particular board.

Overlay assembly combines the kernel, DTBs, modules and Morse firmware,
removes development-machine module links and runs `depmod`. CM4's
`ALL_OVERLAYS=1` includes the full matching Pi overlay set and
`overlay_map.dtb`. Keep it enabled for field-update inputs. RPi5's current
overlay recipe assumes USB HaLow.

## Build controls and trial packages

| Variable | Purpose |
|---|---|
| `MANET_KERNEL_WORK` | Download and output workspace, default `kernel-work/`. |
| `BUILD` | Absolute per-board build directory, also used by overlay assembly. |
| `JOBS` | Compiler jobs, default the host CPU count. |
| `CROSS_COMPILE` | Compiler prefix, default `aarch64-linux-gnu-`. |
| `EXTRA_CONFIG` | Absolute path to a final kernel config fragment. |
| `LOCALVERSION` | Additional kernel release suffix. |
| `PATCHES` | Absolute patch-root override, normally `kernel/patches/`. |
| `OVERLAY` | Absolute output directory for the assembled SBC overlay. |
| `ALL_OVERLAYS` | Set to `1` for the full CM4 Pi overlay set. |

A field kernel update needs a new kernel release identifier and all external
modules rebuilt against it. For example, build CM4 with
`LOCALVERSION=-trial1` and keep the same `BUILD` for overlay assembly. Never
rename a module directory to simulate a new release.

```bash
MANET_KERNEL_BOARD=cm4 \
SBC_OVERLAY_DIR="$PWD/kernel-work/packages/cm4-sbc-overlay" \
  bash MANET/packaging/build-tools-tarball.sh /tmp/manet/cm4-tools.tar.gz
```

That command constructs a candidate. It does not install or qualify it.
The updater also requires the local CM4 trial profile and the activation,
watchdog, health-check and recovery conditions in the
[field update policy](../../docs/node-tools-internals.md#field-update-policy).
Rock 3A kernel activation remains refused. Its current overlay also lacks
the initramfs required by that field-update path. RPi5 updates remain
kernel-less. Do not extract an install archive onto a live radio to bypass
the updater.

## Check a build change

The source-preparation tests use temporary local repositories. The patch
tests use the downloaded kernel objects without modifying their source
trees. Prepare both kernels with `--board all` to exercise both targets.
An absent kernel skips that target's cases. The same `MANET_KERNEL_WORK`
override applies to these tests.

```bash
python3 -m unittest discover -s kernel/build
python3 -m unittest discover -s kernel/patches/mt76-ftm
python3 -m unittest discover -s kernel/patches/mt76-bgobs
```

For source changes, record the input revisions, resulting kernel release,
resolved `.config`, compiler version and patch series. Check module
`vermagic` and that both Morse modules are present in the assembled overlay.
Boot and exercise the changed behavior on the target before claiming
hardware validation. Report the board and limits of that evidence in the
PR. See [contributing](../../CONTRIBUTING.md) for the complete workflow.
