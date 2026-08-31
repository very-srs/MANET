# Packaging

Two kinds of tarball are built from this repository, for three boards.

| Script | Output | Contents |
|--------|--------|----------|
| `build-cm4-tarball.sh` | `cm4-install.tar.gz` | node tools + CM4 SBC overlay |
| `build-r3a-tarball.sh` | `r3a-install.tar.gz` | node tools + Rock 3A SBC overlay |
| `build-rpi5-tarball.sh` | `rpi5-install.tar.gz` | node tools + RPi5 SBC overlay |
| `build-tools-tarball.sh` | `<board>-tools.tar.gz` | node tools only, no kernel |

**Install tarballs** bootstrap a new node: everything universal from this repo,
plus the board's kernel, DTBs, modules and Morse firmware from an SBC overlay.

**Tools tarballs** update a node that already runs: `node-update.sh` fetches one
and extracts it over `/`. Platform-agnostic — the cm4, r3a and rpi5 tools
tarballs are byte-identical content, and are only named per board because
`node-update.sh` asks for its own board's name.

Usage:

```bash
bash MANET/packaging/build-cm4-tarball.sh   MANET/install_packages/cm4-install.tar.gz
bash MANET/packaging/build-r3a-tarball.sh   MANET/install_packages/r3a-install.tar.gz
bash MANET/packaging/build-tools-tarball.sh MANET/install_packages/cm4-tools.tar.gz
bash MANET/packaging/build-tools-tarball.sh MANET/install_packages/r3a-tools.tar.gz
```

## Publishing order matters

`node-update.sh` reads the remote **version** from GitHub `main`
(`MANET/node_tools/version.txt`) but fetches the **tarball** from
colorado-governor.com. Two sources, and they are compared against each other.

**Upload the tarballs first, then push the version bump.** In the reverse
order, a node with `auto_update=y` sees a remote version the server cannot
supply: it downloads the stale tools tarball, extracts the older
`manet_version.txt`, remains behind, and repeats the download on the next
Ethernet carrier event.

`node-update.sh --routine` is throttled by the version file's mtime, skipping a
check while the file is less than a day old. This does not prevent the repeated
downloads, because `tar` restores the build machine's mtime and a published
tarball is normally already older than a day.

## Dev toolchain

`setup-dev-env.sh` builds the venv the repo's Python tooling needs. No root, and
nothing outside the venv is touched:

```bash
bash MANET/packaging/setup-dev-env.sh     # idempotent; re-run any time
source ~/.venvs/manet/bin/activate
```

It pins two versions, both to **what the fleet runs**, not to what is newest:

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

The build scripts in this directory do **not** invoke protoc; they copy the
committed `NodeInfo_pb2.py`. The venv is required when editing
`NodeInfo.proto` and when running the test suite.

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
- `MANET/etc` -> `/etc`
- `MANET/root/regulatory.db` -> `/root/regulatory.db`

Board-specific overlay files come from a release artifact or a local build
directory, never from committed source. Each builder reads its default from
`kernel-work/packages/<board>-sbc-overlay/`, overridable with `SBC_OVERLAY_DIR`:

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

- `libgstlyra.so` — the GStreamer plugin (`lyraenc`, `lyradec`, `rtplyrapay`,
  `rtplyradepay`), statically linked against Lyra, TFLite and abseil, so its only
  runtime needs are glibc, libstdc++ and GStreamer itself.
- `model_coeffs/` — the Lyra v2 model weights, loaded at pipeline build.

They are built in two stages, and the split is not arbitrary:

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
fallback line — the daemon reports the codec it actually built with, and the
VOICE tab flags a mismatch in red.

## RPi5 overlay contract

RPi5 is the CI-driven path; its overlay is published as a release asset.

- Release/tag: `rpi5-sbc-overlay-current`
- Asset: `rpi5-sbc-overlay.tar.gz`
- Release URL: `https://github.com/very-srs/MANET/releases/tag/rpi5-sbc-overlay-current`

CM4 and Rock 3A overlays are built locally by
`kernel-work/real_work/build-{cm4,r3a}.sh` and are not published.

## Output

All builders produce root-relative tarballs with numeric owner/group `0/0`, and
extract with `tar -zxf ... -C /`. Do not rename an install asset: first-boot
provisioning fetches it by exact filename.

## Before releasing

- Bump **both** version files together — `MANET/etc/manet_version.txt` and
  `MANET/node_tools/version.txt`. `node-update.sh` treats only equality as
  "up to date", so a mismatch makes every node update itself in a loop.
- Changes under `MANET/provisioning/` never need a repackage; they are baked
  into the image by `linux.sh` at flash time. Changes anywhere else always do.
- CM4 install tarballs are distributed by hand to
  `colorado-governor.com/manet/`, which is where `firstrun.sh` downloads from.
