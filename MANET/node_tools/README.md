# MANET Node Tools

The scripts that run a mesh node. Everything in this directory is installed to
`/usr/local/bin/` on the node.

- [Core Orchestration](#core-orchestration)
- [Web Interface](#web-interface)
- [Push-to-Talk Voice](#push-to-talk-voice)
- [Service Elections](#service-elections)
- [Channel Selection & Jamming Detection](#channel-selection--jamming-detection)
- [Discovery & Partition Healing](#discovery--partition-healing)
- [Network Management](#network-management)
- [File Synchronization](#file-synchronization)
- [Time Synchronization](#time-synchronization)
- [Mesh Configuration Push](#mesh-configuration-push)
- [Hardware Support](#hardware-support)
- [Recovery & Identity](#recovery--identity)
- [Shutdown](#shutdown)
- [Utilities](#utilities)
- [Setup & Provisioning](#setup--provisioning)

---
## Core Orchestration

**node-manager-acs.sh**

The orchestrator for Automatic Channel Selection mode. RF scans and elections
run on a synchronized schedule; registry/IP management runs every loop:

- RF scanning, every 3 minutes
- Status publishing to Alfred
- Registry building
- Channel elections
- Tourguide windows for partition healing
- Service elections
- Limp mode management

A node on its own at the lobby channel neither elects nor hops. It waits until
a tourguide brings it onto the mesh's data channels, or until another radio
turns up and meshes with it there, and the two then run a joint election and
migrate together. A whole site starting from cold is unaffected, since every
node powers on into the lobby, sees the others there, and they bootstrap
together.

**node-manager-static.sh**

The orchestrator for static channel operation. It handles status publishing,
registry building, service elections and IP management.

**node-manager.sh**

The file `node-manager.service` runs. It is a copy of whichever orchestrator
`acs=` in `/etc/mesh.conf` selects, put in place by `radio-setup.sh`. Changing
`acs` from the Node config tab re-publishes it and restarts `node-manager`.

---

## Web Interface

**mesh-status.py**

The node's only web server, on port 80. It serves an open status page and,
behind the admin password, the management UI. Sized for a phone in the field,
so nothing here needs SSH.

Open routes, no password:

- `/`: force-directed topology with node health, link throughput and per-node
  detail panels. Refreshes every 15 seconds.
- `/api/data`: full mesh topology, node list, gateway status and per-node
  throughput, as JSON. Drives the status page, and available for external
  tooling.
- `/api/local`: this node's own state, meaning interfaces, services, IP state
  and channel info.
- `/api/peer/<ip>`: one peer's detail panel, fetched when you expand a node.
- `/api/debug`: raw `batctl` originator, neighbor and gateway output alongside
  the registry's MAC to hostname mapping. A diagnostic dump. Nothing in the UI
  calls it.

Password-gated routes. The password is `admin_password` from `/etc/mesh.conf`,
shared across the mesh and set at flash time. Logging in sets an HttpOnly
cookie. A missing admin password disables login; radio/AP passwords do not
grant management access:

- `/manage/`: the management UI and every route beneath it, including the
  radio, measurement, voice and uplink APIs.
- `/manage/login`, `/manage/logout`
- `/api/perf-auth`: POST the password, receive the cookie token.
- `/api/admin/*`: mesh config staging, ACK status and apply.
- `/admin`: redirects to `/manage/#config`.

No unauthenticated route changes anything.

Access control has two layers:

- **Application.** Every route is restricted to localhost and the mesh/EUD
  subnet (`ipv4_network`), and management routes additionally require the
  cookie.
- **Kernel.** `manet-ui-firewall.sh` limits port 80 to localhost and this
  node's own DHCP clients, so the pages are unreachable from other radios, from
  other radios' EUDs, and from the uplink LAN.

Link quality is BATMAN_V's metric, which is throughput in Mbit/s and not a
0-255 link quality. `batctl` prints 43.2 for a 43.2 Mbit/s link. The color
thresholds are 30, 15 and 5 Mbit/s, set so a healthy HaLow link reads as good.
HaLow tops out near 43 Mbit/s at 8 MHz.

What the page reads:

- `/var/run/mesh_node_registry`: the peer registry
- `/etc/mesh.conf`: node configuration
- `/etc/mesh_ipv4_state`: current IP allocation
- `batctl o`, `batctl n`, `batctl gwl`: live batman-adv state

Peers are never queried over HTTP. Everything shown about another node comes
from the registry, which is built from Alfred.

**manet_manage.py**

The management UI, reachable under `/manage` after the password check. Tabs:

- **Topology.** Mesh nodes and their interfaces.
- **Radio config.** Interface up/down, TX power, HaLow and Wi-Fi channels.
- **Measure.** iperf3 and ping runs from this node toward a peer.
- **Sessions.** Saved measurement results, as JSON and CSV.
- **Uplink.** USB Wi-Fi uplink credentials.
- **Voice.** Talk group, codec and per-peer voice state.
- **Node config.** EUD mode, AP credentials, mesh SSID and SAE key, IP range,
  regulatory domain, services and admin password, with the per-node ACK table.

Anything that affects other nodes is staged over Alfred and applied once peers
acknowledge it. Nothing is pushed node to node over HTTP. Measurement is the
exception and needs no coordination, since it runs a local `iperf3` or `ping`
client against the peer's always-listening daemon.

**Radio settings**

The HaLow channel plan follows the node's region. A bandwidth is offered only
where the region defines a channel of that width, so EU stops at 2 MHz while US
reaches 8 MHz. The 863-868 MHz allocation EU uses is too narrow for anything
wider. The Radio config tab offers only widths the region can use.

HaLow TX power is fixed per bandwidth by the driver and the BCF. A request
outside those caps is refused with an explanation instead of being silently
clamped. Wi-Fi TX power options come from the phy's own advertised range, and a
change is read back and verified.

**mesh-radio-state.py**

Applies radio changes that were staged over Alfred. Reads the package,
publishes an acknowledgement, and applies at the package's activation time once
the coordinator has collected the rest. Carries interface up/down, TX power,
HaLow and Wi-Fi channel, and uplink credentials.

**manet_peer_radios.py**

Builds the per-peer radio chips and the expandable panel the topology view
shows for another node: role, up and down state, channel, MCS, service pills,
and the inferred `bat0`, `br0` and gateway rows. Everything comes from what
Alfred already replicates, so no node is queried.

**manet-ui-firewall.sh**

Installs the nftables rules above: port 80 restricted to localhost and this
node's DHCP pool, port 5201 (iperf3) to the mesh subnet. Re-run by
`mesh-ip-manager.sh` whenever the DHCP pool moves.

---

## Push-to-Talk Voice

**mesh-voice.py**

Conference-style PTT voice across the mesh. The PTT switch is on a headset
plugged into the node itself, and there is no browser microphone. The web UI is
a status and talk-group readout.

Transmitting is gated by the button. Listening is not: every talker gets their
own decode path and the audio is summed, so two people speaking at once are both
heard. There is no lockout on talking over someone. Set `voice_half_duplex=y` to
refuse the PTT while a remote node is transmitting.

### Talk group

Every node is flashed on group 1. Change it from the VOICE tab of the web UI,
which writes `voice_channel` to `/etc/mesh.conf` and reloads the daemon. The
daemon retunes in place instead of restarting, so the change costs no audible
gap and holds the PTT open if you are mid-transmission.

Talk group is per-radio. It is not pushed across the mesh the way a HaLow or
Wi-Fi channel change is, so each operator picks their own group.

Groups run from 1 to 32. All of them share one multicast group
(`239.192.41.1`) and differ by port: group *n* uses `38801 + (n-1)*2`, with
`port+1` carrying that group's RTCP.

### Codec

`voice_codec` selects the codec, `lyra` by default or `opus`. The two are not
compatible. If you change to opus, every node must have the matching setting or
voice will not work between them.

Codec is therefore a mesh-wide setting. The CODEC card in the VOICE tab stages
the change over Alfred, waits for every node to ACK it, and switches the whole
fleet on a common clock about 20 seconds out. If any node fails to ACK, the
change is canceled and nothing moves. Each node restarts mesh-voice on
activation, because the encoder, payloader and RTP clock rate all change
together.

A node that was powered off during a codec change comes back on the old codec
and stays inaudible until it is changed too. Nothing reconciles this
automatically. Re-issue the change from the UI with the node up.

### Audio hardware

The PTT switch is USB, not GPIO. The OpenVLM board is a C-Media CM108B and the
switch is read from USB HID input reports on `/dev/hidraw*`. A generic CM108
dongle also works, and a strapped OpenVLM board is preferred when both are
present. Hot-plug is handled by reopening, so the daemon runs with no board
fitted and picks one up when it appears.

The mic input expects an electret. The CM108B biases `MIC+` through its own
network, which suits the electret capsule in a commercial headset. A dynamic
element, as fitted to NATO-wired military headsets, is around 25 dB quieter and
has no internal amplifier, so it sits at or below the input's own noise floor.
No mixer or EEPROM setting recovers it. Such a headset needs an external preamp
between the element and `MIC+`.

With a preamp fitted, turn capture AGC off. It winds gain up chasing a quiet
source and costs about 24 dB of noise floor. Keep the analog sidetone at its
minimum as well, or the added gain closes an acoustic loop between the earpiece
and a boom microphone.

`voice_alsa_in` and `voice_alsa_out` override the audio devices. Left empty,
they autodetect the OpenVLM card.

### Transmit shaping

A high-pass filter at 80 Hz is on by default. It sits ahead of the level meter,
so the mic dB the web UI shows is the level of what is actually transmitted. It
removes mains hum, wind, handling rumble and a boom mic rubbing on a face. A
60 Hz mains component only sees about 10 dB of cut at that corner, so raise
`voice_highpass_hz` toward 120 if hum is the actual complaint.

Setting `voice_lowpass_hz` as well turns the stage into a band-pass. Something
like 100 to 4000 Hz gives the classic walkie-talkie sound, clearer and more
cutting on some headsets and thin on others. It is off by default. Compare it on
your own headsets before enabling it.

`voice_eq=y` adds a three-band EQ with fixed centres at 100 Hz, 1100 Hz and
11 kHz, for a formant or presence lift. Under lyra the 11 kHz band is forced to
0, because lyra's raw rate is 16 kHz and that centre is above Nyquist. The
daemon logs when it does this.

Cutoffs and gains retune on the running pipeline, so `systemctl reload
mesh-voice` applies them with no gap in the audio. Adding or removing a stage
changes the pipeline's shape and rebuilds it, through the same checked path a
talk group change uses.

### Bitrate and packet size

Under lyra the daemon adapts packet size to measured receive loss and leaves the
bitrate fixed. Under loss, packets get smaller, which spends airtime to keep
each lost packet short enough for lyra's concealment to hide. It will not go
below 2 frames per packet while the worst neighbor's throughput estimate is
under 2 Mbit/s, since on a saturated link more packets makes it worse.

`voice_lyra_frames_per_packet` sets where it starts. Latency follows packet size
directly, at 20 ms per lyra frame.

End-to-end latency has never been measured. From the settings it is roughly
40 ms of packetization, 100 ms of jitter buffer, and the ALSA periods and codec
at each end, so on the order of 150 to 200 ms mouth to ear at the defaults.
Treat that as arithmetic.

### Health

The VOICE tab shows an Audio path row. A watchdog restarts a pipeline that has
stopped moving audio while still reporting itself as playing. A USB audio device
that reset under the CM108B causes this, as does a lost multicast membership.

`/run/mesh-voice.json` carries the state that row reads: `capture_idle` and
`playback_idle` (both `null` when the flow has never run at all, which means a
missing audio device), `igmp_joined`, `stalls` and `watchdog_sec`.

### Config keys

Config keys in `/etc/mesh.conf`:

| Key | Default | Meaning |
|-----|---------|---------|
| `voice` | `n` | Master enable. `n` makes the daemon exit 0 immediately |
| `voice_highpass_hz` | `80` | Transmit high-pass corner, 0-400. 0 disables the filter |
| `voice_lowpass_hz` | `0` | Upper edge, 0-8000. Non-zero switches the stage to a band-pass. Try 4000 with a 100 Hz corner |
| `voice_eq` | `n` | Add the three-band transmit EQ. Its gains then retune live on reload |
| `voice_eq_low` / `voice_eq_mid` / `voice_eq_high` | `0` | EQ gain in dB at 100 / 1100 / 11000 Hz, -24 to +12. The high band is forced to 0 under lyra |
| `voice_watchdog` | `y` | Stall watchdog. `n` disables the stall judgement only, not the systemd keep-alive |
| `voice_watchdog_sec` | `15` | Seconds without audio flowing before a pipeline counts as stalled, 5-300 |
| `voice_iface` | `br0` | Interface for the multicast group and send socket |
| `voice_channel` | `1` | Talk group, 1–32 |
| `voice_ptt` | `openvlm` | `openvlm`, `always` (open mic), or anything else for receive-only |
| `voice_unicast` | `n` | Send a userspace unicast copy to each peer. Normally unnecessary |
| `voice_unicast_max_peers` | `16` | Cap on unicast copies |
| `voice_half_duplex` | `n` | Refuse PTT while a remote node is transmitting |
| `voice_max_talkers` | `8` | Floor for the number of talkers kept ready to decode. Raised to known nodes + 2, hard cap 64 |
| `voice_beacon_sec` | `600` | Safety-net presence beacon interval. Beacons are normally sent at start-up and when a new peer appears. 0 disables |
| `voice_dscp` | `48` | DSCP marking. 48 is CS6, which reaches the 802.11 voice access category through batman-adv |
| `voice_codec` | `lyra` | `lyra` or `opus`. Mesh-wide: change it from the VOICE tab, not by hand |
| `voice_bitrate` | `32000` | Opus bitrate |
| `voice_frame_ms` | `20` | Opus frame duration: 10, 20, 40 or 60 |
| `voice_lyra_bitrate` | `6000` | Lyra rate: 3200, 6000 or 9200 only |
| `voice_lyra_frames_per_packet` | `2` | Starting packet size in lyra frames. Adapts at runtime |
| `voice_lyra_model` | `/usr/local/share/lyra/model_coeffs` | Lyra model weights |
| `voice_jitter_ms` | `100` | Jitter buffer depth per talker. Raised to two packets at start-up |
| `voice_loss_pct` | `20` | Opus in-band FEC expected-loss level |
| `voice_ttl` | `32` | Multicast TTL |
| `voice_alsa_in` / `voice_alsa_out` | auto | Override the ALSA devices. Empty autodetects the OpenVLM card |
| `voice_test_tone` | `n` | Bench mode: 440 Hz tone in, null sink out, so the transport can be proven with no audio hardware fitted |

---

## Service Elections

Every service election uses the same rule. The best-connected node wins,
measured by `MEAN_THROUGHPUT_MBPS` in the registry, which is the mean of
BATMAN_V's metric across that node's originators in Mbit/s. Nodes not seen
within 10 minutes are excluded, and ties break deterministically on MAC
address.

**mediamtx-election.sh**

Elects the node that hosts the MediaMTX streaming server. The winner takes
static IPv4 and IPv6 VIPs and runs the service. Both are released when it loses
the election.

**mumble-election.sh**

Elects the node that hosts the Mumble server, by the same rule and with the
same kind of VIPs. The winner syncs the Mumble database from the shared
Syncthing folder before starting the service, and syncs it back when it loses,
so user accounts and channel configuration survive a change of host.
Integrity-checked backups are taken before each sync.

---

## Channel Selection & Jamming Detection

**channel-election.sh**

Decentralized election for the 2.4 and 5 GHz channels. It aggregates the scan
reports every node publishes and scores each candidate on how much of the air
is already occupied, with smaller contributions from the noise floor and the
number of competing networks. A bias toward the current channel keeps the mesh
from migrating for a marginal gain. Every node runs the same computation over
the same replicated reports and reaches the same answer without a coordinator.

Occupancy is the share of a scan visit that the channel was busy with traffic
this radio did not send. It is a ratio of two counters from one driver, so it
compares directly between nodes and between radio chips. The noise floor does
not: it is uncalibrated in absolute terms and varies with the chip, which is
why it contributes a capped penalty instead of deciding the election.

A channel is disqualified when enough of the nodes reporting on it call it bad,
either too noisy or too congested. Once three or more nodes report, a single
one cannot take a channel away from the mesh, so a radio with a bad connector
or a driver returning garbage no longer costs everyone a band.

A band whose radios reported no measurements at all is a different case from
one where every candidate was measured and rejected, though both leave the
qualified list empty. Missing data holds the current channel and logs `No scan
data for any candidate channel`. When every measured candidate is disqualified,
the election takes the least bad channel it did measure and asserts limp mode.
It never moves the mesh to the lobby frequencies: those are the rendezvous
point, they are not scanned, and a node parked there stops scanning and
electing.

Channel changes are applied with `wpa_cli reconfigure`, which re-reads the
supplicant configuration in place instead of restarting the service, and the
radio is then polled until it reports the new frequency. A supplicant that does
not answer, or a radio that has not landed within 10 seconds, falls back to
restarting the unit.

**limp-mode-manager.sh**

Watches mesh consensus on jamming. When more than half the nodes report limp
mode, it drops the bitrates to the legacy 802.11 rates to keep the links up. A
minimum duration is enforced before reverting.

**quorum-checker.sh**

Detects partitioning and isolation, and returns the node to the lobby when it
cannot see enough of the mesh.

- **Solo isolation.** No mesh neighbors, but Alfred still shows active nodes.
- **Small functional island.** Keeps operating, and relies on the tourguide to
  heal it.
- **Quorum failure.** Below half the expected neighbors.

Exit code 0 means healthy, 1 means a return to the lobby is needed.

---

## Discovery & Partition Healing

**tourguide-manager.sh**

Partition detection and healing. It runs every 2 minutes, at 30 seconds past:

1. Elects a tourguide, the node with the oldest helper broadcast timestamp,
   excluding service hosts.
2. Hops one radio to the lobby frequency, alternating between 2.4 and 5 GHz.
3. Broadcasts a helper beacon carrying the current data channels.
4. Listens for other partitions.
5. Triggers a migration if the other partition should win.
6. Returns to the data channel.

Which radio hops is derived from the clock, so two split partitions hop to the
same band in the same window and can find each other. For a two-node mesh this
is the only recovery path there is, since `quorum-checker.sh` cannot rescue an
isolated node with fewer than three remembered peers.

The smaller partition migrates. Equal sizes break the tie on MAC address and
the lowest stays put. Both tourguides run the comparison in the same window and
each sees the other's MAC, so exactly one moves. The next election re-optimizes
the channel once both sides are talking again.

**ethernet-autodetect.sh**

Works out what a connected Ethernet cable means:

- **DHCP answers.** Gateway mode. NAT, advertise a default route, and
  optionally keep the AP running.
- **No DHCP.** EUD mode, bridging the port to the mesh.

In auto mode, an Ethernet gateway takes a dual role as gateway and AP, an
Ethernet EUD disables the AP because wired takes priority, and a wireless EUD
enables the AP.

---

## Network Management

**mesh-ip-manager.sh** and **mesh-ip-startup.py**

Chunk-based IPv4 allocation. Each node claims a chunk of addresses sized
`max_euds_per_node + 2`.

- **IP 0 in the chunk.** `br0` primary. The node's mesh address, and what peers
  use to reach it.
- **IP 1 in the chunk.** `br0` secondary. The DHCP gateway handed to EUDs,
  wired or wireless.
- **IPs 2 and up.** The DHCP pool for EUDs.

Both addresses sit on `br0` in the same `/24`, and `br0` bridges `bat0`, so the
mesh and the EUDs are one flat broadcast domain. The split is bookkeeping and
not isolation. `manet-ui-firewall.sh` is what separates them.

The first five addresses network-wide are reserved for services. A chunk is
claimed from those peers have not taken, and a collision is resolved by a MAC
tie-break. `dnsmasq` is configured for the pool when a node needs it.

On each boot, IPv4 allocation waits for usable `br0` link-local IPv6, active
Alfred, and the node's initial identity/telemetry publication. It then observes
BATMAN peers for 10 seconds (one Alfred synchronization period). If visible
peers still lack identity or telemetry, it waits up to 20 seconds total, then
allocates using the claims received so far. Peer changes never restart either
deadline. Transient local failures defer allocation but preserve elapsed time;
the deadline does not bypass a failed registry read or local readiness checks.
Nodes continue publishing over IPv6 throughout the wait. Startup checks run
with a 5-second loop sleep, returning to 15 seconds after allocation; work
within a loop can delay the check past its deadline.

The registry and wait state are rebuilt each boot. A remembered IPv4 chunk in
`/etc/mesh_ipv4_state` must pass the same wait and is reused only if no peer
claims it. Every allocation pass refreshes the registry, and changed claims
are published on the next manager pass instead of waiting for the keepalive.

Chunk size is uniform across the mesh and set at flash time, which is why the
management UI shows `max_euds_per_node` without letting you write it. There is
no per-node override.

**gateway-route-manager.sh**

Watches `batctl` gateway selection and points the system default route at the
selected gateway's mesh IP. Removes the route when no gateway is available.
Polls every 10 seconds.

**manet-uplink-dispatch.sh**

Owns gateway state. Decides whether an interface with carrier is an upstream
uplink or a wired EUD port, and on promotion configures NAT, the firewall,
`radvd` and the EUD services. On demotion it tears them back down. Called by
the networkd-dispatcher hooks and reconciled once a cycle by the node manager.
Both paths return early when nothing has changed.

**mesh-default-route-fix.sh**

Repairs the default route on nodes that are not the gateway. Gateway nodes keep
their own Ethernet default route and are skipped.

**usb-ethernet-watch.sh**

Brings a USB Ethernet interface, such as a tether or an LTE dongle, into the
uplink decision path when it appears. Triggered by udev.

**usb-wifi-uplink.sh**

Configures a USB Wi-Fi adapter as an internet uplink from a stored SSID and
password. The management UI drives it mesh-wide over Alfred.

**mesh-hosts-update.sh**

Populates `/etc/hosts` from the registry, so peer hostnames resolve without DNS.

**mac-to-ip.sh**

Resolves a MAC address to its IPv4 through the registry, handling both primary
and per-interface MACs.

```
mac-to-ip.sh aa:bb:cc:dd:ee:ff
```

**mesh-throughput-mean.sh**

Mean of BATMAN_V's metric across this node's originators, in Mbit/s, published
as `MEAN_THROUGHPUT_MBPS` and used by the service elections. Keeps the best
path per originator, so one peer reachable over two radios counts once.

**manet-ipcalc.sh**

Pure-bash replacement for Debian's `ipcalc`, printing the same `HostMin:` and
`HostMax:` lines the mesh scripts parse.

**verify-bridge.sh**

Operator diagnostic, not wired to any unit. Run it by hand when EUD
connectivity looks wrong. It checks the bridged EUD path end to end: that `br0`
exists with `bat0` enslaved, that the AP interface is in `br0` and not in
`bat0`, that `br0` has its chunk addresses, that dnsmasq is listening on the
right interface, and that multicast forwarding is set.

**batman-if-setup.sh**

Manages the `bat0` lifecycle. Creates the interface, sets the BATMAN_V
algorithm, and enslaves the mesh wireless interfaces while excluding the AP
interface. HaLow is added first so it becomes batman's primary, being the
longest-range link.

**batman-enslave-watch.sh**

8-second watchdog after `batman-enslave.service`. Re-enslaves interfaces that
fall out of `bat0`, and enforces `mesh_plink_timeout=0` on HaLow interfaces,
which the supplicant resets each time it rejoins the mesh.

**sae-watchdog.sh**

Watches the journal for `MESH-SAE-AUTH-BLOCKED` and restarts `wpa_supplicant`
and `batman-enslave` to recover. It acts only when `bat0` is actually missing
interfaces, so a transient block does not cause a restart storm.

**prepare-standard-mesh-iface.sh**

Readies a 2.4 or 5 GHz interface for mesh point mode before the supplicant
starts, clearing rfkill and setting the mode. Called from `radio-setup.sh`.

**unblock-wifi-rfkill.sh**

Clears soft rfkill blocks on the wireless interfaces. Called by
`radio-setup.sh`, `prepare-standard-mesh-iface.sh` and `usb-wifi-uplink.sh`.

**usb-wifi-halow-recovery.sh**

Restarts the HaLow supplicant when a non-Morse USB Wi-Fi adapter appears or
disappears, recovering from USB bus contention. Triggered by udev.

**halow-mcs-summary.py**

Reads the current TX and RX MCS rates and peer for an interface, and prints
them as shell assignments. The node managers use it to fill the MCS fields in
telemetry.

---

## File Synchronization

**syncthing-peer-manager.sh**

Discovers and configures Syncthing peers across the mesh. It checks the
registry every 60 seconds, adds newly discovered peers to the local Syncthing
config, shares the default folder with each one, and restarts Syncthing when
the configuration changes. `mumble-election.sh` uses it to replicate the Mumble
database.

---

## Time Synchronization

**gps-reader.py**

Daemon for an optional u-blox USB GPS receiver. It queries local `gpsd` on
`127.0.0.1:2947` and writes `/run/gps_status.json` every 5 seconds. It reports
`has_fix=false` when gpsd is missing, the dongle is absent, or there is no fix.

**one-shot-time-sync.sh**

Runs once on boot to set the clock. It waits for the registry, finds NTP
servers on the mesh, picks the one with the best mean throughput to it, syncs
through chrony, then disables chrony to keep the traffic off the mesh. It falls
back to the internet when no mesh server is available.

---

## Mesh Configuration Push

A change made in the Node config tab is not written straight to the other
radios. It is staged across the mesh over Alfred, acknowledged by every node,
and then applied by all of them at the same moment.

Control messages and acknowledgements are encrypted and authenticated using
the shared admin password. Mesh membership does not grant permission to send
changes. The public status page and identity/telemetry exchange remain usable
without the admin password. New admin passwords travel inside the encrypted
package, protected using the current password.

Every participating node needs the updated tools and `python3-cryptography`.
The setup script installs it; a tools update also carries
`manet-admin-setup.service` to check/install it at boot. When upgrading from an
older updater, reboot or run `sudo manet-admin-setup.sh` and restart
`mesh-status.service` before using management. Older plaintext control packets
are rejected, so upgrade the whole mesh before changing settings. Keep node
clocks synchronized for scheduled activation and command expiry.

Earlier versions included the admin password in readable config broadcasts.
If that password was exposed, provision a fresh shared password on every node
through a trusted path. Rotating it over Alfred using a known old password
does not exclude someone who already knows that old password.

### The flow

1. **Stage.** The UI writes a package and broadcasts it.
2. **ACK.** Every node validates and stages it, then publishes an authenticated
   acknowledgement. Only authenticated ACKs fill the approval table; the
   public telemetry ACK field is informational.
3. **Apply.** Press Apply once the table shows 100%. The button refuses until
   then. **Force Apply** skips that gate when a node is unreachable.
4. **Activate.** The activation time is set 60 seconds out and rebroadcast, so
   every node applies together.
5. **Trial.** A node whose dangerous settings actually change arms a rollback
   first. If its peers come back it keeps the change. If they do not, it
   restores itself. **Skip the safety net** tells it to keep the change either
   way.

An activation is recorded before applying it, so restarting a service,
rebooting, or rolling back cannot execute the same recorded activation again.
If an apply attempt fails or is interrupted, stage a new change to retry.

### When each setting takes effect

- **Per-node.** `eud`, `lan_ap_ssid` and `lan_ap_key` stay on this radio and
  never go over Alfred, because the EUD access point is a node's own Wi-Fi for
  its own clients. `max_euds_per_node` is per-node too, is set at flash time,
  and the tab shows it without writing it.
- **Safe.** `admin_password`, `mtx`, `mumble` and `auto_update` apply mesh-wide
  straight away.
- **Deferred.** `regulatory_domain` is written now and reaches the radios at
  the next boot, through the module options and the supplicant country code
  that `radio-setup.sh` writes from `mesh.conf`. `acs` selects the orchestrator
  and applies at once, with a `node-manager` restart.
- **Dangerous.** `mesh_ssid`, `mesh_key` and `ipv4_network` rewrite the
  supplicant configs and restart the supplicants, so the mesh drops briefly.

### Rollback

A wrong mesh key takes the mesh down, and with it the only route a correction
could travel, so each node has to be able to undo the change on its own.

Before a dangerous change a node snapshots `/etc/mesh.conf` and the supplicant
configs, and counts distinct peers using BATMAN's `originators_json` output.
If it had peers before the change, at least one must be visible at the
five-minute deadline; otherwise it restores the snapshot and restarts the
supplicants. A failed peer query at that deadline also triggers restoration.
`MANET_ROLLBACK_GRACE` changes that window. A successful empty query before
the change identifies a solo node, which keeps its new settings.

A failed baseline query, incomplete backup, or missing rollback helper blocks
the dangerous change unless **Skip the safety net** was explicitly selected.
An existing trial keeps its original backup and deadline; a second protected
change waits until that trial finishes. Failed preparation can be retried on
the next manager cycle while the activation message remains valid.

If restoring a file or restarting a service fails, the snapshot is kept and
restoration is retried on the next cycle, including after a reboot.

Danger is judged per node against its own current values, so re-broadcasting an
SSID a node already has does not put it into a trial window.

Configuration arriving from another node is validated before it is used. Keys
are whitelisted and values are checked for shape and length, so a peer cannot
use a config broadcast to write arbitrary supplicant configuration.

---

## Hardware Support

**manet-led-status.sh**

Shows the provisioning verdict on the board's two onboard LEDs, so a node can
be read across a bench without logging in.

| LEDs | Meaning |
|---|---|
| Green heartbeat, red off | Provisioned and ready |
| Red heartbeat, green solid | Provisioning did not complete |
| Unchanged from boot | Still provisioning, or nothing recorded yet |

It reads the same state `manet-provision-status.sh` reports on the login
banner, so the LEDs and the banner always agree. `manet-led-status.service`
runs it on every boot, and `radio-setup.sh` runs it again as soon as the
verdict is written, so the pattern survives a reboot and changes only when the
status does.

On a Raspberry Pi or CM4 these are the PWR and ACT LEDs. A board that wires
neither is left alone.

**battery-reader.py**

Reads the UPS HAT battery over I²C and writes `/run/battery_status.json`. The
node managers publish the percentage and the status page shows it. On boards
with no battery hardware the percentage is simply not published.

**button-monitor.sh**

Blocks on a GPIO interrupt and runs `led-info.sh` on each press. Near-zero CPU
when idle.

**led-boot.sh** / **led-info.sh**

Boot-progress states and an on-demand blink sequence giving the neighbor count,
both for an external LED harness driven over GPIO. These are separate from the
onboard LEDs above. Both need libgpiod v2, and the pin wiring is not finalized,
so on current hardware they are inactive.

---

## Recovery & Identity

**ssh-recovery.sh**

Keeps headless SSH reachable on a provisioned node.

**mesh-clone-identity.sh**

Detects a provisioned SD card that has been cloned onto different hardware, and
resets the identity and state that must not be shared between nodes. Without
it, two nodes would claim the same IP chunk and the same hostname.

---

## Shutdown

**mesh-shutdown.sh**

Graceful shutdown. Broadcasts a tombstone announcement carrying
`NODE_STATE=SHUTTING_DOWN` three times over 5 seconds, so other nodes treat the
absence as deliberate.

---

## Utilities

**mtx-ip.sh** / **mumble-ip.sh**

Derive the MediaMTX and Mumble IPv6 VIPs from the ULA prefix in the radvd
config, hashing the normalized /64 prefix into a stable suffix and returning
the address with a /128 mask. Every node computes the same VIP without
coordinating.

**node-update.sh**

Updates the node tools to the latest release. It checks connectivity, compares
the local and remote versions, and installs the board's tools tarball when the
node is behind. `--routine` runs silently and no more than once a day.

The networkd-dispatcher carrier hook is the only thing that calls it. There is
no cron job and no timer, so a node checks for a new release when Ethernet gets
carrier and `auto_update=` is set to a true value in `/etc/mesh.conf`. See
[networkd-dispatcher/README.md](../networkd-dispatcher/README.md).

---

## Setup & Provisioning

The first-boot stage that runs before `radio-setup.sh` is not in this
directory. It installs the apt dependencies, the install tarball, the Morse
firmware and the base networkd, nftables and radvd config. It is generated at
flash time from
[`MANET/provisioning/firstrun.sh.template`](../provisioning/firstrun.sh.template)
with your answers substituted in. See
[provisioning/README.md](../provisioning/README.md).

**radio-setup.sh**

First-boot configuration, run once from `radio-setup-run-once.service`. It sets
up:

- Interface renaming, separating the mesh, HaLow and AP radios.
- A wpa_supplicant config per interface.
- Network services: alfred, batman, radvd, chrony.
- Optional services: MediaMTX and Mumble.
- Optional GPS and NTP support through `gpsd`, `gps-reader.service` and chrony
  `SHM 0`.
- The systemd service for the node manager.

Provisioning takes several reboots and about ten minutes. Every `apt` call
continues on failure, so a missing optional package does not abort the run, but
each step that failed is recorded. If that list is not empty at the end, the
node is marked incomplete, `radio-setup-run-once.service` is left enabled so
the next boot retries, and `manet-provision-status.sh` reports it at login.

Network reachability is checked before each apt phase, so a node with no
network records that once, plainly, instead of a wall of resolver errors.

**manet-provision-status.sh**

Answers one question on every SSH login: has this node finished setting itself
up? Installed as `/etc/update-motd.d/50-manet-provision`, and runnable
directly. It also reports the outcome of your own setup scripts when any were
staged.

- **running.** A banner saying not to disconnect, with how long it has been
  going.
- **incomplete.** A loud banner listing what failed and how to retry.
- **complete.** A single line with the version and when it finished.

It prints nothing when there is no state file, and never exits non-zero.

**manet-power-status.sh**

Answers one question on every SSH login, and on the web status page: is this
board getting enough power? Installed as `/etc/update-motd.d/55-manet-power`,
read by `mesh-status.py` for `/api/local`, and runnable directly. Pass `--json`
for the machine-readable form.

- **ok.** One line.
- **notice.** Throttling has occurred, but no under-voltage.
- **warning.** Under-voltage or throttling has occurred since boot.
- **critical.** Under-voltage right now.

The carrier boards sit at the edge of their envelope with a HaLow card and a
PCIe Wi-Fi card drawing at once, and a sagging supply does not announce itself.
It presents as a radio that will not associate, a USB card that stops
answering, or a board that resets with nothing in the journal.

It decodes the `vcgencmd get_throttled` bitmask, and the sticky bits matter
most, because the event that took a radio down is over by the time anyone logs
in. On hardware without the Broadcom mailbox, such as the Rock 3A, it prints
nothing and reports `available: false`. It never exits non-zero.

**manet-user-scripts.sh**

Runs your own setup scripts, meaning the files placed in
`MANET/provisioning/additional-scripts/` before flashing. They are written to
`/var/lib/manet-user-scripts/` on the first boot and run once, after
provisioning completes.

- Scripts run as root, in `LC_ALL=C` filename order, with the working directory
  `/` and stdin on `/dev/null`.
- Each is allowed 300 seconds. `user_script_timeout=` in `/etc/mesh.conf`
  changes that.
- Every regular file in the directory is a candidate except dotfiles, the
  `.disabled`, `.bak` and `.orig` suffixes, files ending in `~`, and any file
  without `#!` on the first line.
- Scripts are executed directly, so the shebang picks the interpreter. A stock
  node provides bash, dash, python3, perl, lua and mawk, and anything else has
  to be installed by an earlier script. Exit 126 and 127 are reported with the
  name of the missing interpreter.
- Exit codes are appended to `/var/lib/manet-user-scripts.state`, one line per
  completed script. Output goes to `/var/log/manet-user-scripts.log`.
- `--list` reports what is staged and what has run. `--force` re-runs
  everything.

Failures are advisory. A script of yours that fails cannot make a working node
report itself unprovisioned. `manet-provision-status.sh` reports the tally and
names any failures on the login banner. A script that ran and failed is not
retried. One that was interrupted, by a board losing power part way through,
resumes at the script that was cut off.

Flash-time validation of these files is done by the flashers. See
[additional-scripts/README.md](../provisioning/additional-scripts/README.md).
