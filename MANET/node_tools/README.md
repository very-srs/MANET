# MANET Node Tools

This directory contains the core operational scripts for mesh network nodes. These scripts handle service elections, network management, discovery, and coordination.

---

## Core Orchestration

**node-manager-acs.sh**
Main orchestrator for Automatic Channel Selection mode. Coordinates all mesh operations on a synchronized schedule:
- RF scanning (every 3 min)
- Status publishing to Alfred
- Registry building
- Channel elections
- Tourguide windows (partition healing)
- Service elections
- Limp mode management

**node-manager-static.sh**
Simplified orchestrator for Static Channel operation. Handles:
- Status publishing
- Registry building
- Service elections
- IP management

---

## Service Elections

**mediamtx-election.sh**
Elects which node hosts the MediaMTX streaming server based on mesh centrality (TQ).
- Winner gets assigned static IPv4/IPv6 VIPs and manages service lifecycle.
- Excludes stale nodes from consideration.

---

## Channel Selection & Jamming Detection

**channel-election.sh**
Decentralized election for optimal 2.4GHz and 5GHz channels.
- Aggregates scan reports from all nodes.
- Scores channels based on noise floor and BSS count.
- Falls back to lobby channels if all options are jammed.
- Includes channel bias to prevent unnecessary migrations.

**limp-mode-manager.sh**
Monitors mesh consensus on jamming detection.
- When >50% of nodes report limp mode, reduces bitrates to minimum (legacy 802.11 rates) to maintain connectivity under interference.
- Enforces minimum duration before reverting.

**quorum-checker.sh**
Detects network partitioning and isolation.
- **Solo isolation:** Zero mesh neighbors but active Alfred nodes → return to lobby.
- **Small functional island:** Maintains operation, relies on tourguide for healing.
- **Quorum failure:** Below 50% expected neighbors → return to lobby.
- *Exit codes: 0 = healthy, 1 = return to lobby required.*

---

## Discovery & Partition Healing

**tourguide-manager.sh**
Partition detection and healing system. Runs every 2 minutes at :30 seconds:
1. Elects tourguide (node with oldest helper broadcast timestamp, excluding service hosts).
2. Hops one radio to lobby frequency.
3. Broadcasts helper beacon with current data channels.
4. Listens for other partitions.
5. If a larger partition is detected, triggers migration.
6. Returns to data channel.

**ethernet-autodetect.sh**
Auto-detects Ethernet configuration when a cable is connected:
- **DHCP detected:** Gateway mode (NAT, advertise default route, optionally keep AP).
- **No DHCP:** EUD mode (bridge to mesh).
- **Auto Mode Behavior:**
  - *Ethernet Gateway:* Dual role (gateway + AP).
  - *Ethernet EUD:* Disable AP (wired priority).
  - *Wireless EUD:* Enable AP for wireless EUDs.

---

## Network Management

**mesh-ip-manager.sh**
Chunk-based IPv4 allocation. Each node claims a chunk where `size = max_euds + 2`.
- **IP 0 in chunk:** `br0` (mesh interface).
- **IP 1 in chunk:** AP gateway (if wireless/auto mode).
- **IPs 2+:** DHCP pool for EUDs.
- First 5 IPs network-wide are reserved for services.
- Handles conflicts via MAC tie-breaker.
- Configures `dnsmasq` DHCP when needed.

**gateway-route-manager.sh**
Monitors `batctl` gateway selection and updates the system default route to the currently selected gateway's mesh IP.
- Removes route when no gateway is available.
- Uses registry lookups to map MAC → IP.

**batman-if-setup.sh**
Manages BATMAN-ADV interface lifecycle:
- Creates `bat0` interface.
- Enslaves mesh wireless interfaces (excludes AP interface).
- Sets BATMAN_V algorithm.
- Handles start/stop operations.

---

## Data Management

**mesh-registry-builder.sh**
Central registry builder.
- Queries Alfred for all peer protobuf payloads.
- Decodes each message.
- Writes `/var/run/mesh_node_registry` with all node state.
- Tracks claimed IP chunks for conflict detection.

**encoder.py**
Encodes mesh node status to protobuf and Base64 for Alfred broadcast. Inputs include:
- Identity (hostname, MACs, IPs, Syncthing ID)
- Metrics (Avg TQ, uptime, battery, CPU load)
- Service flags (gateway, NTP, MediaMTX, Mumble)
- Channel data (scan reports, current frequencies)
- Tourguide tracking
- Node state (active/shutting down)

**decoder.py**
Decodes Base64 protobuf messages to shell variables.

**NodeInfo.proto**
Protocol buffer schema defining mesh node status message structure.
- Compile with: `protoc --python_out=. NodeInfo.proto`

---

## Time Synchronization

**one-shot-time-sync.sh**
Runs once on boot to establish time synchronization. Tries to reach internet otherwise:
1. Waits for mesh registry.
2. Finds NTP servers on mesh.
3. Selects best server based on local TQ.
4. Syncs time via chrony.
5. Disables chrony to reduce traffic.

---

## Shutdown

**mesh-shutdown.sh**
Graceful shutdown handler.
- Broadcasts "tombstone" announcement with `NODE_STATE=SHUTTING_DOWN` so other nodes ignore the absence.
- Broadcasts 3 times over 5 seconds for reliability.

---

## Utilities

**mac-to-ip.sh**
Queries registry to resolve MAC address to IPv4.
- Handles both primary MACs and interface MACs (`wlan0`/`wlan1`/`end0`).
- Usage: `mac-to-ip.sh aa:bb:cc:dd:ee:ff`

**mtx-ip.sh**
Deterministically derives MediaMTX IPv6 VIP from ULA prefix.
- Hashes the normalized /64 prefix to generate a stable suffix.
- Returns address with /128 mask.

---

## Setup & Provisioning

**radio-setup.sh**
First-boot configuration script. Sets up:
- Interface renaming (mesh, HaLow, AP separation).
- WPA supplicant configs per interface.
- Network services (alfred, batman, radvd, chrony).
- Optional services (MediaMTX, Mumble).
- Systemd services for node manager.
- Called once via `radio-setup-run-once.service`, then disabled.
