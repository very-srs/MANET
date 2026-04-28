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

## Web Status Interface

**mesh-status.py**

Python web server providing real-time mesh network visibility on port 80. Designed for mobile-optimized field access without SSH.

Routes:
- `/` — Force-directed topology visualization with node health indicators, TQ-coloured link quality, and per-node detail panels. Auto-refreshes every 15 seconds.
- `/api/data` — JSON endpoint returning full mesh topology, node list, gateway status, and TQ values. Used by the status page and available for external tooling.
- `/api/local` — JSON endpoint for local node state only (interfaces, services, IP state, channel info).
- `/admin` — Read-only node configuration page. Requires HTTP Basic Auth using `admin_password` from `/etc/mesh.conf`.

Access control: all routes except `/admin` are restricted to localhost and the mesh subnet. `/admin` is accessible from any IP but requires authentication.

Data sources:
- `/var/run/mesh_node_registry` — peer registry built by `mesh-registry-builder.sh`
- `/etc/mesh.conf` — node configuration
- `/etc/mesh_ipv4_state` — current IP allocation state
- `batctl o`, `batctl n`, `batctl gwl` — live BATMAN-ADV state

---

## Service Elections

All service elections share the same TQ-based algorithm: the node with the highest average TQ wins. Stale nodes (not seen within 10 minutes) are excluded. Ties are broken deterministically by MAC address.

**mediamtx-election.sh**

Elects which node hosts the MediaMTX streaming server.
- Winner is assigned static IPv4 and IPv6 VIPs and manages the service lifecycle.
- VIPs are removed and the service is stopped when the node loses the election.

**mumble-election.sh**

Elects which node hosts the Mumble (Murmur) voice server.
- Same TQ-based election algorithm as MediaMTX.
- Winner is assigned static IPv4 and IPv6 VIPs.
- Includes database synchronization via Syncthing: the winner syncs the Mumble SQLite database from the shared Syncthing folder before starting the service, and syncs it back when losing. This preserves user accounts and channel configuration across leader changes.
- Maintains integrity-checked backups before each sync operation.

---

## Channel Selection & Jamming Detection

**channel-election.sh**

Decentralized election for optimal 2.4GHz and 5GHz channels.
- Aggregates scan reports from all nodes via the registry.
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

## File Synchronization

**syncthing-peer-manager.sh**

Daemon that automatically discovers and configures Syncthing peers across the mesh.
- Runs continuously, checking the mesh registry every 60 seconds.
- Reads peer Syncthing device IDs from `/var/run/mesh_node_registry`.
- Adds newly discovered peers to the local Syncthing `config.xml` automatically.
- Shares the default Syncthing folder with each new peer.
- Restarts Syncthing when the configuration changes.
- Used by `mumble-election.sh` to replicate the Mumble database across the mesh.

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

**node-update.sh**

Updates mesh node tools to the latest release from GitHub.
- In normal mode: checks internet connectivity, compares local vs remote version, downloads and installs the appropriate board-specific tools tarball if out of date.
- In `--routine` mode: runs silently, rate-limited to once per 24 hours via version file timestamp. Used by the automatic update cron job.

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
