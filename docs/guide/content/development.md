# Develop and release MANET

## Marine

![Crayon drawing of a laptop preparing software for a green radio.](../assets/flash-crayon.png)

### Change it. Check it. Share it.

The builders change the software and check what it does. Then they make
a package for the radios.

A change on GitHub does not put new software on your radio. Follow
[updates](updates.md) when a package is ready.

## User

### Source, documentation and radio releases are separate

The GitHub repository holds the project's source and documentation. A
documentation update can appear here while your radio stays on the same
software version. Source changes also need packaging and publication before
the ordinary updater can install them.

Normal flashing, first boot and updates follow the full GitHub release
marked **Latest**, the stable selection. A development build is chosen
explicitly and may contain work still under evaluation. Moving a tested
build to stable changes its release status without rebuilding it.

This guide describes the current documented implementation. When comparing
behavior, record the version actually installed on your radio; it may
precede the source being discussed. Hardware support and unverified features
are called out in the relevant chapters.

### Useful contributions

You can edit the guide's Markdown locally and have those changes reviewed
and published. Keep the three reading levels consistent when a fact changes.
Developer explains the mechanism, User explains actions/results, and Marine
explains it simply with drawings. The
[documentation maintenance guide](../README.md) records this convention for
both human contributors and coding agents.

For a fault report, include the radio version, hardware, what you expected,
what happened and the relevant [diagnostic evidence](troubleshooting.md).
For a performance claim, include a saved measurement and conditions, not
just a dashboard color.

## Developer

### Follow ownership from source to installed node

| Repository area | Installed responsibility |
|---|---|
| `MANET/node_tools/` | Runtime executables and Python modules, generally under `/usr/local/bin`. |
| `MANET/systemd/`, `systemd-network/`, `networkd-dispatcher/`, `udev/` | Service lifecycle, interfaces and event hooks. Some units are generated during provisioning. |
| `MANET/etc/`, `share/manet/`, `root/` | Configuration defaults, shared assets and regulatory data. |
| `MANET/binaries_arm64/`, `lyra_arm64/` | Supplied ARM64 programs and audio artifacts. |
| `MANET/provisioning/` | Host flashers, first-boot templates and operator hooks. |
| `MANET/releases/` | Release selection, manifests, publication and promotion. |
| `kernel/patches/` | Tracked kernel patch work; status depends on each area's evidence. |
| `docs/guide/` | This three-tier site and its builder. |

The [node-tool map](../../node-tools-map.md) resolves individual runtime
files. The systemd unit or invoking controller establishes when a helper
runs; a filename alone does not imply a standalone daemon.

### A clean checkout is not the complete build environment

The kernel/Morse build workspace and package builders are maintained on a
development machine outside tracked source. The
[kernel port record](../../../docs/kernel-6.18-morse-port.md) is the durable
description of versions, overlays, modules and bring-up. Do not infer that
an untracked directory mentioned in a handoff will exist for another
contributor. Record prerequisites and any missing reproducibility inputs.

Supplied ELF binaries must retain binary Git attributes; line-ending
normalization corrupts them. Voice packages need both the Lyra GStreamer
plugin and model weights. Omitting them can cause an Opus fallback that
cannot communicate with Lyra peers. See the
[binary reference](../../../MANET/binaries_arm64/README.md) and
[Lyra artifacts](../../../MANET/lyra_arm64/README.md).

For node telemetry, `NodeInfo.proto` is authoritative and its generated
Python binding is committed. The project's documented protoc 3.21.x toolchain
and protobuf 4.21.12 runtime must agree with nodes; a newer local runtime
accepting generated output is insufficient. Use the documented development
environment rather than the host distro's unrelated compiler.

### Validate the changed mechanism and its failure path

Start with the owning runtime/provisioning README and current source.
Historical handoffs and the feature roadmap are context, not proof of
present behavior. Unit tests under `MANET/node_tools/test_*.py` run from
the development virtual environment so protobuf and cryptography match the
project's requirements. The [test reference](../../../docs/node-tools-internals.md#tests)
describes the environment and subsystem coverage.

Select checks that exercise the change's consequences: stale peer data,
missing hardware, restart, partial apply, loss of a path, malformed input or
storage exhaustion where relevant. Hardware behavior needs evidence on
the CM4, the verification target. A simulation cannot establish on-air
timing, wiring, RF output or power-loss boot recovery.

Documentation changes require a guide build and link check; navigation or
presentation changes also need desktop/mobile and all-tier browser review.
The inventory check prevents a production node tool from being silently
omitted, but cannot prove its prose remains accurate after a behavior change.

### Package publication is an explicit boundary

Each new package set increments both source version files and produces
tools/install archives for all three published board targets, with matching
sidecars and manifest digests. Use the
[release publisher](../../../MANET/releases/README.md): it stages a draft,
uploads and verifies remote assets, then publishes. New iterations default
to prereleases; promotion marks a verified full release Latest without
changing package bytes. Cleanup keeps the newest three prereleases while
preserving full releases and Git tags.

The field update tarball can carry any component needed by a release,
including kernels, modules, firmware and migrations. Payload freedom is
separate from implemented activation/recovery: kernel boot-set handling
still needs its documented work and validation. Follow
[updates](updates.md) and the
[field update policy](../../../docs/node-tools-internals.md#field-update-policy).
End-user radios can be sealed; reflashing is not the routine maintenance plan.
