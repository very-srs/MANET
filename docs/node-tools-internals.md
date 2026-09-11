# Node tools internals

Engineering record for the node runtime: reasoning,
measurements, and approaches that were tried and rejected. Operator-facing
behaviour and the config keys live in
[`MANET/node_tools/README.md`](../MANET/node_tools/README.md) and
[`MANET/networkd-dispatcher/README.md`](../MANET/networkd-dispatcher/README.md).

This file is for someone developing on the project. It is not user
documentation.

---

## Packaging and install
Everything here is installed to `/usr/local/bin/` on the node. The current
version is in `version.txt`; `MANET/etc/manet_version.txt` must always match it,
because `node-update.sh` compares the two to decide whether a node is current.

Install tarballs are root-relative (`boot/`, `etc/`, `usr/`, `root/`) and packed
with numeric owner/group `0/0`. Keep the shipped binaries marked as binary in
`.gitattributes` (`morse_cli`, `chronyc`, `alfred`, `batctl`, `wpa_cli_s1g`,
`wpa_supplicant_s1g`), or line-ending normalization corrupts them.

---

## Core orchestration

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
meshes with it there, and only then do the two run a joint election and migrate
together. One node's view of the RF is not a consensus, and a node that elected
alone then had to tourguide back to the lobby every two minutes to stay
findable. Parking costs a solo radio nothing, since there is no mesh link to
optimize. A whole-site cold start is unaffected: every node powers on into the
lobby and meshes with the others there, so each one sees peers and they
bootstrap together, which is the case the lobby dwell was built for.

editing the publish path.

