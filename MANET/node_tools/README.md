# MANET Node Tools

This directory contains the core operational scripts for mesh network nodes. These scripts handle service elections, network management, discovery, and coordination.

Everything here is installed to `/usr/local/bin/` on the node. The current
version is in `version.txt`; `MANET/etc/manet_version.txt` must always match it,
because `node-update.sh` compares the two to decide whether a node is current.

Install tarballs are root-relative (`boot/`, `etc/`, `usr/`, `root/`) and packed
with numeric owner/group `0/0`. Keep the shipped binaries marked as binary in
`.gitattributes` — `morse_cli`, `chronyc`, `alfred`, `batctl`, `wpa_cli_s1g`,
`wpa_supplicant_s1g` — or line-ending normalization corrupts them.

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

**node-manager.sh**

The file the `node-manager.service` unit actually runs. It is **generated**:
`radio-setup.sh` copies either `node-manager-acs.sh` or `node-manager-static.sh`
over it depending on `acs=` in `/etc/mesh.conf`. The committed copy is the
static variant, so it also serves as what an OTA tools update installs — keep
all three in sync when editing the publish path, or a node will run stale code
after an update.

---

## Web Interface

**mesh-status.py**

The node's only web server, on port 80. Serves an open status page and, behind
the admin password, the management UI. Designed for mobile-sized field access
without SSH.

Open routes (no password):
- `/` — Force-directed topology with node health, link throughput, and per-node
  detail panels. Auto-refreshes every 15 seconds.
- `/api/data` — JSON: full mesh topology, node list, gateway status, per-node
  throughput. Used by the status page and available for external tooling.
- `/api/local` — JSON: this node's own state (interfaces, services, IP state,
  channel info).

Password-gated routes — the password is `admin_password` from `/etc/mesh.conf`,
set at flash time. Login sets an HttpOnly cookie:
- `/manage/` — the management UI (see `manet_manage.py`).
- `/manage/login`, `/manage/logout`
- `/api/admin/*` — mesh config staging, ACK status, and apply.
- `/admin` — legacy path, redirects to `/manage/#config`.

Access control has two layers:
- **Application:** every route is restricted to localhost and the mesh/EUD
  subnet (`ipv4_network`); management routes additionally require the cookie.
- **Kernel:** `manet-ui-firewall.sh` limits port 80 to localhost and this node's
  own DHCP clients, so the pages are unreachable from other radios, from other
  radios' EUDs, and from the uplink LAN.

Link quality shown here is BATMAN_V's metric, which is **throughput in Mbit/s**,
not a 0-255 link quality — `batctl` prints e.g. `43.2` for a 43.2 Mbit/s link.
Colour thresholds are 30 / 15 / 5 Mbit/s, chosen so a healthy HaLow link (which
tops out near 43 Mbit/s at 8 MHz) reads as good.

Data sources:
- `/var/run/mesh_node_registry` — peer registry built by `mesh-registry-builder.sh`
- `/etc/mesh.conf` — node configuration
- `/etc/mesh_ipv4_state` — current IP allocation state
- `batctl o`, `batctl n`, `batctl gwl` — live BATMAN-ADV state

Peers are never queried directly over HTTP. Everything shown about another node
comes from the registry, which is built from Alfred.

**manet_manage.py**

The management UI. Not a server — a route set mixed into `mesh-status.py`'s
handler and reachable only under `/manage` after the password check. Tabs:

- **Topology** — mesh nodes and their interfaces, from the registry.
- **Radio config** — interface up/down, TX power, HaLow and Wi-Fi channels.
- **Measure** — iperf3 and ping runs from this node toward a peer.
- **Sessions** — saved measurement results, JSON and CSV.
- **Uplink** — USB Wi-Fi uplink credentials.
- **Node config** — the mesh configuration form (EUD mode, AP credentials, mesh
  SSID/SAE key, IP range, regulatory domain, services, admin password) with the
  per-node ACK table. This was the old unauthenticated `/admin` page.

Anything that affects other nodes is staged over Alfred (type 71) and applied
after peers ACK, never pushed node-to-node over HTTP. Measurement is the
exception and needs no coordination: it runs a local `iperf3`/`ping` client
against the peer's always-listening daemon.

**manet_radio.py**

Radio control primitives shared by the two things that drive the radios:
`mesh-status.py` for a local change from the UI, and `mesh-radio-state.py` when
an Alfred-staged package activates. One implementation, so a channel change
means the same thing whichever path asked for it.

**mesh-radio-state.py**

