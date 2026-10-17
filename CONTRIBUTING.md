# Contributing to MANET

MANET combines Linux networking, radio drivers and distributed control on
small ARM64 computers. Start with the mechanism you want to change, its
owner and its failure paths. A change that works on a laptop still needs
the relevant radio evidence before it is ready for a release.

The [field guide](https://very-srs.github.io/MANET/) explains the system at
three reading levels. Choose **Developer** for mechanisms and design
constraints. This document provides the checkout-to-PR workflow. The
[runtime internals](docs/node-tools-internals.md) contain engineering
rationale, and the [node-tool map](docs/node-tools-map.md) locates files by
responsibility.

## Set up a checkout

Use a Linux host with Python 3.10 or newer, Git, curl, unzip and standard
GNU command-line tools. Some tests execute Bash and system utilities even
when their network and service calls are simulated.

```bash
git clone https://github.com/very-srs/MANET.git
cd MANET
bash MANET/packaging/setup-dev-env.sh
source ~/.venvs/manet/bin/activate
```

The setup script installs development dependencies and `protoc` inside one
virtual environment. Set `MANET_VENV_DIR` to choose another location, then
activate that environment's `bin/activate` instead. It does not install
software onto a radio.

The protobuf Python runtime is pinned to **4.21.12** and `protoc` to
**3.21.12**. Setup regenerates `NodeInfo_pb2.py` in a temporary directory
and checks that it matches the committed file byte for byte. A newer
Python protobuf runtime can hide compatibility errors that occur on the
node. Keep the pinned compiler and runtime together.

The environment also supplies `cryptography` for admin-transport tests,
Python-Markdown for the guide and `pyelftools` for Morse BCF assembly.
The Python dependency pins are in
[requirements-dev.txt](MANET/packaging/requirements-dev.txt).

## Find the owner before changing behavior

| Change | Read first | Main source |
|---|---|---|
| Boot, orchestration, addressing, channels or elections | [Runtime reference](MANET/node_tools/README.md), [internals](docs/node-tools-internals.md) | `MANET/node_tools/`, `MANET/systemd/` |
| Dashboard or management UI | [Web interface](MANET/node_tools/README.md#web-interface) | `mesh-status.py`, `manet_manage.py`, their supporting modules |
| Ethernet, client access or internet gateways | [Network management](MANET/node_tools/README.md#network-management), [dispatcher hooks](MANET/networkd-dispatcher/README.md) | Runtime helpers, networkd configuration and hooks |
| Imaging or first boot | [Provisioning](MANET/provisioning/README.md), [flasher internals](docs/provisioning-internals.md) | `MANET/provisioning/` and `radio-setup.sh` |
| Package contents or development setup | [Packaging](MANET/packaging/README.md) | `MANET/packaging/` |
| Kernel, modules or Morse firmware | [Kernel builds](kernel/build/README.md), [port record](docs/kernel-6.18-morse-port.md) | `kernel/build/`, `kernel/patches/` |
| Wi-Fi/HaLow supplicant binaries | [Binary reference](MANET/binaries_arm64/README.md) | Build configurations and patches under `MANET/binaries_arm64/` |
| Voice codec | [Voice internals](docs/node-tools-internals.md#voice), [Lyra build](MANET/packaging/README.md#lyra-codec-plugin) | Runtime voice code and `MANET/packaging/gst-lyra/` |
| Release selection or publication | [Release tooling](MANET/releases/README.md) | `MANET/releases/` and updater/provisioning callers |
| Documentation | [Maintenance and style](docs/guide/README.md), [coverage](docs/guide/coverage.md) | Topic Markdown and the canonical reference for that area |

Read the invoking service or controller as well as the helper. Systemd
ordering, locks and generated state often determine when a command is safe
to run. A filename does not imply that a tool should be started by hand.

For an architectural change, open an issue or a draft PR early with the
problem, proposed ownership and affected interfaces. Explain which current
mechanism you would extend or replace and why. That gives reviewers a way
to identify overlapping work and constraints before the implementation
becomes large. A focused bug fix can proceed directly to a PR.

## Constraints that changes must preserve

The data path is `batman-adv` Layer 2 forwarding through `bat0` and `br0`.
Alfred exchanges observations and control records. The web server is a
management interface, not the forwarding controller. Keep reachable BATMAN
membership distinct from cached registry observations.

A connected BATMAN component is one channel-decision domain across all
radios. Two Wi-Fi groups linked through HaLow are still connected. Channel
plans, recovery and clock readiness must account for that surviving path.
See [channel agreement](docs/node-tools-internals.md#channel-agreement-and-recovery).

Administrative control requires authentication with the mesh admin
password. Mesh membership and public telemetry do not grant permission.
Local EUD/AP settings stay local. Mesh configuration travels through the
authenticated Alfred transaction, including acknowledgements, replay
protection and recovery. Do not add a peer HTTP configuration push.
See [configuration transactions](docs/node-tools-internals.md#mesh-configuration-push).

`node-manager.sh` is a generated symlink selected from `acs=`. Packages
carry the ACS and static implementations and the selector. Do not add a
third implementation at that generated path. Authoritative config and role
files must not be replaced by guesses based on interface numbering.

`NodeInfo.proto` is the telemetry schema source. Commit its regenerated
Python binding with schema changes, using the pinned environment:

```bash
protoc -I MANET/node_tools --python_out=MANET/node_tools \
  MANET/node_tools/NodeInfo.proto
```

Consider absent fields and older peer records when changing telemetry.
Keep prebuilt ELF programs marked binary in `.gitattributes`. Voice
packages require both the Lyra plugin and model weights because an Opus
fallback cannot communicate with Lyra peers.

Radios may be sealed. Updates can carry any component a release needs,
including kernels, modules, firmware and migrations. A change to package
contents must include its activation and failure-recovery behavior. Follow
the [field update policy](docs/node-tools-internals.md#field-update-policy).
Do not make routine reflashing the maintenance procedure.

## Implement and validate

Follow the [coding conventions](docs/node-tools-internals.md#coding-conventions)
for shell/Python behavior, comments, error reporting and service
restrictions. Keep each PR focused. Explain changes to ownership, ordering,
dependencies or persistent formats so reviewers can assess their effects.

From the repository root, with the development environment active:

```bash
python -m unittest discover -s MANET/node_tools -p 'test_*.py'
python docs/guide/build.py --check
```

During development, run the tests for the mechanism you changed. For
example, a telemetry change should exercise real encoding/decoding as well
as registry handling. A config change should test rejected input, partial
application, restart and rollback. A helper test that mocks out the entire
mechanism does not establish that its callers still work.

The full runtime suite uses simulated commands and temporary files for
many hardware interactions. It does not prove RF behavior, power-loss
recovery or that a systemd restriction permits the required device access.
CM4 is the verification target. Report the board, kernel, MANET version,
configuration and conditions for hardware results. Other boards do not
inherit that evidence. If you have no hardware, say which part remains
unverified so the maintainer can arrange the relevant check.

UI changes need desktop and phone review. Network controls must also be
tested from a client using the affected connection, since an SSH tunnel or
wired browser cannot reveal the loss of that client's access path.

When packaging changes, rebuild and inspect the resulting archive. A
kernel-less update can be built without compiling a kernel:

```bash
bash MANET/packaging/build-tools-tarball.sh /tmp/manet/cm4-tools.tar.gz
```

This downloads the pinned stock MT7916 firmware set if it is not cached.
The helper verifies its hashes. The archive uses the working checkout,
including uncommitted runtime files, so build release candidates from the
intended source snapshot. See [package inputs](MANET/packaging/README.md#inputs)
and [kernel builds](kernel/build/README.md) for install archives and kernel
changes. Build commands produce files, not a deployment to a radio.

## Keep the explanation with the change

Update the owning README or internals section and the affected field-guide
tiers in the same PR. Developer is written for someone contributing code:
explain inputs, authority, state transitions, cross-node messages, timing,
failure handling and why the design chooses them. Include focused source
and test links. User describes the observable behavior and recovery. Marine
uses simple drawings and short captions without contradicting either.

New or removed production node tools also need a
[tool-map](docs/node-tools-map.md) update. The guide builder rejects missing,
duplicate and stale entries. It checks local links and headings as well.
Run the guide's build checks and preview instructions from the
[maintenance guide](docs/guide/README.md#build-and-preview).

Public documentation describes committed implementation and identifies
experimental or unverified behavior explicitly. A feature name, package
build or test result alone does not establish readiness for use. Keep
private bench notes, credentials and machine-specific paths out of the PR.

## Submit a reviewable PR

Describe the concrete problem and resulting behavior. Include the reason
for the approach, affected components, validation performed and remaining
limits. Include logs or measurements that explain a failure or support a
hardware claim, with credentials and personal data removed. Describe the
behaviors checked rather than quoting a test count.

Keep generated packages, downloaded kernel trees, `.ko` modules, firmware
BCFs, caches and local environments out of Git. The existing supplied
binaries have their own documented update paths. Avoid unrelated
reformatting or silently replacing them during a source change.

Do not bump package versions merely to submit a PR. A newly published
package set increments both version files, builds all six board archives
and their checksums, and goes through the
[release publisher](MANET/releases/README.md). Normal iterations are
prereleases. Promotion makes the tested bytes stable without rebuilding
them. A source or documentation push does not itself release radio software.