**The tools tarball does not carry this file.** Only the two variants ship over
the air, and `node-update.sh` re-publishes whichever one `acs=` selects after
extracting, restarting `node-manager` if the file changed. The committed copy is
the static variant, so an update that shipped it would return every ACS node to
the static orchestrator on each routine update, silently, with nothing in the
log to say why. Leaving it out entirely would be the opposite failure: both
variants arrive, but the node keeps running the previous release's orchestrator
until `radio-setup.sh` happens to run again. A mesh config change to `acs` also
re-publishes it (see [Mesh Configuration Push](#mesh-configuration-push)).

The install tarball still carries it, since a node being provisioned needs
something at that path before `radio-setup.sh` has chosen a variant.

The `acs=` test accepts `y`/`yes`/`1`/`true` case-insensitively. It compared
against `"Y"` alone until 2026-08-31, and every writer produces lowercase: both
flashers normalize the answer, the web UI writes `'y':'n'`, and
`mesh-config-sync.py` validates the key as exactly `y` or `n`. So `acs=y`
selected the static variant on every node and the ACS orchestrator never ran.

---

---

## Web interface

There is no unauthenticated route that changes anything. A set of
`/api/control/*` handlers at the site root used to apply interface, TX power and
channel changes locally, behind nothing but the subnet check, and were removed
once every caller had moved to the Alfred-staged path; a local change and a
mesh-wide one now take the same route through `manet_manage.py`.

**manet_radio.py**

Radio primitives shared by the UI that offers a change and the code that
applies one: `mesh-status.py` and `manet_manage.py` read state and build the
menus, `mesh-radio-state.py` applies an Alfred-staged package. One
implementation, so what the UI offers and what the node does cannot diverge.

**The HaLow channel plan is derived from the node's region.** The channel,
bandwidth and S1G operating-class tables are transcribed from the Morse driver's
`dot11ah` tables, and a bandwidth appears for a region only where the driver
defines a channel of that width, which is why **EU stops at 2 MHz** (the whole
863–868 MHz allocation is too narrow for more) while **US reaches 8 MHz**.
`halow_channel_options()` builds the menu the Radio config tab renders, so an
EU node is never offered a width it cannot use. Channel numbers and center
frequencies are both unique within a region, so either resolves the other:
`halow_bandwidth_for_channel()` recovers the width from the channel number,
which is how the status readout avoids `s1g_prim_chwidth`; that reports the
*primary* channel width, 2 MHz for every operating width above 1 MHz, and
reading it as the operating width reports a 4 or 8 MHz channel as 2 MHz.

**HaLow TX power is fixed per bandwidth by the driver and BCF**, not freely
settable: `HALOW_BW_TXPOWER_CAP_DBM` holds the caps, and a request outside them
is refused with an explanation rather than silently clamped
(`txpower_request_allowed`, `unsupported_txpower_response`). Wi-Fi TX power
options come from the phy's own advertised range (`parse_phy_txpower_options`),
and a set is read back and verified rather than assumed
(`set_iface_txpower_verified`).

An unknown region falls back to the EU plan, the narrower of the two, so a
misconfigured node cannot be offered channels its region may not permit.

**manet_peer_radios.py**

Builds the per-peer radio chips and the expandable status panel the topology
views show for another node: role, up/down state, channel, MCS, service pills,
and the inferred `bat0` / `br0` / gateway rows.

Everything comes from what Alfred already replicates, so no node is queried.
Where a peer publishes `INTERFACES_JSON`, that wins. Where it publishes an empty
list, as `node-manager` did historically (leaving the UI with no
`wlan0`/`wlan1`/`wlan2` keys and every peer rendered as 2.4G/5G/HaLow OFF), the
values are reconstructed from the registry's MCS and `DATA_CHANNEL_*` fields, so
an older node still shows its radios.

**manet-ui-firewall.sh**

Installs the nftables rules described above: port 80 restricted to localhost and
this node's DHCP pool, port 5201 (iperf3) to the mesh subnet. Uses source
addresses rather than interfaces, because `br0` bridges `bat0`; a packet from a
remote node arrives on `br0` exactly like one from a local EUD. Re-run by
`mesh-ip-manager.sh` whenever the DHCP pool moves.

---

---

## Voice

The audio path is entirely GStreamer, so no Python code runs on the audio
thread:

```
TX  alsasrc -> level -> valve -> <enc> -> <pay> -> multiudpsink
RX  udpsrc  -> rtpbin -+-> <depay> -> <dec> -\
                       +-> <depay> -> <dec> --+-> audiomixer -> alsasink
                                (one branch per talker)
```

**The two codecs do not interoperate, and the failure mode is silence.** Both
the RTP payload type and the clock rate come from this one setting, and a
receiver builds every decode branch from its *own* configured codec rather
than from what actually arrived, so a node left on the other codec does not
get degraded audio, it gets nothing. That is why the codec is a mesh-wide
setting staged over Alfred (see below) and not a per-radio one like the talk
group, and it is why the silent fallback above matters: on a Lyra mesh, a node
whose plugin is missing is both deaf and mute. Check for the fallback log line
after any install.

The daemon only supervises: it reads the PTT button, keeps the unicast peer
list current, and publishes `/run/mesh-voice.json` for the UI.

**Unicast redundancy: off by default, because batman-adv already does it.** With
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
means `batadv_mcast_forw_mode()` returns `BATADV_FORW_NONE`, dropping the
packet at the *sender*, when no node has announced interest in the group. This
was observed directly: multicast sent with no listener never reached the radio
at all. `udpsrc` performs the IGMP join (`auto-multicast=true`), so this works
in normal operation, but expect a brief window after start-up before joins
propagate, and note that a sender with no listeners is silently idle rather
than wasting air.

**A node must never hear itself, and there are two layers making sure of it.**
`multiudpsink`'s own `loop` property is silently ignored with
`auto-multicast=false`, because GStreamer only applies it on the code path that
also joins the group, so loopback is cleared on the socket instead, reached
through `used-socket`. That call is now retried for up to 5 s: the sink has no
socket until it has started, a GStreamer state change is asynchronous, and the
original code asked once, got `None`, logged a warning and never asked again.
A pipeline that lost that race ran its whole life with loopback on, and what
the operator hears then is their own voice back in the headset one jitter
buffer late.

The second layer is the guarantee: any packet arriving with this node's own
SSRC is dropped at the `udpsrc` probe, before `rtpbin` sees it. That is safe to
key on because the SSRC is a hash of the node's own mesh address plus a per-run
generation byte, so no peer can collide without already sharing its IP. It also
keeps `rx_active` honest, which matters with `voice_half_duplex=y`: without it
a node reads its own transmission as a remote talker and refuses to key.
`rx_loopback` in `/run/mesh-voice.json` counts these; anything other than 0
means the socket-level suppression did not take.

**Note for bench testing:** `IP_MULTICAST_LOOP` has no effect on `lo`. A node
configured with `voice_iface=lo` will always receive its own multicast, and
that is the loopback device, not a bug in the daemon. Verified both ways on a
real interface, where clearing it works correctly.

`voice_half_duplex=y` restores the refuse-to-key behavior if you want it.

This is not just a policy change. A single `rtpjitterbuffer` cannot do it: two
senders' sequence numbers interleave in one buffer and the output is garbage.
That limitation, not policy, is what the old lockout was really working around.
Verified by feeding two senders (440 Hz and 880 Hz, distinct SSRCs) into the
receive pipeline: with one talker only 440 Hz is present; with both, 440 Hz and
880 Hz appear together at comparable amplitude.

A permanent silent input feeds the mixer, because `audiomixer` only produces
output while it has one; without it the sink is starved whenever nobody is
talking and every transmission starts with the DAC spinning up. Branches are
built on `pad-added` and torn down on `pad-removed`, so a decoder is not leaked
per talker; measured, two idle talkers were reaped and the count returned to 0.

**Decode branches are kept, not reaped; this is what stops transmissions
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
`min-upstream-latency` were each tried and none of them helped; the residual
is `rtpbin` establishing a new source rather than the pipeline linking, so the
fix is to ensure the source is not new.

**Pre-establishing a talker: only the sender can do it.** A receive slot in
`rtpbin` is keyed by **SSRC, not by IP**, and it exists only once a packet
carrying that SSRC arrives. Two pieces close that gap.

First, the SSRC is split: **24 bits identifying the node** (a hash of its mesh
address) and **8 bits identifying the run** of the daemon. The prefix lets any
receiver build address → prefix for every node in the registry and name a
talker with no back channel; verified collision-free across a full /24.

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

Beacons are **event driven, not a heartbeat**. There is nothing to refresh
(`autoremove=false` means a source is never forgotten), so one is sent at
start-up (announcing this run's generation) and whenever a node appears in the
registry that cannot yet have heard this node. `voice_beacon_sec` (default 600) is
only a safety net for a peer whose arrival was somehow missed, and 0 disables
it. That is about **21 packets an hour, ~5 bps averaged**, against 420/hour at
the 30 s heartbeat this replaced.

**Synthesizing a source locally from the registry does not work; it is worse
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

**An evicted talker is parked, not abandoned.** Dropping the decode branch
leaves rtpbin's receive pad for that talker with nothing on the end of it, and
rtpbin never takes the pad back, because `autoremove=false` keeps every source
for the life of the daemon. So no second `pad-added` ever arrives to rebuild
the branch. Before this was fixed, an evicted talker was inaudible **for good**,
and silently so at both ends: they hear the mesh fine and have no idea nobody
can hear them. It was also noisy, because pushing to the unlinked pad returned
`not-linked`, which posted a bus error and restarted the whole receive
pipeline, dropping everyone else's audio with it.

Eviction now links the pad to a `fakesink` and puts a buffer probe on it. The
probe is what `pad-added` would have been: it fires when that talker next
speaks, and the branch is rebuilt. The cost is the same measured ~180 ms of
head loss that eviction was always documented to cost, rather than permanent
silence. `talkers_parked` in `/run/mesh-voice.json` says how many are in this
state.

**The ceiling is RAM.** A warm branch costs about **5.3 MB with lyra** and
**0.6 MB with opus**, measured on a CM4. So the hard cap of 64 is roughly
340 MB of lyra decoders against the 3.4 GB a node has free. That is comfortable, but
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
`max(voice_jitter_ms, two packets)`, the two-packet floor being the least
that can absorb a single late packet. At the default 40 ms packing that
resolves to 100 ms.

That figure is computed when the pipelines are built, from the packing in
force at the time, and nothing resizes it while the pipeline runs. So if
adaptive packing climbs to the top of its range (3 frames, 60 ms packets),
a 100 ms buffer is 1.67 packets deep rather than the 2 the formula intends,
until the next rebuild. (A SIGHUP retune rebuilds both pipelines, so it also
re-sizes the buffer for the packing then in force.) This is a margin
question rather than a fault, and the coupling runs in the safe direction: the
controller shrinks packets under loss, which *deepens* the buffer in packet
terms, and only grows them after 30 s below 1 % loss. Note also that a
receiver's buffer has to suit what the *senders* are transmitting, which this
node cannot know and which the formula has never tracked. If the floor is ever
wanted at its stated value, raise `voice_jitter_ms` to 120 rather than
resizing at runtime; a jitter buffer that resyncs mid-stream discards the
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

**Every step of the rebuild is checked, and there is a floor under it.** A
successful `parse_launch()` is not a working pipeline: a port already bound or
an ALSA device held by something else fails during the *state change*, so both
pipelines are taken to `PLAYING` and then confirmed with `get_state()`. Build
failures are caught as `Exception` rather than `GLib.Error`, because `build()`
reaches for elements by name and hangs pad probes off them, and PyGObject
prints an exception raised inside a signal handler and then swallows it, which
`SIGHUP` arrives through. That combination used to leave the daemon alive with
`self.tx` on the new pipeline, `self.rx` on the old one it had already set to
`NULL`, and `self.valve` on elements of neither: no audio, no error in the
journal, and a state file still saying `running`. If the new group does not
come up the daemon reverts to the previous one; if the revert fails too it
exits non-zero and lets systemd rebuild the whole stack, because by then there
is nothing left in process to fall back to.

**Retuning releases what it replaces.** `gst_bus_add_signal_watch()` attaches a
GSource that holds a reference to the bus, so a bus whose watch is never
removed is never finalised, and every `GstBus` carries a `GstPoll` control
pipe. A retune builds two new pipelines, so a talk group change that only set
the old ones to `NULL` leaked **four file descriptors and two live GSources
every time**. Measured on the bench: 4 fds per retune, and dead flat once the
watch is removed. This is exactly the case a rotary channel selector produces,
and at the default 1024-descriptor limit an operator could reach it inside a
single operation, after which the daemon fails to open the very sockets and
ALSA devices voice needs.

Retuning in place rather than restarting the unit is what makes a hardware
channel selector practical; clicking through groups on a rotary switch would
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
safe while loss means fades rather than congestion; on a saturated link,
offering more packets makes it worse, so the controller will not go below 2
frames/packet when batman-adv's throughput estimate for the worst neighbor is
under 2 Mbit/s.

Loss above 5 % steps down immediately; recovery needs 30 s below 1 % per step,
and anything in between holds position. Windows with fewer than 25 packets are
ignored rather than treated as clean. The signal is this node's *own* receive loss
(plain multicast RTP has no back channel), which half duplex makes a fair proxy,
since it measures the same link in the other direction moments before keying
up. An asymmetric link will fool it.

No signaling is needed for any of this: Lyra frames are a fixed size per
bitrate (8/15/23 bytes), so `rtplyradepay` recovers the packet geometry from
the payload length alone and follows a mid-stream change with no renegotiation.
Verified on hardware: switching 2 → 1 → 3 → 2 while playing produced 27/42/57
byte payloads and 1001 frames decoded against 1000 sent.

**QoS: use CS6 (48), not EF (46).** This is counter-intuitive and worth
understanding before changing `voice_dscp`.

Linux 6.12+ added an RFC 8325 mapping to `cfg80211_classify8021d()`
(`net/wireless/util.c`) that sends DSCP 46/EF to 802.1d UP 6, i.e. WMM AC_VO.
On 6.6 and older the naive `dscp >> 5` rule sent it to UP 5 / AC_VI. We ship
6.18 everywhere, so on a plain wireless interface EF would be correct.

**batman-adv never lets that code run.** `batadv_skb_set_priority()`
(`net/batman-adv/main.c`) is called from `batadv_interface_tx()` for every
packet entering `bat0`, locally originated included, and from both forwarding
paths in `routing.c`. It stamps:

```c
if (skb->priority >= 256 && skb->priority <= 263) return;  /* already set */
prio = (ipv4_get_dsfield(ip_hdr) & 0xfc) >> 5;             /* the OLD rule */
skb->priority = prio + 256;
```

Values 256–263 are the 802.1d passthrough range, which `cfg80211_classify8021d()`
checks **first** and returns directly as the UP, so the RFC 8325 DSCP code is
never reached. The result is kernel-version independent:

| DSCP | TOS | batman-adv priority | 802.11 UP | Access category |
|---|---|---|---|---|
| 46 (EF) | 0xB8 | 261 | 5 | AC_VI (video) |
| **48 (CS6)** | **0xC0** | **262** | **6** | **AC_VO (voice)** |

Hence the default of 48. The `return` guard also means an explicitly-set
`SO_PRIORITY` in 256–263 survives batman-adv, which is how OpenMANET pins the
access category: they set `IP_TOS` and `SO_PRIORITY` together, in that order,
because the kernel rewrites `sk_priority` as a side effect of `IP_TOS`.
GStreamer's `multiudpsink` exposes only `qos-dscp`, so batman-adv's derivation
is relied on instead, which is why the DSCP value has to be chosen for what
batman-adv will make of it.

Multicast TTL is set explicitly to 32: the default of 1 silently black-holes
voice one hop out.

Multicast tuning is deliberately untouched; the mesh's arrangement is the way
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
than 6 kbps at 20 ms (27.7 kbps) and sounds far better. The cost is latency
(a 60 ms frame adds 60 ms) and coarser loss, since one dropped packet now takes
60 ms of audio with it. The jitter buffer floor is raised to two frames at
start-up; see Buffering above for what that does and does not cover. For a
PTT system where the multiplier is unicast redundancy rather than continuous
full-duplex, 40–60 ms is usually the right trade.

**High-pass (on by default, 80 Hz).** Measured, identical at 16 and 48 kHz:

| 20 Hz | 40 Hz | 50 Hz | 60 Hz | 80 Hz | 120 Hz | 300 Hz | 3 kHz |
|---|---|---|---|---|---|---|---|
| -53.2 dB | -27.2 dB | -17.9 dB | -9.6 dB | 0.0 dB | 0.0 dB | +0.2 dB | 0.0 dB |

80 Hz keeps a male fundamental (~85 Hz up) and takes out everything below. Note
60 Hz mains only sees -9.6 dB at that corner; raise the corner toward 120 if
hum rather than rumble is the actual complaint.

**Band-pass (off by default).** Setting `voice_lowpass_hz` as well switches the
stage to `audiochebband`. Narrowing to something like 100-4000 Hz is the
classic walkie-talkie sound: clearer and more cutting on some headsets, thin on
others. It is a taste call that needs a field comparison, which is why it is
off rather than defaulted. Measured at 100-4000 Hz:

| 40 Hz | 60 Hz | 100 Hz | 300 Hz | 1 kHz | 4 kHz | 5 kHz | 6 kHz |
|---|---|---|---|---|---|---|---|
| -36.9 dB | -20.6 dB | -0.2 dB | 0.0 dB | -0.1 dB | -0.2 dB | -7.8 dB | -17.2 dB |

**Pole counts are fixed, and they differ per element, because both were
measured.** `audiocheblimit` uses 4: at 8 poles and 48 kHz, an 80 Hz corner is a
normalised frequency of 0.0017 and the coefficients lose their precision, which
shows up not as a bad filter but as **a flat +4.9 dB of gain at every frequency
from 20 Hz to 3 kHz**. `audiochebband` uses 8, because it splits its poles
between the two edges (4 gives a limp -3.5 dB at 60 Hz) and, unlike
`audiocheblimit`, is still stable there at 48 kHz. Both use Chebyshev type 1:
type 2 puts its ripple in the stopband and its cutoff means the stopband edge,
so at the same setting it measures -0.3 dB at 60 Hz against type 1's -9.6 dB.

**Three-band EQ (off by default).** `voice_eq=y` adds `equalizer-3bands` with
fixed centres at 100 Hz, 1100 Hz and 11 kHz, for a formant or presence lift.
`voice_eq_mid=3.0` measures +2.6 dB at 1-1.1 kHz, tapering to +1.5 dB at 500 Hz
and +1.9 dB at 2 kHz, so it is a wide gentle lift rather than a peak.

Two things about it are not obvious:

- **The 11 kHz band is forced to 0 under lyra.** Lyra's raw rate is 16 kHz, so
  11 kHz is above Nyquist, and the band does not politely do nothing there: at
  16 kHz, `band2=+6` lifted a 1 kHz tone by 2.6 dB and clipped 4608 samples,
  and `band2=-12` cut the same tone by 6 dB. The daemon zeroes it and logs why.
- **A boost is paid for with headroom ahead of it.** The block converts back to
  S16LE at its end, so a boost clips there and no attenuation further down the
  pipeline can undo it. A `volume` element is inserted at the *head* of the
  block and trimmed by the largest positive gain, which makes a boost a change
  of tone rather than a change of level. Measured at the encoder input with a
  0.8 full-scale tone and `voice_eq_mid=6.0`: **37,645 clipped samples with the
  trim defeated, zero with it applied.** The cost is level, not headroom, and
  playback has 20 dB spare.

**Tuning is live.** Every property of these elements is `controllable`, so
`systemctl reload mesh-voice` applies new cutoffs and gains to the running
pipeline with no gap in the audio and no TFLite model reload. That matters
because picking a voice band and a formant lift is a listening test across
headsets, and a listening test is useless if every change costs a restart.
Adding or removing a stage does change the pipeline's shape, and that still
rebuilds, through the same checked path a talk group change uses.

Cost is negligible: the high-pass measured 0.03% of realtime at 16 kHz on a dev
box, so under 1% of one CM4 core even allowing 20x, and a flat equalizer is
about a tenth of that.

**`rnnoise` is not packaged.** There is no stock GStreamer element for it, and
`webrtcdsp` (which has noise suppression, AGC and its own high-pass) lives in
`gstreamer1.0-plugins-bad`, which nodes do not install. Either is a real
addition rather than a config change, and neither has been measured on a CM4.
Worth noting before reaching for one: lyra is itself a neural speech codec, so
some of what a denoiser would do is already happening inside it.

**The counters this runs on are not `tx_packets` and `rx_packets`.** In a
push-to-talk system those are flat almost all the time by design: the valve is
shut until somebody keys up, and nothing is received until somebody else does.
A watchdog driven off them would either restart a healthy quiet node or need a
timeout so long it never fires. Two other flows do not stop while the pipelines
are healthy, and those are what is counted, both at about one buffer per 20 ms:

- **Capture buffers arriving at the valve.** The probe sits on the valve's
  *sink* pad, so buffers are counted before the valve drops them, and the count
  therefore runs with the PTT released.
- **Playback buffers reaching the sink**, fed continuously by the permanent
  silent mixer input the receive pipeline already carries for its own reasons.

Multicast membership is checked separately, by reading `/proc/net/igmp`,
because there is no dataflow to miss: a receiver nobody is talking to looks
exactly like one that has fallen out of the group. A membership that cannot be
read at all counts as unknown, never as a fault. Losing the join takes out both
directions, not just receive, since batman-adv drops multicast to a group with
no listeners.

The ladder, in order of cost:

| Symptom | Response |
|---------|----------|
| A flow stopped for `voice_watchdog_sec` | Restart that pipeline in place |
| Three such restarts inside 10 minutes | Exit non-zero; `Restart=on-failure` rebuilds the whole stack |
| Talk group change fails to come up | Revert to the group that was working |
| The revert fails too | Exit non-zero, same whole-stack rebuild |
| The GLib main loop stops turning | `WatchdogSec=60` in the unit; systemd aborts and restarts |

Exiting **is** the restart. `systemctl restart mesh-voice` from inside
mesh-voice blocks on the very unit issuing it, and the `--no-block` form races
the process it is killing; `Restart=on-failure` with `RestartSec=10` already
does it properly. A fresh process is the point: new ALSA handles, new sockets,
a new multicast join, `mesh.conf` read again and, with lyra, freshly loaded
TFLite models. `/etc/mesh.conf` already holds the operator's talk group by the
time a failed change gets this far, so the restarted daemon comes up on the
group they asked for.

Two things it deliberately does **not** do. It never touches a pipeline the
bus-error backoff already owns, so a node provisioned with `voice=y` before its
OpenVLM board was fitted keeps its quiet five-minute retry instead of being
restarted every fifteen seconds. And it never escalates a pipeline that has
never produced a buffer at all: that node was never working, so there is
nothing to recover.

`WatchdogSec=60` in the unit means the unit and `node_tools` must be updated
together. A node given the unit with a `mesh-voice.py` that predates the
keep-alive would be killed every 60 s for ever. Both ship in the same tarballs,
so the only way to reach that state is to install one of them by hand.

The VOICE tab shows all of this on an **Audio path** row, and
`/run/mesh-voice.json` carries `capture_idle`, `playback_idle` (both `null`
when the flow has never run, which is a missing audio device rather than a
stall), `igmp_joined`, `stalls` and `watchdog_sec`.

---

## Elections, channels and healing

All service elections share the same algorithm: the best-connected node wins,
measured by `MEAN_THROUGHPUT_MBPS` in the registry, the mean of BATMAN_V's
metric across that node's originators, in Mbit/s. Stale nodes (not seen within
10 minutes) are excluded. Ties are broken deterministically by MAC address.

This field used to be called `TQ_AVERAGE`, which was wrong: BATMAN_V's metric is
throughput, not a 0-255 link quality. The behavior never changed (highest
wins either way), but the name misled, so it now says what it holds.

- Includes channel bias to prevent unnecessary migrations.

Candidate frequencies are deliberately **not** filtered against the local phy
here. The election reaches its answer by implicit consensus (every node runs
the same computation over the same replicated reports), so a per-node hardware
filter at this stage would let two nodes derive different winners from identical
data. Unusable frequencies are dropped at scan time instead (`phy_usable_freqs`
in `node-manager-acs.sh`), which keeps the exclusion inside the replicated
report and therefore symmetric across the mesh.

**"Every candidate was measured and rejected" and "nothing reported a
measurement at all" are different verdicts.** Both leave the candidate list
empty. The first is a real RF result and drops to the lobby channels. The
second is an outage: the band's radio is absent so its scan report carries no
entries, or the scan request was refused wholesale, or Alfred was down and no
reports replicated, and it now **holds the current channel and does not assert
limp mode**, logging `No scan data for any candidate channel`. Treating it as
jamming throttled the whole mesh to legacy bitrates on the strength of missing
data.

4. Listens for other partitions.
5. If the other partition should win, triggers migration.
6. Returns to data channel.

**Which radio hops is derived from the clock, never from this node's own
history**: `(epoch / 120) % 2`, the same window index `should_perform_tourguide`
uses, so it inherits the wall-clock alignment the rest of the ACS pipeline
already needs and adds no new time-sync requirement. Two split partitions can
only find each other if their tourguides hop to the same band in the same
window. Reading this node's last-used radio out of the registry went permanently
out of phase the moment either side missed a window (an elected tourguide
excluded for hosting a service, a restart, a failed hop), after which the two
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

---

## Network management

- First 5 IPs network-wide are reserved for services.
- Handles conflicts via MAC tie-breaker.
- Configures `dnsmasq` DHCP when needed.

**Which chunks are taken comes from Alfred, one step removed.** Every node
publishes its own chunk in its identity record (`ipv4_chunk`, Alfred type 67),
and `mesh-registry-builder.sh` decodes those into `/tmp/claimed_chunks.txt` as
`<chunk>,<mac>` lines. This script reads that file and never queries Alfred
itself. On a successful claim it writes the chunk to `/var/run/my_ipv4_chunk`,
which the node manager hands back to the encoder.

Four properties of that file matter:

- Only nodes the registry marks **ACTIVE** appear. One unheard from for 300 s
  goes STALE and its chunk returns to the pool.
- A free chunk is chosen at **random**, not lowest-first, so two nodes booting
  together with the same view do not pick the same one.
- The file lives in `/tmp` and is empty at boot. Absent means "nothing claimed",
  so a node starting before Alfred has converged sees the whole space as free.
- **A node with saved state ignores the file** and reasserts its remembered
  chunk. A false conflict from stale `/tmp` data would clear that state and
  cause exactly the churn persistence exists to prevent. Real collisions are
  caught after configuration by the MAC tie-break, which works on live data.

Chunk size is uniform across the mesh: `max_euds_per_node + 2`, set at flash
time, which is why `mesh_config.py` keeps that key display-only in the
management UI. There is no per-node override: pinning a node's chunk by hand
skips the registry check and the MAC tie-break, so two pinned nodes, or a pinned
chunk that a peer later claims, collide with nothing left to resolve them. An
`/etc/manet/mesh-ip-force.conf` mechanism that did this was removed.

Mean of BATMAN_V's metric across this node's originators, in Mbit/s, published
as `MEAN_THROUGHPUT_MBPS` and used by the service elections.

Reads the parenthesized value, never a positional field: `batctl o` shifts
columns on the selected route (prefixed `*`), so `$3` is the last-seen timestamp
there and `(` on the others. Averaging `$3`, as this did until 2026-08-16,
produced 0.14 on a mesh running at 43 Mbit/s. Keeps the best path per
originator, so one peer reachable over two radios is still one peer.

**manet-ipcalc.sh**

Pure-bash replacement for Debian's `ipcalc`, printing the same `HostMin:` /
`HostMax:` lines the mesh scripts parse. The Perl original cost ~1.5 s of CPU
per call on a CM4 and was invoked ~9 times per 15-second cycle, nearly a full
core. This runs in ~10 ms.

**batman-if-setup.sh**

Manages BATMAN-ADV interface lifecycle:
- Creates `bat0` interface.
- Enslaves mesh wireless interfaces (excludes AP interface).
- Sets BATMAN_V algorithm.
- Handles start/stop operations.
- HaLow is added first so it becomes batman's primary (longest-range link).
- Skips writing a `.link` file when the MAC is already pinned by an existing
  one; two link files for one MAC caused a rename ping-pong reboot loop.

---

## Data management

Node state is exchanged over Alfred as two message types, split by how often
the contents change. Alfred replicates every record to every node on a timer,
so anything that repeats is paid for continuously.

| Type | Message | Published | Contents |
|------|---------|-----------|----------|
| 67 | `NodeIdentity` | every 270 s | hostname, MACs, Syncthing ID, chunk, IP |
| 68 | `NodeTelemetry` | every 180 s | everything volatile |
| 69 | `NodeTelemetry` | tourguide window | helper beacons (channels, partition size) |

Alfred stamps every record with the publishing node's MAC; it runs `-i br0`, so
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
- Writes `/tmp/claimed_chunks.txt`, the claimed-chunk index
  [`mesh-ip-manager.sh`](#network-management) allocates from.
- Caches identity across cycles: a node whose identity record has not been
  refreshed yet keeps the values from the previous registry rather than
  appearing nameless.

**encoder.py**

Encodes this node's Alfred payloads to protobuf and Base64. Two subcommands:

- `encoder.py identity`: hostname, secondary MACs, Syncthing ID, chunk, IP.
- `encoder.py telemetry`: mean throughput, service flags, uptime, battery, CPU load,
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

Generated from `NodeInfo.proto`. Checked in because nodes have no protoc. Do
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
  `_reflection`-based form: 786 lines different, and unimportable on every
  node. This is easy to do by accident and nothing downstream catches it: a dev
  box with the matching-vintage protobuf runtime imports the bad file happily.
  `setup-dev-env.sh` puts the right protoc inside the venv precisely so
  activating it cannot give you one without the other.
- Changing a field type is a flag day: there is no compatibility path, so all
  nodes must be reflashed together.

---

---

## Mesh configuration push

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

**mesh-config-rollback.sh**

The safety net for dangerous changes. A wrong mesh key takes the mesh down, and
with it the only way to push a correction, so each node has to be able to undo
the change on its own, with no help from the network.

`arm` snapshots `/etc/mesh.conf` and the supplicant configs, records how many
batman peers the node had, and sets a deadline (default 300 s, `MANET_ROLLBACK_GRACE`).
`check` runs every node-manager cycle and is a no-op until the deadline passes,
then either commits or restores and restarts the supplicants. State lives in
`/var/lib` because a dangerous apply can end in a reboot.

A node that had no peers before the change commits rather than rolling back:
on a solo bench node "the mesh did not come back" cannot be told apart from
"there was never anyone there".

---

## Updates

`auto_update=` set to a true value. See
[networkd-dispatcher/README.md](../MANET/networkd-dispatcher/README.md).

**Publish in the right order.** The remote *version* is read from GitHub `main`
while the *tarball* comes from colorado-governor.com, so the tarballs have to be
uploaded before the version bump is pushed. The 24 h mtime throttle does not
protect against getting this wrong: `tar` restores the build machine's mtime,
and a published tarball is normally already older than a day.
- Version metadata still points at `very-srs/MANET`; that upstream now has matching `.gitattributes` binary protection on all branches as of 2026-05-03.

---

---

## Provisioning

**manet-ap-guard.sh**

Decides, for one interface, whether a mesh supplicant may start on it right
now. Installed as an `ExecCondition` on `wpa_supplicant@.service` through
`/etc/systemd/system/wpa_supplicant@.service.d/10-manet-ap-guard.conf`, so it
applies to every caller rather than to whichever call site was remembered;
twelve places in this directory restart `wpa_supplicant@<iface>` from
interface lists assembled in different ways.

The AP radio legitimately needs a mesh config on disk: in wired EUD mode it is
always a mesh interface, and in auto mode it joins the mesh whenever an EUD
appears on Ethernet. Only `ethernet-autodetect.sh` makes that call, and it
stops hostapd first. Any other start while hostapd holds the radio fails the
mesh join with -95 and, worse, deinits the netdev on the way out, leaving
hostapd `active` over a dead BSS, logging nothing.

Exits 0 to allow (not the AP radio, or hostapd is not holding it), 1 to skip.

`have_package_network` is checked before each apt phase, so "no network" is
recorded once, plainly, instead of as a wall of resolver errors.

This exists because a node reached the field with none of its packages
installed: its Ethernet was unplugged part-way through provisioning, every apt
call failed silently behind `|| true`, and the script still touched
`/var/lib/radio-setup.done`. Nothing on the node said anything was wrong.

The log is opened with `tee -a`, not `tee`. It used to truncate per run, which
destroyed the history of the run that went wrong, and two overlapping runs
interleaved into an unreadable file.

repeating the ones that finished.

Failures are advisory. Nothing here writes to `/var/lib/manet-provision.*`, so
a failing operator script cannot cause a working node to report itself
unprovisioned, and the runner always exits 0 for the same reason.
`manet-provision-status.sh` reports the tally and names any failures on the
login banner. A script that ran and failed is not retried; only an interrupted
one is.

The shebang requirement applies on the node as well as at flash time. The
flasher never embeds a file without one, so the test only affects scripts
copied onto a live node by hand, but it is required there, because executing a
file with no shebang does not fail. The kernel refuses it and the shell falls
back to interpreting it, so a configuration file whose lines happen to parse as
shell would run and report success.

Flash-time validation is performed by the flashers rather than here; see
[additional-scripts/README.md](../MANET/provisioning/additional-scripts/README.md).
The checks in this script cover the files that reach the directory without
passing through a flasher.

---

---

## Tests

Three unit-test files sit alongside the code they cover: 38 tests, pure Python,
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

The venv is not optional for the full run. One case,
`test_peer_radios.test_channel_fields_survive_encode_decode`, shells out to
`encoder.py`, which imports `NodeInfo_pb2.py`, which needs
`google.protobuf.internal.builder` from runtime 3.20 or later. A system Python
with an older protobuf fails that test; a system Python with a *newer* one is
worse, because 5.x and later accept generated code every node refuses to import,
so the test passes while proving nothing.

---

## Dispatcher hooks

The teardown reloads networkd and reconfigures `end0` only. A full
`systemctl restart systemd-networkd` would reconfigure `wlan0` and `wlan2` on
the way past and kick them out of `bat0`, so it is deliberately avoided.


## What actually runs on a node

**Only the contents of `/etc/networkd-dispatcher/<state>.d/` are executed.**
A file named for the state, sitting directly in `/etc/networkd-dispatcher/`, is
not run by anything. Every builder stages this set through
`stage_dispatcher_hooks` in `MANET/packaging/lib-dispatcher.sh`, for the tools
tarball as well as the three install tarballs, so a corrected hook can be
delivered over the air instead of needing a reflash.

| Repo path | Installed as | Runs |
|---|---|---|
| `carrier.d/50-ethernet-detect` | `/etc/networkd-dispatcher/carrier.d/50-ethernet-detect` | **yes**, on carrier |
| `routable.d/50-manet-uplink` | `/etc/networkd-dispatcher/routable.d/50-manet-uplink` | **yes**, on routable |
| `off` | `…/off.d/50-gateway-disable`, and the same file again in `no-carrier.d/` and `degraded.d/` | **yes**, on all three states |
| `carrier` | `/root/networkd-dispatcher/carrier` | no (reference copy) |
| `off` | `/root/networkd-dispatcher/off` | no (reference copy) |
| `no-carrier`, `degraded`, `routable` | nothing installs them | **no** |


## Two scripts called `carrier`

`carrier` and `carrier.d/50-ethernet-detect` are **different scripts** and only
the second one runs. The reference copy calls
`ethernet-autodetect.sh --hotplug`; the installed hook calls
`manet-uplink-dispatch.sh carrier`. `off` is the same file in both places.

`no-carrier`, `degraded` and `routable` in this directory are three-line wrappers
around `manet-uplink-dispatch.sh <state>`. Nothing installs them, and their
states are covered by the `.d/` entries above.


## Adding or changing a hook

Change the file under `carrier.d/`, `routable.d/`, or `off`, and rebuild the
tarballs; every builder picks the set up from `stage_dispatcher_hooks`, so there
is one copy of each. The two generated hooks used to be heredocs duplicated
across the three install builders, which is how one wrong `grep` came to need
fixing in four places and how the rpi5 copy drifted from the other two.

`node-update.sh` extracts the tools tarball and nothing else; it does not run
`daemon-reload` or `udevadm`. Dispatcher hooks need neither; networkd-dispatcher
reads the directory on each event, so a replaced hook is live immediately.

---

## Onboard LEDs

`radio-setup.sh` used to drive the LEDs directly from three places, and the
result was unreliable in both directions.

The success signal was `echo heartbeat > /sys/class/leds/ACT/trigger`, written
unconditionally before the failure gate was reached, so a node that was about
to be marked incomplete got the success colour anyway.

The failure signal was worse. It came from `trap led_error ERR`, and the script
sets neither `set -e` nor `set -E`. An ERR trap still fires without `set -e` on
any command that returns non-zero outside a condition, and this script
continues past failures deliberately: every apt call, and around a dozen
top-level `grep`, `test` and `systemctl is-active` calls, can return non-zero
on a completely healthy run. Nothing ever reset the trigger, so a good
provision could finish heartbeating red.

Neither signal survived a reboot either, because `radio-setup-run-once.service`
disables itself, so on the next boot nothing ran and both LEDs came back on
their kernel defaults.

`manet-led-status.sh` replaces all three. It reads the recorded verdict, the
same `STATE` and `radio-setup.done` fallback that `manet-provision-status.sh`
reports on the login banner, so the two cannot disagree.
`manet-led-status.service` runs it on every boot and `radio-setup.sh` runs it
again the moment the verdict is written, which is what makes the pattern
persist and what makes it change only when the status does.

Solid and off both clear the trigger to `none` before writing `brightness`,
because whatever the kernel had driving the LED will otherwise overwrite the
value immediately. Brightness comes from the LED's own `max_brightness`, since
that is 1 on some class devices and 255 on others.