Applies Alfred-coordinated radio changes. Reads a staged package from type 71,
publishes an ACK on type 72, and applies at the package's `activate_at` once the
coordinator has collected ACKs. Carries interface up/down, TX power, HaLow and
Wi-Fi channel, and uplink credentials.

**manet-ui-firewall.sh**

Installs the nftables rules described above: port 80 restricted to localhost and
this node's DHCP pool, port 5201 (iperf3) to the mesh subnet. Uses source
addresses rather than interfaces, because `br0` bridges `bat0` — a packet from a
remote node arrives on `br0` exactly like one from a local EUD. Re-run by
`mesh-ip-manager.sh` whenever the DHCP pool moves.

---

## Service Elections

All service elections share the same algorithm: the best-connected node wins,
measured by `MEAN_THROUGHPUT_MBPS` in the registry — the mean of BATMAN_V's
metric across that node's originators, in Mbit/s. Stale nodes (not seen within
10 minutes) are excluded. Ties are broken deterministically by MAC address.

This field used to be called `TQ_AVERAGE`, which was wrong: BATMAN_V's metric is
throughput, not a 0-255 link quality. The behaviour never changed — highest
wins either way — but the name misled, so it now says what it holds.

**mediamtx-election.sh**

Elects which node hosts the MediaMTX streaming server.
- Winner is assigned static IPv4 and IPv6 VIPs and manages the service lifecycle.
- VIPs are removed and the service is stopped when the node loses the election.

**mumble-election.sh**

Elects which node hosts the Mumble (Murmur) voice server.
- Same election algorithm as MediaMTX.
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
- **IP 0 in chunk:** `br0` primary — the node's mesh address, what peers use.
- **IP 1 in chunk:** `br0` secondary — the DHCP gateway handed to EUDs, wired or wireless.
- **IPs 2+:** DHCP pool for EUDs.

Both addresses sit on `br0` in the same `/24`, and `br0` bridges `bat0` — so the
mesh and the EUDs are one flat broadcast domain. The split is bookkeeping, not
isolation; `manet-ui-firewall.sh` is what actually separates them.
- First 5 IPs network-wide are reserved for services.
- Handles conflicts via MAC tie-breaker.
- Configures `dnsmasq` DHCP when needed.

**gateway-route-manager.sh**

Monitors `batctl` gateway selection and updates the system default route to the currently selected gateway's mesh IP.
- Removes route when no gateway is available.
- Uses registry lookups to map MAC → IP.
- 10-second poll, single-instance via `flock`.

**manet-uplink-dispatch.sh**

Owns gateway state. Decides whether an interface with carrier is an upstream
uplink or a wired EUD port, and on promotion configures NAT, the firewall,
`radvd`, and the EUD services; on demotion tears them back down. Called by the
networkd-dispatcher hooks and reconciled once a cycle by the node manager. Both
paths early-return when nothing has changed — an unconditional promote used to
cause hundreds of systemd daemon-reloads an hour.

**mesh-default-route-fix.sh**

Repairs the default route on client (non-gateway) nodes. Gateway nodes keep
their own Ethernet default route and are skipped.

**usb-ethernet-watch.sh**

udev-triggered: brings a USB Ethernet interface (tether, LTE dongle) into the
uplink decision path when it appears.

**usb-wifi-uplink.sh**

Configures a USB Wi-Fi adapter as an internet uplink from a stored SSID and
password. `set <ssid> <password> <0|1>` is the interface the management UI
drives, mesh-wide, over Alfred.

**mesh-hosts-update.sh**

Populates `/etc/hosts` from the registry so peer hostnames resolve without DNS.

**mac-to-ip.sh**

Resolves a MAC to its IPv4 via the registry. Handles both primary and
per-interface MACs. Usage: `mac-to-ip.sh aa:bb:cc:dd:ee:ff`

**manet-ipcalc.sh**

Pure-bash replacement for Debian's `ipcalc`, printing the same `HostMin:` /
`HostMax:` lines the mesh scripts parse. The Perl original cost ~1.5 s of CPU
per call on a CM4 and was invoked ~9 times per 15-second cycle — nearly a full
core. This runs in ~10 ms.

**verify-bridge.sh**

Operator diagnostic, not wired to any unit. Checks the bridged EUD architecture
end to end: `br0` exists and has `bat0` enslaved, the AP interface is in `br0`
and not in `bat0`, `br0` has its chunk addresses, dnsmasq is listening on the
right interface, and multicast forwarding is set. Run it by hand when EUD
connectivity looks wrong.

**batman-if-setup.sh**

