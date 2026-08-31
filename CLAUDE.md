# CLAUDE.md

Agent handbook for this repository. Read this first, then the README named for
the area you are touching. Do not copy procedures from those files back into
this one.

## Project overview

MANET radios — SBCs that self-form a mesh with **batman-adv** (Layer 2, BATMAN V)
and **802.11ah HaLow** (Morse Micro), plus 802.11ax/ac/n. Zero-conf IPv4/IPv6,
auto-channel selection, tourguide partition healing, limp mode, decentralized
service elections (MediaMTX, NTP), PTT voice.

**CM4 is the verification target.** RPi5 and Rock 3A work but are deprioritized
(thermals in a sealed enclosure). Hardware table: [README.md](README.md).

## Layout

Git root is this repo. Runtime, packaging, and provisioning live under `MANET/`.
`kernel-work/` is **not in git** — kernel/Morse build lives there on a full
dev machine; the durable record in this repo is
[docs/kernel-6.18-morse-port.md](docs/kernel-6.18-morse-port.md).

```
MANET/
  node_tools/          # Runtime → /usr/local/bin
  systemd/             # Units → /etc/systemd/system
  systemd-network/     # networkd → /etc/systemd/network
  networkd-dispatcher/ # carrier/off hooks
  udev/rules.d/
  etc/                 # sudoers, avahi, sysctl, manet_version.txt
  share/manet/
  root/                # regulatory.db
  binaries_arm64/      # alfred, batctl, wpa_supplicant_s1g, wpa_cli_s1g
  lyra_arm64/          # GStreamer Lyra plugin + model_coeffs
  provisioning/        # linux.sh, windows.ps1, firstrun templates
    additional-scripts/  # operator's own setup scripts, baked in at flash time
  packaging/           # install/tools tarball builders
  install_packages/    # built tarballs (not source)
docs/                  # kernel/Morse port record
```

Ignore `stage-node-tools.sh` if present — it targets old staging dirs.

OS is Debian 13 (Trixie), merged-usr: modules go to `/usr/lib/modules/`.

## Invariants

- Bump **both** `MANET/node_tools/version.txt` and `MANET/etc/manet_version.txt`
  together. `node-update.sh` treats inequality as “not current” and will loop.
- **Upload the tarballs to colorado-governor.com before pushing a version bump
  to GitHub `main`.** `node-update.sh` reads the remote *version* from GitHub
  `main` but fetches the *tarball* from colorado-governor. Push first and every
  `auto_update=y` node re-downloads a tarball that can never satisfy the check,
  on every Ethernet carrier event.
- `node-manager.sh` is **generated**. `radio-setup.sh` copies
  `node-manager-acs.sh` or `node-manager-static.sh` over it. Keep all three in
  sync when changing the publish path.
- Mesh config: EUD/AP keys stay on this node. Mesh SSID/key/CIDR go over Alfred
  (types 70–72) after ACK. Never HTTP-push config to peers.
- Do not commit SBC overlays, `.ko` modules, `bcf_*.bin` firmware, or generated
  install tarballs. Overlays come from `kernel-work/` or a GitHub release asset.
- Keep `alfred`, `batctl`, `wpa_cli_s1g`, `wpa_supplicant_s1g`, `morse_cli`,
  `chronyc` marked binary in `.gitattributes` or line-ending normalization
  corrupts them.
- Voice defaults to Lyra. A tarball without `MANET/lyra_arm64/` artifacts falls
  back to opus and cannot talk to the rest of the mesh.
- Both provisioning templates carry a `# >>> MANET_ADDITIONAL_SCRIPTS <<<` line.
  The flashers substitute the operator's setup scripts for it. Do not delete it,
  and do not switch them back to appending: `rock3a-provision.sh.template` ends
  with `reboot`, so an appended block never runs.
- `NodeInfo.proto` is the source; commit regenerated `NodeInfo_pb2.py` (protoc
  3.21.x only — see node_tools README). Use the venv's protoc, never
  `/usr/bin/protoc`: the distro ships 3.12.4, whose output every node rejects.
  `bash MANET/packaging/setup-dev-env.sh` builds the toolchain and proves it by
  reproducing the committed pb2 byte for byte.
- Never extract a kernel tarball onto a live CM4 FAT32 boot partition. Power
  down and extract on a dev machine; run `depmod` after. Sequence:
  [docs/kernel-6.18-morse-port.md](docs/kernel-6.18-morse-port.md) §8.

## When working on X, read Y

| Task | Read |
|------|------|
| Product overview, hardware support | [README.md](README.md) |
| Runtime, web UI, voice, Alfred, elections | [MANET/node_tools/README.md](MANET/node_tools/README.md) — jump by section, do not ingest the whole file |
| Flash / first boot / placeholders | [MANET/provisioning/README.md](MANET/provisioning/README.md) |
| Operator's own setup scripts (flash-time hook) | [MANET/provisioning/additional-scripts/README.md](MANET/provisioning/additional-scripts/README.md) |
| Install or tools tarball / version bump | [MANET/packaging/README.md](MANET/packaging/README.md) |
| Lyra codec artifacts | [MANET/lyra_arm64/README.md](MANET/lyra_arm64/README.md) |
| Prebuilt alfred / batctl / s1g wpa | [MANET/binaries_arm64/README.md](MANET/binaries_arm64/README.md) |
| Ethernet / uplink / EUD mode switch | [MANET/networkd-dispatcher/README.md](MANET/networkd-dispatcher/README.md) |
| Install vs tools archives | [MANET/install_packages/README.md](MANET/install_packages/README.md) |
| Kernel, Morse driver, overlays, HaLow bring-up | [docs/kernel-6.18-morse-port.md](docs/kernel-6.18-morse-port.md) |

Useful `node_tools/README.md` sections: [Core Orchestration](MANET/node_tools/README.md#core-orchestration),
[Web Interface](MANET/node_tools/README.md#web-interface),
[Push-to-Talk Voice](MANET/node_tools/README.md#push-to-talk-voice),
[Mesh Configuration Push](MANET/node_tools/README.md#mesh-configuration-push),
[Network Management](MANET/node_tools/README.md#network-management),
[Setup & Provisioning](MANET/node_tools/README.md#setup--provisioning).

`MANET/README.md` is a feature roadmap and can disagree with the docs above;
do not treat it as current status.

USB HaLow enumerates (`lsusb` `325b:8100`) but `Driver=[none]`: module built
without `CONFIG_MORSE_USB`. `morse_spi ... CMD63 (ret:-61)` is the SPI overlay
probing a missing hat — unrelated. Details in the kernel port doc §6.1.

## Verify

From the git root, so `node_tools` is on `sys.path`, and **from the dev venv**,
so the protobuf runtime matches the fleet:

```bash
source ~/.venvs/manet/bin/activate     # bash MANET/packaging/setup-dev-env.sh
python -m unittest discover -s MANET/node_tools -p 'test_*.py'
```

The encoder roundtrip test needs `protobuf`, and specifically **4.21.12** — the
version nodes run. A system python without it errors; a system python with a
*newer* one is worse, because it accepts pb2 files nodes refuse to import.
Check behavior on CM4.
Packaging changes also need a tarball rebuild; see
[MANET/packaging/README.md](MANET/packaging/README.md).
