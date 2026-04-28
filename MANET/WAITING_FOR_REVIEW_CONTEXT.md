# waiting-for-review context

This branch is the staging branch for moving the RPi5-developed MANET runtime
work back into the upstream `very-srs/MANET` repository without committing a full
RPi5 root filesystem.

## Repositories

- Upstream repo: `very-srs/MANET`
- Local upstream clone: `C:\Users\pc\Desktop\MANET-DEV\MANET-upstream`
- Working branch: `waiting-for-review`
- Runtime source used for the local sync: `mrleongalaxyum/manet-dev@c2215b1`
- Commit author to use: `mrleongalaxyum <leon.bazdar@fer.hr>`

## Already pushed to upstream main

- `37bd534 Fix first-run radio setup ordering`
  - `radio-setup-run-once.service` is ordered before runtime services:
    `batman-enslave.service`, `node-manager.service`, `mesh-status.service`,
    and `perf-dashboard.service`.
- `bc77a2e Fix Syncthing state directory setup`
  - `radio-setup.sh` creates `/home/radio/.local/state/syncthing` before
    generating the Syncthing config.

## What belongs in this branch

Keep universal runtime and config assets in the existing upstream folders:

- `MANET/node_tools`
  - Runtime scripts.
  - Dashboard scripts: `mesh-status.py`, `perf-dashboard.py`.
  - DNS/dnsmasq helper work, especially `mesh-ip-manager.sh`.
  - Node manager, election, registry, route, watchdog, and protobuf files.
- `MANET/networkd-dispatcher`
  - Carrier/off dispatcher hooks.
- `MANET/systemd`
  - Runtime service units.
- `MANET/systemd-network`
  - Network unit files that can be installed by packaging.
- `MANET/udev`
  - Device rules.
- `MANET/share`
  - Small shared MANET assets such as FER assets and Avahi templates.
- `MANET/etc`
  - Small config files and sudoers snippets that are not board-specific.
- `MANET/root/regulatory.db`
  - Regulatory database needed by the installed system.
- `.github/workflows`
  - CI workflow that builds the RPi5 tarball from universal source plus an
    external RPi5 overlay artifact.
- `MANET/packaging`
  - Tarball builder and packaging documentation.

## What must not be committed here

Do not commit the full `rpi5/` install tree from `manet-dev`.

Do not commit board/kernel-specific RPi5 overlay files directly in source:

- kernel modules such as `dot11ah.ko` and `morse.ko`
- HaLow firmware blobs such as `bcf_*.bin`
- boot files, kernel trees, or full `/usr/lib/modules` contents
- generated install tarballs

The temporary local folder `MANET/rpi5-halow-unlock` was removed from this
branch for that reason.

## CI packaging direction

The branch should build a root-relative, root-owned `rpi5-install.tar.gz`.

The packaging script should assemble universal files from this repo and accept
an optional external SBC overlay directory via `SBC_OVERLAY_DIR`.

The GitHub Actions workflow should download an RPi5 overlay artifact from a
release in the same repo, extract it, pass it as `SBC_OVERLAY_DIR`, build the
tarball, verify expected files, upload a workflow artifact, and publish a
release artifact.

Overlay release contract:

- Release/tag: `rpi5-sbc-overlay-current`
- Asset: `rpi5-sbc-overlay.tar.gz`
- Asset contents: root-relative SBC-specific files only, for example:
  - `usr/lib/modules/6.6.78-manet+/extra/morse/dot11ah.ko`
  - `usr/lib/modules/6.6.78-manet+/extra/morse/morse.ko`
  - `usr/lib/firmware/morse/bcf_*.bin`

The initial overlay release was created at:
`https://github.com/very-srs/MANET/releases/tag/rpi5-sbc-overlay-current`

## Current local status before commit

The `waiting-for-review` branch has uncommitted sync changes in universal
folders and new packaging/CI files. Before committing:

1. Ensure no `MANET/rpi5-halow-unlock` or full `rpi5/` folder is present.
2. Update `MANET/packaging/build-rpi5-tarball.sh` to use `SBC_OVERLAY_DIR`.
3. Update `.github/workflows/rpi5-release.yml` to download the overlay release
   artifact instead of relying on committed RPi5-specific files.
4. Build/verify a tarball locally.
5. Commit and push only to `waiting-for-review`.
