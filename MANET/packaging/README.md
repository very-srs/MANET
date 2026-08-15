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

## Inputs

Universal files come from this repository:

- `MANET/node_tools` -> `/usr/local/bin`
- `MANET/binaries_arm64` -> `/usr/sbin`
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
