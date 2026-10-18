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

The orchestrator for Automatic Channel Selection mode. Home measurements and
elections run on a synchronized schedule. Registry/IP management runs every loop:

- Home RF measurement every 3 minutes, with candidate scans on RF trouble
- Status publishing to Alfred
- Registry building
- Channel elections
- Tourguide windows for partition healing
- Service elections
- Limp mode management

A disconnected node in discovery never elects itself onto data channels. With
time synchronized, it follows the rotating lobby schedule; without time, it
stays on the fixed anchors (2412/5180 MHz). Any BATMAN connection, including
HaLow, holds discovery still for authenticated recovery or joint bootstrap.
When an authenticated destination matches a visited channel, discovery ends
there without restarting that radio or hopping away at the next rotation.

Bootstrap counts distinct BATMAN originators across every mesh interface,
including HaLow. If the peer query fails, it defers bootstrap and requires a
fresh scan/publish round once the query succeeds again.

ACS works with either conventional Wi-Fi band present. Empty role files mean
that band is absent; a disabled band does not block the other one. HaLow stays
outside the channel election and lobby schedule. Nodes with only HaLow keep
publishing status and managing IP addresses without running Wi-Fi elections.

**node-manager-static.sh**

The orchestrator for static channel operation. It handles status publishing,
registry building, service elections, and IP management.

Manual Wi-Fi changes persist in `/etc/manet/static-channels.json`, including
the boot/lobby copies. Changes identify a band, so nodes with different radio
layouts follow the same plan. Manual Wi-Fi controls are disabled while ACS
owns the channels; the API and receiver enforce the same rule.

Both managers keep their one-second discovery and 15-second steady sleeps.
Registry decoding is shared within the IP pass and cached by payload; freshness
still advances on every read. `manet_ip_runtime.py` checks live
addresses, DHCP isolation, EUD readiness, and service state, then skips shell
reconciliation only after identical inputs previously reconciled successfully.
Missing files, changed claims, failed checks, and pending repairs invalidate that
shortcut. The existing channel-agreement process hosts these synchronous helper
requests on a blocking FIFO thread; its ACS loop keeps its own schedule. The
same worker serves primary-address, MCS, interface telemetry, AP-to-mesh,
MediaMTX/Mumble elections, and serving flags. Service observations allow a
one-second client-lock wait so concurrently launched elections can both reuse
the worker. Other callers remain nonblocking. Standalone calls retain one-shot
fallbacks when the worker is absent or still busy after that bound. There is
no additional daemon or polling timer. Static plan parsing and
radio-state JSON parsing also reuse unchanged inputs.

Interface publication uses `manet_interfaces.py`, avoiding web-server imports
and UI-only probes. Syncthing IDs are derived from the configured SSH account's public
certificate and cached until its metadata changes; an absent cert retries at
the next identity publish without opening a PAM session. Once a chunk is held,
failed or empty primary-address lookups defer identity publication and retain
the previous publication timer for retry. A Syncthing lookup error also defers
publication; a missing first-boot certificate is still a valid empty result.
MediaMTX and Mumble rank current candidates and check live VIPs, unit state,
and required wildcard listeners every election pass. Settled owners and
non-owners skip shell reconciliation without starting Python. Waiting
incumbents and pending announcements/publications always reconcile. Listener
checks read proc socket tables, and IPv6 VIP normalization is cached until its
configuration or derivation helper changes. Mumble database checks remain on
transitions. Measure node-manager and channel-agreement CPU together when
comparing this path.

**node-manager.sh**

What `node-manager.service` runs: a symlink to whichever orchestrator `acs=`
in `/etc/mesh.conf` selects, `node-manager-acs.sh` (automatic channels) or
`node-manager-static.sh`. `node-manager-select.sh` sets it before every
service start, so the choice follows `mesh.conf` at each boot. Changing `acs`
from the Node config tab restarts `node-manager`, which switches it.

---

## Web Interface

**mesh-status.py**

The node's only web server, on port 80. It serves an open status page and,
behind the admin password, the management UI. Sized for a phone in the field,
so nothing here needs SSH.

Phone landscape places the public topology beside the node list and uses a
side menu in management. Portrait stacks the public panels and management
tabs. Touch controls remain at least 44 pixels tall in both orientations.
Node disclosures and management tabs support keyboard navigation; the public
portrait topology divider also accepts arrow keys. If a topology refresh
fails, a visible notice identifies the retained status as old until a refresh
succeeds.

Open routes, no password:

- `/`: force-directed topology with node health, link throughput, and per-node
  detail panels. Refreshes every 15 seconds.
- `/api/data`: full mesh topology, node list, gateway status, and per-node
  throughput, as JSON. Drives the status page, and available for external
  tooling.
- `/api/local`: this node's own state, meaning the installed MANET version,
  interfaces, services, IP state, and channel info.
- `/api/peer/<ip>`: one peer's detail panel, fetched when you expand a node.
- `/api/debug`: raw `batctl` originator, neighbor, and gateway output alongside
  the registry's MAC to hostname mapping. A diagnostic dump. Nothing in the UI
  calls it.

Password-gated routes. The password is `admin_password` from `/etc/mesh.conf`,
shared across the mesh and set at flash time. Logging in sets an HttpOnly
cookie. A missing admin password disables login; radio/AP passwords do not
grant management access:

- `/manage/`: the management UI and every route beneath it, including the
  radio, measurement, voice, and uplink APIs.
- `/manage/login`, `/manage/logout`
- `/api/perf-auth`: POST a JSON `password`, set the cookie, and receive its
  `token` and `expires_in` (seconds).
- `/api/admin/*`: mesh config staging, ACK status, and apply.
- `/admin`: redirects to `/manage/#config`.

Each login creates a separate session on this node, valid for 48 hours with no
idle timeout. Activity does not extend it. Logout invalidates that session;
other devices stay logged in. Changing or removing the admin password,
restarting the web service, or rebooting the node ends all its sessions.
GPS/NTP clock corrections do not affect the deadline. When a management request
finds an expired session, the page returns to login and preserves the current
tab for after sign-in. Status pages remain available without a login.

For a client in this node's EUD pool, opening management through `manet.local`
redirects to this node's internal IP address before login. This keeps the
browser on the same node after a hotspot reconnect even if the shared name
resolves differently. Sessions remain private to one node. If its address
allocation changes, reopen `manet.local` and sign in again. API callers,
loopback tunnels, and clients outside the local EUD pool keep their origin.

Failed logins are limited to five per client address and thirty per node in
sixty seconds. Throttled requests return HTTP 429 with `Retry-After`;
existing sessions remain usable.

Public writes are limited to this radio's operator label and the ATAK phone
choices described below. They require a client address in this radio's DHCP
pool and bounded same-origin JSON. Configuration and radio controls require
the admin session.

Every HTML response, including login and error pages, carries a Content
Security Policy. Scripts need a fresh response nonce, and controls use event
listeners instead of inline handlers. Pages use local assets and system fonts,
with no CDN scripts or map tiles. The policy permits inline styles for the
existing layouts, blocks embedding and plugins, and limits requests and forms
to this node. Responses also disable content-type sniffing and send referrers
only to the same origin.

Access control has two layers:

- **Application.** Every route is restricted to localhost and the mesh/EUD
  subnet (`ipv4_network`), and management routes additionally require the
  cookie.
- **Kernel.** `manet-ui-firewall.sh` limits port 80 to localhost and this
  node's DHCP address pool. This is an intentional convenience boundary for
  the trusted team; management actions still require the admin password.

The server accepts at most eight concurrent connections, with a ten-second
idle read timeout and a ten-second total body deadline. Request bodies are
limited to 64 KiB. At most two status collections run together; browser tabs
share results for five seconds (two seconds for voice and config status).
An overloaded server returns HTTP 503 and asks the browser to retry after five
seconds. Only one management POST can run at a time. Background tabs stop
polling, and visible tabs wait for an outstanding poll before starting another.

Measurement batches allow up to 64 pairs and seven test types, with 5-300 seconds
per throughput test and a one-hour total execution budget including command
timeouts. Only this node can be the source. Results record its executing
hostname and address, including in CSV exports. Ping succeeds only when at
least one reply arrives. A second measurement cannot start while one is running.

Expand **Mesh recovery** on the management page or in this node's status panel
for observed and agreed Wi-Fi channels, acknowledgement progress, recovery holds,
the last verified clock source, peer metadata age, and the last automatic Wi-Fi
switch. Reachability covers all radios: a working route through HaLow still
counts as a connected mesh. A ready HaLow interface alone is not proof of a peer
connection. Missing or stale observations are labelled accordingly. These
details come from local files and the existing registry, with no new mesh
announcements.

Link quality is BATMAN_V's metric, which is throughput in Mbit/s and not a
0-255 link quality. `batctl` prints 43.2 for a 43.2 Mbit/s link. The color
thresholds are 30, 15, and 5 Mbit/s, set so a healthy HaLow link reads as good.
HaLow tops out near 43 Mbit/s at 8 MHz.

What the page reads:

- `/var/run/mesh_node_registry`: the peer registry
- `/etc/mesh.conf`: node configuration
- `/etc/mesh_ipv4_state`: current IP allocation
- `batctl o`, `batctl n`, `batctl gwl`: live batman-adv state

Peers are never queried over HTTP. Everything shown about another node comes
from the registry, which is built from Alfred.

**Operator names.** Connect to this radio and enter an optional name or callsign
in **Your operator name** on the public status page, then press **Save**. No
admin password is needed. The public node list and management topology show it
alongside the hostname. Clear the field and save again to remove the label.
Anyone connected to this radio can replace the name when it changes hands.
Names are limited to 32 UTF-8 bytes and apply
locally without a mesh ACK or restart. Both static and ACS modes share the
name in their next telemetry publication, including ACS discovery. Peer pages
update after receiving that publication. This label does not change ATAK phone
callsigns or which phone controls location.

`POST /api/operator-name` accepts only `{"operator_name": "..."}` and writes
that local key atomically. It shares the management write lock and invalidates
the status cache. It cannot target a peer or change other settings. The local
DHCP-pool and same-origin JSON checks apply even without an admin password
configured or ATAK enabled. The browser keeps unsaved edits during polling.

**manet_manage.py**

The management UI, reachable under `/manage` after the password check. Tabs:

- **Topology.** Mesh nodes and their interfaces.
- **Radio config.** Interface up/down, TX power, HaLow and Wi-Fi channels.
- **Measure.** iperf3 and ping runs from this node toward a peer.
- **Sessions.** Saved measurement results, as JSON and CSV.
- **Uplink.** Node-local USB or built-in Wi-Fi client, scan list and connection status.
- **Voice.** Talk group, codec, and per-peer voice state.
- **Node config.** EUD mode, AP credentials, mesh SSID and SAE key, IP range,
  regulatory domain, services, and admin password, with the per-node ACK table.

Anything that affects other nodes is staged over Alfred and applied once peers
acknowledge it. Nothing is pushed node to node over HTTP. Measurement is the
exception and needs no coordination, since it runs a local `iperf3` or `ping`
client against the peer's always-listening daemon.

**Radio settings**

The HaLow channel plan follows the node's region. A bandwidth is offered only
where the region defines a channel of that width and the HaLow supplicant
accepts it. US reaches 8 MHz. EU is 1 MHz only: its 863-868 MHz allocation
is too narrow for 4 or 8 MHz, and the supplicant refuses EU 2 MHz. New US
nodes start on channel 10 (907 MHz, 2 MHz), the best balance of range and
reliability. New EU nodes start on channel 1 (863.5 MHz, 1 MHz). The Radio config tab offers only widths the region can use.

Mesh radios, HaLow and Wi-Fi alike, start at 30 dBm in every region. MANET
sets no lower ceiling of its own; the card's firmware and hardware apply
their own limits. Wi-Fi TX power options run up to the highest power the
radio's channels advertise. After a change the radio's reported power is read
back and shown. That is the driver's report, not a measurement of RF output,
and it can be lower than the request when the card limits itself. A requested
reduction that does not take is reported as a failure.

Wi-Fi power changes apply to the interface's PHY. A change is refused if
another UP interface shares that PHY or the PHY cannot be identified. UI
power selections are not saved for radio restarts or reboots; the boot mesh
power service requests 30 dBm again.

