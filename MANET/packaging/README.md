# Packaging

Build scripts and their support files are tracked in this directory. Start
with [contributing](../../CONTRIBUTING.md) for host setup and validation.
Two kinds of tarball are built from this repository, for three boards.

A kernel-less update needs the checkout, Bash, Python 3, curl and GNU tar,
findutils and coreutils. Tools and CM4 install builders fetch the pinned
stock MT7916 firmware
when absent and verify its SHA-256 against
`MANET/share/manet/mt7916-firmware.json`. Set `MANET_FIRMWARE_CACHE` to an
absolute directory containing those three verified files for offline use.
The default cache is under `kernel-work/cache/linux-firmware/<commit>/`.
Install archives also need a board overlay. Build one using the tracked
[kernel recipes](../../kernel/build/README.md).

The builders copy this working tree, including uncommitted runtime files.
Use an isolated checkout for a candidate whose source must be unambiguous.
They write only staging directories, caches and output files. They do not
install anything on a radio or publish to GitHub.

| Script | Output | Contents |
|--------|--------|----------|
| `build-cm4-tarball.sh` | `cm4-install.tar.gz` | node tools + CM4 SBC overlay |
| `build-r3a-tarball.sh` | `r3a-install.tar.gz` | node tools + Rock 3A SBC overlay |
| `build-rpi5-tarball.sh` | `rpi5-install.tar.gz` | node tools + RPi5 SBC overlay |
| `build-tools-tarball.sh` | `<board>-tools.tar.gz` | payload selected for an existing node's update |

**Install tarballs** bootstrap a new node: everything universal from this repo,
plus the board's kernel, DTBs, modules and Morse firmware from an SBC overlay.

