# MANET Node Tools

This directory contains the core operational scripts for mesh network nodes. These scripts handle service elections, network management, discovery, and coordination.

Everything here is installed to `/usr/local/bin/` on the node. The current
version is in `version.txt`; `MANET/etc/manet_version.txt` must always match it,
because `node-update.sh` compares the two to decide whether a node is current.

Install tarballs are root-relative (`boot/`, `etc/`, `usr/`, `root/`) and packed
with numeric owner/group `0/0`. Keep the shipped binaries marked as binary in
`.gitattributes` — `morse_cli`, `chronyc`, `alfred`, `batctl`, `wpa_cli_s1g`,
`wpa_supplicant_s1g` — or line-ending normalization corrupts them.

- [Core Orchestration](#core-orchestration)
- [Web Interface](#web-interface)
- [Push-to-Talk Voice](#push-to-talk-voice)
- [Service Elections](#service-elections)
- [Channel Selection & Jamming Detection](#channel-selection--jamming-detection)
- [Discovery & Partition Healing](#discovery--partition-healing)
- [Network Management](#network-management)
- [Data Management](#data-management)
- [File Synchronization](#file-synchronization)
- [Time Synchronization](#time-synchronization)
- [Mesh Configuration Push](#mesh-configuration-push)
- [Hardware Support](#hardware-support)
- [Recovery & Identity](#recovery--identity)
- [Shutdown](#shutdown)
- [Utilities](#utilities)
- [Setup & Provisioning](#setup--provisioning)
- [Tests](#tests)

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

Scan frequencies are filtered against the interface's phy before the request
goes out (`phy_usable_freqs`). `iw scan freq` refuses the **whole** request if
any single frequency is not permitted on that phy, so one bad entry takes out
the scan for every channel on the radio and the only symptom is an empty
survey. The filter fails open: if the phy cannot be read, or nothing parses, the
requested list is used unchanged, because filtering to an empty set would take a
band off the air on every node at once.

**A solo node at the lobby neither elects nor hops.** It waits until a
tourguide brings it onto the mesh's data channels, or another radio turns up and
meshes with it there — and only then do the two run a joint election and migrate
together. One node's view of the RF is not a consensus, and a node that elected
alone then had to tourguide back to the lobby every two minutes to stay
findable. Parking costs a solo radio nothing, since there is no mesh link to
optimize. A whole-site cold start is unaffected: every node powers on into the
lobby and meshes with the others there, so each one sees peers and they
bootstrap together, which is the case the lobby dwell was built for.

**node-manager-static.sh**

Simplified orchestrator for Static Channel operation. Handles:
- Status publishing
- Registry building
- Service elections
- IP management

**node-manager.sh**

The file the `node-manager.service` unit actually runs. It is **generated**:
`radio-setup.sh` copies either `node-manager-acs.sh` or `node-manager-static.sh`
over it depending on `acs=` in `/etc/mesh.conf`. Keep all three in sync when
editing the publish path.

**The tools tarball does not carry this file.** Only the two variants ship over
the air, and `node-update.sh` re-publishes whichever one `acs=` selects after
extracting, restarting `node-manager` if the file changed. The committed copy is
the static variant, so an update that shipped it would return every ACS node to
the static orchestrator on each routine update — silently, with nothing in the
log to say why. Leaving it out entirely would be the opposite failure: both
variants arrive, but the node keeps running the previous release's orchestrator
until `radio-setup.sh` happens to run again. A mesh config change to `acs` also
re-publishes it (see [Mesh Configuration Push](#mesh-configuration-push)).

The install tarball still carries it, since a node being provisioned needs
something at that path before `radio-setup.sh` has chosen a variant.

The `acs=` test accepts `y`/`yes`/`1`/`true` case-insensitively. It compared
against `"Y"` alone until 2026-08-31, and every writer produces lowercase — both
flashers normalize the answer, the web UI writes `'y':'n'`, and
`mesh-config-sync.py` validates the key as exactly `y` or `n` — so `acs=y`
selected the static variant on every node and the ACS orchestrator never ran.

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
- `/api/peer/<ip>` — JSON: one peer's detail panel, assembled by
  `manet_peer_radios.py`. Fetched by the status page when a node is expanded.
- `/api/debug` — JSON: raw `batctl` originator, neighbor and gateway output
  alongside the registry's MAC/hostname mapping. A diagnostic dump; nothing in
  the UI calls it.

Password-gated routes — the password is `admin_password` from `/etc/mesh.conf`,
set at flash time. Login sets an HttpOnly cookie:
- `/manage/` — the management UI (see `manet_manage.py`), and every `/manage`
  route beneath it, including the radio, measurement, voice and uplink APIs.
- `/manage/login`, `/manage/logout`
- `/api/perf-auth` — POST the password, receive the cookie token.
- `/api/admin/*` — mesh config staging, ACK status, and apply.
- `/admin` — legacy path, redirects to `/manage/#config`.

There is no unauthenticated route that changes anything. A set of
`/api/control/*` handlers at the site root used to apply interface, TX power and
channel changes locally, behind nothing but the subnet check, and were removed
once every caller had moved to the Alfred-staged path — a local change and a
mesh-wide one now take the same route through `manet_manage.py`.

Access control has two layers:
- **Application:** every route is restricted to localhost and the mesh/EUD
  subnet (`ipv4_network`); management routes additionally require the cookie.
- **Kernel:** `manet-ui-firewall.sh` limits port 80 to localhost and this node's
  own DHCP clients, so the pages are unreachable from other radios, from other
  radios' EUDs, and from the uplink LAN.

Link quality shown here is BATMAN_V's metric, which is **throughput in Mbit/s**,
not a 0-255 link quality — `batctl` prints e.g. `43.2` for a 43.2 Mbit/s link.
Color thresholds are 30 / 15 / 5 Mbit/s, chosen so a healthy HaLow link (which
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
- **Voice** — talk group, codec, and per-peer voice state (see
  [Push-to-Talk Voice](#push-to-talk-voice)).
- **Node config** — the mesh configuration form (EUD mode, AP credentials, mesh
  SSID/SAE key, IP range, regulatory domain, services, admin password) with the
  per-node ACK table. This was the old unauthenticated `/admin` page.

Anything that affects other nodes is staged over Alfred (type 71) and applied
after peers ACK, never pushed node-to-node over HTTP. Measurement is the
exception and needs no coordination: it runs a local `iperf3`/`ping` client
against the peer's always-listening daemon.

**manet_radio.py**

Radio primitives shared by the UI that offers a change and the code that
applies one: `mesh-status.py` and `manet_manage.py` read state and build the
menus, `mesh-radio-state.py` applies an Alfred-staged package. One
implementation, so what the UI offers and what the node does cannot diverge.

**The HaLow channel plan is derived from the node's region.** The channel,
bandwidth and S1G operating-class tables are transcribed from the Morse driver's
`dot11ah` tables, and a bandwidth appears for a region only where the driver
defines a channel of that width — which is why **EU stops at 2 MHz** (the whole
863–868 MHz allocation is too narrow for more) while **US reaches 8 MHz**.
`halow_channel_options()` builds the menu the Radio config tab renders, so an
EU node is never offered a width it cannot use. Channel numbers and center
frequencies are both unique within a region, so either resolves the other:
`halow_bandwidth_for_channel()` recovers the width from the channel number,
which is how the status readout avoids `s1g_prim_chwidth` — that reports the
*primary* channel width, 2 MHz for every operating width above 1 MHz, and
reading it as the operating width reports a 4 or 8 MHz channel as 2 MHz.

**HaLow TX power is fixed per bandwidth by the driver and BCF**, not freely
settable: `HALOW_BW_TXPOWER_CAP_DBM` holds the caps, and a request outside them
is refused with an explanation rather than silently clamped
(`txpower_request_allowed`, `unsupported_txpower_response`). Wi-Fi TX power
options come from the phy's own advertised range (`parse_phy_txpower_options`),
and a set is read back and verified rather than assumed
(`set_iface_txpower_verified`).

An unknown region falls back to the EU plan — the narrower of the two, so a
misconfigured node cannot be offered channels its region may not permit.

**mesh-radio-state.py**

Applies Alfred-coordinated radio changes. Reads a staged package from type 71,
publishes an ACK on type 72, and applies at the package's `activate_at` once the
coordinator has collected ACKs. Carries interface up/down, TX power, HaLow and
Wi-Fi channel, and uplink credentials.

**manet_peer_radios.py**

Builds the per-peer radio chips and the expandable status panel the topology
views show for another node — role, up/down state, channel, MCS, service pills,
and the inferred `bat0` / `br0` / gateway rows.

Everything comes from what Alfred already replicates, so no node is queried.
Where a peer publishes `INTERFACES_JSON`, that wins. Where it publishes an empty
list — which `node-manager` did historically, leaving the UI with no
`wlan0`/`wlan1`/`wlan2` keys and every peer rendered as 2.4G/5G/HaLow OFF — the
values are reconstructed from the registry's MCS and `DATA_CHANNEL_*` fields, so
an older node still shows its radios.

**manet-ui-firewall.sh**

Installs the nftables rules described above: port 80 restricted to localhost and
this node's DHCP pool, port 5201 (iperf3) to the mesh subnet. Uses source
addresses rather than interfaces, because `br0` bridges `bat0` — a packet from a
remote node arrives on `br0` exactly like one from a local EUD. Re-run by
`mesh-ip-manager.sh` whenever the DHCP pool moves.

---

## Push-to-Talk Voice

**mesh-voice.py**

Conference-style PTT voice across the mesh, with a headset PTT plugged into the
node itself — there is no browser microphone and the web UI is a status and
talk-group readout only. Talking is gated by the button, but listening mixes
everyone, so two people speaking at once are summed rather than corrupting each
other.

The audio path is entirely GStreamer, so no Python code runs on the audio
thread:

```
TX  alsasrc -> level -> valve -> <enc> -> <pay> -> multiudpsink
RX  udpsrc  -> rtpbin -+-> <depay> -> <dec> -\
                       +-> <depay> -> <dec> --+-> audiomixer -> alsasink
                                (one branch per talker)
```

`voice_codec` selects the codec pair: `lyra` (the default; needs
`libgstlyra.so` and the model weights) or `opus` (stock elements). A node
missing the lyra plugin or the model directory falls back to opus and logs why.

**The two codecs do not interoperate, and the failure mode is silence.** Both
the RTP payload type and the clock rate come from this one setting, and a
receiver builds every decode branch from its *own* configured codec rather
than from what actually arrived — so a node left on the other codec does not
get degraded audio, it gets nothing. That is why the codec is a mesh-wide
setting staged over Alfred (see below) and not a per-radio one like the talk
group, and it is why the silent fallback above matters: on a Lyra mesh, a node
whose plugin is missing is both deaf and mute. Check for the fallback log line
after any install.

The daemon only supervises: it reads the PTT button, keeps the unicast peer
list current, and publishes `/run/mesh-voice.json` for the UI.

**Addressing.** Every talk group shares one multicast group (`239.192.41.1`)
and differs only by port — channel *n* uses `38801 + (n-1)*2`, the stride being
2 because `port+1` is that channel's RTCP. Sharing the group address means
changing channel never causes an IGMP leave/join.

**Unicast redundancy: off by default — batman-adv already does it.** With
`multicast_forceflood` disabled, as this node configures it, and listeners at or below
`multicast_fanout` (default 16), `batadv_mcast_forw_mode_by_count()` returns
`BATADV_FORW_UCASTS` and emits **one unicast frame per listener**. Measured on
the bench: 200 multicast packets produced exactly 200 unicast frames addressed
to the peer's MAC on `wlan2`, with no broadcast frames above baseline, and the
peer received all 200. Those frames already get 802.11 ACKs and retries, so a
userspace unicast copy per peer would double airtime for no extra reliability.

`voice_unicast=y` remains available for the two cases where it stops being
redundant: more than `multicast_fanout` listeners, where batman-adv falls back
to `BATADV_FORW_BCAST`; and any future configuration that enables
`multicast_forceflood`. When enabled, `multiudpsink`'s `clients` property is
rewritten live from the registry rather than from learned senders, because in a
PTT system a node that has never transmitted is exactly the one that needs to
hear you.

**Receivers must join the group or nothing transmits.** The same optimization
means `batadv_mcast_forw_mode()` returns `BATADV_FORW_NONE` — dropping the
packet at the *sender* — when no node has announced interest in the group. This
was observed directly: multicast sent with no listener never reached the radio
at all. `udpsrc` performs the IGMP join (`auto-multicast=true`), so this works
in normal operation, but expect a brief window after start-up before joins
propagate, and note that a sender with no listeners is silently idle rather
than wasting air.

**Receive is conference style; transmit is still push-to-talk.** `rtpbin`
demultiplexes by SSRC and gives every talker their own jitter buffer, and
`audiomixer` sums them — so two people speaking at once are *mixed*, the way a
conference bridge behaves. Nobody is hot-miked: the valve stays shut until the
button is pressed. What is gone is the software lockout on talking over
someone; that is etiquette now, like any conference bridge.
`voice_half_duplex=y` restores the refuse-to-key behavior if you want it.

This is not just a policy change. A single `rtpjitterbuffer` cannot do it — two
senders' sequence numbers interleave in one buffer and the output is garbage.
That limitation, not policy, is what the old lockout was really working around.
Verified by feeding two senders (440 Hz and 880 Hz, distinct SSRCs) into the
receive pipeline: with one talker only 440 Hz is present; with both, 440 Hz and
880 Hz appear together at comparable amplitude.

A permanent silent input feeds the mixer, because `audiomixer` only produces
output while it has one — without it the sink is starved whenever nobody is
talking and every transmission starts with the DAC spinning up. Branches are
built on `pad-added` and torn down on `pad-removed`, so a decoder is not leaked
per talker; measured, two idle talkers were reaped and the count returned to 0.

**Decode branches are kept, not reaped — this is what stops transmissions
being clipped.** `rtpbin autoremove=false` (its own default) means a talker's
branch survives their silence, so the next thing they say plays from the first
frame. Measured on a CM4 with lyra, 3.00 s bursts:

| receiver state when the talker keys up | audio arrived | lost |
|---|---|---|
| branch rebuilt on demand | 2.82 s | 180 ms |
| branch pre-built and attached | 2.92 s | 80 ms |
| talker already established | 3.04 s | none |

Only a source `rtpbin` has already seen costs nothing. Blocking the pad during
construction, lowering RTP source probation, and raising the mixer's
`min-upstream-latency` were each tried and none of them helped — the residual
is `rtpbin` establishing a new source rather than the pipeline linking, so the
fix is to ensure the source is not new.

**Pre-establishing a talker: only the sender can do it.** A receive slot in
`rtpbin` is keyed by **SSRC, not by IP**, and it exists only once a packet
carrying that SSRC arrives. Two pieces close that gap.

First, the SSRC is split: **24 bits identifying the node** (a hash of its mesh
address) and **8 bits identifying the run** of the daemon. The prefix lets any
receiver build address → prefix for every node in the registry and name a
talker with no back channel — verified collision-free across a full /24.

The generation is not cosmetic, and this is the subtle part. Because a receiver
never forgets a source, a node that restarted and reused its SSRC would find
its fresh sequence-number base did not match the source the receiver was still
holding. Measured: a three-second transmission arrived as **nothing at all**,
and neither a beacon nor `max-misorder-time`/`max-dropout-time` tuning rescued
it. A new generation makes the restarted node a new source, which is clean:

| after a sender restart | speech arrived of 3.00 s |
|---|---|
| same SSRC reused | **never arrived** |
| new generation | 2.96 s |
| new generation + beacon | **3.00 s** |

Second, each node sends a **presence beacon**: a ~140 ms muted transmission
(`volume` to 0, valve open, valve shut, volume back) carrying real RTP from the
real payloader with real sequence numbers, so receivers establish the source
before anything is said.

Beacons are **event driven, not a heartbeat**. There is nothing to refresh —
`autoremove=false` means a source is never forgotten — so one is sent at
start-up (announcing this run's generation) and whenever a node appears in the
registry that cannot yet have heard this node. `voice_beacon_sec` (default 600) is
only a safety net for a peer whose arrival was somehow missed, and 0 disables
it. That is about **21 packets an hour, ~5 bps averaged**, against 420/hour at
the 30 s heartbeat this replaced.

**Synthesizing a source locally from the registry does not work — it is worse
than doing nothing.** Every peer's address is already known, so the apparent
solution is to inject a packet with their SSRC and pre-fill the slot. Measured,
it does create the slot, and then it destroys the stream. The injected sequence
numbers and timestamps become the source's base; the real sender's do not
match; the jitter buffer resyncs and discards. The peer talked for three
seconds and the output was **digital silence, peak amplitude zero**, with
`rtpjitterbuffer` logging a single `resync`. Do not reintroduce this.

**Table size follows the node registry.** Every known node is a potential
talker, and an evicted one pays the first-contact penalty again, so
`voice_max_talkers` (default 8) is a floor rather than a fixed size: on each
registry poll the table is raised to known nodes + 2 headroom. It never
shrinks below the configured value and never exceeds the hard cap of 64.

**The ceiling is RAM.** A warm branch costs about **5.3 MB with lyra** and
**0.6 MB with opus**, measured on a CM4. So the hard cap of 64 is roughly
340 MB of lyra decoders against the 3.4 GB a node has free — comfortable, but
it is the reason a cap exists at all rather than tracking the registry without
limit. A mesh larger than 62 nodes on one talk group will log that it is
capped, and the least recently heard talkers will be evicted and pay the
first-contact delay when they next speak. If that ever matters, raising
`VOICE_MAX_TALKERS_HARD` is safe until roughly 600 lyra branches on a 3.7 GB
node, at which point the decoders, not the audio, are the constraint.

`ignore-inactive-pads` on the mixer is required rather than optional once
branches are kept: they are silent between transmissions and the mixer would
otherwise wait on them.

**Buffering: there are no queues.** Neither pipeline contains a `queue`
element. Each is a single push thread from source to sink, and every buffer in
the audio path is one of four things:

| Where | Size | Set by |
|---|---|---|
| ALSA capture and playback rings | driver defaults, not configured | `alsasrc` / `alsasink` |
| Kernel UDP receive socket | 1 MB (`buffer-size=1048576`) | `udpsrc` |
| Per-talker RTP jitter buffer | `voice_jitter_ms`, default 100 ms | `rtpbin latency=` |
| Mixer blend window | internal | `audiomixer` |

The socket buffer is burst tolerance, not a working set: steady-state traffic
is around 20 kbps, and 1 MB is there so a scheduling stall cannot cost
packets before `udpsrc` runs again.

Both sinks run `sync=false`, for different reasons. On transmit `alsasrc` is
the clock for a live capture, so there is nothing for the sink to synchronize
to. On receive the jitter buffer already does the timing, so making `alsasink`
wait on running time as well would only add latency.

**The jitter buffer is per talker and sized once.** `rtpbin` creates one
`rtpjitterbuffer` per SSRC, and its depth is
`max(voice_jitter_ms, two packets)` — the two-packet floor being the least
that can absorb a single late packet. At the default 40 ms packing that
resolves to 100 ms.

That figure is computed when the pipelines are built, from the packing in
force at the time, and nothing resizes it while the pipeline runs. So if
adaptive packing climbs to the top of its range — 3 frames, 60 ms packets —
a 100 ms buffer is 1.67 packets deep rather than the 2 the formula intends,
until the next rebuild. (A SIGHUP retune rebuilds both pipelines, so it also
re-sizes the buffer for the packing then in force.) This is a margin
question rather than a fault, and the coupling runs in the safe direction: the
controller shrinks packets under loss, which *deepens* the buffer in packet
terms, and only grows them after 30 s below 1 % loss. Note also that a
receiver's buffer has to suit what the *senders* are transmitting, which this
node cannot know and which the formula has never tracked. If the floor is ever
wanted at its stated value, raise `voice_jitter_ms` to 120 rather than
resizing at runtime — a jitter buffer that resyncs mid-stream discards the
transmission outright, which is the same failure the SSRC generation byte
exists to prevent.

`do-lost=true` makes `rtpbin` emit a gap event for every lost packet so the
decoder conceals rather than glitches; `opusdec` additionally runs `plc=true`
and in-band FEC, and Lyra conceals internally.

**Transmit does not buffer at all.** The PTT valve *drops* upstream buffers
while the button is released rather than holding them, so keying the
microphone plays live audio rather than flushing a backlog of whatever the
capture device collected beforehand. The only delay on the transmit side is
packetization: 20 ms per Lyra frame times `frames-per-packet` must accumulate
before a packet leaves.

**End-to-end latency has never been measured.** From the settings it is
roughly 40 ms of packetization plus 100 ms of jitter buffer plus the ALSA
periods and codec at each end, so on the order of 150–200 ms mouth to ear at
the defaults. Treat that as arithmetic, not as a measurement.

Loss accounting comes from the same buffers. `num-pushed`, `num-lost`,
`num-late` and `num-duplicates` are summed across every talker's jitter buffer
and written to `/run/mesh-voice.json` every 2 s; the same window drives
adaptive packing, ignoring any window carrying fewer than 25 packets.

**Codec is mesh-wide and staged like a channel change.** The CODEC card in
the VOICE tab posts to `/api/voice/codec`, which calls
`coordinate_radio_change({'voice_codec': ...})` — the same two-phase path as a
HaLow channel or WPA key change. The package is staged over Alfred, every node
ACKs it, and only then is `activate_at` set ~20 s out so the whole fleet
switches on a common clock. If any node fails to ACK, the change is canceled
and nothing moves: staying wholly on the old codec is always better than
splitting the mesh in half. On activation each node runs
`apply_voice_codec()` (in `manet_radio.py`), which rewrites `voice_codec` in
`/etc/mesh.conf` and *restarts* mesh-voice — a restart rather than a reload,
because swapping the codec changes the encoder, payloader, RTP clock rate and
the raw caps either side of them, so both pipelines have to be rebuilt.

A node that was powered off during the change comes back on the old codec and
is mutually inaudible until it is changed too. There is no automatic
reconciliation; re-issue the change from the UI with the node up.

**Talk group is per-radio and changed at runtime.** Every node is flashed on
group 1; the operator moves it from the VOICE tab of the web UI, which writes
`voice_channel` to `/etc/mesh.conf` and runs `systemctl reload mesh-voice`.
This is deliberately *not* pushed across the mesh the way HaLow and Wi-Fi
channel changes are — each operator picks their own group.

Reload is a `SIGHUP`, and the daemon retunes in place rather than restarting:
it drops both pipelines to `NULL`, rebuilds them on the new port and returns to
`PLAYING`, holding the PTT valve open if the operator was mid-transmission.
Only the talk group is re-read; codec, bitrate and audio devices are start-up
settings, because rebuilding the audio path under someone's thumb on the PTT is
a good way to lose a transmission mid-word. If the rebuild fails the daemon
reverts to the previous group rather than leaving the node deaf.

Retuning in place rather than restarting the unit is what makes a hardware
channel selector practical — clicking through groups on a rotary switch would
otherwise mean a systemd restart per detent, several seconds each, plus a
TFLite model reload under lyra. Anything that can write `mesh.conf` and send
`SIGHUP` drives this, so the web UI and a future panel switch share one path.

**Adaptive packing: the lever is packet size, not codec bitrate.** With
`voice_codec=lyra` the daemon adapts `frames-per-packet` to measured receive
loss, and deliberately does *not* adapt bitrate. The reason is measured. On the
HaLow link a batman-adv frame carrying one 20 ms Lyra frame is 101 bytes, of
which 86 is header, so at 6 kbps the packet overhead dominates completely:

| frames/packet | interval | on-air | a lost packet costs | listening test at 10 % loss |
|---|---|---|---|---|
| 1 | 20 ms | 40.4 kbps | 20 ms | clean |
| 2 | 40 ms | 23.2 kbps | 40 ms | barely audible |
| 3 | 60 ms | 17.5 kbps | 60 ms | audible glitches |
| 4 | 80 ms | 14.6 kbps | 80 ms | unpleasant |

Going 1 → 2 frames/packet takes **43 %** off the wire. Dropping the codec from
6000 to 3200 bps takes **12 %** off (23.2 → 20.4 kbps at 40 ms) and is plainly
audible. So bitrate stays fixed and packing moves.

The direction is the opposite of the intuitive one: **under loss, packetization
gets smaller.** A lost packet takes `frames-per-packet` frames with it, and the
audibility knee sits exactly in this range, so the loss response spends airtime
to keep each loss short enough for Lyra's concealment to hide. That is only
safe while loss means fades rather than congestion — on a saturated link,
offering more packets makes it worse — so the controller will not go below 2
frames/packet when batman-adv's throughput estimate for the worst neighbor is
under 2 Mbit/s.

Loss above 5 % steps down immediately; recovery needs 30 s below 1 % per step,
and anything in between holds position. Windows with fewer than 25 packets are
ignored rather than treated as clean. The signal is this node's *own* receive loss —
plain multicast RTP has no back channel — which half duplex makes a fair proxy,
since it measures the same link in the other direction moments before keying
up. An asymmetric link will fool it.

No signaling is needed for any of this: Lyra frames are a fixed size per
bitrate (8/15/23 bytes), so `rtplyradepay` recovers the packet geometry from
the payload length alone and follows a mid-stream change with no renegotiation.
Verified on hardware — switching 2 → 1 → 3 → 2 while playing produced 27/42/57
byte payloads and 1001 frames decoded against 1000 sent.

**The mic input expects an electret.** The CM108B biases `MIC+` through its own
network, which suits the electret capsule in a commercial headset. A **dynamic**
element — as fitted to NATO-wired military headsets — is roughly 25 dB quieter and
has no internal amplifier, so it sits at or below the input's own noise floor and
no mixer or EEPROM setting recovers it. Such a headset needs an external preamp
between the element and `MIC+`. Two further points apply once one is fitted: turn
capture AGC **off**, since it winds gain up chasing a quiet source and costs about
24 dB of noise floor, and keep the analog sidetone at its minimum, or the added
gain closes an acoustic loop between the earpiece and a boom microphone.

**PTT is USB, not GPIO.** The OpenVLM board is a C-Media CM108B; the switch
lands on the *codec's* GPIO3 and is read from USB HID input reports on
`/dev/hidraw*` — bit 2 of IR1, valid only when `IR0[7:6] == 0`. Bit 0 of IR1 is
the strap that distinguishes a real OpenVLM from a generic CM108 dongle, and
strapped devices are preferred when both are present. Hot-plug is handled by
reopening; the daemon runs fine with no board fitted and picks one up when it
appears.

**QoS — use CS6 (48), not EF (46).** This is counter-intuitive and worth
understanding before changing `voice_dscp`.

Linux 6.12+ added an RFC 8325 mapping to `cfg80211_classify8021d()`
(`net/wireless/util.c`) that sends DSCP 46/EF to 802.1d UP 6, i.e. WMM AC_VO.
On 6.6 and older the naive `dscp >> 5` rule sent it to UP 5 / AC_VI. We ship
6.18 everywhere, so on a plain wireless interface EF would be correct.

**batman-adv never lets that code run.** `batadv_skb_set_priority()`
(`net/batman-adv/main.c`) is called from `batadv_interface_tx()` for every
packet entering `bat0` — locally originated included — and from both forwarding
paths in `routing.c`. It stamps:

```c
if (skb->priority >= 256 && skb->priority <= 263) return;  /* already set */
prio = (ipv4_get_dsfield(ip_hdr) & 0xfc) >> 5;             /* the OLD rule */
skb->priority = prio + 256;
```

Values 256–263 are the 802.1d passthrough range, which `cfg80211_classify8021d()`
checks **first** and returns directly as the UP — so the RFC 8325 DSCP code is
never reached. The result is kernel-version independent:

| DSCP | TOS | batman-adv priority | 802.11 UP | Access category |
|---|---|---|---|---|
| 46 (EF) | 0xB8 | 261 | 5 | AC_VI (video) |
| **48 (CS6)** | **0xC0** | **262** | **6** | **AC_VO (voice)** |

Hence the default of 48. The `return` guard also means an explicitly-set
`SO_PRIORITY` in 256–263 survives batman-adv, which is how OpenMANET pins the
access category — they set `IP_TOS` and `SO_PRIORITY` together, in that order,
because the kernel rewrites `sk_priority` as a side effect of `IP_TOS`.
GStreamer's `multiudpsink` exposes only `qos-dscp`, so batman-adv's derivation
is relied on instead — which is why the DSCP value has to be chosen for what
batman-adv will make of it.

Multicast TTL is set explicitly to 32: the default of 1 silently black-holes
voice one hop out.

Multicast tuning is deliberately untouched — the mesh's arrangement is the way
it is on purpose, and `multicast_forceflood` stays off, which is what leaves
batman-adv's own multicast→unicast fanout available.

**Bandwidth: headers dominate, not the codec.** Every packet carries 12 B RTP +
8 UDP + 20 IP + 14 Ethernet = 54 B of overhead. At 20 ms framing that is 50
packets/sec, so ~21.6 kbps is spent on headers no matter which codec is used.
Measured on-wire cost:

| Opus bitrate | 20 ms frames | 60 ms frames |
|---|---|---|
| 24 kbps | 45.6 kbps | 31.8 kbps |
| 16 kbps | 37.6 kbps | **23.7 kbps** |
| 12 kbps | 33.6 kbps | 19.6 kbps |
| 8 kbps | 29.6 kbps | 15.5 kbps |

Frame size is therefore the larger lever: 16 kbps at 60 ms costs less on air
than 6 kbps at 20 ms (27.7 kbps) and sounds far better. The cost is latency —
a 60 ms frame adds 60 ms — and coarser loss, since one dropped packet now takes
60 ms of audio with it. The jitter buffer floor is raised to two frames at
start-up — see Buffering above for what that does and does not cover. For a
PTT system where the multiplier is unicast redundancy rather than continuous
full-duplex, 40–60 ms is usually the right trade.

Config keys in `/etc/mesh.conf`:

| Key | Default | Meaning |
|-----|---------|---------|
| `voice` | `n` | Master enable; `n` makes the daemon exit 0 immediately |
| `voice_iface` | `br0` | Interface for the multicast group and send socket |
| `voice_channel` | `1` | Talk group, 1–32 |
| `voice_ptt` | `openvlm` | `openvlm`, `always` (open mic), or anything else for receive-only |
| `voice_unicast` | `n` | Userspace unicast copies — normally unnecessary, see above |
| `voice_unicast_max_peers` | `16` | Cap on unicast copies |
| `voice_half_duplex` | `n` | Refuse PTT while a remote node is transmitting — off, see below |
| `voice_max_talkers` | `8` | Floor for warm decode branches; raised to registry nodes + 2, hard cap 64 (~5.3 MB each with lyra) |
| `voice_beacon_sec` | `600` | Safety-net beacon interval; beacons are normally sent on start-up and on new peers. 0 disables |
| `voice_dscp` | `48` | DSCP marking — CS6, see the QoS note above |
| `voice_codec` | `lyra` | `lyra` or `opus`; lyra falls back to opus if the plugin or model weights are missing. Mesh-wide — change it from the UI, not by hand, or the node goes silent |
| `voice_bitrate` | `32000` | Opus bitrate |
| `voice_frame_ms` | `20` | Opus frame duration: 10, 20, 40 or 60 |
| `voice_lyra_bitrate` | `6000` | Lyra rate: 3200, 6000 or 9200 only |
| `voice_lyra_frames_per_packet` | `2` | Starting packing; adapts at runtime, see below |
| `voice_lyra_model` | `/usr/local/share/lyra/model_coeffs` | Lyra model weights |
| `voice_jitter_ms` | `100` | Jitter buffer depth per talker; raised to two packets at start-up |
| `voice_loss_pct` | `20` | Opus in-band FEC expected-loss level |
| `voice_ttl` | `32` | Multicast TTL |
| `voice_alsa_in` / `voice_alsa_out` | auto | Override ALSA devices; empty autodetects the OpenVLM card |
| `voice_test_tone` | `n` | Bench mode: 440 Hz tone in, null sink out, so the transport can be proven with no audio hardware fitted |

---

## Service Elections

All service elections share the same algorithm: the best-connected node wins,
measured by `MEAN_THROUGHPUT_MBPS` in the registry — the mean of BATMAN_V's
metric across that node's originators, in Mbit/s. Stale nodes (not seen within
10 minutes) are excluded. Ties are broken deterministically by MAC address.

This field used to be called `TQ_AVERAGE`, which was wrong: BATMAN_V's metric is
throughput, not a 0-255 link quality. The behavior never changed — highest
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
- Includes channel bias to prevent unnecessary migrations.

Candidate frequencies are deliberately **not** filtered against the local phy
here. The election reaches its answer by implicit consensus — every node runs
the same computation over the same replicated reports — so a per-node hardware
filter at this stage would let two nodes derive different winners from identical
data. Unusable frequencies are dropped at scan time instead (`phy_usable_freqs`
in `node-manager-acs.sh`), which keeps the exclusion inside the replicated
report and therefore symmetric across the mesh.

**"Every candidate was measured and rejected" and "nothing reported a
measurement at all" are different verdicts.** Both leave the candidate list
empty. The first is a real RF result and drops to the lobby channels. The
second is an outage — the band's radio is absent so its scan report carries no
entries, or the scan request was refused wholesale, or Alfred was down and no
reports replicated — and it now **holds the current channel and does not assert
limp mode**, logging `No scan data for any candidate channel`. Treating it as
jamming throttled the whole mesh to legacy bitrates on the strength of missing
data.

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
2. Hops one radio to lobby frequency — 2.4 or 5 GHz, alternating.
3. Broadcasts helper beacon with current data channels.
4. Listens for other partitions.
5. If the other partition should win, triggers migration.
6. Returns to data channel.

**Which radio hops is derived from the clock, never from this node's own
history** — `(epoch / 120) % 2`, the same window index `should_perform_tourguide`
uses, so it inherits the wall-clock alignment the rest of the ACS pipeline
already needs and adds no new time-sync requirement. Two split partitions can
only find each other if their tourguides hop to the same band in the same
window. Reading this node's last-used radio out of the registry went permanently
out of phase the moment either side missed a window — an elected tourguide
excluded for hosting a service, a restart, a failed hop — after which the two
partitions alternated to opposite bands forever and partition healing was
silently dead. For a 2-node mesh that is the only recovery path there is:
`quorum-checker.sh` cannot rescue an isolated node below 3 remembered peers.

**The smaller partition migrates; equal sizes break the tie on MAC**, lowest
stays put. Both tourguides run the comparison in the same window and each sees
the other's MAC, so exactly one moves. The tie-break is deliberately not the
config string: deciding a split by channel number biases every equal-size merge
toward the numerically lower pair, and can pull a node straight back onto the
channel it fled. Identity is neutral, and the next election re-optimizes the
channel once both sides are talking again.

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

Chunk size is uniform across the mesh — `max_euds_per_node + 2`, set at flash
time, which is why `mesh_config.py` keeps that key display-only in the
management UI. There is no per-node override: pinning a node's chunk by hand
skips the registry check and the MAC tie-break, so two pinned nodes, or a pinned
chunk that a peer later claims, collide with nothing left to resolve them. An
`/etc/manet/mesh-ip-force.conf` mechanism that did this was removed.

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

**mesh-throughput-mean.sh**

Mean of BATMAN_V's metric across this node's originators, in Mbit/s — published
as `MEAN_THROUGHPUT_MBPS` and used by the service elections.

Reads the parenthesized value, never a positional field: `batctl o` shifts
columns on the selected route (prefixed `*`), so `$3` is the last-seen timestamp
there and `(` on the others. Averaging `$3`, as this did until 2026-08-16,
produced 0.14 on a mesh running at 43 Mbit/s. Keeps the best path per
originator, so one peer reachable over two radios is still one peer.

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
- Compile with: `protoc --python_out=. NodeInfo.proto`, from **the dev venv**:

  ```bash
  source ~/.venvs/manet/bin/activate    # bash MANET/packaging/setup-dev-env.sh
  cd MANET/node_tools && protoc --python_out=. NodeInfo.proto
  ```

- **Use protoc 3.21.x.** Nodes run the protobuf 4.21.12 Python runtime, which
  rejects generated code from protoc older than 3.19, and 5.x emits a
  `runtime_version` gate that runtime does not have.
- **Not `/usr/bin/protoc`.** Ubuntu 22.04 ships 3.12.4, which emits the old
  `_reflection`-based form — 786 lines different, and unimportable on every
  node. This is easy to do by accident and nothing downstream catches it: a dev
  box with the matching-vintage protobuf runtime imports the bad file happily.
  `setup-dev-env.sh` puts the right protoc inside the venv precisely so
  activating it cannot give you one without the other.
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

**mesh-config-sync.py**

The receiving half. Each node-manager cycle it reads the newest package from
Alfred type 70, validates it, stages it to `/var/run/mesh_pending_config.json`,
publishes an ACK by writing `/var/run/mesh_config_ack_version`, and runs
`mesh-config-apply.sh` once `activate_at` has passed.

Everything in a package is remote input that ends up in `/etc/mesh.conf` and in
wpa_supplicant configs, so it is validated rather than trusted: keys are
whitelisted, and values are rejected if they contain a quote, newline or NUL, or
fail a per-key check (CIDR shape, SSID and SAE key lengths, enum values). A peer
cannot use a config broadcast to write arbitrary supplicant configuration.

**mesh_config.py**

The one place that decides which settings are this node's own and which belong
to the whole mesh. `LOCAL_KEYS` is `eud`, `lan_ap_ssid`, `lan_ap_key` and
`max_euds_per_node`; `split_config()` partitions a submitted form into a local
half and a mesh half, and `strip_local_keys()` is what keeps the local half off
Alfred. The EUD AP is a node's own Wi-Fi for its own clients, and broadcasting
it renamed every AP on the mesh.

`max_euds_per_node` is stripped with the others but excluded from
`LOCAL_APPLY_KEYS`: it sizes the IPv4 chunk allocated at flash time, so the
management UI shows it and never writes it. Shared by `mesh-config-sync.py`,
`mesh-status.py` and `manet_manage.py`, so the receiving end and the submitting
end cannot disagree about which half a key is in.

**mesh-config-apply.sh**

Applies a staged package to `/etc/mesh.conf`, splitting settings into three
classes:

- **Per-node** (this radio only, never Alfred): `eud`, `lan_ap_ssid`,
  `lan_ap_key`. `max_euds_per_node` is also stripped from Alfred but is not
  written from the management UI (set at flash).
- **Safe** (applied mesh-wide immediately): `admin_password`, `mtx`,
  `mumble`, `auto_update`.
- **Deferred** (written now, radio effect later): `regulatory_domain`, `acs`.
  The regulatory domain reaches the radios through
  `/etc/modprobe.d/{cfg80211,morse}.conf` and the supplicant `country_code`,
  all of which `radio-setup.sh` writes from `mesh.conf`, so the value is
  persisted here and takes hold at the next boot. `acs` selects which
  orchestrator variant is copied over `node-manager.sh` and is applied at once,
  with a `node-manager` restart.
- **Dangerous** (brief mesh outage): `mesh_ssid`, `mesh_key`, `ipv4_network` —
  these rewrite the supplicant configs and restart the supplicants.

Every key `mesh-config-sync.py` admits must fall into one of those three
classes. `regulatory_domain` and `acs` were validated and staged but matched no
apply block, so a change to either ACKed, reported itself applied, and wrote
nothing.

Also callable by hand for testing: `mesh-config-apply.sh --force`.

**mesh-config-rollback.sh**

The safety net for dangerous changes. A wrong mesh key takes the mesh down, and
with it the only way to push a correction — so each node has to be able to undo
the change on its own, with no help from the network.

`arm` snapshots `/etc/mesh.conf` and the supplicant configs, records how many
batman peers the node had, and sets a deadline (default 300 s, `MANET_ROLLBACK_GRACE`).
`check` runs every node-manager cycle and is a no-op until the deadline passes,
then either commits or restores and restarts the supplicants. State lives in
`/var/lib` because a dangerous apply can end in a reboot.

A node that had no peers before the change commits rather than rolling back —
on a solo bench node "the mesh did not come back" cannot be told apart from
"there was never anyone there".

### The whole flow

1. **Stage** — the UI writes a package and broadcasts it on type 70 with
   `activate_at=0`.
2. **ACK** — every node stages it and writes its ACK version; the node managers
   carry that into telemetry, which fills the ACK table. A change to the ACK
   brings the next publish forward so the table fills in seconds.
3. **Apply** — the operator presses Apply once the table shows 100%.
   `/api/admin/activate` refuses until then; **Force Apply** skips that gate for
   unreachable nodes.
4. **Activate** — `activate_at` is set 60 s out and rebroadcast, so every node
   applies at the same moment.
5. **Trial** — a node whose dangerous settings actually change arms the rollback
   first. If its peers come back it commits; if they do not it restores itself.
   **Skip the safety net** in the UI sets `no_rollback` in the package for a
   change the operator wants kept regardless.

Danger is judged per node against local values, so re-broadcasting the SSID a
node already has does not put it into a five-minute trial window.

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

Boot-progress LED states, and an on-demand neighbor-count blink sequence.
Both need libgpiod **v2** syntax and both exit 0 immediately if the configured
GPIO chip does not exist — the pin wiring is not finalized, so on current
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
- In `--routine` mode: runs silently, rate-limited to once per 24 hours by the
  mtime of `/etc/manet_version.txt`.

The only thing that ever calls it is the networkd-dispatcher carrier hook,
`carrier.d/50-ethernet-detect` — there is no cron job. That hook runs it once
Ethernet gets carrier and a ping succeeds, and only when `/etc/mesh.conf` has
`auto_update=` set to a true value. See
[networkd-dispatcher/README.md](../networkd-dispatcher/README.md).

**Publish in the right order.** The remote *version* is read from GitHub `main`
while the *tarball* comes from colorado-governor.com, so the tarballs have to be
uploaded before the version bump is pushed. The 24 h mtime throttle does not
protect against getting this wrong: `tar` restores the build machine's mtime,
and a published tarball is normally already older than a day.
- Version metadata still points at `very-srs/MANET`; that upstream now has matching `.gitattributes` binary protection on all branches as of 2026-05-03.

---

## Setup & Provisioning

The first-boot stage that runs before `radio-setup.sh` — apt dependencies, the
install tarball, the Morse firmware, and the base networkd/nftables/radvd
config — is not in this directory. It is generated as
`/usr/local/bin/provision-mesh.sh` from
[`MANET/provisioning/firstrun.sh.template`](../provisioning/firstrun.sh.template)
at flash time, with the operator's answers substituted in, and runs from
`mesh-provision.service`. See
[provisioning/README.md](../provisioning/README.md).

**manet-ap-guard.sh**

Decides, for one interface, whether a mesh supplicant may start on it right
now. Installed as an `ExecCondition` on `wpa_supplicant@.service` through
`/etc/systemd/system/wpa_supplicant@.service.d/10-manet-ap-guard.conf`, so it
applies to every caller rather than to whichever call site was remembered —
twelve places in this directory restart `wpa_supplicant@<iface>` from
interface lists assembled in different ways.

The AP radio legitimately needs a mesh config on disk: in wired EUD mode it is
always a mesh interface, and in auto mode it joins the mesh whenever an EUD
appears on Ethernet. Only `ethernet-autodetect.sh` makes that call, and it
stops hostapd first. Any other start while hostapd holds the radio fails the
mesh join with -95 and, worse, deinits the netdev on the way out — leaving
hostapd `active` over a dead BSS, logging nothing.

Exits 0 to allow (not the AP radio, or hostapd is not holding it), 1 to skip.

**manet-power-status.sh**

Answers one question on every SSH login, and on the web status page: is this
board getting enough power? Installed as `/etc/update-motd.d/55-manet-power`,
read by `mesh-status.py` for `/api/local`, and runnable directly. Pass
`--json` for the machine-readable form.

The carrier boards sit at the edge of their envelope with a HaLow card and a
PCIe Wi-Fi card drawing at once, and a sagging supply does not announce
itself — it presents as a radio that will not associate, a USB card that stops
answering, or a board that resets with no shutdown in the journal. Hours have
gone into chasing those as driver bugs.

- **ok** — one line.
- **notice** — throttling has occurred, but no under-voltage.
- **warning** — under-voltage or throttling has occurred since boot.
- **critical** — under-voltage right now.

Decodes the `vcgencmd get_throttled` bitmask; the sticky bits 16+ matter most,
because the event that killed a radio is over by the time anyone logs in.
Prints nothing and reports `available: false` on hardware without the Broadcom
mailbox, such as the Rock 3A. It never exits non-zero.

**manet-provision-status.sh**

Answers one question on every SSH login: is this node finished setting itself
up? Installed as `/etc/update-motd.d/50-manet-provision`, and runnable directly.
It also reports the outcome of the operator setup scripts, when any were staged
— see `manet-user-scripts.sh` below.

- **running** — a banner saying not to disconnect, with how long it has been going.
- **incomplete** — a loud banner listing what failed and how to retry.
- **complete** — a single line with the version and when it finished.

Reads `/var/lib/manet-provision.state` and `/var/lib/manet-provision.failures`.
Prints nothing when there is no state file, so nodes provisioned before this
existed look normal. It never exits non-zero — a motd hook that errors breaks
the login banner.

**radio-setup.sh**

First-boot configuration script. Sets up:
- Interface renaming (mesh, HaLow, AP separation).
- WPA supplicant configs per interface.
- Network services (alfred, batman, radvd, chrony).
- Optional services (MediaMTX, Mumble).
- Optional GPS/NTP support through `gpsd`, `gps-reader.service`, and chrony `SHM 0`.
- Systemd services for node manager.
- Called once via `radio-setup-run-once.service`, then disabled — **but only if
  everything worked**.

Provisioning takes several reboots and about ten minutes, and every `apt` call
here deliberately continues on failure, because a node with no GPS tooling is
still a useful node. What is not acceptable is calling such a node finished.

`provision_fail` records any step that did not work, and the end of the script
refuses to mark the node provisioned if the list is non-empty: it writes
`STATE=incomplete`, leaves `radio-setup-run-once.service` **enabled** so the next
boot retries, and exits 1. `manet-provision-status.sh` then reports it on login.

`have_package_network` is checked before each apt phase, so "no network" is
recorded once, plainly, instead of as a wall of resolver errors.

This exists because a node reached the field with none of its packages
installed: its Ethernet was unplugged part-way through provisioning, every apt
call failed silently behind `|| true`, and the script still touched
`/var/lib/radio-setup.done`. Nothing on the node said anything was wrong.

The log is opened with `tee -a`, not `tee`. It used to truncate per run, which
destroyed the history of the run that went wrong — and two overlapping runs
interleaved into an unreadable file.

**manet-user-scripts.sh**

Runs the operator's setup scripts — the files placed in
`MANET/provisioning/additional-scripts/` before flashing. They are embedded in
`firstrun.sh` as one quoted heredoc each and written to
`/var/lib/manet-user-scripts/` on the first boot. This script runs them **once**,
under `manet-user-scripts.service`, which `radio-setup.sh` enables and starts
after provisioning completes.

Behavior:

- Scripts run as root, in `LC_ALL=C` filename order, with the working directory
  `/` and stdin on `/dev/null`.
- Each is allowed 300 s, overridable with `user_script_timeout=` in
  `/etc/mesh.conf`. That key is read with `sed` rather than sourced, because
  `mesh.conf` is operator-editable and its contents must not be executed as
  shell.
- Candidates are every regular file in the directory except dotfiles, the
  `.disabled`, `.bak`, `.orig` and `~` suffixes, and any file without `#!` on
  line one.
- Scripts are executed directly, so the shebang selects the interpreter. A
  stock node provides bash, dash, python3, perl, lua and mawk; anything else
  must be installed by an earlier script. Exit 126 and 127 are reported with
  the name of the missing interpreter rather than as a bare status.
- Exit codes are appended to `/var/lib/manet-user-scripts.state` as
  `name<TAB>exit<TAB>epoch`, one line per completed script. Output goes to
  `/var/log/manet-user-scripts.log`, appended rather than truncated.
- `--list` reports what is staged and what has run. `--force` re-runs
  everything, discarding previous state.

The service is separate from `radio-setup.sh` for three reasons. Operator code
cannot be allowed to affect the script that determines whether a node is
provisioned, so the unit is started with `--no-block` and systemd owns the run.
`radio-setup.sh` is re-runnable to apply new mesh settings, whereas a site hook
that adds a route or installs a package is not. And a one-shot unit tolerates
an interrupted run: completion is recorded per script, so a board that loses
power part-way through resumes at the script that was cut off rather than
repeating the ones that finished.

Failures are advisory. Nothing here writes to `/var/lib/manet-provision.*`, so
a failing operator script cannot cause a working node to report itself
unprovisioned, and the runner always exits 0 for the same reason.
`manet-provision-status.sh` reports the tally and names any failures on the
login banner. A script that ran and failed is not retried; only an interrupted
one is.

The shebang requirement applies on the node as well as at flash time. The
flasher never embeds a file without one, so the test only affects scripts
copied onto a live node by hand — but it is required there, because executing a
file with no shebang does not fail. The kernel refuses it and the shell falls
back to interpreting it, so a configuration file whose lines happen to parse as
shell would run and report success.

Flash-time validation is performed by the flashers rather than here; see
[additional-scripts/README.md](../provisioning/additional-scripts/README.md).
The checks in this script cover the files that reach the directory without
passing through a flasher.

---

## Tests

Three unit-test files sit alongside the code they cover — 38 tests, pure Python,
no hardware and no node:

| File | Covers |
|------|--------|
| `test_halow_plan.py` | The region HaLow plan in `manet_radio.py`: EU capping at 2 MHz and US reaching 8, channel numbers and center frequencies unique within a region and resolving each other, every region/bandwidth pair carrying an operating class, an unknown region falling back to EU, and a channel or bandwidth the region does not have being refused |
| `test_mesh_config.py` | The local/mesh key split in `mesh_config.py`: EUD and AP keys never reaching Alfred, mesh keys still going, only values that differ from `mesh.conf` counting as changes, and `max_euds_per_node` never being written |
| `test_peer_radios.py` | The peer radio chips in `manet_peer_radios.py`: frequency-to-channel conversion, published `INTERFACES_JSON` winning over the registry fallback, the fallback filling in when it is empty, and the channel fields surviving an encode/decode round trip |

Run them from the git root, so this directory is on `sys.path`, and from the dev
venv, so the protobuf runtime matches the fleet:

```bash
source ~/.venvs/manet/bin/activate    # bash ../packaging/setup-dev-env.sh
python -m unittest discover -s MANET/node_tools -p 'test_*.py'
```

The venv is not optional for the full run. One case —
`test_peer_radios.test_channel_fields_survive_encode_decode` — shells out to
`encoder.py`, which imports `NodeInfo_pb2.py`, which needs
`google.protobuf.internal.builder` from runtime 3.20 or later. A system Python
with an older protobuf fails that test; a system Python with a *newer* one is
worse, because 5.x and later accept generated code every node refuses to import,
so the test passes while proving nothing.
