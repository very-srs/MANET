# Meet the mesh

## Marine

![Crayon drawing of three green radios linked wirelessly, with a phone connected to one radio.](../assets/overview-crayon.png)

### Radios talk to radios

Your phone connects to a nearby radio. That radio carries traffic through other radios in the mesh.

- **Same mesh settings.** Set the radios up for the same mesh.
- **Power them on.** Nearby radios find each other.
- **Internet is extra.** The mesh can work without it. A radio with internet can share it.

New radio? Start with [flashing](flash.md).

## User

### What MANET does

MANET turns supported single-board computers into mesh radios. Nodes form links with nearby peers and carry traffic across multiple hops. Each node manages addresses for its connected phones and computers.

Conventional Wi-Fi provides the faster links. HaLow uses a lower radio band and is intended for longer reach at lower speed. Actual range and throughput depend on the hardware, antennas, placement, and surroundings.

Radios coordinate channels, recover connections, and elect hosts for shared services. A node with internet access can act as a gateway for the mesh. Internet access is needed for initial provisioning; an already provisioned mesh can operate without it.

### Start with a CM4

The Compute Module 4 is the primary verification target. Raspberry Pi 5 and Rock 3A are supported, with different hardware and thermal considerations. Check the [hardware table](../../../README.md#supported-hardware) and [radio hardware requirements](../../../MANET/provisioning/README.md#radio-hardware) before buying or assembling a node.

1. [Flash a radio](flash.md) with the settings for your mesh.
2. [Complete first boot](first-boot.md) using Ethernet internet access.
3. [Connect your device](connect.md) and open the local dashboard.

### Once the radios are running

- [Read the dashboard](dashboard.md) to inspect peers and measure a path.
- [Change settings](settings.md) with the local and mesh scopes in mind.
- [Update a radio](updates.md) when a tools release is ready.
- [Troubleshoot a problem](troubleshooting.md) from the symptom you can see.

### What is still developing

This guide describes the current source tree. Normal installation selects the stable release marked **Latest**, which can lag behind source changes. Use `--development` explicitly when testing newer published builds.

ATAK integration and experimental radio-ranging work are separate features. ATAK runs by default; ranging-based positioning is disabled by default and should not be assumed to provide a working location solution.

## Developer

### Runtime architecture

MANET is a Debian 13 appliance stack built around `batman-adv` Layer 2 routing with BATMAN V. Conventional Wi-Fi mesh links and Morse Micro 802.11ah HaLow interfaces attach to `bat0`; the addressed `br0` bridge carries the node's mesh and EUD connectivity. EUD means end-user device.

The `MANET/` tree contains runtime scripts, systemd units, networkd configuration, dispatcher hooks, provisioning templates, and release tooling. Scripts under `MANET/node_tools/` install into `/usr/local/bin`. Runtime coordination is distributed; a temporarily elected service host or ACS coordinator is not a permanent controller.

| Area | Mechanism |
|---|---|
| Identity and telemetry | Protobuf records over Alfred; local registry built from those records |
| Addresses | Distributed IPv4 chunk claims with collision handling; IPv6 support through the network stack |
| Channels | Static or ACS orchestrator, authenticated plan agreement and recovery |
| Shared services | Deterministic elections and service virtual IPs |
| Administration | Local web UI; authenticated encrypted control and ACKs over Alfred |
| Voice | GStreamer, Lyra by default, RTP multicast, local USB audio/PTT |
| Location | GPS and ATAK integration; experimental MT7916 timing and positioning work |

### Authority and boundaries

Current implementation and focused verification establish behavior. Historical handoffs record useful rationale, but old bench results and pending-work notes do not establish current support. The feature roadmap also needs interpretation against the implementation.

Mesh membership alone does not authorize administrative changes. Nodes share an admin password for control traffic; each node's AP credentials stay local. Management access is restricted to the node's own clients and localhost.

### Build and release boundaries

Normal provisioning and updates follow the full GitHub release marked Latest. Development selection is explicit. Publishing uses `MANET/releases/publish.py`, which stages assets, verifies their stored sizes and digests, and publishes the complete set. A source push does not announce a radio update.

Kernel builds and package builders are maintained outside the tracked source layout. Do not infer that a clean repository checkout can reproduce those machine-local build environments without the documented prerequisites.

### Detailed references

- [Runtime and configuration reference](../../../MANET/node_tools/README.md)
- [Runtime internals](../../../docs/node-tools-internals.md)
- [Kernel, Morse driver, and build chain](../../../docs/kernel-6.18-morse-port.md)
- [Release publication and selection](../../../MANET/releases/README.md)