**Tools tarballs** update an existing node: `node-update.sh` fetches, verifies,
stages, and installs one into the root filesystem. The archive can carry
**anything the release needs**, including kernels, DTBs, modules, firmware,
binaries, services, interface definitions, and configuration migrations. Sealed
field radios must be maintainable without routine opening and reflashing.
Follow the [field update policy](../../docs/node-tools-internals.md#field-update-policy)
when adding payloads that need activation or recovery handling.

The cm4, r3a and rpi5 tools archives share the common runtime and are named
per board because the updater requests its board's package. Board-specific
kernel and release inputs make their contents differ. The current builder carries the node scripts
and version file, systemd units together with their `multi-user.target.wants`
enable symlinks, udev rules, the networkd-dispatcher hooks, the Lyra plugin and
model weights, the dashboard assets under `share/manet`, and the journald
persistence drop-in. It also stages managed MT7916 firmware. A one-shot unit
that runs a migration is a legitimate payload for a conditional fix.

Kernel-less tools packages remain the default. Set `MANET_KERNEL_BOARD=cm4`
or `r3a` and optionally `SBC_OVERLAY_DIR` to package a matched set through
`stage-kernel-payload.py`. The default input is
`kernel-work/packages/<board>-sbc-overlay`. The helper creates the
`manet-kernel/` inventory and validates it with the runtime validator. Only
regular boot inputs, modules and Morse firmware are copied. Module build/source
links are omitted. CM4 gets the private-overlay README sentinel. Include every
configured overlay from the matching kernel build, not only the Morse overlay.
Use `ALL_OVERLAYS=1` with `kernel/build/build-cm4-sbc-overlay.sh` to
include the matching Pi overlays and `overlay_map.dtb`. A node rejects a
candidate missing overlays used by its boot configuration.

CM4 and Rock 3A install builders also record the normalized matched-set inventory
in `/usr/local/share/manet/installed-kernel.json`, alongside the existing root
SBC overlay. They do not duplicate the kernel set. All builders check compressed
size (256 MiB), expanded tar size (1536 MiB), and member count (20,000) before
writing the checksum. This does not qualify an archive for live activation.
The field updater requires a new unique kernel release. CM4 derives its trial
profile from local boot files and hardware and activates by default unless
`/etc/manet-kernel-update.conf` contains `enabled=n`. A local bench profile
can override the generic health check. Rock 3A activation is currently rejected. RPi5 keeps its
existing install payload and kernel-less update path.

Example local build for inspection:

```bash
MANET_KERNEL_BOARD=cm4 SBC_OVERLAY_DIR=/path/to/matched-cm4-overlay \
  bash MANET/packaging/build-tools-tarball.sh /tmp/cm4-tools.tar.gz
```

A CM4 tools archive can bootstrap stable v0.569 in one release. Set
`MANET_LEGACY_BOOTSTRAP=1` together with `MANET_KERNEL_BOARD=cm4` and the
matched overlay. The builder first creates the complete new-updater archive,
then wraps it under `usr/local/share/manet/bootstrap/` inside a tools-only
outer archive. The outer archive must fit v0.569's 64 MiB download and 512 MiB
expanded limits and its path allowlist. A failed compatibility check is a
build failure, not a package to publish.

The old updater installs the tools and starts a retry timer through its
existing firmware-setup call. After the outer transaction commits, the timer
runs the new updater on the embedded archive with its exact version and
checksum. CM4 trial confirmation removes the bootstrap payload and units.
An explicit kernel cancellation stops automatic retries of that manifest.
Other boards ignore a copied CM4 wrapper. Build their normal tools-only
archives without either kernel variable. The wrapper uses the same default-on
CM4 policy and derived profile as the new updater. Opt-out retains the embedded
payload so the timer can resume if enabled later. Neither archive carries a
radio-specific boot profile or operator policy file. The fallback mechanism
has been benched, while the generic field health policy needs on-node acceptance.

Release userland inputs use `MANET_HOSTAPD_DEB` and, for CM4,
`MANET_REGDB`. The former must be the native Debian ARM64
`hostapd_2.10-24+manet1_arm64.deb`, not a standalone cross-built binary.
The latter is the DFS-tags-only database. All builders validate package
identity and stage a digest inventory and APT pin. Only CM4 carries this
unsigned regulatory DB. Rock 3A enforces signatures and must retain its
existing signed database. Tools builds infer the board from their standard
output basename, or accept `MANET_PACKAGE_BOARD` explicitly.
The updater's firmware setup verifies and installs the package, checks the
installed follower marker and APT candidate, and copies the DB for next boot.
Missing requested inputs fail the build. No package is downloaded implicitly.

```bash
MANET_HOSTAPD_DEB=/path/to/hostapd_2.10-24+manet1_arm64.deb \
MANET_REGDB=/path/to/regulatory.db \
MANET_KERNEL_BOARD=cm4 MANET_LEGACY_BOOTSTRAP=1 \
SBC_OVERLAY_DIR=/path/to/matched-cm4-overlay \
  bash MANET/packaging/build-tools-tarball.sh /tmp/cm4-tools.tar.gz
```

A wrapper that exceeds the old limits requires a tools-only bridge release
first. v0.569 installs that bridge through its existing updater. The bridge's
new updater then installs a second release carrying the ordinary
`manet-kernel/` namespace. The release-selection protocol does not force a
node that skipped the bridge through it, so do not promote the second release
as a general v0.569 target without an enforced bootstrap path.

`node-manager.sh` is generated on the node from `acs=` rather than shipped.
Both real variants are carried, and the updater regenerates the selected
symlink after installing the payload, using the installed configuration.

The Wi-Fi mesh binaries `wpa_supplicant_manet` and `wpa_cli_manet` are carried
by every builder through `lib-supplicant.sh`. DFS is enabled by default, and
builders reject a missing or incompatible `v2.11-manet-dfs3` supplicant. The
binary must contain the paired 0007/0008 control ABI. Native Debian build
instructions are in the [binaries README](../binaries_arm64/README.md#native-debian-13-build).
`MANET_DFS_ENABLED=0` explicitly builds a non-DFS package, permits the Debian
fallback, and stages `usr/local/share/manet/dfs-enabled` as `0`. Runtime
capability also queries the running BSS, so a stale process cannot be enabled
by a newly copied executable. Other prebuilt binaries remain
absent from the current tools payload. HaLow candidate names are never staged.
`node-update.sh` verifies
and stages the archive, installs it, reloads systemd, and restarts mesh-status and
node-manager. It also prepares mesh inactivity settings, enforces 20 MHz in
both live and lobby Wi-Fi mesh configs, and restarts active Wi-Fi mesh
instances. This migrates existing radios without reflashing. HaLow widths
and client uplinks are unchanged. Other newly enabled units become active
at the next boot.
It does
not run `udevadm`.

Usage:

```bash
bash MANET/packaging/build-cm4-tarball.sh   MANET/install_packages/cm4-install.tar.gz
bash MANET/packaging/build-r3a-tarball.sh   MANET/install_packages/r3a-install.tar.gz
SBC_OVERLAY_DIR="$PWD/kernel-work/packages/rpi5-sbc-overlay" \
  bash MANET/packaging/build-rpi5-tarball.sh MANET/install_packages/rpi5-install.tar.gz
bash MANET/packaging/build-tools-tarball.sh MANET/install_packages/cm4-tools.tar.gz
bash MANET/packaging/build-tools-tarball.sh MANET/install_packages/r3a-tools.tar.gz
bash MANET/packaging/build-tools-tarball.sh MANET/install_packages/rpi5-tools.tar.gz
```

## Publishing

Build all six archives with matching version files and checksum sidecars, commit
and push the source, then use `MANET/releases/publish.py`. GitHub release assets
hold the packages; source pushes no longer announce updates. Normal installs and
updates follow the full release marked Latest. Iterations are prereleases and
`--development` explicitly selects the newest published build. The publisher
retains the newest three prereleases and preserves stable releases.

See [release tooling](../releases/README.md) for draft uploads, verification,
promotion and cleanup. First-boot provisioning and the tools updater both verify
checksums and embedded versions. Keep canonical outputs in
`MANET/install_packages/`; do not commit generated archives.

## Dev toolchain

`setup-dev-env.sh` builds the venv the repo's Python tooling needs. No root, and
nothing outside the venv is touched:

```bash
bash MANET/packaging/setup-dev-env.sh     # idempotent; re-run any time
source ~/.venvs/manet/bin/activate
```

The compiler and runtime pins match the node-compatible versions:

| | Version | For |
|---|---|---|
| `protoc` | 3.21.12 | regenerating `NodeInfo_pb2.py` |
| `protobuf` (Python) | 4.21.12 | the runtime nodes have; what the tests must run against |

protoc is installed inside the venv, so activating it places the correct
compiler on `PATH` together with the correct runtime and the two cannot diverge.
The script finishes by regenerating `NodeInfo_pb2.py` into a temporary directory
and comparing it with the committed copy. A mismatch fails the setup, since it
indicates that a later regeneration would produce differences unrelated to the
`.proto` change being made.

The same environment installs `cryptography` for transport tests,
Python-Markdown for guide builds and `pyelftools` for BCF assembly. Their
requirements are recorded in [requirements-dev.txt](requirements-dev.txt).

The build scripts in this directory do **not** invoke protoc; they copy the
committed `NodeInfo_pb2.py`. The venv is required when editing
`NodeInfo.proto`, and when running the unit tests; those live with the code
they cover, in
[the test reference](../../docs/node-tools-internals.md#tests), not here.

The location can be overridden with `MANET_VENV_DIR=`. `python3-venv` is not
required.

## Inputs

Universal files come from this repository:

- `MANET/node_tools` -> `/usr/local/bin`
- `MANET/binaries_arm64` -> `/usr/sbin`
- `MANET/lyra_arm64/libgstlyra.so` -> `/usr/lib/aarch64-linux-gnu/gstreamer-1.0`
- `MANET/lyra_arm64/model_coeffs` -> `/usr/local/share/lyra/model_coeffs`
- `MANET/systemd` -> `/etc/systemd/system`
- `MANET/systemd-network` -> `/etc/systemd/network`
- `MANET/udev/rules.d` -> `/etc/udev/rules.d`
- `MANET/networkd-dispatcher` -> dispatcher hook locations
- `MANET/share/manet` -> `/usr/local/share/manet`
- `MANET/man/man8` -> `/usr/local/share/man/man8` (mode 0644)
- `MANET/etc` -> `/etc`
- `MANET/root/regulatory.db` -> `/root/regulatory.db`

All four builders source `lib-man.sh` and call `stage_man_pages` for the
section 8 manuals. Keep command suffixes in page names so commands such as
`man 8 node-update.sh` work. Lint source pages with `mandoc -T lint`, or
`groff -mandoc -ww -z` when mandoc is unavailable.

Board-specific overlay files come from a release artifact or a local build
directory, never from committed source. CM4 and Rock 3A default to
`kernel-work/packages/<board>-sbc-overlay/`, overridable with `SBC_OVERLAY_DIR`.
Pass `SBC_OVERLAY_DIR` explicitly for RPi5:

```bash
SBC_OVERLAY_DIR=/path/to/extracted/overlay \
  bash MANET/packaging/build-rpi5-tarball.sh rpi5-install.tar.gz
```

An overlay archive must be root-relative and contain only SBC-specific files:

- `usr/lib/modules/<kver>/extra/morse/dot11ah.ko`
- `usr/lib/modules/<kver>/extra/morse/morse.ko`
- `usr/lib/firmware/morse/bcf_*.bin`
- the kernel image and DTBs

It must not contain universal scripts from `MANET/node_tools`, systemd units,
dispatcher hooks, dashboard files, or generated config from a live node.

## Lyra codec plugin

`voice_codec` defaults to `lyra` and provisioning writes it explicitly, so the
plugin is a hard requirement rather than an optional extra: a node without it
falls back to opus, and because the two codecs cannot hear each other, that node
is deaf and mute on the rest of the mesh. Every builder warns loudly if the
artifacts are absent.

Two artifacts, both committed under `MANET/lyra_arm64/`:

- `libgstlyra.so`: the GStreamer plugin (`lyraenc`, `lyradec`, `rtplyrapay`,
  `rtplyradepay`), statically linked against Lyra, TFLite and abseil, so its only
  runtime needs are glibc, libstdc++ and GStreamer itself.
- `model_coeffs/`: the Lyra v2 model weights, loaded at pipeline build.

They are built in two stages. Cross-build the static codec dependencies,
then copy that complete build directory and this source checkout to an
ARM64 Debian 13 build machine for the plugin link. Keep `LYRA_BUILD` pointed
at the copied directory. Use a cross compiler compatible with the target's
glibc and libstdc++ ABI. This native step needs the GStreamer headers and
libraries from the target environment.

```bash
# 1. Cross-build Lyra and its dependencies on the dev machine. Needs cmake, git,
#    make and the aarch64-linux-gnu toolchain. Long, and several GB of disk.
bash MANET/packaging/build-lyra-aarch64.sh ~/lyra-aarch64-build

# 2. Build the plugin *natively on an aarch64 node*. Cross-linking this stage
#    would need a full aarch64 sysroot with GStreamer and glib headers, which is
#    more setup than compiling ~500 lines on the target.
sudo apt install -y build-essential pkg-config \
     libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev
make -C MANET/packaging/gst-lyra LYRA_BUILD=~/lyra-aarch64-build

# 3. Commit the results so every tarball carries them.
cp MANET/packaging/gst-lyra/libgstlyra.so   MANET/lyra_arm64/
cp -r ~/lyra-aarch64-build/out/model_coeffs MANET/lyra_arm64/
```

Stage 1 output is only needed to produce stage 2; it does not ship. Verify on a
node with `gst-inspect-1.0 lyraenc`, and check `journalctl -u mesh-voice` for the
fallback line: the daemon reports the codec it actually built with, and the
VOICE tab flags a mismatch in red.

## Overlay source and outputs

The recipes and their configuration live in
[`kernel/build/`](../../kernel/build/README.md). Preparation fetches pinned
upstream sources and applies the tracked Morse patch. Generated overlays
live in `kernel-work/packages/{cm4,r3a,rpi5}-sbc-overlay/`. They are build
inputs, not committed directories. Install release archives carry their
contents together with the universal node software.

Do not assume a standalone overlay release asset exists. Build the overlay
from the pinned inputs, or use an explicitly supplied matched overlay with
`SBC_OVERLAY_DIR`. Kernel-less tools archives do not need an overlay.

## Output

All builders produce root-relative tarballs with numeric owner/group `0/0`, plus
`<output>.sha256`. Install archives extract with `tar -zxf ... -C /`; tools archives
are verified and staged by the updater before installation. First-boot provisioning
checks the install checksum and embedded versions before extraction.
Do not rename an install asset: provisioning fetches it by exact filename.

## Before releasing

- Bump both version files for every newly published package set.
- Build all boards' install/tools packages and checksum sidecars.
- Run tests, commit and push source, then publish a verified GitHub prerelease.
- Provisioning changes are included in the publisher's versioned flasher ZIP.
  The install packages and setup scripts belong to the same release.