Manages BATMAN-ADV interface lifecycle:
- Creates `bat0` interface.
- Enslaves mesh wireless interfaces (excludes AP interface).
- Sets BATMAN_V algorithm.
- Handles start/stop operations.
- HaLow is added first so it becomes batman's primary (longest-range link).
- Skips writing a `.link` file when the MAC is already pinned by an existing
  one — two link files for one MAC caused a rename ping-pong reboot loop.

**batman-enslave-watch.sh**

8-second watchdog after `batman-enslave.service`. Re-enslaves interfaces that
fall out of `bat0`, and enforces `mesh_plink_timeout=0` on HaLow interfaces —
the parameter resets every time the supplicant rejoins the mesh, so a one-shot
at boot would not hold. This is the right place for any idempotent "keep this
runtime parameter set" logic.

**sae-watchdog.sh**

Tails journald for `MESH-SAE-AUTH-BLOCKED` and restarts `wpa_supplicant` plus
`batman-enslave` to recover — but only when `bat0` is actually missing
interfaces, so a transient block does not cause a restart storm.

**prepare-standard-mesh-iface.sh**

Readies a 2.4/5 GHz interface for mesh point mode before the supplicant starts
(rfkill unblock, mode set). Called from `radio-setup.sh`.

**unblock-wifi-rfkill.sh**

Clears soft rfkill blocks on the wireless interfaces. Called by
`radio-setup.sh`, `prepare-standard-mesh-iface.sh`, and `usb-wifi-uplink.sh`.

**usb-wifi-halow-recovery.sh**

Restarts the HaLow supplicant when a non-Morse USB Wi-Fi adapter appears or
disappears, recovering from USB bus contention. Triggered by udev.

**halow-mcs-summary.py**

Reads the current TX/RX MCS rates and peer for an interface and prints them as
shell assignments. Used by the node managers to fill the MCS fields in
telemetry.

---

## Data Management

Node state is exchanged over Alfred as two message types, split by how often
the contents change. Alfred replicates every record to every node on a timer,
so anything that repeats is paid for continuously.

| Type | Message | Published | Contents |
|------|---------|-----------|----------|
| 67 | `NodeIdentity` | every 270 s | hostname, MACs, Syncthing ID, chunk, IP |
| 68 | `NodeTelemetry` | every 180 s | everything volatile |
| 69 | `NodeTelemetry` | tourguide window | helper beacons (channels, partition size) |

Alfred stamps every record with the publishing node's MAC — it runs `-i br0`, so
that key *is* the node's primary MAC. It is the join column between the two
types, and the reason neither message repeats it.

Identity is republished at 270 s because Alfred purges any record it has not
seen for `ALFRED_DATA_TIMEOUT` (600 s, confirmed on hardware: a test record was
purged at 618 s). At 270 s a publish can fail once and the record still
survives.

**mesh-registry-builder.sh**

Central registry builder.
- Reads both Alfred types and joins them on the record key.
- Decodes each message.
- Writes `/var/run/mesh_node_registry` with all node state.
- Tracks claimed IP chunks for conflict detection.
- Caches identity across cycles: a node whose identity record has not been
  refreshed yet keeps the values from the previous registry rather than
  appearing nameless.

**encoder.py**

Encodes this node's Alfred payloads to protobuf and Base64. Two subcommands:

- `encoder.py identity` — hostname, secondary MACs, Syncthing ID, chunk, IP.
- `encoder.py telemetry` — mean throughput, service flags, uptime, battery, CPU load,
  GPS (when `/run/gps_status.json` reports `has_fix=true`), channels and scan
  reports, MCS rates, interface list, EUD mode/SSID/count, tourguide tracking,
  node state.

**decoder.py**

Decodes a payload to shell variables: `decoder.py identity <b64> --node-mac <mac>`
or `decoder.py telemetry <b64>`. Identity needs the record key passed back in so
it can reassemble `MAC_ADDRESS` / `MAC_ADDRESSES`.

**manet_ids.py**

Wire/display conversions shared by the encoder and decoder. MACs travel as 6 raw
bytes rather than 17 characters, Syncthing device IDs as their underlying 32
bytes rather than the 63-character dashed form (Luhn check characters and all),
and IPv4 as `fixed32`. Formatting back into human shapes happens on decode.

**NodeInfo_pb2.py**

Generated from `NodeInfo.proto`. Checked in because nodes have no protoc — do
not hand-edit; regenerate.

**NodeInfo.proto**

Protocol buffer schema for both messages.
- Compile with: `protoc --python_out=. NodeInfo.proto`
- **Use protoc 3.21.x.** Nodes run the protobuf 4.21.12 Python runtime, which
  rejects generated code from protoc older than 3.19, and 5.x emits a
  `runtime_version` gate that runtime does not have.
