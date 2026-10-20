# MANET

MANET builds self-forming mesh radios from ARM64 single-board computers,
802.11ax/ac/n Wi-Fi, and Morse Micro 802.11ah HaLow. The data path uses
`batman-adv` with BATMAN V at Layer 2. Nodes coordinate addresses, channels,
time sources, and shared services as links appear, partitions form, and groups
rejoin.

**Using a radio:** start with the [field guide](https://very-srs.github.io/MANET/)
for hardware, flashing, operation, and troubleshooting. Choose User for normal
instructions or Marine for illustrated explanations.

**Contributing code:** start with [CONTRIBUTING.md](CONTRIBUTING.md), then
choose Developer in the field guide for mechanisms and engineering
constraints. Build source and developer setup belong in this repository.
Generated packages are distributed through GitHub Releases.

## Development quick start

On a Linux host with Python 3.10+, Git, curl, and unzip:

```bash
git clone https://github.com/very-srs/MANET.git
cd MANET
bash MANET/packaging/setup-dev-env.sh
source ~/.venvs/manet/bin/activate
python -m unittest discover -s MANET/node_tools -p 'test_*.py'
python docs/guide/build.py --check
```

Setup pins the protobuf compiler and Python runtime to the node-compatible
versions and verifies the generated schema. See the
[contributor workflow](CONTRIBUTING.md) for dependencies, focused validation,
coding conventions, design constraints, and PR expectations.

Build a kernel-less update archive from this checkout:

```bash
bash MANET/packaging/build-tools-tarball.sh /tmp/manet/cm4-tools.tar.gz
```

The builder obtains and verifies pinned MT7916 firmware when it is not
cached. Install archives additionally need a board overlay containing the
kernel, device trees, modules, and Morse firmware. Follow
[packaging](MANET/packaging/README.md) and
[kernel builds](kernel/build/README.md) to produce those inputs. Building an
archive does not deploy it to a radio or publish a release.

## Architecture and source ownership

Wi-Fi mesh and HaLow interfaces feed `bat0`. The addressed bridge `br0`
connects that forwarding domain to local end-user devices (EUDs). Alfred
exchanges identity, telemetry, and authenticated control records. Each node
builds a local registry and reconciles its own state. Elected channel
coordinators and service hosts have specific responsibilities, rather than
acting as permanent controllers for all traffic.

A connected BATMAN component is one channel-decision domain even when
HaLow is the only link between two Wi-Fi groups. Authentication, observation
freshness, clock readiness, and recovery matter separately from forwarding.
Read the [Developer overview](docs/guide/content/overview.md#developer-runtime-architecture),
[runtime internals](docs/node-tools-internals.md), and
[node-tool map](docs/node-tools-map.md) before changing that coordination.

| Area | Responsibility |
|---|---|
| `MANET/node_tools/` | Runtime scripts, Python modules, telemetry schema, and tests. |
| `MANET/systemd/`, `systemd-network/`, `networkd-dispatcher/`, `udev/` | Service lifecycle, network configuration, and event hooks. |
| `MANET/etc/`, `share/manet/`, `root/` | Defaults, shared assets, and regulatory data. |
| `MANET/provisioning/` | Linux/Windows flashers, first-boot templates, and operator setup hooks. |
| `MANET/packaging/` | Development setup, install/update archive builders, and Lyra source. |
| `MANET/binaries_arm64/`, `lyra_arm64/` | Supplied ARM64 programs and voice codec artifacts. |
| `kernel/build/`, `kernel/patches/` | Pinned source preparation, kernel/module recipes, and project patches. |
| `MANET/releases/` | Release selection, manifests, publication, and promotion. |
| `docs/guide/` | Three-level field guide, style rules, illustrations, and site builder. |

## Supported Hardware

| Hardware | Status |
|---|---|
| Compute Module 4 | Primary development and verification target, Wi-Fi plus HaLow. |
| Raspberry Pi 4B | Briefly tested, limited by lack of PCIe radio hardware. |
| Raspberry Pi 5 | Functional but deprioritized. Results require board-specific verification. |
| Radxa Rock 3A | Functional but deprioritized. Results require board-specific verification. |

CM4 is the focus because of thermal constraints in sealed enclosures.
See [hardware requirements](MANET/provisioning/README.md#radio-hardware)
and the [kernel port record](docs/kernel-6.18-morse-port.md) for board and
radio details. Experimental capabilities are identified in their owning
references. A compiled feature is not automatically qualified for use.

## Getting Started

For flashing and operating a radio, use the
[field guide](https://very-srs.github.io/MANET/) or the
[provisioning reference](MANET/provisioning/README.md). Normal installs and
updates select the stable GitHub release marked **Latest**. Development
builds are selected explicitly with `--development`.

### Connectivity Modes

An EUD connects to its radio over Ethernet or a local Wi-Fi access point.
The Wired, Wireless, and Auto settings determine how those roles are
assigned. Ethernet can instead provide an internet uplink, making the node
a gateway. See [client connections](docs/guide/content/connect.md) and
[internet gateways](docs/guide/content/internet.md) for behavior and recovery.

### ATAK phones

ATAK integration is enabled by default. The radio supplies external GPS
and exchanges CoT with the attached phone. Follow the
[location and ATAK chapter](docs/guide/content/location.md) for phone
configuration, contact selection, and position-source limitations.

## Documentation

- [Contributing and PR workflow](CONTRIBUTING.md)
- [Field guide](https://very-srs.github.io/MANET/) and [documentation style](docs/guide/README.md)
- [Runtime reference](MANET/node_tools/README.md), [internals](docs/node-tools-internals.md), and [tool map](docs/node-tools-map.md)
- [Provisioning reference](MANET/provisioning/README.md) and [flasher internals](docs/provisioning-internals.md)
- [Packaging and development tools](MANET/packaging/README.md)
- [Kernel builds](kernel/build/README.md) and [Morse port record](docs/kernel-6.18-morse-port.md)
- [Supplied binaries and supplicant builds](MANET/binaries_arm64/README.md)
- [Release publication and selection](MANET/releases/README.md)

A source push, a guide deployment, and a radio release are separate events.
Contributions need review and validation before packaging. The release
publisher verifies assets before publishing, and promotion makes the tested
package set stable without rebuilding it.

## License

MANET's own code is released under the [MIT license](LICENSE). The
copyright holders are listed in [AUTHORS](AUTHORS).
[THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES) lists every third-party component
in the release archives, with its license and the offer of source code.
Archives install all three files under `/usr/local/share/doc/manet/`.

Some files in this repository are other projects' work and keep their own
licenses:

- The patch series under `kernel/patches/` modify Linux kernel and driver
  sources and carry the license of each file they change, GPL-2.0 or ISC as
  the file states.
- The prebuilt programs `alfred`, `batctl`, `openvlm`, and the hostap-based
  supplicants in `MANET/binaries_arm64/`, and `chronyc` and `morse_cli` in
  `MANET/node_tools/`, are distributed under their upstream projects'
  licenses.
- The Lyra codec plugin and model files in `MANET/lyra_arm64/` and
  `MANET/root/regulatory.db` come from their upstream projects under those
  projects' licenses.