HaLow power is shown but cannot be changed from the UI. The Morse driver sets
it when the radio starts, and reloading the driver can wedge the USB card, so
a change needs a reboot. The bench MM8108 firmware reports 24 dBm at 1 and
2 MHz, 22.25 dBm at 4 MHz, and 22 dBm at 8 MHz.

The standalone EUD access point is the exception and stays at 5 dBm. When
sharing its PHY with a Wi-Fi uplink, it uses the client PHY power policy.

Wi-Fi uplink selection and credentials are also local admin actions. They
withdraw a local radio role without sending the credentials to other nodes.

**mesh-radio-state.py**

Applies radio changes that were staged over Alfred. Reads the package,
publishes an acknowledgement, and applies at the package's activation time once
the coordinator has collected the rest. Carries interface up/down, TX power,
HaLow and Wi-Fi channel, and voice codec. Uplink credentials are rejected by
this transport and remain on the node.

**manet_peer_radios.py**

Builds the per-peer radio chips and the expandable panel the topology view
shows for another node: role, up and down state, channel, MCS, service pills,
and the inferred `bat0`, `br0`, and gateway rows. Everything comes from what
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
(`239.192.41.1`) and differ by port: group *n* uses `38801 + (n-1)*2`.
The next port is reserved for RTCP, but the current pipelines send and receive
RTP only.

### Codec

`voice_codec` selects the codec, `lyra` by default or `opus`. The two are not
compatible. If you change to opus, every node must have the matching setting or
voice will not work between them.

Codec is therefore a mesh-wide setting. The CODEC card in the VOICE tab stages
the change over Alfred, waits for every node to ACK it, and switches the whole
fleet on a common clock about 20 seconds out. If any node fails to ACK, the
change is canceled and nothing moves. Each node restarts mesh-voice on
activation, because the encoder, payloader, and RTP clock rate all change
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
removes mains hum, wind, handling rumble, and a boom mic rubbing on a face. A
60 Hz mains component only sees about 10 dB of cut at that corner, so raise
`voice_highpass_hz` toward 120 if hum is the actual complaint.

Setting `voice_lowpass_hz` as well turns the stage into a band-pass. Something
like 100 to 4000 Hz gives the classic walkie-talkie sound, clearer and more
cutting on some headsets and thin on others. It is off by default. Compare it on
your own headsets before enabling it.

`voice_eq=y` adds a three-band EQ with fixed centres at 100 Hz, 1100 Hz, and
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
missing audio device), `igmp_joined`, `stalls`, and `watchdog_sec`.

Transmit stays blocked until the send socket is bound to `voice_iface` and
its IPv4 address. The existing peer poll checks every 30 seconds and rebuilds
the pipelines when that address appears or changes. `tx_blocked` gives the
reason in the state file and the Audio path row; `bind_address` records the
selected address. A held PTT resumes after binding, subject to half duplex.
If capture cannot open and no send socket exists, `tx_blocked` reports the
transmit pipeline's audio-device error. Socket binding is checked once the
socket exists.

Receive state has separate limits: at most 128 admitted RTP sources, 64
parked sources, and the decoder limit set by `voice_max_talkers`. A sender
address can retain four source identities. New identities use a burst allowance
of two per address, replenished by one every 30 seconds, and a shared burst of
64, replenished by one per second. Established sources pass immediately.
An ordinary restart and a newly arriving talker need no admission wait.
Repeated rapid restarts, a full table, or traffic exceeding these allowances
can prevent a new source from being heard until capacity returns.

Inactive sources expire through GStreamer's native timer after a nominal
15-minute idle threshold, on its next maintenance pass. This preserves normal
conversation gaps. The default 10-minute muted beacon refreshes present peers
before expiry. Disabling beacons or setting them longer than the retention
interval can bring back the existing first-contact clipping after long silence.
Parking overflow retires the evicted source until it expires or the sender
uses a new identity. These limits add no packet queue or PTT holdoff.

`/run/mesh-voice.json` also reports `rtp_sources`, `rtp_source_limit`,
`rtp_admission_drops`, `talkers_parked`, and `parked_limit`. Source names come
from UDP sender addresses matched to the registry. Each daemon run uses a
random 32-bit SSRC so a restart is unlikely to reuse retained sequence state.

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
| `voice_beacon_sec` | `600` | Presence refresh interval, also covers missed peer arrivals. Beacons also run at start-up and on new peers. 0 disables |
| `voice_dscp` | `48` | DSCP marking. 48 is CS6, which reaches the 802.11 voice access category through batman-adv |
| `voice_codec` | `lyra` | `lyra` or `opus`. Mesh-wide: change it from the VOICE tab, not by hand |
| `voice_bitrate` | `32000` | Opus bitrate |
| `voice_frame_ms` | `20` | Opus frame duration: 10, 20, 40, or 60 |
| `voice_lyra_bitrate` | `6000` | Lyra rate: 3200, 6000, or 9200 only |
| `voice_lyra_frames_per_packet` | `2` | Starting packet size in lyra frames. Adapts at runtime |
| `voice_lyra_model` | `/usr/local/share/lyra/model_coeffs` | Lyra model weights |
| `voice_jitter_ms` | `100` | Jitter buffer depth per talker. Raised to two packets at start-up |
| `voice_loss_pct` | `20` | Opus in-band FEC expected-loss level |
| `voice_ttl` | `32` | Multicast TTL |
| `voice_alsa_in` / `voice_alsa_out` | auto | Override the ALSA devices. Empty autodetects the OpenVLM card |
| `voice_test_tone` | `n` | Bench mode: 440 Hz tone in, null sink out, so the transport can be proven with no audio hardware fitted |

---

## Service Elections

MediaMTX and Mumble use the same election rule, applied by one shared helper,
`mesh-service-election.py`. The best-connected node wins, measured by
`MEAN_THROUGHPUT_MBPS` in the registry, which is the mean of BATMAN_V's metric
across that node's originators in Mbit/s. A node that announced its shutdown,
or that this node has not heard from for 5 minutes, cannot win or keep the
service. The current host gets a small bonus so normal metric jitter does not
move the service. Ties break deterministically on MAC address.

Service elections start after address allocation and run at most once every
fifteen seconds. Their locks prevent overlapping local runs. Separate network
partitions may each elect a server; they converge after connectivity returns.

**mediamtx-election.sh**

Elects the node that hosts the MediaMTX streaming server. The winner starts
the service and checks its listeners before taking static IPv4 and IPv6 VIPs.
It sends three unsolicited ARPs and an IPv6 Neighbor Advertisement. A losing
host keeps serving until it hears the winner claim the IPv4 VIP by ARP or
the IPv6 VIP by an unsolicited NA, or receives a new registry observation
showing the winner serving. The release log names the signal. NA supports
handover when batman-adv DAT absorbs gratuitous ARP. At the start of each wait,
the incumbent also sends an IPv6 Neighbor Solicitation to each winner MAC
alias, using its own link-local source address. A solicited reply confirms a
claim that preceded the wait, logged as `via NS probe`. A failed or disabled
successor never causes the incumbent to withdraw merely because it lost rank.
Failed candidates are stopped and reset after the readiness deadline so
unit restarts cannot continue between election passes. The next election
retries. Enable each elected service on every node in the mesh.

**mumble-election.sh**

Elects the node that hosts the Mumble server, by the same rule and with the
same kind of VIPs. The winner imports the Mumble database from the shared
Syncthing folder before starting the service. While serving, it exports changed
working data at election cadence. The resident runtime checks DB and WAL file
metadata, so unchanged data adds no process starts or SQLite queries.
Successful cadence exports log one summary line. Failures retain full details.
Integrity-checked backups are taken before each sync. On release, the stopped
owner exports its final data to a unique file under `Sync/mumble/retired/`,
leaving the winner's canonical snapshot alone. Failed final exports retry on
the next election. After a successful final export, pruning keeps at most
three snapshots for that owner and removes those older than 30 days, always
protecting the snapshot just written. Mumble uses the same confirmed handover
as MediaMTX, with a valid working database and listening
TCP/UDP ports required before claiming its VIPs. Syncthing remains asynchronous:
changes since the last delivered cadence export may be absent on the new host.
Late snapshots and Syncthing conflict copies are not automatically re-imported
or merged. The retired snapshots preserve final data for manual recovery.

---

## Channel Selection & Jamming Detection

Both Wi-Fi mesh bands use 20 MHz channels. On 5 GHz this favors range and
lets the MT7916 third-chain observer, background CAC, and DFS moves use the
same primary channel width. Bench throughput at 20 MHz was 137 to 178 Mbit/s
between nodes, not a guaranteed field rate. HE and VHT rates remain enabled.
ACS scores individual 20 MHz primaries, and DFS centers equal those primaries.

**channel-election.sh**

Requests an election round from **mesh-channel-agreement.py**, whose service
starts with either node manager. `channel-election.sh --score` only scores a
proposal; it never changes a radio. The lowest ready ACS node in the discovered
connected component coordinates one plan, using the reachable members' registry
scan reports and a shared incumbent. Connectivity includes **every BATMAN radio**:
Wi-Fi groups linked through HaLow remain one network and do not elect separate
Wi-Fi channel plans. Cached reports from unreachable groups are excluded.
Peers ACK that exact plan instead of independently choosing channels from
potentially different reports.

The current channel receives a ten-point score preference. To avoid moves from
brief load swings, the coordinator also requires another channel to beat it in
three consecutive three-minute rounds before proposing a preference move.
Each band counts independently, and different challengers can win those rounds.
An allowed data-channel incumbent with missing home telemetry stays put until
a valid comparison arrives. Missing data alone never triggers an urgent move.
An incumbent that is disqualified, outside the candidate list, absent, or
above the limp-mode score threshold bypasses this hold-down when
there is a measured winner. A same-boot service restart keeps the streak.
A new coordinator or boot starts fresh. Peers still follow the frozen proposal
without re-scoring.

Scheduled scans, elections, and tourguide duty wait for the node's first
verified GPS/NTP sync. Static and HaLow-only nodes also wait before voting.
Discovery, IP allocation, and normal mesh traffic continue while waiting; a
node without any time source defers timed decisions. Authenticated cold recovery
over a surviving mesh link or a lobby encounter remains available as described below.

The coordinator freezes the participant list and collects ACKs for **60 seconds**.
Before scoring, the coordinator waits for enough fresh ready participants for
a majority of the full membership. It checks target compatibility after
scoring. Unready and unknown participants remain in the denominator, and
ineligible rounds do not advance the preference streak.

A strict majority permits a commit, with activation **30 seconds later**. Missing
ACKs do not extend the deadline or shrink the participant list. Without a
majority, the attempt expires and the next three-minute round discovers peers
afresh. Two participants require two ACKs; three require two. Unavailable status
still counts in the denominator, and a failed peer query never means solo.

Votes and commits use the shared admin password on encrypted Alfred type 74.
Votes survive process restarts and bind a single plan per round. Static nodes
ACK only compatible plans that leave their own bands unchanged; HaLow-only
participants do not propose Wi-Fi changes. Unsupported candidates cause a node
to abstain. The plan covers at most 64 canonical node identities.

After a move, a **26-minute election hold** allows two twelve-minute rendezvous
cycles plus an exchange window. The current plan remains recoverable afterward: a reachable
peer operating that plan advertises a fresh authenticated destination through
the existing type-74 exchange. A follower already moved by a peer CSA also
persists the target configuration and takes the moving plan's recovery hold
once. Repeated advertisements and same-boot restarts do not extend that hold.
Matching live channels never receive a second CSA just to repair config.
Ordinary committed plans already landed by peer CSA remain valid only on the
same boot and within their original activation grace.

Ordinary and DFS transitions log the plan identity, round, proposer, targets,
vote quorum, readiness, and remaining recovery hold. Expiry records distinguish
membership expansion, changed local snapshots, missing majority, missing
commit, and missed activation. Adoption logs old and new live/configured MHz.

One publisher per plan carries the full majority
certificate; other holders send its ID, with automatic publisher failover.
A new or straggling node follows over HaLow or any surviving path without a
lobby visit. Only mismatched Wi-Fi radios change; HaLow is left in place.