- Changing a field type is a flag day: there is no compatibility path, so all
  nodes must be reflashed together.

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

**gps-reader.py**

Daemon for optional u-blox USB GPS receivers. It queries local `gpsd` on `127.0.0.1:2947`, writes `/run/gps_status.json` every 5 seconds, and reports `has_fix=false` safely when gpsd is missing, the dongle is absent, or there is no fix.

**one-shot-time-sync.sh**

Runs once on boot to establish time synchronization. Tries to reach internet otherwise:
1. Waits for mesh registry.
2. Finds NTP servers on mesh.
3. Selects best server by mean throughput to it.
4. Syncs time via chrony.
5. Disables chrony to reduce traffic.

---

## Mesh Configuration Push

**mesh-config-apply.sh**

Applies a staged config package from `/var/run/mesh_pending_config.json` to
`/etc/mesh.conf`, splitting settings into two classes:

- **Safe** (applied immediately): `admin_password`, `eud`, `lan_ap_ssid`,
  `lan_ap_key`, `max_euds_per_node`, `mtx`, `mumble`, `auto_update`.
- **Dangerous** (brief mesh outage): `mesh_ssid`, `mesh_key`, `ipv4_network`.

> **Not currently wired up.** The script is complete and works when run by hand
> (`mesh-config-apply.sh --force`), but nothing invokes it and nothing consumes
> Alfred type 70. The NODE CONFIG tab stages a package and broadcasts it, and no
> node — including the one that staged it — ever applies it. Two links are
> missing: something to read type 70 into `/var/run/mesh_pending_config.json`
> and call this at `activate_at`, and something to publish
> `/var/run/mesh_config_ack_version` as `--config-ack-version` so peers'
> `CONFIG_ACK_VERSION` is non-empty. Until then the ACK gate in
> `/api/admin/activate` can never pass, and Force Apply reports success without
> changing anything.

---

## Hardware Support

**battery-reader.py**

Reads the UPS HAT battery over I²C and writes `/run/battery_status.json`. The
node managers publish the percentage; the status page shows it. Absent on boards
with no battery hardware, in which case the percentage is simply not published.

**button-monitor.sh**

Blocks on a GPIO interrupt and runs `led-info.sh` on each press. Near-zero CPU
when idle. Exits cleanly at startup if the GPIO chip is unusable, rather than
spinning.

**led-boot.sh** / **led-info.sh**

Boot-progress LED states, and an on-demand neighbour-count blink sequence.
Both need libgpiod **v2** syntax and both exit 0 immediately if the configured
GPIO chip does not exist — the pin wiring is not finalised, so on current
hardware they are expected to be inactive.

---

## Recovery & Identity

**ssh-recovery.sh**

Keeps headless SSH reachable on a provisioned node.

**mesh-clone-identity.sh**

Detects a provisioned SD card cloned onto different hardware and resets the
local-only identity and state that must not be shared between nodes — otherwise
two nodes claim the same IP chunk and the same hostname.

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

**mtx-ip.sh** / **mumble-ip.sh**

Deterministically derive the MediaMTX and Mumble IPv6 VIPs from the ULA prefix
in the radvd config.
- Hash the normalized /64 prefix to generate a stable suffix.
- Return the address with a /128 mask.
- Deterministic so every node computes the same VIP without coordinating.

**node-update.sh**

Updates mesh node tools to the latest release from GitHub.
- In normal mode: checks internet connectivity, compares local vs remote version, downloads and installs the appropriate board-specific tools tarball if out of date.
- In `--routine` mode: runs silently, rate-limited to once per 24 hours via version file timestamp. Used by the automatic update cron job.
- Version metadata still points at `very-srs/MANET`; that upstream now has matching `.gitattributes` binary protection on all branches as of 2026-05-03.

---

## Setup & Provisioning

**provision-mesh.sh**

Runs on the first boot from `mesh-provision.service`, before `radio-setup.sh`.
Installs the apt dependencies, downloads and extracts the install tarball,
places the Morse firmware, and writes the base networkd/nftables/radvd config.
The copy that actually runs on a node is generated from
`MANET/provisioning/firstrun.sh.template` at flash time, with the operator's
answers substituted in — the copy here is the reference.

**radio-setup.sh**

First-boot configuration script. Sets up:
- Interface renaming (mesh, HaLow, AP separation).
- WPA supplicant configs per interface.
- Network services (alfred, batman, radvd, chrony).
- Optional services (MediaMTX, Mumble).
- Optional GPS/NTP support through `gpsd`, `gps-reader.service`, and chrony `SHM 0`.
- Systemd services for node manager.
- Called once via `radio-setup-run-once.service`, then disabled.
