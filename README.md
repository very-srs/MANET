# MANET Project

This repository contains a complete software suite for provisioning, configuring, and orchestrating Mobile Ad-hoc Network (MANET) nodes on Single Board Computers (SBCs).

The project transforms hardware like the Raspberry Pi CM4 into self-forming, self-healing mesh nodes using **B.A.T.M.A.N. Advanced** (Layer 2 routing) and **802.11s / 802.11ah HaLow** (Layer 1/2). It features orchestration for automatic addressing and channel selection, partition healing, jamming detection, and decentralized service elections.

## Key Features

* **Advanced Mesh Networking**:
    * Utilizes `batman-adv` (BATMAN V algorithm) for Layer 2 routing.
    * Supports standard 802.11ax/ac/n (2.4GHz/5GHz) and long-range 802.11ah (Wi-Fi HaLow).
    * **Auto-Channel Selection (ACS)**: Decentralized scanning and election to avoid interference.
    * **Limp Mode**: Detects jamming/interference and automatically downgrades bitrates to maintain connectivity.
* **Zero-Conf Architecture**:
    * **Distributed IPv4 Management**: Nodes automatically claim non-conflicting IP chunks for connected clients (EUDs).
    * **IPv6 Support**: SLAAC for mesh infrastructure and auto-configured gateways.
    * **EUD Support**: Connect End User Devices (phones/laptops) via Ethernet or a local WiFi Access Point.
* **Resilience & Healing**:
    * **Tourguide System**: Detects network partitions and "guides" isolated clusters back to the main mesh.
    * **Quorum Checking**: Monitors network health and resets isolated nodes to a "Lobby" state to re-establish connections.
* **Decentralized Services**:
    * **Service Elections**: Nodes elect hosts for services like **MediaMTX** (video streaming) based on mesh centrality (TQ).
    * **Distributed NTP**: Time synchronization across the mesh without internet access.

## Repository Structure

* **`provisioning/`**: Scripts and templates for flashing the OS image.
* **`node_tools/`**: The runtime logic for the node. Contains the scripts that run the mesh, including:
    * `node-manager`: The core orchestrator for cooperative mesh functions.
    * `radio-setup.sh`: Initial provisioning tool.
    * `mesh-registry-builder.sh`: Decodes gossip data (via Alfred) to build a map of the network.
* **`binaries_arm64/`**: Pre-compiled custom binaries for ARM64, including `alfred`, `batctl`, and a modified `wpa_supplicant` for HaLow support.

## Supported Hardware

| Hardware | Support Level | Notes |
| :--- | :--- | :--- |
| **Compute Module 4 (CM4)** | Functional | Primary dev target. Supports 802.11ax + HaLow. |
| **Raspberry Pi 4B** | Untested | Support expected soon. |
| **Raspberry Pi 5** | Pre-release | No HaLow support yet. |
| **Radxa Rock 3A** | Pre-release | Full support expected Feb 2026. |

## Getting Started

### 1. Prerequisites
You will need a supported SBC and a Linux or Windows machine to flash from, and ethernet Internet access for the SBC being flashed. 
See [provisioning/README.md](provisioning/README.md) for detailed requirements and download links.

### 2. Provisioning a Node
1.  Navigate to the `provisioning` directory.
2.  Run the flashing script appropriate for your host OS (`linux.sh` or `windows.ps1`).
3.  Load a saved config or follow the interactive prompts to configure:
    * **EUD Connection**: Wired, Wireless (local AP), or Auto.
    * **Optional Services**: MediaMTX, (Mumble is untested).
    * **Mesh Security**: SSID and SAE Password.
    * **Network Settings**: CIDR blocks and addressing.

### 3. First Boot
Insert the storage media into the node and power it on. The `firstrun.sh` script will, over the course of a few reboots:
1.  Disable default setup wizards.
2.  Wait for internet connectivity (via Ethernet) to download the latest kernel and tools.
3.  Install necessary packages (`batctl`, `alfred`, `wpa_supplicant`, etc.).
4.  Configure the radio interfaces.
5.  Result in a fully functional mesh node

## Connectivity Modes

The nodes support connecting external devices (End User Devices) in three ways:

* **Wired**: Connect via Ethernet. The node acts as a bridge or gateway depending on upstream internet access.
* **Wireless**: The node broadcasts a local 5GHz AP (separate from the mesh backhaul) for clients to join.
* **Auto**: Default behavior. Acts as "Wireless" unless an Ethernet device is detected, then switches priority to "Wired".

## Documentation
* [Provisioning Guide](MANET/provisioning/README.md)
* [Node Tools Documentation](MANET/node_tools/README.md)
* [Binary Details](MANET/binaries_arm64/README.md)