When truly isolated groups reconnect with competing plans, the hold is lifted
and the next common scan/election reconciles their shared observations. A newer
majority plan covering the entire reachable membership can recover stragglers;
an island's newer timestamp alone does not win. Expanded membership cancels an
uncommitted island proposal; an already issued commitment remains binding.
Being on the agreed channels without Wi-Fi peers can simply mean being outside
Wi-Fi range: a healthy HaLow connection does not trigger another channel move.
Disconnected nodes rejoin through the authenticated tourguide path below. Missed final delivery
can still leave stragglers: this is bounded agreement within a discovered
partition, not an atomic switch across unreliable links. It requires aligned
clocks. Firmware/radio validation on CM4 is still required.

Each ACS cycle measures the home channel over the interval since the previous
cycle, usually about 180 seconds. It reports total busy airtime, foreign
airtime, noise, the real window length, and a per-band RF trouble flag. Foreign
airtime subtracts this radio's transmit time and summed peer receive duration
from busy time. Overheard unicast between two other mesh nodes still counts
as foreign. Windows shorter than 60 seconds are rejected. A counter decrease,
channel or interface change, or manager restart starts a new baseline.

RF trouble starts after two consecutive windows with foreign airtime at least
60% or noise at least -80 dBm. It clears after three consecutive windows with
foreign airtime below 50% and noise below -85 dBm. The airtime threshold is
calibrated on 2.4 GHz; its 5 GHz use and the noise threshold remain provisional. Each band scans candidates only
while this node or a member of its connected ACS view reports trouble on that
band. Healthy bands make no off-channel scan visits. Lobby bootstrap,
tourguide, and discovery visits keep their existing behavior.

An invalid measurement window breaks the consecutive-window streak but does
not clear an active trouble flag. When the home frequency is known, the report
still carries that flag without inventing airtime or noise measurements.

When the gate is open, 2.4 GHz candidates get one 250 ms remain-on-channel
visit each. Data mode performs no 2.4 GHz flush scan and reports zero BSS
counts for both the home channel and candidates. Occupancy and noise capture
co-channel networks' real airtime without charging candidates a BSS penalty
that the home channel never pays. Removing the scan also removes a failure
seen on both bench MT7916 cards: about 10 to 13% of 2.4 GHz scans timed out
after five seconds without a results event, skipping all ROC visits that cycle.

Occupancy comes from the visit's busy and transmit counter differences divided
by its active time. The ROC command returns before the visit ends, so the
manager waits the requested dwell plus 150 ms before reading the post-survey.
Visits with less than 250 ms of measured active time are dropped. Each valid
long-dwell result is published immediately. On an MT7916 with its third RX
chain reserved, 5 GHz candidates use a 250 ms background energy measurement.
The main radio stays on its channel. The estimate corrects for the receiver
being blanked during local transmissions. Home and candidates have zero BSS
counts on this path; candidate noise is unmeasured. Windows with at least 10%
local transmit time are omitted: heavy local TX biased quiet-channel estimates
on the reservation bench. This provisional limit preserves low-TX controls;
it is not a calibrated correction for heavier load. Other invalid windows are
also omitted. With the matched DFS kernel, the passive observer also measures
DFS candidates whose live PHY flags permit listening, excluding disabled
channels and channels in a radar non-occupancy period. Every connected node
must support the candidate, and at least one fresh occupancy measurement is
required to score it. Noise or network counts alone cannot elect a DFS channel.
A background CAC owner keeps the receiver and supplies its own read-only
occupancy sample. If receive-path reservation is deferred, the observer omits
the sweep without falling back to a main-radio scan.

Without an observer, 5 GHz keeps its main-radio flush scan behind the same
gate, including its three-sample occupancy warmup. That scan includes the
home channel and supplies BSS counts for both home and candidates. A failed scan discards candidates and leaves the home BSS count
at zero while preserving valid home measurements. Lobby bootstrap and discovery
keep their flush scans and BSS counts. Observations older than three minutes
are omitted from telemetry.

Long home windows on MT7916 measured 29 to 31% busy where short scan brackets
varied from 35 to 62%. Candidate software scans spent about 75 ms off-channel,
with 330 to 620 ms traffic stalls. A 250 ms remain-on-channel request measured
273 ms of active time with a 486 ms stall. No loss was observed in that bench
measurement. The
[channel-scoring reference](../../docs/node-tools-internals.md#channel-scoring)
explains the counters, driver behavior, and remaining hardware validation.

The incumbent is scored on foreign airtime, while candidates use dwell
occupancy. Total home busy airtime remains visible without treating this
node's own mesh traffic as interference. Noise contributes a weighted penalty
because its absolute value varies between radio chips. BSS counts add half a
point per network on the 5 GHz fallback scans and bootstrap scan paths. They
contribute nothing on 2.4 GHz visits or 5 GHz observer measurements.

A channel is disqualified when enough of the nodes reporting on it call it bad,
either too noisy or too congested. Once three or more nodes report, a single
one cannot take a channel away from the mesh, so a radio with a bad connector
or a driver returning garbage no longer costs everyone a band.

With no measured alternative, the election holds the current channel without
urgency or limp mode. A bad incumbent waits for candidate data before an
urgent move is possible. Once alternatives are measured, disqualification and
limp rules use the incumbent's foreign airtime. When every measured channel
is disqualified, the election takes the least bad measured channel and asserts
limp mode. Lobby frequencies remain reserved for rendezvous.

Channel changes first use mesh CSA, then poll the radio for the new frequency.
An ordinary non-DFS move falls back to `wpa_cli reconfigure` if CSA fails,
and restarts the unit if reconfiguration fails. Landing checks allow twenty
reads with half-second sleeps, plus command time. A non-DFS radio that has not
landed then gets one restart and another check sequence. DFS entry never uses
a supplicant restart as a fallback. A failed DFS landing takes the interface down.
A 30-second settling
period suppresses isolation handling while links re-form. Ordinary status/IP
work continues during the agreement wait; scanning and tourguide hops wait.

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

Exit code 0 means stay put, 1 means a return to the lobby is needed, and 2 means
the peer query failed. The orchestrator holds its current channels on a failed
check and retries on its next loop. Counts come from BATMAN's JSON originator
table; multiple routes to the same originator count once.

---

## Discovery & Partition Healing

**tourguide-manager.sh**

HaLow takes priority over Wi-Fi tourguide visits. A node with a working S1G mesh
radio skips guide duty and keeps its Wi-Fi radios participating on data channels.
The local check uses the `halow_if` role, enabled/up state, active BATMAN attachment,
running S1G supplicant, and a responsive joined mesh. It does not require a current
peer: the operating assumption is that HaLow reaches at least as far as Wi-Fi.
Missing, disabled, unjoined, or failed HaLow restores Wi-Fi fallback automatically.

Readiness rides existing encrypted type-74 status, so a mixed group's election
can exclude nodes using HaLow and select a guide without it. Local readiness is
checked again just before departure. No extra network announcement is added.

Fallback visits run every 2 minutes, at 30 seconds past; connected-node recovery
above does not wait for them. `manet_rendezvous.py` defines their common rotation:

| Band | Frequencies, MHz (channels) | Revisit of each frequency |
|------|----------------------------|---------------------------|
| 2.4 GHz | 2412 (1), 2437 (6), 2462 (11) | 12 minutes |
| 5 GHz | 5180 (36), 5220 (44), 5745 (149) | 12 minutes |

Guides alternate bands every 120 seconds; the per-band entry is
`floor(UTC epoch / 240) modulo 3`. A synchronized disconnected searcher keeps
each enabled band on that entry until the next four-minute boundary. A
clockless searcher stays on its fixed anchors, which remain in the rotation.
Nodes with working HaLow skip elected guide duty. HaLow never participates in
the rotation. Fully disconnected searchers retain the discovery behavior above;
any surviving BATMAN path holds them still for recovery or joint bootstrap.

1. Elects a tourguide from reachable BATMAN originators mapped to Alfred node
   identities. Candidates need an active radio on the scheduled band, must
   not advertise working HaLow, and must not be internet gateways. The
   oldest helper timestamp wins, then lowest MAC. Service hosts are excluded
   when another eligible node is available.
2. Visits the scheduled band/frequency, staying put if it is already its data channel.
3. Repeats authenticated helper beacons carrying the original data channels.
4. Listens through a common lobby dwell ending at second 75 of the window.
5. Triggers a migration if the other partition should win.
6. Returns to the data channel.

Which radio hops is derived from the clock, so two split partitions hop to the
same band in the same window and can find each other. For a two-node mesh this
is the fallback when HaLow is unavailable and no other band remains connected, since `quorum-checker.sh` cannot rescue
an isolated node with fewer than three remembered peers.

A node missing the scheduled band or permission to initiate on its frequency
skips that window. It never substitutes or renumbers entries. Disabled,
non-initiating, and DFS frequencies are excluded using the actual PHY report.
Missing peer identities or failed peer queries defer election. The existing
dwell is retained pending CM4 switch, peering, Alfred, and traffic measurements.

Discovery is explicit in `/run/manet-rendezvous.json`, surviving daemon restarts.
Frequency alone no longer identifies a lobby: rotating entries can also be data
channels. Fresh authenticated type-74 plans, type-75 helpers, or nonce-bound cold
replies end discovery even when already on the destination. Only mismatched
radios change. Radio setup and boot anchor restoration reset discovery state.

Once synchronized, lobby adoption and partition comparison require encrypted
type-75 beacons no more than 45 seconds old. The largest compatible partition wins
consideration; stale cached records and unsigned type-69 telemetry cannot move
a radio. Partition comparison excludes members captured before the hop.

A node that has never synchronized cannot check a beacon's wall-clock age. It
instead sends an encrypted challenge when BATMAN peers are present, at most
once per minute, whether its Wi-Fi radios are on lobby or old data channels.
A synchronized holder of the current plan can answer over HaLow without a tour.
The lowest reachable compatible source answers; tourguides also answer during
their existing visits for otherwise disconnected nodes.
The reply must match that node's current boot and random challenge, arrive
within 60 seconds measured locally, and name compatible channels. The challenge
is consumed before returning a destination. This lets a cold node rejoin and
reach time sources without accepting cached recovery instructions. It does not
set its clock or enable timed elections; GPS/NTP must still synchronize it.
These exchanges use Alfred types 76–77 and stop once synchronization succeeds.
The daemon can apply a consumed cold reply directly under the shared channel
lock, without enabling timed agreement or changing any HaLow radio.

The smaller partition migrates. Equal sizes break the tie on MAC address and
the lowest stays put. Both tourguides run the comparison in the same window and
each sees the other's MAC, so exactly one moves. The next election re-optimizes
the channel once both sides are talking again.

Partition size counts distinct BATMAN originators plus this node. The helper
beacon and partition comparison use the same channels and size captured before
the hop; the temporary lobby topology does not redefine the original partition.
An unavailable peer table skips the hop before a helper beacon is built. A
beacon may carry only one band, and receivers adopt only shared, enabled bands.

Tourguide and channel election share a local lock. The orchestrator waits while
a hop is in progress, so it does not mistake a temporary single-radio hop for
isolation or a return to lobby operation.

**ethernet-autodetect.sh**

Works out what a connected Ethernet cable means. Automatic callers pass
`--iface end0` explicitly. A manual call defaults to `end0`, never the saved
Wi-Fi/USB uplink, and wireless targets are rejected. A usable existing lease
is reconciled without restarting DHCP on that connection. Gateway and EUD
service policy is handed to the common uplink dispatcher under the same lock.

- **DHCP answers.** Gateway mode requires an interface-bound HTTP 204
  connectivity check. A lease alone does not enable NAT or advertise a gateway.
- **No DHCP.** A single device requesting an address is treated as a wired EUD
  after about five seconds. An inconclusive or silent connection falls back to
  wired EUD after twenty seconds. Router traffic, switch traffic, or multiple devices
  keep the port unbridged without serving DHCP onto the attached network.

In auto mode, an Ethernet gateway takes a dual role as gateway and AP, an
Ethernet EUD disables the AP because wired takes priority, and a wireless EUD
enables the AP.

With automatic channels (`acs=y`), auto mode serves the AP only on a gateway.
The AP radio is normally the 5 GHz mesh radio. While it serves the AP, the
gateway meshes on its other radios and HaLow, follows agreed channel moves, and
keeps the AP radio's mesh configuration on the agreed channel. When the last
uplink is unplugged, the radio returns to the mesh on the agreed channel. Gateways never act as tourguide.

---

## Network Management

**manet-radio-names.py**

Finishes MAC-pinned radio naming before networking starts. When udev cannot
exchange occupied `wlan` names, it moves the affected radios aside, assigns
their intended names, and retries their device events. The boot service runs
before networkd and supplicants so radio roles do not depend on driver order.

**manet-os-cleanup.py**

Previews or applies the Raspberry Pi OS Trixie footprint cleanup. Setup runs it
once before operator scripts. It protects MANET runtime packages, previews APT's
removal plan, refuses removals of protected dependencies, and then clears the
inactive stock swap file and downloaded package cache. See the provisioning
README for manual use and the saved package inventory.

Profile 2 also stops and masks unused system/user services and timers. It
removes PulseAudio and rtkit and masks the unused user D-Bus units. The
`dbus-user-session` package stays installed to preserve GStreamer dependencies.
Voice continues to use ALSA.
It preserves system D-Bus, polkit, per-radio supplicants,
credential-agent sockets, and the iperf3 server used by the measurement UI.
Custom cron jobs or LVM tools/volumes preserve their maintenance services.
The preview includes the unit inventory and reasons; apply records each action.
Profile 1 installations advance to profile 2 on the next explicit apply/setup.

**manet-atak.py** and **manet-cpu-sample.py**

The ATAK service runs by default on every node for wired phones and phones on
its enabled Wi-Fi access point. Set `atak=n` in
`/etc/mesh.conf` and restart `manet-atak.service` to disable it; only explicit
`n`, `no`, `0`, or `false` (case-insensitive) disables the daemon and its firewall.
Fresh installs enable and start it; updated packages enable it for the next boot.

Every MT7916 node defaults to the positioning-patched 2024 WM with stock
2024 WA/ROM at the next boot when the installed driver has `manet_timing=1`.
`positioning=` controls ranging only: changing it does not select firmware or
retry a failed patch. `manet-positioning.service` ships but remains disabled
and default off. A driver without the timing tag uses the pinned unmodified
2024 firmware set. An unconfirmed patched boot also falls back to that pinned
stock set on the following boot; the journal reports `AUTO-DISABLED`. After
addressing the cause, run `sudo manet-mt7916-firmware.py --rearm` to retry on
the next boot. An update with a different patched WM hash also rearms the
trial. Reinstalling the same hash leaves the latch in place.

On CM4, ACS starts with all three 5 GHz receive paths. When the local RF
trouble flag is set, `manet_rx_policy.py` requests two receive paths plus the
observer through the kernel's runtime transition. A CAC/hold owner keeps the
reservation after trouble clears. Once both clear, the controller requests
three paths again. Transitions are at least 60 seconds apart. Busy or throttled
transitions are retried on a later measurement cycle without stopping another
receiver owner. Peer trouble alone does not reserve this node's third path.
Static mode also requests three paths, preserving any active kernel hold.
The managed boot reservation is removed on deployed boards so the runtime
transition can restore the native EEPROM configuration.

This requires the paired kernel's `rx_transition_probe` interface and currently
supports HT20 mesh operation only. Rock 3A and RPi5 do not enable it. Main-link
receive throughput can fall by up to a third while reserved. Candidate samples
with at least 10 percent local transmit time are discarded.

DFS mesh selection is enabled on CM4 when the loaded driver advertises
`dfs_mesh_v1`, cfg80211 exposes the matching transfer ABI, and the live PHY
reports DFS flags. The running mesh supplicant must implement the paired DFS control ABI, and
the independent radar guardian must report ready. Every connected node must
support it. Tools alone cannot
open the gate. The coordinator selects a measured DFS candidate and the best
common non-DFS fallback, then distributes a bounded CAC intention through
authenticated Alfred. Every node reserves its receiver, completes background
CAC, and keeps a live hold. Only then can all nodes approve a timed channel
plan binding their exact receiver epochs and generations.

During preparation and the entire pending plan, the agreement daemon refreshes
clearance each one-second pass so Alfred's ten-second synchronization fits
inside the unchanged 15-second lease. It reads authenticated peer status again
before deciding and before activation. A recently expired peer observation may
wait up to five seconds for replacement, bounded by the original apply window.
That wait publishes no vote or commitment and cannot authorize activation.
Local clearance loss, changed membership or receiver identity, and radar still
reject the plan immediately.

The selected candidate and measured fallback stay fixed through CAC. A busy
kernel snapshot keeps ordinary status publication working, with DFS capability
and clearance unavailable for that pass. Pending preparations and watcher
rechecks wait for a fresh read, without renewing a
clearance lease. Missing peer observations retain an unexpired preparation
but cannot authorize a move. The Wi-Fi supplicant needs the MANET background
CAC and external-radar patches so these events leave the existing mesh and
SAE state intact. Radar still marks the channel unavailable, while MANET
owns the fallback choice and the kernel enforces NOP.
The complete path passed secured-mesh acceptance with the real supplicant
on CM4 and Rock 3A. Radar on a hand-set DFS channel has not passed yet.

Each participant initiates mesh CSA at the agreed time. DFS entry includes
`NL80211_ATTR_HANDLE_DFS`. RDD2 remains active until the main detector starts
and cfg80211 accepts the exact token transfer. The channel stays AVAILABLE
through background teardown. Main-channel radar keeps the transferred token
and reports `main-radar` through the kernel latch, alongside nl80211 events.
The independent watcher requests a three-beacon block-TX CSA to the agreed
fallback before writing an event or publishing over Alfred.
Native `owner=0` BSSs retain their own evacuation, including after a BSS restart
leaves an old contract on disk. Unknown ownership still requires fencing.
Accepted CSA has ten seconds from its request to land with TX blocked. The
watcher also records kernel switch-start notifications for the agreed fallback,
bound to the current boot, contract, and interface. A matching notification must
confirm TX blocking and the agreed 20 MHz channel. It lets a follower finish
a peer's countdown without a second local CSA acknowledgment. The original
deadline survives daemon restarts and cannot be renewed by duplicate events.
The daemon verifies the live frequency before saving a successful fallback.
Failed or expired CSA uses interface-down recovery
and a full supplicant restart with the safe fallback configuration.
Peers can follow CSA or the
authenticated report over any surviving mesh path. Radar starts a 30-minute
DFS cooldown. Peer loss alone never triggers this fallback.

The operating contract lives in `/run/manet-dfs-contract.json`, separately
from ordinary agreements. A later 2.4 GHz-only plan cannot erase the 5 GHz
fallback. Only verified departure or a verified replacement DFS activation
retires it. A matching radar event without a valid contract stops an externally
owned radio. A running BSS reporting native ownership keeps its own evacuation.
The supplicant's external owner is a per-BSS runtime opt-in. Static and
unmanaged meshes retain native radar evacuation.

The guardian survives manager stops and config applies. It reports readiness
after subscribing and checking the startup latch. Its stop/failure hook fences
DFS meshes before stopping their supplicants. Tools updates use the staged,
standalone safety helper before replacing files and recheck after quiescing
config producers. They restart the guardian before resuming saved non-DFS
fallbacks, and require its readiness before committing the version. Failed
installs leave affected radios stopped. The existing Debian supplicant fallback
remains usable for non-DFS ACS without advertising DFS support.

The watcher sleeps on nl80211 and file notifications with no polling timer.
It checks the radar latch on startup. A kernel radar notification can request
evacuation before a busy debugfs read. An outstanding read requests one fresh
snapshot from the agreement daemon, delivered by a file notification. Pending
event acknowledgements and saved-plan changes also wake delivery. A retained
radar latch from an older generation is superseded only by a newer live receiver
on that frequency with an exact accepted local contract binding. Missing or
unknown bindings still fence. Fresh kernel radar notifications fence before
this historical-latch check.

Later measurement rounds refresh the fallback through a new unanimous plan.
The reserved receive configuration remains in place while a DFS main detector
is active because the benched receive-path transition accepts non-DFS channels
only. It can return to three paths after leaving DFS and releasing all owners.
The source and built candidate support this sequence. Hardware acceptance of
the complete CSA and main-radar path remains required before publication.

Connect by Ethernet or join this radio's own Wi-Fi network. For a radio with GPS,
turn **Android location OFF**, enable **ATAK external GPS input on UDP 4349**, and
leave **Default Mesh SA ON**. ATAK then uses the radio position on its own map and
all outputs, including mesh, unicast, and TLS to a TAK server. Keep phones near
their radio. If the radio has no GPS hardware, keep **Android location ON** so the
controlling phone supplies the team's GPS. Leave stock CoT input enabled. No
per-radio output entry is needed. Existing unicast XML output to
UDP 4242 still works, including on the radio's secondary client-gateway address.
Phone setup is in the [location guide](../../docs/guide/content/location.md).

No GPS receiver means no missing-GPS prompt. When neither trusted radio GPS nor
a held manual point is usable, the controlling phone's admitted GPS supplies the
status position, labelled `phone GPS (<callsign>)`. It never supplies receiver
trust, clock authority, or external input on 4349. It expires with its SA, at most
75 seconds after receipt. If the phone's path disappears first, a radio beacon
can use only that remaining fresh interval. A present receiver without a fix
still warns after three seconds, with unread spoken reminders every 60 seconds
and an explicit 30-second minimum.

Without a trusted own fix, a held manual point uses the controller's fresh phone
GPS at acceptance as its movement anchor, or the first fresh fix afterward.
Within 30 m of that anchor it holds indefinitely. Distinct fresh reports more
than 30 m away for 15 seconds release it, as does any fresh report more than
200 m from the held point. These operating thresholds allow phone GPS noise and
small manual corrections while limiting obsolete geometry. Stale GPS, loss of
the controller's path, or a trusted own fix resets the movement interval.
The anchor survives restart, while the interval does not. A new controller gets
a new anchor. No phone GPS means the point holds. On GPS-less radios this is a
phone-track decision, without a spoof monitor. Trusted-own-GPS confirmation and
override rules retain their existing behavior.

The controller can also tap **Use phone GPS again** beside the held point in the
local **ATAK PHONES** panel, without an admin password. The POST checks its source
address against the current controller, and the daemon rechecks the point,
controller path, session, and five-second request age. Guests cannot release.
Either release queues normal marker retirement to its original owner, clears
rewrite authority, and makes one quiet best-effort GeoChat and journal entry,
without a release audio alert. Existing receiver warnings remain independent.
Deleting a marker inside ATAK does not release it on the radio. A Pixel test
observed no phone-to-radio CoT traffic after local deletion.

Each newly accepted held point gets one quiet explanation chat, including
USER/drag selections. It tells the controller to open manet.local in a browser
and tap **Use phone GPS again**, or move on with fresh phone GPS. No chat repeats
on restart, reanchoring, or marker refresh. For a Send-created point, the daemon
retains its exact bounded XML and quietly replays that marker to the present
controller every five minutes. The UID, content, and timestamps stay unchanged.
Retirement always takes priority, including GPS confirmation, replacement, and
release. Failed sends wait for the next five-minute interval. USER/drag selections
have no separate marker to replay. Older checkpoints without original XML need
a new Send before refresh can work. Reappearance after the original stale time
and one-minute refresh behavior still need Pixel validation.

As a safety net for forgotten settings on multicast-only networks, the daemon
rewrites only default mesh SA toward bat0, using trusted radio GPS or
the held manual point under untrusted GPS. Position and precision source fields
change, while other phone fields and Ethernet/IP sender identity are preserved.
Per-interface/MAC/IP nft set elements expire after three seconds unless renewed
by the existing daemon cycle. Stop cleanup removes the table too. Unadmitted
devices and local EUD-to-EUD reports pass natively, as do TAK-server and unicast
outputs. Unsupported or malformed bounded reports from blocked senders are
forwarded unchanged. There is no new process or polling loop.

The kernel records local EUD TCP traffic to ports 8089 and 8087 in the dynamic
`tak_phones` MAC set. Each packet refreshes a ten-minute timeout without waking
userspace. IPv4 and IPv6 use the same rule, including a server on the EUD side
when its traffic crosses the radio. While the MAC is present, the kernel skips
its native mesh-SA block even if the daemon is paused. Admitted phones appear
as **TAK server** after a bounded netlink read on the existing event cycle. Packet
sockets capture mesh SA only. TLS remains unchanged, so correct phone settings
still provide one position on every output.

Userspace reads membership before replacing mesh SA and on meaningful status changes,
reusing a complete snapshot for up to five seconds. Idle timer cycles issue no
reads, even with a present phone. A blocked SA needing a new read waits in a
bounded slot, one frame per EUD port, with that port's capture reader paused
until the reply. The two-second read timeout stops the service on failure.
New membership requires fresh data. Existing leases may renew using the kernel's
native TAK exemption, but replacement always needs a fresh snapshot. A frame
captured around a new kernel insertion may
still be rewritten using the preceding snapshot. After membership is learned,
possibly intercepted frames are forwarded unchanged until the old lease window
ends and the queue drains, which can duplicate native originals. This transition
is not an atomic handoff. Capture loss now suspends rewriting for three seconds,
without affecting kernel TAK detection. Nonstandard ports and traffic the radio
cannot see remain outside detection. Restarting the unit recreates both sets.

CM4 testing with a Pixel's Android location on verified XML phone-GPS replacement
toward bat0 and one quiet disagreement GeoChat, journal entry, and panel note.
The phone sent XML, so TAK v1 replacement remains covered by tests rather than
hardware evidence. Before on-demand membership reads, the service used 1.20 CPU
seconds per minute with one phone and four child executions per minute, compared
with about 0.80 before kernel detection. Remeasure on CM4 to assess the read change.

Inspect membership with `sudo nft -j list set bridge manet_atak rewrite_phones`
and `sudo nft -j list set bridge manet_atak tak_phones`. Use JSON output. On the
CM4, plain-text listing omitted populated concatenated rewrite-set elements.

When an admitted phone reports fresh own GPS and the radio has a trusted own
fix, separation over 50 m creates a local status note, one journal line, and one
quiet GeoChat handoff per episode. No audio, retry, or trust change follows.
The threshold assumes 10 m error per GPS plus 30 m phone-to-radio separation.
Fresh agreement within 35 m for 30 seconds rearms notification. Missing fixes
interrupt that timer. Agreement never restores trust or clears distrust. Phones
with location off supply no GPS observation to compare. Episode state is ephemeral.

With no phone present, a radio with a usable position appears on the team's
maps under its hostname or `atak_callsign`, using a radio/signal symbol.
It shares trusted GPS, its held manual fallback, or the fresh phone GPS described
above, never a suspect GPS fix or
0,0. A distinct beacon UID keeps the hidden phone-facing contact unchanged.
The beacon has no chat or Send endpoint. It sends XML to `239.2.3.1:6969`
through br0 and bat0 about every 30 seconds, or after five metres of movement
with a five-second minimum interval, and has 90-second staleness, shortened to
the remaining report lifetime for phone GPS. A local phone's
arrival stops it and requests deletion of its marker. It resumes after the last
phone's presence expires when radio GPS or a manual point remains. Phone GPS
uses the earlier path-loss interval described above. The owned firewall confines these local announcements
to bat0, excluding this radio's EUD ports. No extra setting is needed, and
`atak=n` disables all of it. Pixel rendering and multicast handoff still need
hardware validation.

One controlling phone sets the radio's manual location. A saved choice on the
status page wins while that phone is present. Otherwise, the only wired phone
wins; if there is no sole wired phone, the present controller stays selected.
If none remains, the radio picks a present phone at random and keeps that choice.
Replacement phones therefore work without deleting state. Phone presence ends
when its SA expires, at most 75 seconds after the latest admitted report.

Open **http://manet.local/**, expand this radio, and find **ATAK PHONES**. It lists
callsigns, **Wired** or **Wi-Fi**, **Controls location**, selected position/source,
**TAK server**, and **radio and phone GPS disagree by N m** when applicable.
Use **Set location
from this phone** beside another phone to choose it. No admin password is needed.
The choice survives reboot and absence; while it is absent the automatic rules
supply a controller, and the saved choice takes over when it returns. A held
manual point belongs to the radio and survives a controller change. Only the
controller's sent points, drags, USER selections, and alert receipts set manual
location or acknowledge alerts. Its admitted GPS is the automatic fallback.
Other phones still receive radio-selected external position
and GPS warnings; a rejected guest point gets a chat explaining who controls.

When another phone joins or the controller changes, ATAK chat tells everyone
which callsign sets the group's location. Coordinate before changing it. The
phone table uses the provisioned `max_euds_per_node` capacity (zero reserves one
wired phone; missing or invalid values log a warning and use eight).

The service verifies its firewall and EUD path before admitting a new peer or
TCP stream, and after network/policy or role-generation changes while an admitted
SA lease remains live. With no live lease, events only discard proof. The next
arrival or due relay beacon triggers verification. Beacon output also requires
a forwarding bat0 path, even when no local EUD port is ready.
Passive br0 listeners use a cheap address ioctl, and
packet sockets follow local port changes on the existing event cycle. Their
kernel filter admits only default mesh SA, without a multicast group join or
TCP packet wakeups. TAK-server detection and timeout refresh run in the bridge
firewall. Late address setup needs no timer-driven
snapshot. `path_ready` is false
while verification is pending. Unchanged ticks use the cached proof, and
notification loss fails closed. No extra monitoring process is started.
Receiver inputs are read before any phone joins so a relay can advertise its
position. Unchanged inputs and cycles without due work skip policy evaluation
and status writes. Producer failures, beacon work, and alert deadlines still run.
An idle `status.json` is a snapshot, not a heartbeat. The phone contact and
position-feed cadence is unchanged. Measure service CPU, including its
children, with `sudo python3 /usr/local/bin/manet-cpu-sample.py --label before`, then
repeat with `--label after` under the same phone/traffic conditions. Each run
takes two counter snapshots 60 seconds apart and exits, reporting CPU seconds
per minute and percent of one core. A restart invalidates the sample.

**mesh-ip-manager.sh** and **mesh-ip-startup.py**

Chunk-based IPv4 allocation. Each node claims a chunk of addresses sized
`max_euds_per_node + 2`.

- **IP 0 in the chunk.** `br0` primary. The node's mesh address, and what peers
  use to reach it.
- **IP 1 in the chunk.** `br0` secondary. The DHCP gateway handed to EUDs,
  wired or wireless.
- **IPs 2 and up.** The DHCP pool for EUDs.

Both addresses sit on `br0` in the configured subnet, and `br0` bridges
`bat0`, so the mesh and the EUDs are one flat broadcast domain. The split is
bookkeeping and not isolation. `manet-ui-firewall.sh` is what separates them.

The first five addresses network-wide are reserved for services. A chunk is
claimed from those peers have not taken, and a collision is resolved by a MAC
tie-break. `dnsmasq` is configured for the pool when a node needs it.
The [allocation and collision-recovery explanation](../../docs/node-tools-internals.md#ipv4-allocation-and-collision-recovery)
covers the address calculation, occupied-range selection, mixed block widths,
startup window, and why simultaneous claims still require later repair.

When `ipv4_network` changes, the allocator retires the saved node address,
EUD gateway, and locally held old MediaMTX/Mumble IPv4 VIPs before claiming a
new block. It replaces the DHCP pool and management names and clears old
leases. Exact address ownership comes from the saved allocation, which stays
durable until retirement succeeds so an interrupted pass can retry. Operator
aliases, uplink addresses, and IPv6 remain intact. Removing the last old-prefix
address also removes its connected route, unless an operator alias still needs
that route. The new allocation is saved before its addresses are installed.
Retirement reads the kernel's secondary-promotion policy without changing it.
With promotion disabled it removes owned secondary addresses before the
primary, or defers without stopping DHCP if an operator alias would be lost.
Locks, ownership, capabilities, and file access are checked before disruption.
A failed retirement restores the old addresses, pool, and leases and restarts
previously active DHCP. Interrupted restoration keeps a checkpoint in `/run`
for the next attempt. Failures retry at most once every 15 seconds and repeat
the diagnostic only when the failure changes.

**manet-dhcp-isolation.py**

Keeps DHCP and EUD discovery (mDNS, LLMNR, SSDP/UPnP, NetBIOS, and
WS-Discovery) on the local node so phones and laptops do not send that chatter
across the mesh. Wired and AP clients retain local discovery and resolve
`manet.local` through DNS or mDNS to their node's internal EUD gateway
address, which stays with the node when shared services change leaders.
Management and measurements are at `http://manet.local/manage/`.
ATAK, mesh voice, and other mesh services retain their transports.

**manet-mesh-census.py**

Summarizes traffic observed on the mesh port to help identify other chatter.
The traffic inventory, implementation details, and bench procedures are in
[Node tools internals](../../docs/node-tools-internals.md#local-eud-discovery).

On each boot, IPv4 allocation waits for usable `br0` link-local IPv6, active
Alfred, and the node's initial identity/telemetry publication. It then observes
BATMAN peers for 10 seconds (one Alfred synchronization period). If visible
peers still lack identity or telemetry, it waits up to 20 seconds total, then
allocates using the claims received so far. Peer changes never restart either
deadline. Transient local failures defer allocation but preserve elapsed time;
the deadline does not bypass a failed registry read or local readiness checks.
Nodes continue publishing over IPv6 throughout the wait. Startup checks run
with a 1-second loop sleep, returning to 15 seconds after allocation; work
within a loop can delay the check past its deadline. Both managers publish
their initial records before the first discovery check, so the observation
window can start on that pass. A successful Syncthing ID lookup is cached for
the manager's lifetime; a missing ID is retried.

The registry and wait state are rebuilt each boot. A remembered IPv4 chunk in
`/etc/mesh_ipv4_state` must pass the same wait and is reused only if no peer
claims it. Every allocation pass refreshes the registry, and changed claims
are published on the next manager pass instead of waiting for the keepalive.
After the first allocation, that next pass runs without the usual 15-second
sleep, so peers can learn the new claim promptly.

Each node's chunk size is fixed at provisioning; the management UI shows
`max_euds_per_node` without allowing changes. Nodes may have different sizes:
allocation compares advertised absolute address ranges. A peer without a
known block size blocks new allocations until it publishes one.

**gateway-route-manager.sh**

Chooses this node's internet gateway from every gateway in `batctl gwl` and
points the system default route at its mesh IP. batman-adv's own choice is
ignored: it never reaches IPv4 routing. The node uses the gateway that gives it
the fastest internet. Traffic crosses the mesh path to a gateway and then that
gateway's uplink, so each gateway's score is the slower of the two: the mesh
path throughput and the download bandwidth it announces. The highest score is
taken when the node has no gateway, or when its gateway stops
announcing or misses two reachability checks (about 20 seconds). Otherwise the
node switches only for a gateway scoring at least 1.5 times as much and 2 Mbit/s
more, sustained for 60 seconds, and never within 5 minutes of its last switch,
because a switch breaks every open internet connection. A restart keeps the
gateway the existing route uses. Removes the route when no gateway is
available. While a mesh route is pending, sleeps 1 second between checks during its first
90 seconds, measured with monotonic uptime. Once ready, or after that startup
period, sleeps 10 seconds. Failed reachability checks and route installs retry.
Uses the node's assigned primary address as the route source, excluding service
VIPs and the EUD gateway alias. The mesh default uses metric 500, between a
promoted direct uplink at 100 and networkd's unqualified DHCP default at 1024.
A DHCP lease alone does not block mesh selection. Local gateway state removes
the mesh default. Before that marker is written, a metric-100 default on the
saved upstream interface also preserves direct routing.

**manet-uplink-speed.sh**

Measures an Ethernet uplink's internet download speed and announces it as the
gateway's bandwidth (`batctl gw_mode server`). The test downloads at most 5 MB
over HTTPS from Cloudflare's speed endpoint, or OVH as a fallback, once per
uplink address and router. HTTP 200/206 samples of at least 250 kB count even
when the 15-second transfer deadline expires. The rate excludes DNS/TCP/TLS
setup. A slow sample receives a low score and still qualifies as connected.
A failed download, including TLS failure before clock synchronization, means
unknown bandwidth: Ethernet announces a conservative 1/0.2 Mbit/s and retries
at most once a minute on subsequent reconciliation. A successful estimate is
cached until its interface/address/router/carrier generation changes or the
node is demoted. Promotion queues `manet-uplink-measure@IFACE.service` with
`--no-block`, so neither the download nor its timeout delays forwarding.
The worker checks the selected promotion generation under the Ethernet lock
before publishing a result and before updating BATMAN. Stale work cannot
announce Ethernet bandwidth on Wi-Fi or restore a withdrawn gateway.
Phone tethers, cellular modems, and Wi-Fi uplinks are never download-tested and
announce batman's default 10/2 Mbit/s.

**manet-uplink-dispatch.sh**

Owns gateway state. Decides whether an interface with carrier is an upstream
uplink or a wired EUD port, and on promotion configures NAT, the firewall,
`radvd`, and the EUD services. On demotion it tears them back down. Called by
the networkd-dispatcher hooks and reconciled once a cycle by the node manager.
Both paths return early when nothing has changed.

Local selection prefers qualified wired Ethernet, then supported USB
tether/cellular links, then built-in or USB Wi-Fi clients. Wired EUD ports
are excluded. The saved promoted interface and current route take precedence
over events within each class, so an event cannot displace a working peer
of the same class. Reconciliation moves back from Wi-Fi to working Ethernet
without waiting for Wi-Fi to fail. Ethernet loss tries other local uplinks
in the same pass before withdrawing gateway status. Wi-Fi can remain
associated while Ethernet carries internet traffic. The unplug hook invokes
this selection before slow Ethernet cleanup, carrying its held policy lock.
A ready fallback keeps the gateway marker and avoids a transient mesh default.
The loss pass skips unaddressed standbys, leaving DHCP qualification to a later
event. dnsmasq continues serving the existing pool and follows resolver-file
changes without restarting. Unchanged DHCP configuration also preserves leases.
Optional conntrack cleanup deletes only old-address source-NAT mappings when
the address differs. Established internet sessions still need reconnection.

Ethernet promotion requires an interface-bound IPv4 HTTP 204 response from
one of two connectivity endpoints. ICMP alone and HTTP 200/302 portal replies
cannot qualify it. This probe needs no TLS clock, and speed-test failure does
not block promotion. Ethernet auto-detection uses the same policy. Metered
and wireless uplinks retain their ICMP/HTTP probe and TCP fallback.
Demotion explicitly removes the promoted metric-100 route and restarts mesh
route selection. DNS resolution may use another working link, but the HTTP
connection itself must work through the candidate interface. If normal name
resolution fails, the probe queries the candidate's DHCP resolver through a
device-bound socket and gives curl the answer with `--resolve`. A lone node
can therefore qualify even when only its LAN's resolver is allowed.

**manet-dns-setup.sh**

Sets the shared resolved upstream file and public fallback servers.
Provisioning, updates, and dnsmasq startup run this idempotent helper.
`manet.local` and service aliases remain local to dnsmasq.

**manet-uplink-dns.py**

Publishes DHCPv4 DNS and search domains only for the promoted direct uplink.
Networkd's managed DHCP files permanently disable automatic DNS, domains, and
resolver host routes. An isolated LAN therefore cannot hide fallback DNS or
pin its traffic away from the mesh route. A path-triggered oneshot follows
gateway state and lease changes, updating a resolved runtime drop-in only
when the selection changes. Renewal does not reload networkd or restart
dnsmasq. Resolver addresses are plain IPv4: on-link LAN DNS uses its connected
route, and public DNS uses the promoted metric-100 default. The path unit
watches the gateway marker, selected-interface file, and lease directory.
Routine networkd link-state rewrites do not trigger it.
With no qualified resolver, public fallback DNS follows the best
default route. The helper also performs the bound candidate lookup above.

First boot and the updater run `install` to preserve existing MAC matches
while migrating active DHCP files, saved defaults, and inactive templates.
The first migration can briefly interrupt DHCP while networkd reloads.
Promotion, demotion, and renewal never toggle that persistent policy.

**prepare-ap-iface.sh**

Runs before every hostapd start or restart, including starts from uplink and
Ethernet auto-detection. It stops the AP radio's mesh supplicant, detaches the
radio from its old bridge/mesh master, and prepares managed mode. A failed
preparation prevents hostapd startup. Concurrent preparation requests are
serialized, and a standalone request leaves an already-running AP untouched.
The shared `manet_ap_mesh.py` helper removes active mesh roles on AP entry and
restores the original provisioned band on mesh return. It rebuilds supplicant
files from current credentials and the saved/authenticated channel plan,
checks the resulting radio channel, and rolls back failed transitions. Fresh
registry observations validate that channel without authorizing a plan change.
AP-only hardware stays out of the mesh; disabled mesh radios remain down.

AP preparation runs through hostapd, and its post-start helper applies the
5 dBm cap only to the active AP role. The old independently enabled AP boot
units and the conventional mesh lab-power unit are retired. Mesh radios request
30 dBm per PHY through `manet-mesh-power.sh`, leaving the limit to the driver
and firmware; mesh return clears the AP cap. The AP setter verifies the cap
and refuses a PHY shared with another active interface. Healthy mesh reconciliation preserves operator
power changes; manual power settings currently do not persist across reboot.

`gateway-route-manager.sh` is the sole mesh default-route manager. The obsolete
`mesh-default-route-fix.service` is retired during updates and setup; it could
remove elected service VIPs.

**usb-ethernet-watch.sh**

Brings a USB Ethernet interface, such as a tether or an LTE dongle, into the
uplink decision path when it appears. Triggered by udev.

**usb-wifi-uplink.sh**

Configures a USB Wi-Fi adapter as an internet uplink from a stored SSID and
password. The management UI applies it only on this node, passing credentials
through stdin. The existing hotplug lifecycle and gateway dispatcher remain
in use. `scan-json` scans the eligible adapter without stopping its client
service, restoring its prior link-up state afterward. Open and WPA2 Personal
networks can be selected. Mixed WPA2/WPA3 networks use WPA2, while WPA3-only
networks remain visible with a disabled explanation. Menu selections must
match a selectable result in `/run/manet-usb-wifi-scan.json` from the same
adapter within five minutes. The saved BSSID pins that access point. The
operator `set` action is still accepted with its existing SSID/password form.

**manet_wifi_uplink.py**

Reserves one built-in MT7916 radio as a local Wi-Fi client. Configure it in
**Manage → Uplink**, select wlan0 or wlan1, scan, choose the upstream AP, and
enter its password. Open, WPA2 Personal, and WPA3 Personal networks are
supported. Hidden and enterprise networks are displayed but cannot be selected.
WPA3 passwords containing double quotes are rejected by the current renderer.
SSID, BSSID, signal, security, and channel come from the selected radio's scan.
The scan result expires after five minutes. Valid UTF-8 SSIDs, including `iw`
hex-escaped emoji and edge spaces, retain their exact bytes. Invalid UTF-8 and
control-byte names remain visible as escapes but cannot be selected.
Credentials never enter mesh.conf or Alfred.

Both paths omit exact matches of the current local `lan_ap_ssid` followed by
`-` and four lowercase hex digits, including this node's hotspot seen by another
radio. These names are removed before saving or grouping scan results and are
not counted. Old built-in scan caches are filtered when read. Typed and scanned
connection requests are also rejected on the server, including USB CLI `set`.
The prefix is literal and case-sensitive. Other prefixes, uppercase suffixes
and suffixes of another length are unaffected. This naming rule cannot identify
peers configured with a different hotspot prefix.

A matching saved built-in profile is persistently disabled, its client is
stopped, and its normal AP/mesh role restored. USB keeps its saved credentials,
but stops and disables the client and clears the interface address. Its
connect/reconcile path refuses to reactivate the match. Both show the reason
in status. The gateway dispatcher checks saved names before qualification and
also disconnects an already associated USB match, closing the interval before
client cleanup. A blocked built-in profile stays disabled even if the hotspot
prefix later changes, until the user selects a new connection.

Both radio paths use a scrolling, touch-sized network list. Matching SSID
bytes are grouped by band, security, DFS status and availability, retaining
the strongest BSSID. Hidden names remain separate by BSSID. The current
connected group appears once at the top with its state. Different bands,
security methods or DFS choices remain separate. Selecting a secured network
shows the password field and Connect. Open networks show only Connect.
Status polling preserves a typed password, while radio changes and rescans
clear selection. Rescan is required after five minutes. The server remains
authoritative for freshness and rejects disabled choices before changing roles.

| EUD mode | Upstream | Remaining mesh and AP roles |
| --- | --- | --- |
| Wired | wlan0, 2.4 GHz | HaLow and 5 GHz mesh |
| Wired | wlan1, 5 GHz | HaLow and 2.4 GHz mesh |
| Wi-Fi | wlan0, 2.4 GHz | HaLow mesh, existing 5 GHz EUD AP |
| Wi-Fi | wlan1, 5 GHz | HaLow and 2.4 GHz mesh, shared EUD AP with DFS follower support |

Disconnect a built-in uplink before changing the configured EUD mode.
Auto mode retains its wired-EUD preference. A reserved uplink PHY never returns
to mesh until disconnected. A remaining active mesh interface is required.
Selecting wlan0 with Wi-Fi or Auto EUD requires active HaLow. The shared AP is
`manetap` on wlan1's PHY, uses 20 MHz, and follows the upstream channel locally.
It does not change the mesh channel plan. The usual 5 dBm AP-only PHY cap is
skipped while sharing, so it cannot reduce uplink range.

On-demand scans keep an associated shared hotspot serving when it is on a
non-DFS channel or running as a supported DFS follower on the station's
channel. Other AP scans require confirmation: phones on that hotspot will
disconnect for about 15 seconds, with actual duration depending on the radio.
The page reloads the saved scan after reconnection and shows its age.
A mesh scan temporarily stops that band, then restores its live starting
channel, or the agreed/static channel if live information was unavailable.
Restore failures leave results usable and show a separate warning while the
watcher retries. The dispatcher shares the AP role lock with scans and the
channel follower. Shared `manetap` startup belongs to the follower after
station association, so the dispatcher cannot start it early between watcher
passes. A busy scan does not mark the client connection as failed.
The CLI uses `scan-plan wlan1` for the interruption check and requires
`scan wlan1 --allow-ap-stop` when the serving AP must stop.

The watcher sleeps on supplicant association/channel events, hostapd events,
link/address changes, and local profile/policy changes. It tries hostapd CSA
for non-DFS changes and verifies completion, with a three-second check if the
completion event does not arrive. Losing association stops the shared AP
before station reconnection. Pending lock, scan-repair, or AP-start work also
retries after three seconds. A healthy follower has no three-second tick.
Service start/stop hooks cover crashes and replaced control sockets.
Notifications and scan repair live under `/run/manet-wifi-uplink/`; the shared
gateway marker is `/run/manet-uplink/mesh-gateway.state`. The watcher creates
missing input and control-socket directories and watches them directly, so
unrelated `/run` writes do not wake it.

A disabled profile leaves no watcher process after cleanup. A systemd path
unit wakes it for profile changes or pending scan restoration, and the boot
service restores enabled profiles. While enabled, a 120-second safety check
covers lost notifications and silent control failures. Recovery from a missed
radio event can take up to two minutes instead of the former three-second
poll. It requests the uplink dispatcher only when the observed policy tuple
changes. Node-manager's existing 60-second gateway check handles internet
reachability changes without a local event. Normal channel and association
events trigger work immediately.

A DFS upstream can also serve the shared hotspot with the matched kernel and
hostapd from `kernel/patches/dfs-follow-ap/`. The station associates first.
The AP then starts on its channel without local CAC or its own radar detector,
using the upstream master. The station is never disconnected to start the AP.
The kernel stops a follower when its supporting BSS disappears, including
channels already marked available. At upstream CSA start it stops the follower
and refuses another follower start until the station completes the move or
disconnects. The watcher restarts the hotspot on the new live channel. This
also handles the upstream master's CSA away from radar. Phone clients briefly
lose the hotspot during a channel move. Non-DFS CSA retains its existing path.

The controller checks the loaded `cfg80211` option `dfs_follow_ap_v1` and the
paired hostapd version. Without both, DFS keeps the shared hotspot off with
the existing plain status reason. Association, address, and gateway status
remain separate. Use Ethernet or another node for access in that case.
Every AP start rechecks live association and channel restrictions. Three
unsuccessful follower starts, or any unexpected local CAC, latch the hotspot
off until an observed loss/reconnection or channel change. Radar-unavailable
channels remain blocked. Normal APs retain their CAC/radar handling. This
source path and the matched binaries still need hardware qualification.

The private desired profile is `/etc/manet/wifi-uplink.json`. A persistent
`/var/lib/wifi_uplink_if` role blocks mesh supplicants, ACS, BATMAN repair, and
radio controls before association or DHCP. `manet-wifi-uplink.service` restores
ownership at boot and follows channels. `manet-wifi-client@.service` owns the
managed supplicant. Gateway qualification, DNS, and NAT use the existing
dispatcher on connection/address changes and the enabled safety check.
Disconnect restores agreed/static mesh channels or the current EUD AP policy.
A HaLow-only gateway's announcement is capped at 1 Mbit/s in each direction,
without raising a lower upstream estimate. This is a conservative estimate,
not a HaLow speed measurement.

`waiting-address` means the upstream is associated but has no IPv4 address.
`connected` requires an IPv4 address; internet qualification is still separate.
Disconnect also works during channel agreement, restoring the existing valid
commitment or recovery anchor under the channel lock, or the static channel
when ACS is disabled. It does not initiate a new agreement round.

**mesh-hosts-update.sh**

Populates `/etc/hosts` from the registry, so peer hostnames resolve without DNS.
Only single-label DNS names with usable IPv4 addresses inside `ipv4_network`
are accepted. Localhost and aliases outside the managed block are reserved. Names are compared without case. Conflicting addresses
for one name are omitted, while identical claims collapse to one mapping.
The two-minute refresh compares mappings in memory and leaves the file untouched
when they are unchanged. Changed mappings replace only the marked mesh block,
preserving other entries, ownership, and permissions. Publication uses a temporary
file beside `/etc/hosts`, followed by file/directory syncs and an atomic rename.
Missing/empty registry snapshots and malformed block markers leave hosts intact.
The shell entry point runs `manet_hosts.py`, which reads registry assignments as
data rather than sourcing them as shell commands.

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
algorithm, and enslaves the active mesh wireless roles, including an AP
candidate that has returned to mesh. HaLow is added first so it becomes batman's primary, being the
longest-range link.

**batman-enslave-watch.sh**

8-second watchdog after `batman-enslave.service`. Re-enslaves interfaces that
fall out of `bat0`, and enforces `mesh_plink_timeout=0` on HaLow interfaces,
which the supplicant resets each time it rejoins the mesh. It also restarts
`wpa_supplicant-s1g-<iface>` when that unit is active but HaLow has no joined
mesh for four passes in a row (about 30 seconds), for example after the
interface was taken down or the driver disabled the BSS. Repeat restarts back
off from 60 seconds to 16 minutes, and a rejoin resets the back-off.

**sae-watchdog.sh**

Watches the journal for `MESH-SAE-AUTH-BLOCKED` and restarts `wpa_supplicant`
and `batman-enslave` to recover. It acts only when `bat0` is actually missing
interfaces, so a transient block does not cause a restart storm. Until there is
an enabled mesh interface to watch (first boot before roles are assigned, or
every mesh radio turned off), it waits rather than exiting.

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

**mesh-time-sync.py** / **one-shot-time-sync.sh**

The time service owns chrony across GPS, internet, and mesh-client roles. A GPS
node disciplines its clock locally; a node with a direct internet uplink uses
authenticated public time (NTS) through that interface, never plain internet
NTP. Both can serve time to the mesh once chrony confirms a recent selected
source and a settled clock; an internet source must also be NTS-authenticated. A GPS fix or gateway
flag alone does not qualify a node as a time server.

The existing `is_ntp_server` field in Alfred telemetry advertises availability
at the normal publication cadence. There are no extra time-server announcements.

Other nodes discover advertised servers through the registry and rank them by
their selected BATMAN_V route throughput. A sync attempt lasts up to 90 seconds;
a failed peer is skipped for five minutes while discovery continues. Missing
servers or failed queries are retried, so a source can appear after boot.
After a verified sync, chrony stops and mesh NTP polling pauses for six hours,
plus up to ten minutes of per-node staggering. A brief refresh then uses the
same discovery, validation, and timeout rules. This covers deployments lasting
a day or two and longer runs without continuous mesh polling. A missing source
leaves the node running on its last synchronized clock while discovery retries.
After the first sync, corrections are gradual; refreshes and source changes do
not request clock jumps. The service keeps checking local GPS/uplink state so
a node can become a source later. Losing an uplink preserves GPS service when
a usable GPS source remains.

`one-shot-time-sync.service` runs the shell wrapper and Python controller. It
starts with node-manager and follows its restarts. Refresh timing survives
service restarts and wall-clock changes. Actual drift still needs bench
measurement. Timed ACS and mesh administration wait for initial synchronization;
cold lobby recovery can establish contact before then.
Details:
[time synchronization internals](../../docs/node-tools-internals.md#time-synchronization).

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

Timed mesh control is deferred until the first verified GPS/NTP sync, including
publishing, receiving, and ACKing mesh changes. The configuration UI reports
when it is waiting for time. Local-only configuration and public status remain
available. Later brief loss of GPS/NTP does not revoke the initial-sync marker;
the periodic refresh/holdover policy above applies.

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

Each node saves a private application journal in `/var/lib/manet-config`
before consuming the activation. A failed or interrupted apply remains local
work. It resumes through config sync, including after a restart or password
rotation, without needing the original encrypted message. Staging still needs
verified time and activation waits for its scheduled time. Once activation
begins, local recovery does not depend on the clock-sync marker.

A staged ACK means the node saved the package. It does not mean application
succeeded. Type-73 acknowledgements carry `application_status` and a separate
`applied_version`. The pending package in the admin API also carries
`application_status`. The durable journal distinguishes `prepared`, `applying`,
`verified`, `committed`, and `rolled_back`. Only verified or committed work
produces the volatile applied-version marker. Rollback is terminal for that
transaction. A deliberate retry after rollback requires a new staged edit.

### When each setting takes effect

- **Operator name.** `operator_name` is stored only on this radio. **Save**
  on the public status page saves or clears it immediately, without a password.
  It is shared as a telemetry label, never
  as a setting that overwrites another radio's name.
- **Per-node.** `eud` and `lan_ap_key` apply only on this radio. The AP password
  remains editable. `lan_ap_ssid` and `max_euds_per_node` are fixed at flash
  time. Node config shows the full broadcast AP name, including its four-digit
  suffix, without editing it. These local fields never go over Alfred. Keeping
  the same hotspot prefix across a mesh lets the uplink picker exclude its own
  hotspots. AP-name edits through the API, local activation, config writer, or
  setup reruns are refused before other settings change.
- **Safe.** `admin_password`, `mtx`, `mumble`, and `auto_update` apply mesh-wide
  straight away.
- **Deferred.** `regulatory_domain` is written now, together with every radio
  file that carries the country (`manet-region.py`): the cfg80211 and Morse
  module options, crda, hostapd, and each supplicant. The radios use it from
  their next boot. Moving between the US and EU HaLow plans also puts HaLow
  on that region's default channel, since the two plans share none. `acs` selects the orchestrator
  and applies at once, with a `node-manager` restart.
- **Dangerous.** `mesh_ssid`, `mesh_key`, and `ipv4_network` rewrite the
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

A failed baseline query or incomplete backup blocks the dangerous change
unless **Skip the safety net** was explicitly selected. The original backup
and deadline remain in the journal across retries. All newer config work waits
while an activation or restoration is unfinished. Failed preparation remains
local work even after the transport message expires.

The deadline uses the boot clock. If a node reboots during a trial, it checks
the outcome immediately instead of granting a fresh five-minute window.
Only an already verified change with healthy files/services and a recovered
peer (or a successful original solo-node baseline) can commit. Otherwise the
node restores its original snapshot.
Rebooting during the trial can undo the change before radios reconnect.
Leave the node powered until the trial finishes to give the change its full
safety window.

If restoring a file or restarting a service fails, the snapshot is kept and
restoration is retried on the next cycle, including after a reboot.

Danger is judged against both mesh.conf and every mesh supplicant file.
Retries reconcile each requested value, restart and verify the relevant
services, and check their actual files. Equality with mesh.conf alone cannot
complete a job. A verified trial also checks files and service health before
committing. Interrupted restoration keeps restoring even if peers return.

### Updating a node with pending work

The updater stops node-manager and any running `mesh-config-apply` unit before
replacing files. It saves the pending package and ACK in its private recovery
directory, installs the receiver, applier, and `manet_config_journal.py`, then
runs `mesh-config-sync.py migrate` before activation. A successful import
fsyncs the pending package and any legacy rollback snapshot/deadline before
disabling the legacy checker for that trial. An already consumed legacy
activation is recognized from its replay history and matching ACK marker.
Legacy trials whose applier already finished are imported too. No journal is
needed when there is no pending package or trial. Migration failure leaves
the update pending with its old version markers and the manager stopped.
Offline update recovery repeats migration before resuming services, including
after a reboot clears `/run`.

Config sync also imports legacy state on its first ordinary pass, but that is
not a substitute for the updater hook before an immediate reboot. Once
imported, losing `/run` does not lose the job. Keep the existing admin replay
history and the new journal during updates and rollback.

The manager invokes config sync for local pending work even if Alfred is empty
or unavailable. The status API reads application and rollback state from the
journal, falling back to legacy state only when no journal job exists.

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

**button-monitor.sh**

Blocks on a GPIO interrupt and runs `led-info.sh` on each press. Near-zero CPU
when idle. A GPIO monitor error ends the process instead of spinning.

**led-boot.sh** / **led-info.sh**

Boot-progress states and an on-demand blink sequence for an external GPIO LED
harness, separate from the onboard provisioning LEDs above. The button display
counts **directly connected neighbor nodes**, using a bounded BATMAN
`neighbors_json` query. HaLow, Wi-Fi, and wired mesh links all count; registry
radio aliases merge multiple links to the same node into one blink. Multihop
nodes are excluded, keeping this a quick local connectivity check. ACS continues
to use its separate whole-mesh originator counter.

| Button display | Meaning |
|---|---|
| N green blinks | N directly connected neighbor nodes |
| Solid green for three seconds | Connected; identity metadata is still needed for an exact count |
| Solid red for three seconds | Confirmed zero peers |
| Solid amber for three seconds | Peer count unavailable; retry with another press |

The boot display blinks red until node-manager is active and bat0 exists, then
green while awaiting peers. An unavailable peer query changes it to amber
blinking until a successful query. At least one confirmed direct neighbor produces ten
seconds of solid green, then the process exits. It never blocks boot completion.
Button displays take exclusive ownership; boot blinks yield between half-cycles.
A button press waits up to twelve seconds for ownership, including an ongoing
boot success indication. GPIO failures stop a display and termination releases
its holder process.

The live neighbor table establishes connectivity; the registry only joins radio
addresses to node identities. Old registry entries cannot create neighbors. If
multiple radio addresses cannot yet be resolved, solid green confirms the
connection without inventing a count. Zero or one neighbor address needs no
registry metadata. Boot can complete its connected indication while identities
are still arriving. No extra network announcement or polling stream is added.

These scripts require libgpiod v2 and are **disabled unless explicitly configured**:
the presence of a GPIO controller does not establish that a harness is wired.
After confirming the wiring, create `/etc/default/manet-led` with shell assignments:

```sh
LED_ENABLED=1
GPIO_CHIP=gpiochip0
LED_R=20
LED_G=21
LED_B=22
BTN_LINE=23
```

The pins above are provisional active-high LED / active-low button assignments,
not a verified CM4 wiring diagram. Restart `button-monitor.service` and
`led-boot.service` after configuring them. The harness must provide an inactive
electrical bias after GPIO release; libgpiod does not guarantee the output state
after its holder exits. Pin assignments, polarity, debounce, and off-state remain
bench checks pending hardware. Configuration is shared by all three scripts
through `manet-led-common.sh`.

---

## Recovery & Identity

**ssh-recovery.sh**

Keeps headless SSH reachable on a provisioned node. Reads `ssh_user` from
`/etc/mesh.conf` (default `radio`) and resolves the account's home directory
and primary group. Setup uses the same account for diagnostic sudo rules,
Syncthing and Mumble shared files. The username is selected at flash time,
not changed through mesh configuration synchronization.

**mesh-clone-identity.sh**

Detects a provisioned SD card that has been cloned onto different hardware, and
resets the identity and state that must not be shared between nodes. Without
it, two nodes would claim the same IP chunk and the same hostname.

---

## Shutdown

**mesh-shutdown.sh**

Graceful shutdown. Broadcasts a tombstone announcement carrying
`NODE_STATE=SHUTTING_DOWN` three times at two-second intervals, then waits
three seconds for propagation before returning to systemd's shutdown sequence.
Peers can treat the absence as deliberate. Running the helper alone does not
power off the node.

---

## Utilities

**mtx-ip.sh** / **mumble-ip.sh**

Derive the MediaMTX and Mumble IPv6 VIPs from the ULA prefix in the radvd
config, hashing the normalized /64 prefix into a stable suffix and returning
the address with a /128 mask. Every node computes the same VIP without
coordinating.

**node-update.sh**

The update archive can carry any component required by a release, including
kernels, modules, firmware, and configuration migrations. The `tools` name
does not restrict payload scope. Fielded radios may be sealed, so maintenance
must be designed around updates in place. Current packages include runtime
software and managed MT7916 firmware. A supplied CM4 kernel payload starts a
one-shot trial by default, using this node's boot configuration and a unique
kernel release. Set `enabled=n` in `/etc/manet-kernel-update.conf` to install
only the runtime payload. The setting is local and cannot be replaced by an
update archive. An already armed trial still resolves safely. Rock 3A kernel
activation is disabled pending bootloader and reset-path validation. See the
[field update policy](../../docs/node-tools-internals.md#field-update-policy).

Updates node tools to the stable GitHub release marked Latest. It compares
the local and selected versions and installs the board's tools archive when a
newer release is available. The shell entry point runs `node-update.py` (Python standard
library plus the existing `curl` command). `--routine` is quiet on the console
and skips checks for 24 hours after a successful check or installation.

Every board reads `manet-release.json` from that published release. The manifest
pins the version, source commit, package sizes, and SHA-256 digests. Archives and
checksum sidecars come from URLs for that exact release. Pushing source code to
`main` does not announce a tools update.

`sudo node-update.sh --development` selects the most recently published build,
including prereleases. This affects only that invocation. Stable checks leave a
newer installed development version alone; use `--allow-downgrade` explicitly
to return to an older stable build.

For an offline bench package, use
`sudo node-update.sh --local-archive /path/cm4-tools.tar.gz --local-version VERSION`
with the adjacent `cm4-tools.tar.gz.sha256`. Use the board's exact filename.
This explicitly trusts the supplied local archive and sidecar instead of GitHub
release metadata, checks its embedded version, and reinstalls an equal version.
Downgrades still require `--allow-downgrade`. Pending recovery takes precedence.

The updater verifies the manifest digest and adjacent `<filename>.sha256`, reads
the entire gzip stream, validates archive paths and required files, and checks
both embedded version files before staging the payload. It checks free space
and dependencies before installing files. Concurrent attempts are locked out.

The installed version is recorded only after file installation, manager
selection, systemd reload, and the required restarts and active-state checks
succeed. Other shipped runtime daemons use try-restart, keeping stopped voice
and ATAK services stopped. Previously active daemons must still be active
before the version is committed. That obligation survives retries in the same
boot, so a failed daemon cannot silently become optional on retry. Failures return a nonzero exit status and are logged under
`manet-update`; inspect them with `journalctl -t manet-update`. An interrupted
installation leaves `/var/lib/manet-update/in-progress` and a durable recovery
archive under `/var/lib/manet-update/recovery`. Every subsequent attempt first
resumes that exact release locally, even without Ethernet or internet access.
The archive is checked again before use. The saved updater, release helper,
and kernel helper allow recovery even if the installed tools contain files
from different releases.

At boot, `manet-update-recover.service` restores the pending payload before
radio coldplug and firmware preparation. `manet-update-finish.service` then
runs activation and service checks once normal service startup is available.
Dependencies are installed before an update is armed; recovery does not run APT.
To retry manually, use `sudo node-update.sh --recover-only`. It does nothing
when no update is pending. A missing or corrupt recovery archive fails with an
error and retains the pending marker for repair; it is never silently replaced
with a different release.

A kernel payload stages a separate boot set and module tree before replacing
runtime software. The old kernel remains the normal boot choice during the
CM4 one-shot trial. The updater derives a profile from the node's config,
command line, FAT UUID, and detected hardware. Confirmation requires multi-user
startup, healthy critical services, PID 1 holding an active hardware watchdog,
mounted root and boot filesystems, matching modules, and an up `bat0`.
PCI/USB identities determine which radio drivers and PHYs must work, including
the MT7916 timing marker. A bound Morse SPI hat is also recorded before reboot.
No Ethernet, internet, peers, or clock synchronization is required. A root-owned
`/etc/manet-kernel-trial.json` and its pinned health program remain an explicit
bench override. Health gets up to 60 seconds within the 240-second boot
confirmation deadline. A failed trial is retained for diagnosis and is never retried
automatically. After returning to the original kernel, use
`sudo node-update.sh --cancel-kernel-trial` to cancel an uncommitted trial.
Candidate files are retained. Trials use the firmware kernel watchdog,
`watchdog.open_timeout=120`, PID 1's runtime watchdog, and an independent
300-second confirmation guard. The reset/fallback mechanism has been benched
on CM4. The generic field health policy has confirmed real kernel updates on CM4.
An EEPROM boot watchdog is optional and its status is recorded. Without it,
a failure before kernel handoff can need a power cycle to select the previous
kernel. The sealed radio's power supply must remain accessible.

Older CM4 images with `auto_initramfs=1` can trial without a manual boot edit.
Only `tryboot.txt` disables automatic initramfs loading during the trial. The
original `config.txt` and initramfs stay intact for fallback. After health
confirmation, one atomic selector replacement commits both the new slot and
`auto_initramfs=0`. The transaction records the previous settings. Preflight
explains refusals for custom initramfs directives, root selectors needing
userspace, encrypted/LVM/network roots, or modular root-storage/filesystem
drivers. For radios already on v0.574, the next CM4 tools wrapper installs the
fixed updater before its kernel follow-up runs. Stale bootstrap payloads and
timers are retired when a direct update takes over recovery.

This completes an interrupted update; it does not roll back a defective release
or provide an operating-system snapshot. The protection applies to installations
started by the new updater. Configuration and operator software outside the
tools payload are preserved. Recovery storage is removed after success, and
abandoned download directories are cleaned under the update lock. Space checks
leave 128 MiB free in addition to the estimated installation requirements.

Automatic updates remain opt-in through `auto_update=`. The routable hook,
acquisition of a mesh-gateway default route, and initial qualified clock sync
queue checks. A daily timer also queues a check with up to two hours of random
delay and catches missed calendar events after downtime. Checks accept either
the selected direct uplink's IPv4 default or a next-hop default on `br0`;
mesh-only nodes need no saved upstream interface. Mesh checks add a random
0–15 minute delay before the updater, then recheck opt-in and route availability.
Direct-uplink checks do not add that delay. Gateway switches and route
refreshes do not queue extra checks. The updater's 24-hour marker throttle
remains the real limiter, and its lock prevents overlapping updates.

Checks defer until `/run/initial_time_synced` exists; a path unit queues a check
when it appears. Manual downloads also require this marker and report a clear
clock error when it is absent. Local recovery remains available without clock
sync. Disabled automatic updates exit successfully even with the timer enabled. See
[networkd-dispatcher/README.md](../networkd-dispatcher/README.md).

**Storage limits**

Peer allocation claims live in `/run/manet-registry/claimed-chunks.txt`, alongside
the registry's observation and decoding caches. The builder recreates the
directory on demand after boot. These shared files survive individual service
restarts and remain visible to services with private `/tmp` directories; reboot
clears them. The allocator and reconciliation cache use the same path. Saved
allocation preferences in `/etc/mesh_ipv4_state` remain persistent.

The persistent journal retains its existing 200 MiB budget and ten-second
sync interval. `manet-logrotate.timer` checks MANET's standalone logs five
minutes after boot and every fifteen minutes afterwards. The policy in
`/usr/local/share/manet/logrotate.conf` rotates each named log above 5 MiB,
keeps three archives, and compresses older archives. These are periodic rotation
thresholds: a fast writer can exceed 5 MiB between checks. Open shell log
descriptors stay usable through copy-and-truncate rotation; a few diagnostic
lines can be lost during the copy. Application logs and Syncthing data are
outside this policy.

Saved measurements default to 256 MiB, 4096 result files, and 128 sessions,
with a 1 MiB limit per result and 128 MiB free-space reserve. Capacity is checked
before starting work and before each result is saved. When full, recording
stops with an error; existing results remain available for export and deletion
in the Sessions tab. They are never automatically pruned. A failed write does
not publish partial JSON. Temporary result files left by a power cut count
toward the budget and are removed when their session is deleted.

Operators can adjust measurement limits through a `mesh-status.service`
environment drop-in. The positive-integer variables are
`MANET_MEASUREMENTS_MAX_BYTES`, `MANET_MEASUREMENTS_MAX_FILES`,
`MANET_MEASUREMENTS_MAX_SESSIONS`, `MANET_MEASUREMENTS_MAX_RESULT_BYTES`, and
`MANET_MEASUREMENTS_MIN_FREE_BYTES`. For a custom logging policy, set
`MANET_LOGROTATE_CONFIG` in a `manet-logrotate.service` environment drop-in
to a root-owned configuration file outside the shipped policy path.

---

## Setup & Provisioning

The first-boot stage that runs before `radio-setup.sh` is not in this
directory. It installs the apt dependencies, the install tarball, the Morse
firmware, and the base networkd, nftables, and radvd config. It is generated at
flash time from
[`MANET/provisioning/firstrun.sh.template`](../provisioning/firstrun.sh.template)
with your answers substituted in. See
[provisioning/README.md](../provisioning/README.md).

**radio-setup.sh**

First-boot configuration, run once from `radio-setup-run-once.service`. It sets
up:

- Interface renaming, separating the mesh, HaLow, and AP radios.
- A wpa_supplicant config per interface.
- Network services: alfred, batman, radvd, chrony.
- Optional services: MediaMTX and Mumble.
- Optional GPS and NTP support through `gpsd`, `gps-reader.service`, and chrony
  `SHM 0`.
- The systemd service for the node manager.

Before assigning roles, `manet-wait-radios.py` observes wireless enumeration
for at least ten seconds and requires five seconds with unchanged devices and
bound drivers, plus an empty udev queue. The total wait is at most sixty seconds.
An unstable or failed query leaves existing role files intact and provisioning
incomplete. No radios throughout the window permits wired-only setup. This is
a bounded settling check, not proof that an undetected radio cannot arrive later.

Role files use live names through setup, then switch to the pinned names just
before a rename reboot. A failed reboot restores the live names. Extra radios
without a `.link` pin retain their detected names. On the next setup pass,
failed radio units are cleared after the mesh restart only if their failure
state has not changed since before setup changed the radio services.

The final setup pass starts the runtime services before recording success;
the dashboard and BATMAN watchdog do not need another reboot. It also enables
the onboard LED verdict for later boots. CPU policy keeps every CPU online and
uses schedutil, falling back to ondemand or the kernel default if unavailable.
CM4 is capped at 800 MHz (or the next lower supported frequency); other boards
are uncapped. Kernels without CPU hotplug or frequency controls are supported.

Provisioning takes several reboots and about ten minutes. Every `apt` call
continues on failure, so a missing optional package does not abort the run, but
each step that failed is recorded. If that list is not empty at the end, the
node is marked incomplete, `radio-setup-run-once.service` is left enabled so
the next boot retries, and `manet-provision-status.sh` reports it at login.

Network reachability is checked before each apt phase, so a node with no
network records that once before attempting package downloads.
If repository DNS fails, setup makes one bounded
`manet-uplink-dispatch.sh provision` attempt per run, then checks DNS again.
This qualifies the direct uplink and publishes its DHCP resolver even when
the LAN blocks public DNS and node-manager has not started. It defers radio
role changes, bandwidth measurement, announcements, and manager startup until
normal reconciliation, so rename reboots and unfinished AP configuration
are safe. Working mesh or public DNS needs no extra dispatch.

**manet-provision-status.sh**

Answers one question on every SSH login: has this node finished setting itself
up? Installed as `/etc/update-motd.d/50-manet-provision`, and runnable
directly. It also reports the outcome of your own setup scripts when any were
staged.

The templates record early download-resource timeouts as `bootstrap-network`
in the same state file. Before the archive is available they install a small
standalone banner reader. The full status command later reads that state and
its log path, and recommends restarting `mesh-provision.service` or rebooting
for that phase. Radio setup keeps its separate retry command.

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
  `.disabled`, `.bak`, and `.orig` suffixes, files ending in `~`, and any file
  without `#!` on the first line.
- Scripts are executed directly, so the shebang picks the interpreter. A stock
  node provides bash, dash, python3, perl, lua, and mawk, and anything else has
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
