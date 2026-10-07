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

### Tools update failure handling

`node-update.sh` executes `node-update.py`. Loading the Python program before
installation allows it to replace its own installed sources safely. A nonblocking
`flock` on `/run/manet-update.lock` covers the entire update. Each attempt has a
private directory under `/var/lib/manet-update`, removed on ordinary success or
failure. Killed processes may leave a staging directory for manual cleanup.

Downloads use HTTPS, bounded curl retries/timeouts, and limits of 4 MiB for
release metadata, 1 KiB for checksum files and 64 MiB for tools archives. Builders emit a single-line
SHA-256 sidecar naming the exact archive basename. The updater requires a hash
match, reads gzip through its footer (with a 512 MiB expanded limit), and rejects
unsafe paths, duplicate names, hardlinks/devices/sparse files, non-root ownership,
special mode bits, writable directories, and symlink traversal. Relative symlinks
must target regular files included in the archive. Existing directory modes are
preserved, and installation cannot traverse existing directory symlinks. Tools
archives cannot carry kernel modules/firmware, networkd interface definitions,
`mesh.conf`, or `node-manager.sh` (a node-local symlink the updater points at
the selected orchestrator, as `node-manager-select.sh` does at every start).

Both embedded version files must match each other and the advertised release.
Required updater, manager, status, dependency, agreement, time-service and MOTD
files must be present, including the node-manager service drop-ins.
The updater estimates staging plus installation space per filesystem, leaving
16 MiB headroom. It stages regular files, resolves the admin dependency, checks
installation space again, then creates a durable `in-progress` marker before
replacing payload files. Files are copied to temporary siblings, fsynced and
renamed; the version files are withheld until the end. Required commands have
checked exit status and timeouts; timeout/interruption terminates their process
groups so child installers cannot continue after the updater exits.

The selected static/ACS manager is regenerated, MOTD links refreshed, and systemd
reloaded. Both mesh-status and node-manager are restarted on every installation
attempt, including retries. They and the agreement/time services are checked
active; the latter follow node-manager through their drop-ins and `PartOf=`.
Only then are the version files
replaced and the retry marker removed. This fixes false success after extraction,
copy, dependency or service failure. It does not provide whole-update rollback
or guarantee that radio functionality is healthy just because services are active.
After interruption, the durable marker bypasses version equality and the routine
24-hour throttle on the next attempt. Error details go to the journal even in
routine mode. Ethernet carrier remains the only automatic update trigger.

---

## Core orchestration

`mesh-acs-common.sh` supplies the role/config handling shared by the ACS
orchestrator, channel election and tourguide. Explicit `mesh_24_if` and
`mesh_5_if` files are authoritative: an empty file is an absent band, not a
request to guess from `mesh_if` array positions. Readiness and lobby detection
consider enabled roles only. One band is sufficient; no conventional Wi-Fi
roles means status/IP management continues without Wi-Fi ACS activity.
Helper adoption and partition merge require at least one shared enabled band,
write only the channels supplied for those bands, and preserve others.

`radio-setup.sh` assigns roles by capabilities even with one standard Wi-Fi
radio, so a lone 5 GHz interface is not mislabeled as 2.4 GHz. A dual-band
interface assigned to the 5 GHz role starts on 5180 rather than the first
frequency its phy supports. Reserving an interface for AP use clears either
mesh role that referred to it, but preserves the original band in
`/var/lib/ap_mesh_band`. An empty saved band denotes AP-only hardware.

- Limp mode management

Scan frequencies are filtered against the interface's phy before the request
goes out (`phy_usable_freqs`). `iw scan freq` refuses the **whole** request if
any single frequency is not permitted on that phy, so one bad entry takes out
the scan for every channel on the radio and the only symptom is an empty
survey. The filter fails open: if the phy cannot be read, or nothing parses, the
requested list is used unchanged, because filtering to an empty set would take a
band off the air on every node at once.

**A solo discovery node never elects itself onto data channels.** With a qualified
clock it follows the rotating rendezvous schedule; without one it parks on the
fixed anchors. Any BATMAN peer contact stops rotation while nodes recover or
bootstrap together. Authenticated confirmation of a live destination ends
discovery, including when the visited channel already is the data channel.
The explicit mode survives daemon restarts; a frequency match alone cannot
identify whether a node is searching or operating a data plan.

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

## Web interface

There is no unauthenticated route that changes anything. A set of
`/api/control/*` handlers at the site root used to apply interface, TX power and
channel changes locally, behind nothing but the subnet check, and were removed
once every caller had moved to the Alfred-staged path; a local change and a
mesh-wide one now take the same route through `manet_manage.py`.

**manet_web_sessions.py**

Management logins use a fresh 256-bit random token per session. The threaded
web process stores only the token's SHA-256 digest and deadline, under a lock.
The 48-hour absolute limit has no idle timeout, allowing a login to cover a
typical deployment. Deadlines use monotonic elapsed time, so GPS/NTP corrections
cannot extend or prematurely end a session. No session state is written to disk
or announced to peers; a service restart or reboot requires a new login.

Login and validation read the current admin password under the same lock. A
changed or missing password clears all sessions when observed. An unrelated
config edit preserves them. Logout revokes the presented token; successful
re-login replaces that browser's token. Each node keeps at most 64 sessions,
discarding expired entries first and then the oldest if a new login needs room.

The cookie is HttpOnly with SameSite=Lax. Its Max-Age matches the server's
lifetime, but the server enforces expiry and revocation independently. Login,
logout and dynamic HTML/JSON/CSV responses use `Cache-Control: no-store`.
Management APIs return JSON 401 for invalid sessions; the dashboard returns to
login while retaining its tab and query. Both form-login aliases and the JSON
login endpoint use the same store. `test_web_sessions.py` checks token replay,
expiry, concurrent requests, password changes and HTTP route authorization.

Login failures share a monotonic sixty-second window across all login aliases:
five failures per client address, thirty per node. The bounded list is updated
under the session lock. HTTP 429 includes `Retry-After`; throttling does not
revoke established sessions or grow memory with attacker-selected addresses.

**Web resource limits and recovery status**

`manet_web_limits.py` bounds the HTTP worker count at eight and concurrent status
collectors at two. Fixed cache keys share status work across clients; a busy
collector returns 503 instead of starting duplicate subprocesses. Management
POSTs are serialized and invalidate cached observations when they finish.
Authentication is checked before serving any cached management data. Body
validation rejects oversized, ambiguous and incomplete requests before routing
them to actions. Reads have a ten-second socket timeout and bodies also have a
ten-second elapsed-time deadline.

`mesh-channel-agreement.py` writes a local observation to
`/run/manet-acs-status.json` each tick. Successful automatic channel changes
record their reason in `/run/manet-last-channel-change.json`. Both are volatile;
failure to write display state cannot stop channel recovery. No extra Alfred
fields or messages are added. `manet_recovery_status.py` combines these with the
existing time-client state and registry, and rejects observations older than
45 seconds as current evidence. A HaLow route is identified by its assigned
interface, while HaLow readiness only explains why Wi-Fi tours are suppressed.

`test_mesh_recovery_sequence.py` runs three independent ACS instances with real
authenticated messages, simulated radios and delayed/lost Alfred delivery. It
covers missing votes, restart before activation, straggler recovery, complete
link loss and reconnection, and the existing advertisement interval. This does
not measure physical switching time or RF airtime.

**manet_radio.py**

Radio primitives shared by the UI that offers a change and the code that
applies one: `mesh-status.py` and `manet_manage.py` read state and build the
menus, `mesh-radio-state.py` applies an Alfred-staged package. One
implementation, so what the UI offers and what the node does cannot diverge.

**The HaLow channel plan is derived from the node's region.** The channel,
bandwidth and S1G operating-class tables are transcribed from the Morse driver's
`dot11ah` tables, and a bandwidth appears for a region only where the driver
defines a channel of that width and the Morse supplicant will join it. **US
reaches 8 MHz.** **EU is 1 MHz only**: the 863–868 MHz allocation has no room
for 4 or 8 MHz, and `wpa_supplicant_s1g` 1.16.4 rejects every EU 2 MHz mesh
config ("Invalid S1G configuration of operating class, country code and
channel", channels 2 and 6, op_class 67 and 7, tested on cm4.2 2026-10-02).
`halow_channel_options()` builds the menu the Radio config tab renders, so an
EU node is never offered a width it cannot use. Channel numbers and center
frequencies are both unique within a region, so either resolves the other:
`halow_bandwidth_for_channel()` recovers the width from the channel number,
which is how the status readout avoids `s1g_prim_chwidth`; that reports the
*primary* channel width, 2 MHz for every operating width above 1 MHz, and
reading it as the operating width reports a 4 or 8 MHz channel as 2 MHz.

TX power options for every radio, HaLow included, come from the phy's own
advertised channel range (`parse_phy_txpower_options`). There is no
per-bandwidth HaLow table any more. A request above that range is refused
(`txpower_request_allowed`, `unsupported_txpower_response`).
`set_iface_txpower_verified` requests the power, reads it back and returns the
reported value, which may be lower than requested when the card limits itself.
Only a radio reporting no power at all is an error.

`manet-region.py apply` is the runtime counterpart of radio-setup's region
writes. `mesh-config-apply.sh` runs it when a `regulatory_domain` change is
applied, so a web UI change reaches `/etc/modprobe.d/{cfg80211,morse}.conf`,
`/etc/default/crda`, hostapd's `country_code` and every supplicant's country
before the reboot that loads them. A US/EU plan change also rewrites the
HaLow supplicant to that region's template channel (`HALOW_DEFAULT_CHANNEL`,
checked against `radio-setup.sh` by `test_region.py`, as is the EU country
list). Within one plan the operator's HaLow channel is kept. Verified on
cm4/cm4.2 2026-10-02: DE to US through the apply step, reboot, both nodes up
on 907 MHz / 2 MHz.

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
`mesh-ip-manager.sh` whenever the DHCP pool moves. The pool-based policy is
intentional. Replacement runs as one nftables transaction under a local lock;
if any rule fails, the previous table and success marker remain intact. The
marker is replaced only after nft reports success, so a later invocation can
retry. Missing pool data continues to leave existing rules alone.

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

All nodes must use the same codec. The codec setting determines RTP parameters
and receive decoders; mismatched nodes cannot exchange voice. Codec changes
are staged across the mesh over Alfred (see below). Missing Lyra plugins cause
a logged fallback to Opus, so check for that message after installation.

The daemon only supervises: it reads the PTT button, keeps the unicast peer
list current, and publishes `/run/mesh-voice.json` for the UI.

**Unicast redundancy defaults off.** With
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
rewritten live from the registry, including nodes that have never transmitted.

**Receivers must join the group or nothing transmits.** The same optimization
means `batadv_mcast_forw_mode()` returns `BATADV_FORW_NONE`, dropping the
packet at the *sender*, when no node has announced interest in the group. This
was observed directly: multicast sent with no listener never reached the radio
at all. `udpsrc` performs the IGMP join (`auto-multicast=true`), so this works
in normal operation, but expect a brief window after start-up before joins
propagate, and note that a sender with no listeners is silently idle rather
than wasting air.

**Loopback suppression uses both the socket and the RTP source ID.**
`multiudpsink`'s own `loop` property is silently ignored with
`auto-multicast=false`, because GStreamer only applies it on the code path that
also joins the group, so loopback is cleared on the socket instead, reached
through `used-socket`. That call is now retried for up to 5 s: the sink has no
socket until it has started, a GStreamer state change is asynchronous, and the
original code asked once, got `None`, logged a warning and never asked again.
A pipeline that lost that race ran its whole life with loopback on, and what
the operator hears then is their own voice back in the headset one jitter
buffer late.

Any packet arriving with this node's own
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

A single `rtpjitterbuffer` mixes the two senders' sequence numbers and corrupts
the output. Each talker therefore needs a separate receive branch.
Verified by feeding two senders (440 Hz and 880 Hz, distinct SSRCs) into the
receive pipeline: with one talker only 440 Hz is present; with both, 440 Hz and
880 Hz appear together at comparable amplitude.

A permanent silent input feeds the mixer, because `audiomixer` only produces
output while it has one; without it the sink is starved whenever nobody is
talking and every transmission starts with the DAC spinning up. Branches are
built on `pad-added` and torn down on `pad-removed`, so a decoder is not leaked
per talker; measured, two idle talkers were reaped and the count returned to 0.

**Keeping decode branches avoids clipping the next transmission.**
`rtpbin autoremove=false` (its own default) means a talker's
branch survives their silence, so the next thing they say plays from the first
frame. Measured on a CM4 with lyra, 3.00 s bursts:

| receiver state when the talker keys up | audio arrived | lost |
|---|---|---|
| branch rebuilt on demand | 2.82 s | 180 ms |
| branch pre-built and attached | 2.92 s | 80 ms |
| talker already established | 3.04 s | none |

An established `rtpbin` source avoids this delay. Blocking the pad during
construction, lowering RTP source probation, and raising the mixer's
`min-upstream-latency` were each tried and none of them helped; the residual
is `rtpbin` establishing a new source rather than the pipeline linking, so the
fix is to ensure the source is not new.

**The sender establishes its RTP source.** A receive slot in
`rtpbin` is keyed by **SSRC, not by IP**, and it exists only once a packet
carrying that SSRC arrives. Two pieces close that gap.

First, the SSRC is split: **24 bits identifying the node** (a hash of its mesh
address) and **8 bits identifying the run** of the daemon. The prefix lets any
receiver build address → prefix for every node in the registry and name a
talker with no back channel; verified collision-free across a full /24.

A node that reuses its SSRC after restarting has a fresh sequence-number base
that no longer matches the receiver's stored source. In testing, an entire
three-second transmission was lost; neither a beacon nor
`max-misorder-time`/`max-dropout-time` tuning recovered it. A new generation
lets the receiver establish a new source:

| after a sender restart | speech arrived of 3.00 s |
|---|---|
| same SSRC reused | **never arrived** |
| new generation | 2.96 s |
| new generation + beacon | **3.00 s** |

Second, each node sends a **presence beacon**: a ~140 ms muted transmission
(`volume` to 0, valve open, valve shut, volume back) through the normal RTP
payloader, so receivers establish the source before speech begins.

Beacons are **event driven, not a heartbeat**. There is nothing to refresh
(`autoremove=false` means a source is never forgotten), so one is sent at
start-up (announcing this run's generation) and whenever a node appears in the
registry that cannot yet have heard this node. `voice_beacon_sec` (default 600) is
only a safety net for a peer whose arrival was somehow missed, and 0 disables
it. That is about **21 packets an hour, ~5 bps averaged**, against 420/hour at
the 30 s heartbeat this replaced.

**Locally injected RTP cannot establish a peer's source.** Injecting a packet
with a peer's SSRC creates a slot with sequence numbers and timestamps that do
not match the sender. The jitter buffer then resyncs and discards incoming
audio. In testing, a three-second transmission produced silence (peak
amplitude zero), with `rtpjitterbuffer` logging one `resync`. Use the sender's
beacon to establish the source.

**Table size follows the node registry.** Every known node is a potential
talker, and an evicted one pays the first-contact penalty again, so
`voice_max_talkers` (default 8) is a floor rather than a fixed size: on each
registry poll the table is raised to known nodes + 2 headroom. It never
shrinks below the configured value and never exceeds the hard cap of 64.

**Evicted talkers retain a receive pad.** Dropping the decode branch
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

**Pipeline rebuilds are checked and can roll back.** A
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

**Adaptive packing changes packet size.** With
`voice_codec=lyra` the daemon adapts `frames-per-packet` to measured receive
loss while keeping bitrate fixed. On the
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

Under loss, packets get smaller. A lost packet takes `frames-per-packet` frames
with it, and the
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

**QoS uses CS6 (48).** The `voice_dscp` value accounts for batman-adv's priority
mapping.

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

`multicast_forceflood` stays off to allow batman-adv's multicast-to-unicast
fanout.

**Packet overhead.** Every packet carries 12 B RTP +
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

**Filter pole counts follow the measurements.** `audiocheblimit` uses 4:
at 8 poles and 48 kHz, an 80 Hz corner is a
normalised frequency of 0.0017 and the coefficients lose their precision, which
produces **a flat +4.9 dB of gain at every frequency
from 20 Hz to 3 kHz**. `audiochebband` uses 8, because it splits its poles
between the two edges (4 gives a limp -3.5 dB at 60 Hz) and, unlike
`audiocheblimit`, is still stable there at 48 kHz. Both use Chebyshev type 1:
type 2 puts its ripple in the stopband and its cutoff means the stopband edge,
so at the same setting it measures -0.3 dB at 60 Hz against type 1's -9.6 dB.

**Three-band EQ (off by default).** `voice_eq=y` adds `equalizer-3bands` with
fixed centres at 100 Hz, 1100 Hz and 11 kHz, for a formant or presence lift.
`voice_eq_mid=3.0` measures +2.6 dB at 1-1.1 kHz, tapering to +1.5 dB at 500 Hz
and +1.9 dB at 2 kHz, so it is a wide gentle lift rather than a peak.

EQ constraints:

- **The 11 kHz band is forced to 0 under lyra.** Lyra's raw rate is 16 kHz, so
  11 kHz is above Nyquist. At
  16 kHz, `band2=+6` lifted a 1 kHz tone by 2.6 dB and clipped 4608 samples,
  and `band2=-12` cut the same tone by 6 dB. The daemon zeroes it and logs why.
- **Attenuation before the EQ prevents clipping.** The block converts back to
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

**The stall watchdog counts continuous audio buffers.** In a
push-to-talk system `tx_packets` and `rx_packets` stay flat while idle: the valve is
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
read at all counts as unknown, never as a fault. Losing the join stops both
transmission and reception, since batman-adv drops multicast to a group with
no listeners.

The ladder, in order of cost:

| Symptom | Response |
|---------|----------|
| A flow stopped for `voice_watchdog_sec` | Restart that pipeline in place |
| Three such restarts inside 10 minutes | Exit non-zero; `Restart=on-failure` rebuilds the whole stack |
| Talk group change fails to come up | Revert to the group that was working |
| The revert fails too | Exit non-zero, same whole-stack rebuild |
| The GLib main loop stops turning | `WatchdogSec=60` in the unit; systemd aborts and restarts |

The daemon exits to let systemd restart it. `systemctl restart mesh-voice` from inside
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

### BATMAN peer counts

`mesh-peer-count.py` queries `batctl meshif bat0 originators_json` with a
five-second timeout. It validates the complete list, normalizes MAC case, and
counts distinct `orig_address` values across all interfaces. Only a successful
empty list means zero peers; command errors, timeouts and malformed rows return
nonzero without a count. The ACS bootstrap, quorum checker and tourguide
partition sizing share this reader.

The old `awk 'NR>1 {print $1}' | sort -u | wc -l` counted the table's second
header and treated every selected route as the same `*` entry. It could let a
solo lobby node bootstrap, hide isolation, and distort partition comparisons.
Multiple routes to one originator could also inflate the count.

On a query failure, lobby bootstrap clears its start window and waits for a
fresh scan/publish round after recovery. `quorum-checker.sh` returns 2 for an
unavailable check; its caller returns to the lobby only for exit 1, so a failed
query cannot force a channel change. The quorum thresholds are unchanged.
Tourguide sizing adds self only to a valid peer count and aborts a hop if sizing
fails before beacon encoding. The pre-hop size is retained for partition
comparison rather than querying the temporary lobby topology. `--list` on the
same peer helper supplies originator MACs for tourguide election.

### Service elections

All service elections share the same algorithm: the best-connected node wins,
measured by `MEAN_THROUGHPUT_MBPS` in the registry, the mean of BATMAN_V's
metric across that node's originators, in Mbit/s. Stale nodes (not seen within
10 minutes) are excluded. Ties are broken deterministically by MAC address.

This field used to be called `TQ_AVERAGE`, which was wrong: BATMAN_V's metric is
throughput, not a 0-255 link quality. The behavior never changed (highest
wins either way), but the name misled, so it now says what it holds.

Managers wait for `/run/my_ipv4_chunk` before running service elections and
limit them to one pass per fifteen seconds of boot uptime, including discovery
loops. MediaMTX and Mumble serialize their own local runs. These locks cannot
prevent one winner in each disconnected partition; the existing deterministic
election releases a losing node's service and VIP after the partitions merge.

Manual Wi-Fi changes use the band (`2.4` or `5`) as their Alfred identity,
then resolve each receiver's `mesh_24_if` or `mesh_5_if`. Static plans live in
`/etc/manet/static-channels.json`; an absent band is saved without restarting
a radio. The API, receiver and apply path reject manual changes while ACS is
enabled. Plan, live config, lobby config and restart share the channel lock;
an apply waits at most ten seconds for it. Failure restores the previous plan
and configs. Static enforcement reads the plan only while holding that lock,
skips a busy pass, and leaves radios alone if the plan is malformed.

### Channel agreement and recovery

Independent scoring used to diverge when scan reports or incumbents differed.
For channels 2437/2462, A reporting busy 10/80 and B reporting 80/10 gives a
45/45 median tie. With both reports the incumbent wins; B seeing only itself
moves to 2462. Equal reports arriving later preserve that split through bias.

`channel-election.sh` now requests readiness for the current 180-second round.
Only `--score` executes its scoring logic, without radio writes. The
`mesh-channel-agreement.service` starts through the node-manager drop-in and
restarts with it. Static nodes participate passively as compatibility voters.
The generated/static manager publication path is unchanged.

`manet_acs_agreement.py` contains the protocol; `mesh-channel-agreement.py`
handles discovery, authentication, persistence and application:

- The :25 manager request follows scan/publication. Proposal creation is allowed
  at :45–:60 to allow readiness to replicate. The lowest ready ACS identity
  coordinates; it freezes one result, participant list and activation time.
- Discovery maps BATMAN originators to canonical Alfred identities using
  registry identities and authenticated status aliases. Missing identity or
  query failure defers discovery. Missing status keeps that member in the
  denominator. The protocol supports up to 64 participants.
- The view is the entire component reachable over **any** BATMAN radio. HaLow
  linking two Wi-Fi neighborhoods makes them one decision domain. The coordinator
  scores their shared registry observations, filtered to reachable identities;
  no per-Wi-Fi-island channel decision or separate discovery registry is created.
- A node ACKs only a compatible plan whose membership matches its view. Votes
  bind the full plan hash and boot session. Durable state in
  `/var/lib/manet-acs/agreement.json` precedes publishing or applying; a process
  restart cannot cast a different vote in the same round.
- The ACK deadline is exactly proposal time + 60 seconds. The coordinator
  attests a strict majority of the frozen membership after that deadline.
  Two nodes need both; three need two. No majority means expiry. Topology churn
  cannot extend the deadline or remove a nonresponder mid-attempt.
  Expansion of reachable membership cancels an uncommitted island proposal;
  commitments already issued remain binding, then reconcile in a shared round.
- Activation is deadline + 30 seconds. A receiver accepts a commit only before
  activation minus five seconds and applies within a five-second grace. Slow
  discovery refreshes the clock before deciding, so a timed-out query cannot
  authorize a late switch. A fresh round rediscovers departed peers.
- Candidates come from advertised radio capabilities; peers validate their
  own shared bands rather than re-score local reports. Missing bands remain
  unchanged. Static nodes refuse changes to their configured bands. HaLow-only
  nodes can ACK without becoming the Wi-Fi coordinator.

Types 74 (`acs_state`) and 75 (`acs_helper`) reuse `manet_admin.py` encryption
and the shared admin password; payload identities are bound to Alfred record
keys. Records older than 45 seconds or over five seconds in the future are
ignored. A commit is the authenticated coordinator's attestation, not a set
of independent cryptographic signatures. Members sharing the admin password
are trusted. This does not provide global consensus across different partition
views or atomic final-message delivery; clock alignment remains required.

The daemon polls separately from the manager. An expiring `/run/manet-acs-busy`
marker suppresses ACS mutations during preparation while ordinary status/IP
work continues. Activation and tourguide hops use the shared channel lock.
After switching, a 30-second settling interval prevents premature quorum loss.
Application reconfigures all changed radios, checks actual frequencies, and
uses a bounded restart fallback with a second frequency check. Failed landing
is reported, with bounded repair retries; systemd active state alone is not
radio success.

After a moving plan, participants suppress further election votes for 1560
seconds (26 minutes), allowing two twelve-minute rendezvous cycles and an
exchange window even when a band has only one usable entry.
An unchanged plan can update limp mode without starting that hold. A node
still reachable on another band can follow a freshly advertised committed
destination from a reachable peer already operating there, even days after
the original switch. Otherwise lobby recollection handles the straggler. No radio that
is physically out of range is guaranteed to rejoin until contact returns.

### Recovery and reconciliation across all radios

Type 74 now carries the currently operating plan for as long as it remains
applicable, rather than stopping at the end of the election hold.
The 45-second envelope age limit remains: an old plan is usable only through a
fresh authenticated statement from a currently reachable node whose actual,
stable radio settings match it. Old cached Alfred records, future activation,
incomplete majority certificates, changed radio settings and failed discovery
cannot authorize recovery. Only enabled conventional Wi-Fi radios are touched;
there is no S1G hop, new transport or new periodic announcement stream.

To limit airtime, holders piggyback the plan hash on their existing type-74
status. The lowest reachable holder of each plan includes its full certificate;
stale or unreachable publishers no longer suppress another holder. The old
certificate is omitted when publishing the next round's commit so two full
64-member certificates cannot exceed the authenticated envelope limit. It
returns after activation. A newly recovered node can retain the certificate
without hopping again when its radios already match. After a reboot only a
validated, actually operating destination survives; old rounds/votes/holds do not.

A newer committed plan that includes the entire currently reachable membership
supersedes a straggler's older plan. If incompatible operating plans came from
independent islands, neither a newer timestamp nor a certificate hash chooses
the winner: clear the recollection hold and use the next normal shared
scan/publication/majority round. Conflicting plans in the same latest round also
require reconciliation. Per-band capability checks and the frozen majority
deadline remain. Static participants can ACK an unchanged plan without running
the ACS radio-application path.

The distinction is between **different radio settings** and **lost connectivity**.
No Wi-Fi peers while on the agreed frequencies is not an invitation to hop if
HaLow still links the node. Different Wi-Fi RF observations across a HaLow-linked
area remain inputs to one channel decision. Only loss of all paths separates
decision domains; any re-established path brings their registry data and ACS
state together again. Asymmetric or incomplete discovery can delay convergence;
this remains bounded agreement, not an atomic global consensus guarantee.

Cold nodes use the challenge exchange below over any surviving link. A holder
of the live plan answers without leaving its data channel, with one lowest-MAC
source per request/common band and duplicate suppression. Under inconsistent
discovery more than one source can temporarily reply; nonce validation still
applies. A matching channel plan needs no radio change. A clockless requester
in discovery still receives confirmation to finish searching; an established
data node with matching channels does not provoke a reply.
Failed radio applications retry at most once per 30 seconds. Working HaLow now
suppresses local Wi-Fi tourguide duty. Nodes without it retain scheduled visits
for newcomers and genuinely disconnected groups.

### Rotating rendezvous fallback

`manet_rendezvous.py` supplies one absolute schedule to the agreement daemon
and tourguide. Schedule v1 uses 2412/2437/2462 MHz on 2.4 GHz and
5180/5220/5745 MHz on 5 GHz. The existing 2412/5180 anchors are the first entries.
Globally aligned 120-second windows alternate bands; `floor(epoch / 240) modulo 3`
selects each band's entry. Each frequency recurs every twelve minutes. A node
skips unsupported, disabled, non-initiating or DFS entries using its actual PHY
report; it never substitutes frequencies or prunes/reindexes its schedule.
All nodes need the same provisioned schedule. Existing mesh width/mode settings
are retained; compatibility and geographic guide coverage remain bench checks.

A disconnected synchronized searcher tunes each enabled band to its entry at
four-minute boundaries. A clockless searcher stays at the fixed anchors until
authenticated recovery or clock qualification; it does not perform an unsynchronized
scan rotation. On cold daemon startup a disconnected searcher left at a rotating
frequency returns to its anchors. Any surviving BATMAN path, including HaLow,
holds discovery still for recovery or joint bootstrap. A failed peer query does
not authorize movement. A solo searcher cannot self-elect a data plan.

Healthy data nodes with working HaLow keep their Wi-Fi channels throughout.
Other nodes can perform elected guide duty: visit one band/frequency per window,
enter between seconds 30 and 49,
and dwell until second 75. The entry deadline is rechecked after slow preparation;
existing restoration traps and the monotonic duration limit remain. The other
band and HaLow stay in place. A guide whose data frequency equals that slot
advertises and listens there without outbound or return reconfiguration.
Nodes without working HaLow still serve unknown newcomers even when all known
nodes are reachable. No extra periodic announcement stream or S1G hopping was added.

`manet_rendezvous.halow_ready()` identifies S1G radios from `/var/lib/halow_if`,
which provisioning populates for either USB or SPI devices. It requires an
enabled interface with the administrative UP flag, an active entry in `batctl if`,
an active `wpa_supplicant-s1g-<iface>.service`, `iw` mesh-point mode and a successful
`mesh_plink_timeout` query (the same joined-mesh query used by the watchdog).
Each command has a two-second timeout. It does not infer S1G from an `iw` frequency
or a fixed interface name: Morse can expose an ordinary Wi-Fi frequency there.
No current peer is required. The policy assumes a working HaLow radio reaches at
least as far as Wi-Fi; this is a local readiness check, not an RF range test.

The guide checks readiness before election and immediately before departure, so
HaLow becoming available during preparation cancels that visit. Missing/down,
disabled, inactive, unjoined or unresponsive HaLow permits the existing fallback
without a manager restart. The agreement daemon caches readiness for fifteen
monotonic seconds and includes `halow_ready` in existing encrypted type-74 status.
The guide reads fresh authenticated peer flags through `tourguide-exclusions`;
the election removes those canonical identities from its band-capable candidates.
This prevents a mixed group from repeatedly electing a node that will skip duty.
The live local probe overrides a cached report about this node. Stale or missing
peer flags do not suppress fallback; inconsistent views can defer a visit until
status converges. There is no new registry schema or additional broadcast stream.

This gate applies to elected guide excursions from data channels. Fully
disconnected searchers retain their rotating/fixed-anchor discovery behavior;
any BATMAN connection already holds them still for authenticated recovery or
joint bootstrap. The twelve-minute fallback cycle and 26-minute recovery hold
remain, allowing nodes without usable HaLow to recover too.

`/run/manet-rendezvous.json` stores `search` or `data`. The first usable config
seeds it from the fixed anchors; it survives process restarts and is reset by
radio setup/boot anchor restoration. Ordinary frequency changes never redefine
mode. This lets a rotating rendezvous also be a valid data channel. A fresh
authenticated operating certificate, helper beacon or consumed cold nonce reply
can end discovery on the current channel, without restarting that radio. The
next slot cannot pull the recovered node away. A searcher visiting the frequency
of an old saved certificate cannot advertise it as its operating plan.

Discovery uses the shared channel lock and respects prepared/committed work and
the busy marker. Failed discovery applications retry at most every 30 seconds,
with a boot-bound monotonic marker surviving daemon restarts. The recovery hold
is now 26 minutes: two full cycles plus an exchange window. Reconnected conflicting
plans still lift that hold for one shared decision. Bounded collection requires
at least one common usable frequency, overlapping coverage and successful
delivery. A clockless node cannot escape jammed fixed anchors through rotation;
GPS or a surviving HaLow/time path is needed to enable the rotating fallback.

CM4 bench to-do remains deferred for hardware setup: measure request-to-actual
frequency, first authenticated peer packet, BATMAN forwarding recovery, Alfred
request/reply latency, and PTT/traffic disruption on both the hopped and remaining
MT7916 band. Also test HaLow-only continuity, one jammed rendezvous frequency,
single-band and clockless newcomers, and completely separated groups reconnecting
through HaLow. The rotation offers frequency diversity, not immunity to a jammer
covering/following the full channel set.

**"Every candidate was measured and rejected" and "nothing reported a
measurement at all" are different verdicts.** Both leave the qualified list
empty. The first is a real RF result, and the least bad measured channel is
elected with limp mode asserted. The second is an outage: the band's radio is
absent so its scan report carries no entries, or the scan request was refused
wholesale, or Alfred was down and no reports replicated, and it now **holds the
current channel and does not assert limp mode**, logging `No scan data for any
candidate channel`. Treating it as jamming throttled the whole mesh to legacy
bitrates on the strength of missing data.

### Clock readiness and cold-node recovery

Timed ACS and admin transport wait for `/run/initial_time_synced`, created only
after the time service qualifies GPS or NTP. This includes scan/election
requests, static/HaLow compatibility votes, timed channel-plan application and scheduled
tourguide duty. Until then the agreement daemon neither advances protocol state
nor publishes timestamped envelopes. The common admin transport gates types
70–75 on send and receive, preventing a bad startup clock from writing future
sender/replay history. The configuration UI reports the wait before staging a
mesh change; local-only changes remain available. Public status, identity,
allocation and ordinary forwarding continue without this gate.
Parking a disconnected searcher at fixed anchors is also allowed before sync;
rotation itself waits for clock qualification.

The managers reset publication/action timers on the first observed sync, so a
backward startup step does not suppress discovery. The agreement state records
the local boot identity after synchronization. A different boot clears old ACS
rounds and holds; a validated destination can survive if the actual radios still
operate it. A process restart in the same boot preserves
votes. This is a new voting session, already bound into the protocol. Persistent
admin replay history is never cleared. An existing admin history timestamp far
ahead of correct UTC still needs investigation; this change prevents creating
one at unsynchronized startup rather than bypassing its replay protection.

Simply waiting for time before accepting any helper creates a loop: a cold
node may need recovery onto data channels to reach NTP. It cannot safely compare
a type-75 beacon's timestamp yet. A separate authenticated challenge exchange
provides freshness without UTC:

- A clockless ACS node with BATMAN peers present, on lobby or old data channels, publishes an
  encrypted type-76 `acs_probe` at most once per minute. A random 128-bit nonce
  is bound to its canonical MAC and boot. Its monotonic 60-second lifetime is
  stored in `/run/manet-acs-probe.json`; neither process restarts nor clock steps
  extend it. Sixty seconds allows Alfred request/reply replication and a normal
  15-second manager wakeup. There is no probe when no peer is present.
- A synchronized current-plan holder checks probes in the agreement daemon and
  answers over any surviving mesh path, without waiting for a physical visit.
  A synchronized tourguide also checks probes through the existing `helper-encode`
  calls around its lobby visit. It replies only to currently reachable radio
  aliases. Type 77 carries its original compatible data channels, partition
  size and up to 64 recipient boot/nonce pairs. Replies are bounded to one batch
  per five seconds, with a bounded ten-minute cache suppressing repeated probes.
  The extra exchange is demand-driven; ordinary type-75 beacons are unchanged.
- `helper-select` accepts only a reply authenticated under the admin password,
  bound to its Alfred publisher, containing this node's current boot and exact
  outstanding nonce. The largest compatible partition wins, with the usual MAC
  tie-break. Deadline checks run again after I/O. The challenge is atomically
  consumed before exposing the destination to the manager. Wrong, expired,
  replayed, foreign-recipient and previous-boot replies cannot authorize a hop.
- The exception is limited to following a consumed nonce-bound reply. It never sets the clock,
  advertises an NTP server, votes, runs a scheduled partition merge or authorizes
  an admin action. The node still needs GPS/NTP after joining. Unsynchronized
  nodes cannot answer probes or act as tourguides.

`seal_challenge`/`open_challenge` accept only types 76–77 and their corresponding
kinds. They reuse AES-GCM with channel binding but separate salt state and a
fixed zero timestamp; they never alter the timed sender counter. Normal
`seal`/`open` cannot use these types, and admin/control types cannot enter the
challenge path. A probe itself grants no authority; its matching, timely reply
is the sole freshness proof for cold recovery. Shared admin-key holders remain
trusted as in the rest of ACS.

If no usable source or synchronized tourguide ever appears, nodes retain their
current/lobby mesh operation and defer timed changes. No unsynchronized election
fallback or fake time server is introduced. Arbitrary external clock steps after
initial synchronization and unbounded source outages remain outside the timing
guarantees. Actual Alfred loss/latency and cold single-band recovery need the
pending CM4 bench validation.

### Channel scoring

The score, lowest wins, is

    occupancy% + max(0, median_noise - (-95 dBm)) * 0.5 + mean_bss_count * 0.5

Occupancy comes from the survey counters bracketing each scan,
`(busy - transmit) / active`, taken as the difference between a `survey dump`
before the scan request and one after. That bracket is what makes the numbers
comparable: the counters are cumulative since the interface came up, so without
it the home channel reports a running average over its whole uptime while the
other candidates report only the milliseconds the radio spent visiting them. A
channel with under 25 ms of dwell in the window reports no occupancy at all,
because busy time counts in whole milliseconds and the ratio would move in 10%
steps.

The score was `avg_noise + total_bss * 0.1` until 0.548, which decided every
election on `survey dump`'s noise field. A BSS was worth 0.1 dB, so ten
co-channel networks equalled 1 dB of noise floor, and the elections turned
almost entirely on the weakest number collected: driver-derived, uncalibrated
in absolute terms, and different between chip revisions. `perform_scan` was
already running the command that returns busy, receive and transmit time and
discarding everything but the `noise:` line. Noise still contributes, capped
and weighted at 0.5 per dB above -95 dBm, because it catches non-802.11 energy
that busy time can attribute to nothing. It no longer decides the answer alone.
The BSS count is a mean per reporting node, not a sum, so a channel is not
scored worse for having been measured by more radios.

**Disqualification takes a quorum, not one node.** `max_noise > -70` removed a
channel from the election mesh-wide if any single node reported it, with no
outlier rejection and no weighting by whether that node carried traffic. One
bad connector, one radio parked next to a microwave, or one mt76 instance
returning garbage disqualified the channel for everybody, and with only two
candidates at 2.4 GHz a single misbehaving node could take out the whole band.
It now takes `ceil(reporters * 0.34)` nodes, minimum one, to disqualify: 1 of
1, 1 of 2, 2 of 3, 2 of 5, 3 of 6. Two nodes cannot outvote each other and a
veto still stands there, which is the honest answer for a two-node mesh.
Scoring takes the median across reporters, so an outlier that fails to reach
quorum also fails to drag the score.

**The jamming fallback is no longer the lobby.** Both failure exits used to
land on 2412/5180: a hardcoded pair, published in this repo, excluded from
every scan list, and converged on by every node by design. The answer to "every
channel I can measure is unusable" was a move to two frequencies with no
measurements behind them, and the most predictable destination available.
Landing there also made `is_in_lobby` true, which drops the node out of data
state, and the lobby bootstrap only elects with `mesh_peer_count > 0`, so a
node that was jammed and alone sat in the lobby indefinitely. The comment above
`CHANNELS_2_4` had said for some time that the lobby pair must never be elected
for exactly this reason, while the failure path elected it anyway. Total
disqualification now takes the least bad measured channel and asserts limp
mode. The channel is not a good one and limp mode says so, but the node keeps
scanning and re-electing from it.

**`LIMP_MODE_SCORE_THRESHOLD` is reachable now.** It was not before. A channel
that survived disqualification had `max_noise <= -70`, so `avg_noise <= -70`
too, and crossing the old -60 threshold needed `total_bss * 0.1 > 10`, which is
more than 100 BSSes summed across every node's report for one channel. The
branch logging `JAMMING DETECTED` was a congestion detector wearing a jamming
label: it fires in a dense urban RF environment and essentially never in the
field, real jamming always exited through `ALL CHANNELS DISQUALIFIED`, and the
threshold that looked like the jamming sensitivity knob did nothing. In
occupancy units the threshold of 60 reads as "the quietest channel available is
still about two thirds occupied". It is compared against the raw score, not the
one `CHANNEL_BIAS_SCORE` has discounted, so sitting on a band while it goes bad
cannot hide it.

**Migration reconfigures the supplicant instead of restarting it.** The
election used to `sed` the config and `systemctl restart
wpa_supplicant@<iface>.service`, then let the caller sleep 5 seconds and assume
the move worked. `tourguide-manager.sh` already had the better path for its
lobby hops: `wpa_cli reconfigure` re-reads the config in place, so the mesh
point is not destroyed and rebuilt and SAE does not start over from scratch,
and a poll loop confirms `iw dev ... info` reports the target frequency. The
election now uses the same path, keeping the unit restart as the fallback for a
supplicant that does not answer or a radio that has not landed within 10
seconds. This also matters for any SAE problem in the shared `radio-setup.sh`
config, since the election was hitting the full-restart path on every channel
change while the tourguide was not.

One malformed report no longer takes out the election. The registry's reports
were concatenated into a JSON array in awk, so one truncated or corrupt payload
made the whole document unparseable, and the `bc` comparisons that followed
then ran on empty operands under `set -eo pipefail` inside the `flock`
subshell, leaving nothing behind but an exit status. Reports are parsed one per
line now (`jq -Rn '[inputs | fromjson? // empty]'`), a bad one is dropped and
the rest of the mesh still votes, and every per-channel arithmetic step happens
in a single jq pass with no `bc` anywhere.

`ChannelScanResult.busy_pct` in `NodeInfo.proto` is `optional`, so it carries
explicit presence. A channel measured at 0% busy and a channel the radio could
not measure are different answers, and a proto3 scalar default cannot tell them
apart. The election filters absent values out of the median instead of counting
them as zero, so a node on an older build still contributes its noise and BSS
counts without making every channel it reports look empty. A rolling update
can also make nodes disagree: a node still running the old scoring reads the
same reports and can pick a different channel, so update the whole mesh in one
pass. All participants need the agreement service; an older independently switching
node is not a participant in this protocol.

### Tourguide election and partition comparison

`mesh-tourguide-election.py` maps reachable originator MACs to Alfred identities
using the registry's `MAC_ADDRESSES`, then elects across the reachable partition.
Direct-neighbor interface addresses were previously compared to the local
bridge MAC, often producing a winner that no node recognized as itself; the
text-table wlan pattern could also discard all neighbors. JSON discovery
includes HaLow and multi-hop reachability. Missing or ambiguous identities
defer the election. Candidates must advertise an UP mesh interface on the
scheduled band; service hosts are avoided when another candidate is eligible.
Oldest helper timestamp wins, with a MAC tie-break and the same ranking when
every eligible node hosts a service. A confirmed solo node elects itself.

The helper beacon and foreign-partition comparison use the channels and size
captured before hopping. Reading configs after the hop compares a data channel
against the temporarily written lobby frequency; recounting peers then can
include visitors or lose the original partition. Missing bands encode as absent
protobuf fields for public diagnostics. Actual adoption uses encrypted type 75
and checks the local PHY's supported shared bands. Helpers are repeated every
five seconds until second 75 of the common 120-second window, with a monotonic
dwell cap. A late start outside seconds 30–49 is skipped. A trap restores the
data channel on exit/signals; both outward and return hops check the radio.

Receivers reject unsigned or stale (>45-second) helpers. The largest compatible
partition wins consideration, with a MAC tie-break. The lobby no longer takes
the first cached type-69 record. Partition comparisons exclude canonical peers
captured before hopping instead of trusting cached ACTIVE registry entries.

Tourguide holds `channel-election.lock` for its whole run. Agreement activation
uses the same lock; the orchestrator waits while it is held and launches
tourguide only after its other data-state work. This prevents local ACS work
from interpreting a single-radio hop as a genuine channel or topology change.

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

An absent or disabled scheduled band skips the window rather than substituting
the other band. A single-band node therefore has one usable window every four
minutes, aligned with dual-band nodes using that band.

**The smaller partition migrates; equal sizes break the tie on MAC**, lowest
stays put. Both tourguides run the comparison in the same window and each sees
the other's MAC, so exactly one moves. The tie-break is deliberately not the
config string: deciding a split by channel number biases every equal-size merge
toward the numerically lower pair, and can pull a node straight back onto the
channel it fled. Identity is neutral, and the next election re-optimizes the
channel once both sides are talking again.

---

## Network management

`manet-dns-setup.sh` makes `/etc/resolv.conf` point to
`/run/systemd/resolve/resolv.conf`. A managed resolved drop-in clears static
global DNS and supplies `1.1.1.1`/`8.8.8.8` as `FallbackDNS`, used only when no
other servers are known, not when a DHCP server returns an unwanted answer.
dnsmasq reads that same upstream file with `clear-on-reload`, so DHCP renewal
changes its upstreams and invalidates stale cached answers without restarting
DHCP. No localhost forwarding chain is involved. This follows local uplink
DNS; it does not distribute a gateway's private DNS servers over the mesh.

Both first-boot templates invoke the helper, and a dnsmasq `ExecStartPre`
invokes it on service startup. Unchanged configuration does not restart
resolved. The IP manager recognizes this resolver configuration as current
and checks service aliases only when their VIPs are available, avoiding a
repeated DHCP restart when no optional service VIP is configured.

The generated bridge configuration uses `bind-dynamic`: br0 and its IPv4
addresses can appear after dnsmasq starts. Static `bind-interfaces` could leave
DNS listening only on loopback and IPv6 for the rest of the boot. An existing
static binding is replaced once; unchanged dynamic configuration is retained.

`manet_node_ipv4.py` selects the persisted allocation's primary address only
while it is present on br0 and belongs to the configured subnet. The allocator,
both node managers and dashboard share this selection. Address-list order is
not identity: MediaMTX's service VIP or the EUD gateway can appear first.

The hostapd drop-in runs `prepare-ap-iface.sh` before each real start/restart.
The older independently enabled `ap-interface-setup.service` is retired:
it could prepare an AP at boot even while that radio belonged to the mesh.
Hostapd's post-start action applies the AP PHY power cap under the channel
lock after checking active roles and actual AP type. The packaged
`ap-txpower.service` offers the same guarded action for reconciliation and
has no boot enable link. A cap already at or below 5 dBm is left alone; a
later power reset above that ceiling is corrected. Hostapd also queues an
asynchronous dnsmasq start after its AP setup, including boot and recovery;
dnsmasq's own port and isolation guards still apply. The fixed 23/24 dBm lab-power unit, the generated
HaLow fixed-ceiling units and the auto-power HaLow unit are retired.
`manet-mesh-power.service` asks every radio named in `mesh_if` or `halow_if`
for 30 dBm (`iw dev <if> set txpower fixed 3000`) after bat0 enslavement,
skips any interface currently in AP mode, and logs the reported value. The
AP-to-mesh transition requests the same 30 dBm when it clears the AP cap. The
kernel side removes the matching driver and firmware ceilings (see
[the kernel port record](kernel-6.18-morse-port.md) §4.2 and §7). The AP cap is read back after setting it, accepting a lower
regulatory ceiling; a PHY shared with another active interface is rejected.
`manet_ap_mesh.py` owns both directions. It withdraws active mesh roles under
`channel-election.lock` before preparing AP mode. Returning to mesh restores
the provisioned band, rebuilds live/lobby supplicant files from current mesh
credentials and the current band plan, clears the AP PHY power cap, verifies
PONG and the actual mesh channel, then attaches the radio to bat0. Disabled
mesh radios keep their roles/configuration while remaining down; AP-only
hardware never becomes mesh. Failed transitions restore prior files/roles
and restart the previous AP outside the channel lock. Busy transitions leave
the AP serving and retry at the next uplink reconcile. An unplug during a
tourguide visit can similarly delay AP startup until the channel lock is
released; the next reconcile retries. Hostapd start/stop jobs are bounded by
the unit itself, so a timed-out client cannot leave a stale start queued.

Static mode uses the saved authenticated band plan, including changes received
while that band was reserved for AP use. Fresh registry radio observations
validate the chosen frequency and report confirmed/conflict/unknown; they never
rewrite the plan. A node powered off during a static change still needs that
change re-applied through the authenticated management path. ACS uses a valid
local committed destination supported by the returning PHY and consistent with
remaining active radios, or the band's anchor and authenticated recovery. With
no other active Wi-Fi band it enters discovery. Telemetry cannot authorize an
ACS move. Registry evidence excludes stale, down, AP and recent tourguide
radios; its publication interval means validation can lag a channel change.

BATMAN setup/watch and ACS activation use the same channel lock and reload
active roles after acquiring it. They cannot restart or enslave a radio using
an interface list cached before an AP transition. Ethernet detection, unplug
cleanup and uplink reconciliation also serialize their policy decisions; the
radio helper never calls those policy scripts. Hostapd starts happen outside
the channel lock because its preparation helper takes that lock.

- First 5 IPs network-wide are reserved for services.
- Handles conflicts via MAC tie-breaker.
- Configures `dnsmasq` DHCP when needed.

**Which chunks are taken comes from Alfred, one step removed.** Every node
publishes its chunk, primary address and provisioned width in its identity
record (`ipv4_chunk`, `ipv4_address`, `ipv4_chunk_size`, Alfred type 67),
and `mesh-registry-builder.sh` decodes those into `/tmp/claimed_chunks.txt` as
`<chunk>,<mac>,<first-address-integer>,<size>` lines. Allocation and conflict
detection compare absolute ranges, since chunk numbers depend on local width.
This script reads that file and never queries Alfred itself. A successful claim
writes `/var/run/my_ipv4_chunk` and `/var/run/my_ipv4_chunk_size`, which the
node manager hands back to the encoder. The IP manager calls
`mesh-ip-startup.py` before any allocation or restoration. That helper refreshes
the registry on every pass, including after startup; ACS no longer waits up to
180 seconds to rebuild the allocation snapshot. Failed Alfred reads leave the
previous registry and claims intact and defer IP management.

Chunk numbers start at **zero**, after the five reserved service addresses:
in `10.30.0.0/24`, chunk zero starts at `10.30.0.6`. The registry includes it
in the claimed-chunk index. Because protobuf also returns zero for an unset
chunk, a claim requires an advertised IPv4 address as well as a chunk number.
The node managers omit that address until `/var/run/my_ipv4_chunk` exists;
on an already allocated node, the static manager runs IP management before
reading the chunk, so a changed allocation is advertised in the same pass.

**Boot discovery is deliberately nonblocking.** The node manager must keep
publishing types 67 and 68 while IPv4 is unassigned, or a whole mesh booting
together would wait forever for records nobody has published. Both manager
variants publish each loop until allocated; allocation changes bypass the
270-second identity keepalive, and failed identity sends are retried.
Cold nodes publish both records before the first discovery check, allowing the
observation window to start on that pass. After the first allocation, both
managers immediately start another pass to advertise the new claim, without
the usual 15-second sleep. A successful Syncthing ID lookup is cached for the
manager process; a missing ID is retried and each lookup has a three-second
timeout.

The startup helper requires a usable (non-tentative, non-failed) link-local
IPv6 address on `br0`, an active Alfred primary on `br0`, a successful BATMAN
originator query, and our own joined identity/telemetry in the registry. The
generated Alfred unit now waits on `br0`, the interface Alfred actually uses.
It explicitly sets Alfred's existing default synchronization period to 10 s.

With those prerequisites met, start **one fixed observation window** using
monotonic time. Allow allocation after **10 seconds** if every visible peer has
joined identity/telemetry (including the solo case). Missing peer records can
extend discovery only until **20 seconds from the original start**, when the
node logs the missing peers and proceeds with the claims received so far.
Peer arrivals, departures, and repeated membership changes never reset the
start time or extend the deadline. Records arriving between 10 and 20 seconds
can complete discovery immediately on the next successful check.

These limits allow one Alfred synchronization period normally and a second
period for incomplete peer data. All nodes are primary on the same BATMAN L2
domain, so the delay is not multiplied by RF hop count. `alfred -r` reads the
primary's local cache; an empty reply does not establish that no peers exist.
The helper uses
`batctl meshif bat0 originators_json` and matches originator MACs against the
identity's interface MAC list. Peers **do not need IPv4** to satisfy that check.

Proceeding at the deadline deliberately accepts an incomplete view. Delayed
radios, lost data, simultaneous choices, and partition merges can cause
collisions; prompt claim publication and MAC conflict resolution remain
necessary. Failed local commands or missing local readiness still defer
allocation even after the deadline, but preserve the original start time.
On recovery, the node uses that elapsed time rather than starting over.

Both managers sleep 1 second between loops while unallocated, then return to
15 seconds. The deadline is acted on at the next successful check; work within
a loop can delay it, so 20 seconds is a limit on the deliberate peer-data wait,
not a guarantee of IPv4 within 20 seconds of boot. Progress is recorded in
`/var/run/mesh-ip-startup.json` only for this boot. No registry is required from
a prior boot.

The gateway route manager similarly sleeps one second between pending-route
checks during its first 90 seconds, using `/proc/uptime` so an NTP clock step
cannot extend that period. A ready route, a local uplink, or expiry of that
startup period returns it to ten-second sleeps. Missing registry data, a
missing primary address, failed pings and failed route installation all retry.
`manet_node_ipv4.py` chooses the route's source address from the current
allocation, excluding service VIPs and the EUD alias; a changed source triggers
route replacement. An existing non-`br0` default route is preserved even before
the uplink dispatcher writes its gateway marker.

### Local EUD discovery

`manet-dhcp-isolation.py` keeps each node's DHCP pool and EUD discovery local using the native nftables
bridge table `manet_dhcp` (the established table/service names are retained).
The original four DHCP rules are unchanged: forwarding in both directions,
remote requests to the node's server, and local server replies onto the mesh.
For IPv4 and IPv6, the discovery rules drop UDP source **or** destination ports
137/138 (NetBIOS name/datagram), 1900 (SSDP/UPnP), 3702 (WS-Discovery),
5353 (mDNS), and 5355 (LLMNR). TCP 137/5355 also covers the name-service
fallbacks. They apply to forwarding in both directions, input from `bat0`,
and output to `bat0`. Port matches include multicast, broadcast and unicast
queries/replies, including replies to ephemeral client ports. They do not
match other ports or blanket-drop multicast. Noninitial IP fragments have no
transport ports to match; this is a chatter boundary, not fragment reassembly.
Traffic between `br0` and local `end0`/AP clients, and between those local
clients, remains allowed by this table. Mesh names still come from
`mesh-hosts-update.sh` and the registry.

The service installs rules before dnsmasq; dnsmasq's start guard and the IP
manager verify and repair them. Failure prevents DHCP service. `check` is
read-only; `ensure` preserves healthy rules and counters. Readback accepts
nft's omitted implied protocol dependencies but requires every effective
interface, family, transport, port, drop verdict and hook. The fixture's
original DHCP entries came from CM4 nft 1.1.3; added discovery entries model
that format and are not a new hardware capture. Only ordinary `iifname` and
`oifname` metadata is used; node kernels lack `ibrname`/`obrname` support.
The private table is replaced atomically, leaving NAT and UI policy intact.

After successful live verification, `apply`/`ensure` reconcile Avahi's
`[server] allow-interfaces` to the AP from `/var/lib/no_mesh_if`, plus `br0`
when present. Without a verified boundary, the failure path removes `br0`;
without either interface name the allowlist is `lo`, never empty. The
responder remains IPv4-only with its reflector disabled. `radio-setup.sh`
calls the same helper after copying the base config. The tools builder carries
the scripts, units, drop-ins and support files; the updater reloads systemd
and calls `ensure`, applying the live configuration. The IP manager reconciles
it again when `br0` appears later.

**The management name uses the node's internal EUD address.** The CM4
boundary bench found `manet.local` resolving to `10.30.2.2`, MediaMTX's
elected VIP, when Avahi automatically published addresses on `br0`. That
bridge also holds `.146` (the primary mesh identity) and `.147` (the internal
EUD gateway/DNS address); Mumble's `.3` VIP can appear too. The correct address
is `.147`, next to the `.148`–`.150` DHCP pool: it is intended for this node's
own EUDs and stays on the node through service elections. The UI firewall
accepts only localhost and this node's DHCP pool, so a stale VIP answer after
an election fails. These are example allocations, not constants in the code.

`mesh-ip-manager.sh` writes `address=/manet.local/<br0_secondary>` for dnsmasq
and `<br0_secondary> manet.local` in `/etc/avahi/hosts` from the same chunk
allocation. Both files are replaced atomically. The internal address must be
a valid IPv4 address immediately before the DHCP pool; a missing or invalid
address removes the static entry. Pending discovery and releasing a chunk
also withdraw it. Other static host entries and comments are preserved.
The normal allocation pass repairs a missing entry even if DNS is unchanged.
Avahi reloads only after a hosts change, with a pending marker to retry a
failed reload. An inactive Avahi reads the file when started. There is no
additional publisher process or polling loop.

The isolation helper fixes the host name to `manet` and keeps
`publish-addresses=no`, so the primary mesh address and elected VIPs are not
automatically announced. It owns the guarded interface allowlist; the IP
manager owns the static address. A single static name needs no alias/PTR
workaround. The existing HTTP service targets `manet.local` on port 80;
management and measurements live under `/manage/`. Requests use the same
routes for every Host header, including a cached old hostname. Once an old
DNS answer expires, a retired-hostname bookmark must be changed to
`http://manet.local/manage/`; an HTTP redirect cannot repair a DNS lookup that
never reaches the server. `mtx.local` and `mumble.local` remain unicast DNS
service aliases. Client caches must process Avahi's withdrawals/new answers
or expire when the chunk changes.

The base `nftables.service` flushes the ruleset on restart. The IP manager
repairs isolation on its next allocation pass; this is not continuous
protection against an external flush. If repair fails, DHCP stops and Avahi
is narrowed, then DHCP resumes with existing leases after recovery.
DHCP runs only while a local EUD port on `br0` has carrier and is forwarding:
the active AP or a bridged Ethernet client port. A routed uplink and `bat0`
alone do not qualify. Every service start checks this condition, and the IP
manager stops DHCP if the last EUD port disappears. Every hostapd start queues
a DHCP start after the AP is ready. Other EUD port changes are reconciled on
the next IP-manager pass (normally within about 15 seconds).

#### Discovery boundary: mesh traffic inventory

Audit of the runtime scripts and the services they configure, including ATAK
EUD defaults. All cross-mesh ports below are disjoint from the UDP
`{137,138,1900,3702,5353,5355}` and TCP `{137,5355}` sets. The packet-policy
tests exercise these destinations/ports and both request/reply directions.
Custom ATAK/video/plugin ports are not fixed by this repository; they remain
open unless explicitly configured onto one of the blocked discovery ports.

| Sender / dependency | Multicast group or broadcast destination and transport | Effect of discovery rules |
| --- | --- | --- |
| `mesh-voice.py` | `239.192.41.1`, UDP RTP `38801 + 2*(channel-1)` for channels 1–32; paired RTCP is RTP+1 (reserved by the code's convention) | Entire 38801–38864 range passes, including optional unicast redundancy |
| Alfred (`radio-setup.sh`: `alfred -m -i br0 -f -p 10`) | IPv6 `ff02::1`, UDP 16962, plus unicast synchronization | Passes; identity, telemetry, ACS, recovery, config push, elections and shutdown announcements all use Alfred rather than separate IP broadcast ports |
| Syncthing local discovery | IPv4 subnet broadcast / `255.255.255.255`:21027 and IPv6 `ff12::8384`:21027 UDP | Passes; direct file sync TCP/UDP 22000 also passes |
| MediaMTX optional multicast RTSP transport | Allocated group in `224.1.0.0/16`, UDP RTP/RTCP 8002/8003; encrypted variants 8006/8007 if enabled | Passes; no fixed single group |
| ATAK EUD SA, chat, optional sensor input | UDP `239.2.3.1:6969`, `224.10.10.1:17012`, `239.5.5.55:7171` (sensor input disabled by default upstream) | All pass; PRC-152 input UDP 10011 and CoT TCP/UDP 4242 also pass |
| IPv4 neighbor and multicast control | ARP Ethernet broadcast `ff:ff:ff:ff:ff:ff`; IGMP `224.0.0.1`, `224.0.0.2`, `224.0.0.22` and group-specific reports | Non-TCP/UDP; passes |
| IPv6 neighbor/router and multicast control, including radvd | ICMPv6 `ff02::1`, `ff02::2`, `ff02::16`, solicited-node `ff02::1:ff00:0/104`, group-specific reports | Non-TCP/UDP; passes |
| BATMAN V underlay | EtherType `0x4305`, including Ethernet broadcast on mesh radios | Not filtered; underlay runs below `bat0`, not as an IP discovery service |
| DHCP / dnsmasq | Local IPv4 broadcast UDP 67/68 | Existing DHCP isolation unchanged; local EUD operation retained |
| Avahi | `224.0.0.251:5353` (IPv6 mDNS group is `ff02::fb`, if responder IPv6 is later enabled) | Intentionally local only; wired/AP `manet.local` remains local |

No further node-tools IP broadcast/multicast destinations were found. NTP /
chrony uses unicast UDP 123, Mumble TCP/UDP 64738, the UI TCP 80, and the ATAK
node service unicast UDP 4242/4349. MediaMTX unicast defaults TCP
8554/8322/1935/1936/8888/8889 and UDP 8000/8001/8004/8005/8189/8890 are also
outside the sets. No election, registry, service configuration or other
firewall policy is changed. Syncthing's optional UPnP router search **does**
use SSDP 1900: the requested block prevents discovering a UPnP gateway across
`bat0`, while preserving local discovery and mesh file transfers. Setup
disables global discovery/relays, but does not explicitly disable NAT mapping.

Protocol references: [Alfred sockets](https://raw.githubusercontent.com/open-mesh-mirror/alfred/main/netsock.c)
and [Alfred protocol analysis](https://downloads.open-mesh.org/batman/papers/Positionsdaten_in_Wireless_Mesh_Networks.pdf),
[Syncthing discovery](https://docs.syncthing.net/specs/localdisco-v4.html) and
[configuration](https://docs.syncthing.net/users/config.html),
[MediaMTX 1.15.3 defaults](https://raw.githubusercontent.com/bluenviron/mediamtx/v1.15.3/mediamtx.yml),
[ATAK CoT defaults](https://raw.githubusercontent.com/deptofdefense/AndroidTacticalAssaultKit-CIV/main/atak/ATAK/app/src/main/java/com/atakmap/comms/CotService.java)
and [chat defaults](https://raw.githubusercontent.com/deptofdefense/AndroidTacticalAssaultKit-CIV/main/atak/ATAK/app/src/main/java/com/atakmap/android/chat/ChatManagerMapComponent.java).

#### Mesh traffic census

Run `sudo /usr/local/bin/manet-mesh-census.py --minutes 5 > /tmp/mesh-census.json`
on the node with a real phone/laptop attached. It passively captures Ethernet
headers on `bat0`, with no probes, firewall rules, multicast joins, promiscuous
mode or retained payloads. JSON rows, sorted by bytes, group traffic by
direction, inferred source bridge port, source MAC, protocol, TCP/UDP source
and destination ports, destination address/kind (including multicast group
and broadcast), and visible VLAN IDs. Counts include packets and Ethernet
bytes; `kernel_drops`, snapshot failures and bounded-row overflow are explicit.
Ctrl-C prints the partial census. Non-IP, unknown protocols and fragments are
counted too; transport ports on noninitial fragments cannot be decoded.

`source_port` is `end0(fdb)` / an AP port from bridge learning, `local(mac/ip)`
from local MAC/IP ownership, `bat0` for ingress, or `unknown` /
`routed-or-unknown`. This is inference from snapshots every two seconds, not
physical ingress tracing: MAC spoofing, VLANs, routing, learning lag and port
moves limit attribution. `sport` is the separate transport source port.
`in` sees arrival before input/forward filtering, so incoming dropped packets
still appear. `out` has passed bridge filtering but does not prove reception
by another mesh node. Nonzero losses/overflow mean detail is incomplete.
Use the observed output to decide further drops later; no additional discovery
protocols are filtered speculatively.

#### Verify the management name

Install the updated IP manager, isolation helper and web server, remove the
retired publisher service and drop-in, and run `manet-dhcp-isolation.py ensure`.
The complete commands follow below. After the next allocation pass, inspect
the generated DNS and static mDNS entries:

```bash
ssh cm4 'set -e
sudo /usr/local/bin/manet-dhcp-isolation.py check
grep -E "^(dhcp-range=|dhcp-option=3,|address=/manet\.local/)" /etc/dnsmasq.d/mesh-eud.conf
cat /etc/avahi/hosts
grep -E "^(host-name=|host-name-from-machine-id=|publish-addresses=|allow-interfaces=)" /etc/avahi/avahi-daemon.conf
systemctl is-active avahi-daemon
ip -4 address show dev br0'
```

The reported allocation expects DNS and the static mDNS A record to be `.147`,
with `publish-addresses=no`, regardless of whether `.2`/`.3` VIPs are present.
On the wired laptop, set `EUD_IF` to its Ethernet interface:

```bash
sudo resolvectl mdns "$EUD_IF" yes
sudo resolvectl flush-caches
resolvectl query -i "$EUD_IF" --protocol=mdns --type=A manet.local
avahi-resolve -4 -n manet.local
dig @10.30.2.147 manet.local A +short
```

For uncached wire evidence, capture on CM4 `end0` while querying from the
laptop: `sudo timeout 30 tcpdump -Q out -nn -vv -i end0 'udp src port 5353'`.
Read the A records themselves; the reply's source IP is not the advertised
address. Expect only `.147` in answers for `manet.local`, never `.146`, `.2`
or `.3`. Check `/etc/avahi/hosts` and `journalctl -u avahi-daemon -n 30 --no-pager` if absent.
An Avahi client has its own cache, separate from systemd-resolved; restart
that laptop's Avahi daemon or wait for expiry if its old VIP answer persists.
Repeat during an otherwise scheduled service election; do not alter VIPs just
for this name test. When the allocator changes the EUD gateway, compare the
new generated DNS and hosts entries with fresh mDNS answers after the
allocation pass reloads Avahi. Offline tests exercise this change without
moving a live node's allocation.

#### Apply and verify the discovery boundary

From the checkout on the dev laptop (these are instructions, not an automated
node operation):

```bash
ssh cm4 'mkdir -p /tmp/manet-discovery'
scp MANET/share/manet/dhcp-isolation.nft \
    MANET/node_tools/manet-dhcp-isolation.py MANET/node_tools/manet-mesh-census.py \
    MANET/node_tools/radio-setup.sh MANET/node_tools/mesh-ip-manager.sh \
    MANET/node_tools/mesh-status.py cm4:/tmp/manet-discovery/
ssh cm4 'set -e
sudo systemctl stop manet-avahi-publish.service 2>/dev/null || true
sudo rm -f /usr/local/bin/manet-avahi-publish.py /etc/systemd/system/manet-avahi-publish.service /etc/systemd/system/avahi-daemon.service.d/20-manet-publish.conf
sudo install -m 0644 /tmp/manet-discovery/dhcp-isolation.nft /usr/local/share/manet/
sudo install -m 0755 /tmp/manet-discovery/manet-dhcp-isolation.py /tmp/manet-discovery/manet-mesh-census.py /tmp/manet-discovery/radio-setup.sh /tmp/manet-discovery/mesh-ip-manager.sh /tmp/manet-discovery/mesh-status.py /usr/local/bin/
sudo systemctl daemon-reload
sudo /usr/local/bin/manet-dhcp-isolation.py apply
sudo systemctl restart node-manager.service mesh-status.service
sleep 20
sudo /usr/local/bin/manet-dhcp-isolation.py check
sudo nft list table bridge manet_dhcp
cat /etc/avahi/hosts
grep -E "^(dhcp-range=|dhcp-option=3,|address=/)" /etc/dnsmasq.d/mesh-eud.conf
grep -E "^(allow-interfaces=|publish-addresses=)" /etc/avahi/avahi-daemon.conf
test ! -e /usr/local/bin/manet-avahi-publish.py
test ! -e /etc/systemd/system/manet-avahi-publish.service
test ! -e /etc/systemd/system/avahi-daemon.service.d/20-manet-publish.conf
if systemctl is-active --quiet manet-avahi-publish.service; then exit 1; fi
systemctl is-active avahi-daemon dnsmasq mesh-status node-manager'
```

Expect `allow-interfaces=<AP>,br0` (or just `br0` without an AP), passing
verification and active services with a connected EUD. In a CM4 terminal,
start this capture, then run the laptop sender below within 40 seconds:

```bash
sudo timeout 40 tcpdump -Q out -nn -i bat0 \
  'udp and (port 137 or port 138 or port 1900 or port 3702 or port 5353 or port 5355)'
```

Expect **zero egress packets** and rising nft `forward`/`oifname bat0`
discovery counters. Capture on `end0` with the same filter to verify the
stimulus arrived and local replies work; zero egress without arriving probes
or increasing counters is inconclusive. Incoming `bat0` captures are before
the drop and cannot be expected to be zero. To exercise node-originated
output as well, run the same Python sender on CM4 with `br0` and its primary
IPv4 address; `output` counters should increase, still with zero egress.

On the wired laptop, substitute its Ethernet interface and assigned IPv4:

```bash
EUD_IF=enxYOUR_ETHERNET
EUD_IP=10.30.2.149
python3 - "$EUD_IF" "$EUD_IP" <<'PY'
import socket, struct, sys, time
iface, ip = sys.argv[1:]
index = socket.if_nametoindex(iface)
def question(name):
    labels = b''.join(bytes([len(s)]) + s.encode() for s in name.split('.')) + b'\0'
    return struct.pack('!6H', 0, 0, 1, 0, 0, 0) + labels + struct.pack('!HH', 1, 1)
for family, groups in [(socket.AF_INET, ('224.0.0.251', '224.0.0.252', '239.255.255.250')),
                       (socket.AF_INET6, ('ff02::fb', 'ff02::1:3', 'ff02::c'))]:
    with socket.socket(family, socket.SOCK_DGRAM) as s:
        if family == socket.AF_INET:
            s.bind((ip, 0))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        else:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF, index)
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 255)
        for group, port, payload in [(groups[0], 5353, question('manet.local')),
                                     (groups[1], 5355, question('manet')),
                                     (groups[2], 1900, ('M-SEARCH * HTTP/1.1\r\nHOST: ' +
                                      (f'[{groups[2]}]' if family == socket.AF_INET6 else groups[2]) +
                                      ':1900\r\nMAN: "ssdp:discover"\r\nMX: 1\r\nST: ssdp:all\r\n\r\n').encode())]:
            for _ in range(3):
                s.sendto(payload, (group, port) if family == socket.AF_INET else (group, port, 0, index))
                time.sleep(.1)
            print('sent', group, port)
PY
sudo resolvectl mdns "$EUD_IF" yes
sudo resolvectl flush-caches
resolvectl query -i "$EUD_IF" --protocol=mdns --type=A manet.local
```

Expect only the internal EUD gateway IPv4 address **via mDNS** for `manet.local`;
unicast `dig` alone does not prove this. The laptop needs IPv6 enabled on the
link for the IPv6 probes. Repeat from the AP when available.

For an SA positive control, attach an ATAK client to another mesh node and
enable its SA multicast input (so BATMAN learns a remote listener). On CM4:

```bash
sudo timeout 40 tcpdump -Q out -nn -i bat0 'udp dst port 6969 and dst host 239.2.3.1'
```

During that capture, run on the wired laptop (same `EUD_IF`/`EUD_IP`):

```bash
python3 - "$EUD_IP" <<'PY'
import datetime as dt, socket, sys, time
now = dt.datetime.now(dt.timezone.utc)
def stamp(t): return t.strftime('%Y-%m-%dT%H:%M:%SZ')
cot = (f'<event version="2.0" uid="manet-s12-probe" type="a-f-G-U-C" how="h-e" '
       f'time="{stamp(now)}" start="{stamp(now)}" stale="{stamp(now+dt.timedelta(seconds=60))}">'
       '<point lat="0" lon="0" hae="0" ce="9999999" le="9999999"/>'
       '<detail><contact callsign="S12 TEST"/></detail></event>').encode()
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
    s.bind((sys.argv[1], 0))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(sys.argv[1]))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 64)
    for _ in range(5):
        s.sendto(cot, ('239.2.3.1', 6969))
        time.sleep(.2)
PY
```

Expect five CoT datagrams leaving `bat0`. Confirm reception on the other
node with `tcpdump -Q in -nn -i bat0 'udp dst port 6969 and dst host 239.2.3.1'`;
the test marker is named `S12 TEST`, at 0/0, and expires after 60 seconds.
Then collect the five-minute census with the real EUD workload.

### Gateway choice and uplink speed

batman-adv picks a gateway itself (the `*` in `batctl gwl`), but that pick only
steers its DHCP handling. Linux needs a default route, which is why this
script exists. It used to route to batman's pick, which switches as
soon as another gateway scores 5 Mbit/s more (`gw_sel_class` 50, BATMAN_V's
absolute threshold), on a single reading, with no time component. On a 6
Mbit/s HaLow path that margin is huge; on a 200 Mbit/s Wi-Fi path it is noise.
And every gateway announced batman's default 10/2 Mbit/s, so the score could
not see uplink speed, and paths faster than 10 Mbit/s all tied.

The script now reads every gateway from `batctl gwl -H -n` and decides itself.
The score matches batman's idea, the bottleneck: the lower of the path
throughput and the announced download bandwidth. A switch breaks every open
internet connection, because the new gateway NATs from a different public
address, so a voluntary switch needs a noticeable gain: at least 1.5 times
the current score and 2 Mbit/s more (relative and absolute, so it means the
same at HaLow and Wi-Fi rates), sustained for 60 seconds, and not within 300
seconds of the last switch. The first choice counts as a switch, so a node that
picked early from a partial list waits out the hold before improving. A gateway
missing from the list, or failing two consecutive one-second pings (about 20
seconds at the steady poll), is replaced at once, best remaining first. After
a restart the gateway the existing `br0` route points at is current, so
restarting the service does not move the node.

Gateways announce a measured download speed. `manet-uplink-speed.sh` downloads
5 MB over HTTPS (Cloudflare's speed endpoint, or a 5 MB range of an OVH test
file) and divides by the time after the first byte, excluding DNS, TCP and TLS
setup. 5 MB keeps it cheap but under-reads fast links, since TCP is still
ramping. That matters little because the mesh path, not the uplink, is the
bottleneck above roughly 100 Mbit/s. A result is reused while the interface
keeps the same address and router, and dropped on demotion, so a replug or a
new network measures again. The download doubles as the internet check for
Ethernet uplinks: the earlier ICMP or 204 probe passes through a captive portal
that allows them, but a portal cannot complete HTTPS to the test host. A
failure is retried at most once a minute (exit 3 until then, so the dispatcher
does not log every pass). In practice the next attempt comes from the node
manager's status publish, every 3 minutes. Only Ethernet is tested: a phone
tether or cellular modem (by driver: `rndis_host`, `ipheth`, `cdc_ether`,
`cdc_ncm`, `qmi_wwan` and similar) and Wi-Fi uplinks pay for the data, so they
keep the probe and announce 10/2. `batctl gw_mode server` takes whole kbit
values and keeps the previous bandwidth when given none, so every announce
passes an explicit `down/up`. Upload is not measured; it is announced as a
fifth of download, batman's usual ratio, and nothing selects on it.

Four properties of the claimed-chunk file matter:

- Only nodes the registry marks **ACTIVE** appear. One unheard from for 300 s
  goes STALE and its chunk returns to the pool.
- A free chunk is chosen at **random**, reducing but not eliminating the
  chance that nodes booting together pick the same one.
- The file lives in `/tmp` and is rebuilt at boot. Its absence cannot bypass
  discovery or a successful registry refresh.
- A saved chunk in `/etc/mesh_ipv4_state` is a preference. After discovery,
  reuse it only if no other node claims it; otherwise choose a free chunk.
  Ignore our own cached advertisement when checking the saved chunk. Later
  collisions are resolved by the MAC tie-break using Alfred's claim list.

Chunk size is `max(max_euds_per_node, 1) + 2`, fixed for each node at provisioning.
Mixed sizes are supported; unknown or implausible advertised widths block new
allocations until complete identities arrive. Existing allocations still check
the known primary address for conflict. `mesh_config.py` rejects attempts to
change the provisioned width through the UI. There is no manual allocation
override: pinning a node's chunk by hand
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
- Enslaves active mesh roles (including a returned AP candidate).
- Sets BATMAN_V algorithm.
- Handles start/stop operations.
- HaLow is added first so it becomes batman's primary (longest-range link).
- Skips writing a `.link` file when the MAC is already pinned by an existing
  one; two link files for one MAC caused a rename ping-pong reboot loop.

---

## Time synchronization

`mesh-time-sync.py`, started by `one-shot-time-sync.service` through the retained
shell entry point, is the sole runtime owner of chrony's configuration and
start/stop lifecycle. The manager, Ethernet detector and off hook no longer
replace its config or stop it independently. This prevents a completing client
attempt or departing Ethernet link from stopping a newly available GPS source.
The active uplink dispatcher already records `mesh-gateway.state` and
`upstream_iface`; the controller observes those files as well as carrier, so
both the active dispatcher and the legacy detector can trigger internet sync.

Local roles are checked every 15 seconds. A recent `gps_status.json` fix enables
the GPS SHM refclock. A direct uplink enables the public pool, with
`bindacqdevice` restricting NTP acquisition to that interface. Source profiles
allow the configured IPv4 mesh CIDR and the existing IPv6 mesh prefix. Client
and idle profiles deny serving; no profile has a `local stratum` fallback or
unconditional internet sources on an ordinary mesh client. Both provisioning
templates emit the same service and a quiet initial chrony config.

The controller reads `chronyc -n tracking` and `sources` with checked exit status
and bounded command timeouts. Qualification requires a real reference, stratum
1–15, a synchronized leap status, at most 0.1 seconds of remaining system-clock
correction, and a recent selected source with nonzero reachability. This is a
completion criterion, **not a bound on absolute UTC error**. GPS and internet
markers require a selected GPS refclock or an uplink NTP source respectively.
Invalid queries or loss of qualification withdraw those markers. The existing
manager publish path ORs `/run/mesh-ntp-gps.state` and `/run/mesh-ntp.state` into
type-68 `is_ntp_server`; cadence is unchanged and no separate announcement is
sent. Cached advertisements can therefore outlive source availability until
the next telemetry update; actual NTP validation still has to succeed.

Ordinary clients parse registry assignments as data, never source them as shell.
`MAC_ADDRESSES` maps interface originators to canonical identities, and IPv4
addresses must be unambiguous host addresses within the mesh CIDR. Only selected
`originators_json` routes (`best: true`) with positive BATMAN_V throughput and
last-seen age at most 30 seconds qualify. Highest throughput wins, with a MAC
tie-break. Self, shutting-down and unadvertised nodes are excluded. Registry
wall-clock timestamps and the derived STALE state are deliberately not liveness
gates: the local clock being repaired cannot reliably compare them. Kernel
route age and an actual recent NTP measurement provide bootstrap liveness.

No usable peer or failed discovery causes a local retry after 30 seconds. A
selected peer gets a 90-second attempt, followed by a five-minute exclusion on
failure so another source can be tried. Deadlines and exclusions use monotonic
time and persist in `/run/mesh-time-client.json` across service restarts, without
extending the attempt. Successful client sync requires the selected source to
match the chosen address and the sample to be from the current attempt. It
creates `/run/initial_time_synced`, writes the quiet profile and stops chrony.
The marker survives process restarts, not reboot. A nonblocking file lock
prevents duplicate owners.

The operational expectation is one or two days of use, sometimes longer, with
most nodes directly using GPS. ACS needs alignment on a seconds scale, not
millisecond accuracy. Ordinary mesh clients therefore refresh after **six
hours plus 0–600 seconds of per-node staggering**. The jitter is generated once
per boot and persisted with the monotonic refresh deadline. Service restarts
and wall-clock corrections cannot postpone it. Local source qualification
renews the deadline, so losing GPS/internet starts a holdover period based on
the last verified source. Between refreshes there are only local role checks;
no BATMAN query, NTP polling or extra Alfred announcement is required.

For scale, an assumed residual error of 50 ppm accumulates 1.08 seconds in six
hours, versus 8.64 seconds in two days. Oppositely drifting nodes can differ by
twice that amount. Those are arithmetic examples, **not measured CM4 limits**.
ACS has a five-second future-message allowance and late-apply grace; those
checks are not a blanket guarantee that every operation tolerates five seconds
of skew. Hardware measurements should determine whether the refresh interval
needs adjustment. No freshness guarantee is made if sources remain unreachable.

Each due refresh uses the same 90-second attempt and peer backoff as initial
sync. An unsuccessful refresh retains the boot-sync marker and previous success
time, keeps serving flags off, and retries discovery without claiming accuracy
or blocking ordinary mesh operation. If the refresh state is missing, a boot
marker alone cannot grant another six-hour delay. Startup may step the clock;
after the first verified sync the controller disables remaining live startup
steps with `chronyc makestep 0.1 0` and removes the directive from the config.
All later source changes, daemon restarts and client refreshes use slewing, so
routine correction does not jump backwards through saved ACS/admin timestamps.
`leapsecmode slew` also avoids a kernel leap-second step. Peer profiles use
`corrtimeratio 1`, preferring an average correction over one poll interval rather
than three to fit the bounded attempt, still subject to chrony's slew-rate cap.
The current source daemon is not restarted just to disarm stepping.

The old positional `batctl o` reader treated last-seen timestamps or `(` as the
metric and did not map radio MACs. Its unchecked command failures, executable
registry input and unbounded registry wait are removed. The old
`waitsync 60 0 0 1` also skipped the remaining-correction check: zero disables
that bound. See the primary [chronyc command reference](https://chrony-project.org/doc/4.6/chronyc.html)
for `tracking`, `sources` and `waitsync`, and the [chrony configuration reference](https://chrony-project.org/doc/4.6/chrony.conf.html)
for acquisition binding and source settings.

Remaining limits: ACS/admin activation and tourguide rendezvous still depend
on wall-clock alignment. Initial synchronization now gates timed control, with
nonce-bound cold lobby recovery as described above. Arbitrary external clock
steps after synchronization are not repaired by ACS. An unusually large
later correction may not settle inside a bounded refresh; it is not forced by
stepping. CM4 checks of real chrony/GPS timing, 24–48-hour drift, source loss,
missed telemetry and on-air traffic remain pending hardware setup.

---

## Data management

Node state is exchanged over Alfred as two message types, split by how often
the contents change. Alfred replicates every record to every node on a timer,
so anything that repeats is paid for continuously.

| Type | Message | Published | Contents |
|------|---------|-----------|----------|
| 67 | `NodeIdentity` | startup, allocation changes, 270 s keepalive | hostname, MACs, Syncthing ID, chunk, IP |
| 68 | `NodeTelemetry` | every startup loop, then 180 s | everything volatile |
| 69 | `NodeTelemetry` | tourguide window | public helper diagnostics; not migration authority |
| 74 | encrypted `acs_state` | 5 s and protocol changes | readiness, proposal, votes, commit, operating plan ID; one full-certificate publisher per plan |
| 75 | encrypted `acs_helper` | 5 s during lobby dwell | authenticated recovery channels and partition size |
| 76 | encrypted `acs_probe` | at most once per 60 s, only clockless nodes with peers | requester boot, random challenge, current Wi-Fi channels and radio aliases |
| 77 | encrypted `acs_probe_reply` | on demand over surviving paths or during tourguide visits, at most once per 5 s | channels/size and recipient boot/challenge pairs |

Alfred stamps every record with the publishing node's MAC; it runs `-i br0`, so
that key *is* the node's primary MAC. It is the join column between the two
types, and the reason neither message repeats it.

Identity is republished at 270 s because Alfred purges any record it has not
seen for `ALFRED_DATA_TIMEOUT` (600 s, confirmed on hardware: a test record was
purged at 618 s). At 270 s a publish can fail once and the record still
survives.

**mesh-registry-builder.sh**

Central registry builder.
The shell entry point now execs `manet_registry_builder.py`; IP discovery
imports the same builder directly. It decodes all records in one interpreter
and caches decoded payloads beside `observed.tsv`, invalidating that cache when
the decoder/schema changes. Every pass still reads both Alfred types and ages
observations, including tombstones and stale-claim removal. Identical claim
files keep their inode/mtime; registry display ages remain current.

`mesh-ip-manager.sh` requests `manet_ip_runtime.py` work from the existing
channel-agreement process through `manet-runtime-client.sh`. A worker thread
sleeps in `select` until requested; the main ACS loop keeps its one-second
schedule. Root-only FIFOs and a client lock serialize requests, accept only
allowlisted operations/arguments, and return status plus data. An unavailable
or busy worker permits a standalone one-shot fallback without queuing; a submitted
request's failure or timeout never replays a potentially applied operation.
Operation framing accepts digits (`ipv4`); malformed requests with valid reply
tokens return an error instead of silently timing out. Data-returning operations
reject empty success replies, except Syncthing's missing first-boot certificate.
Allocated identities retry failed/empty address observations without publishing
an empty IPv4 or advancing the keepalive timer. Discovery, DHCP isolation
and primary-address selection share the resident interpreter. Before
skipping the shell allocator it verifies live nft isolation, EUD forwarding,
addresses/prefixes, dnsmasq state and UI-table existence. It compares config,
claims, allocation markers, generated DNS/mDNS files, local MACs and helper
generations with a previously successful, unchanged reconciliation. Contested
or incomplete claims, missing addresses, pending reloads, failed checks and
changed files take the normal allocator path. The cache is volatile in `/run`;
no polling interval, service-election gate or publication deadline changes.

- Reads both Alfred types and joins them on the record key.
- Decodes each message.
- Writes `/var/run/mesh_node_registry` with all node state.
- Writes `/tmp/claimed_chunks.txt`, the claimed-chunk index
  [`mesh-ip-manager.sh`](#network-management) allocates from.
- Caches identity across cycles: a node whose identity record has not been
  refreshed yet keeps the values from the previous registry rather than
  appearing nameless.

Freshness uses this node's boot clock and changes to the telemetry payload,
not the sender's timestamp. `/run/manet-registry/observed.tsv` stores the payload
hash, the uptime when it last changed, and the uptime when last present. Reading
an identical record does not refresh it. Missing records retain tombstones for
900 seconds, beyond Alfred's record expiry, so a cached record reappearing does
not regain freshness. Failed Alfred reads preserve the last complete snapshot.

`OBSERVED_AGE_SECONDS` is the local observation age at registry construction;
`OBSERVED_AT_UPTIME` is the corresponding `/proc/uptime` time and lets readers
age a saved registry without wall clocks. After 300 seconds the registry marks
a record `STALE` and excludes its claim. `LAST_SEEN_TIMESTAMP` is sender time
for display only; `LAST_REGISTRY_UPDATE` is local wall time for display. A peer
first observed after our boot initially counts as fresh, conservatively keeping
its claim while discovery proceeds.

**encoder.py**

Encodes this node's Alfred payloads to protobuf and Base64. Two subcommands:

- `encoder.py identity`: hostname, secondary MACs, Syncthing ID, chunk, block size, IP.
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

## Mesh configuration push

**manet_admin.py: shared admin authority over Alfred**

One administrator normally knows the password; other users read status. Every
node has the same `admin_password`. SAE membership, a publisher MAC, and
unencrypted telemetry cannot authorize a change. Receivers authenticate before
inspecting actions, choosing the newest message, staging, ACKing, cancelling,
or applying. Web authentication uses only `admin_password`, with no radio/AP
password fallback.

| Alfred type | Authenticated payloads |
|-------------|------------------------|
| 70 | `mesh_config`, `mesh_config_cancel` |
| 71 | `radio_state`, `radio_cancel` |
| 72 | `radio_ack` |
| 73 | `config_ack` |
| 74 | `acs_state` |
| 75 | `acs_helper` |
| 76 | `acs_probe` (clock-independent discovery only) |
| 77 | `acs_probe_reply` (nonce-bound cold recovery only) |

The wire format is a `manet_admin_v1` envelope carrying a 16-byte publisher
salt, 12-byte random nonce, and AES-256-GCM ciphertext with its authentication
tag. The encrypted body carries the action, a random message ID, and a send
timestamp in nanoseconds. Additional authenticated data binds the protocol
version and Alfred type, preventing a message from being used on another
control channel. A new nonce is generated on every send. Password-derived
keys use scrypt (`n=32768`, `r=8`, `p=1`, 32 MiB) and a domain-separated random
salt per publisher. Keys are cached only in process memory. The implementation
uses the cryptography library's
[AEAD](https://cryptography.io/en/43.0.0/hazmat/primitives/aead/) and
[scrypt](https://cryptography.io/en/43.0.0/hazmat/primitives/key-derivation-functions/#scrypt)
APIs. Password rotation encrypts the new password under the current password;
the apply log never prints either value.

`manet_admin_limits.py` bounds receive-side scrypt work across processes using
root-private tmpfs state in `/run/manet-admin-receive`. Unknown salts share a
burst of eight derivations, refilling at one per second; cache-miss derivations
serialize their memory use. A bounded allowlist of authenticated salt identifiers
keeps healthy senders off that budget across receiver restarts. Derived keys
remain in process memory. Exact invalid envelopes are cached for sixty seconds;
salt alone is never a rejection key. Credential-file generation scopes both
caches. Separate bounded-wait locks keep cache reads independent of slow KDFs.

Replay history lives in `/var/lib/manet-admin`, with directory mode 0700 and
atomic, fsynced 0600 files. Each control channel remembers its newest accepted
message and whether it was consumed. Repeated delivery of a pending stage is
allowed. Older messages and consumed activations/cancellations are rejected,
including after loss of `/run` or a rollback. The receiver records consumption
**before** applying: an ACS change can kill its own node manager, and a radio
change can interrupt the process. This gives at most one apply attempt per
message; an interrupted/failed attempt requires a fresh administrator request.
Do not erase this history during rollback. Corrupt history fails closed.

Normal authenticated envelopes expire after 900 seconds and may be at most 60 seconds
ahead of the receiver's clock. Newly provisioned nodes lack replay history,
so expiry bounds their acceptance of previously recorded, otherwise valid
messages. These checks require synchronized clocks, as scheduled activation
already does. Each staged edit also gets a fresh random transaction version;
repeating identical settings does not reuse an earlier ACK. Config activation
uses encrypted type-73 ACKs, never `CONFIG_ACK_VERSION` from public telemetry.
Both send and receive wait for the initial-sync marker. Types 76–77 use only
the separate challenge API and the monotonic nonce lifetime described above;
their fixed zero timestamp is never considered a time source or an admin order.

The dependency is Debian's `python3-cryptography`; imports are lazy so its
absence disables control without disabling the public status page. Setup and
the updater install it, and an enabled `manet-admin-setup.service` handles
older tools updaters at the next boot. The service runs independently of
node-manager startup. Upgrade every node and restart the web publisher before
using control; there is no plaintext compatibility fallback. No protobuf
schema changes are needed.

Earlier publishers included `admin_password` in plaintext config packages.
An exposed password must be replaced through a trusted path on all nodes;
encrypting a replacement under a password already known to an observer cannot
revoke that observer. The encryption change does not retroactively protect
old broadcasts or old apply logs containing passwords.

This protects the Alfred control path. Browser management intentionally uses
HTTP over the local AP or Ethernet for trusted-team deployments. The shared
admin password primarily prevents accidental settings changes by teammates;
HTTPS is optional rather than a deployment prerequisite. HTTP does not
protect credentials against a participant who intercepts the connection.
The shared password also exists on each node: root access to a node conveys
network administration.

**mesh-config-write.py**

Config and supplicant values are written as literal data using atomic file
replacement, preserving ownership and permissions. The old interpolated sed
replacement interpreted delimiters, ampersands, and executable sed commands
inside accepted values. Regression tests cover those strings, backslashes,
quoted SSIDs, and rejected newlines.

Ordinary supplicant quotes delimit literal bytes, so the writer does not double
backslashes. Embedded quotes use hexadecimal string encoding. Mesh changes and
rollback exclude USB `*-uplink.conf` files. A shared helper selects the standard
or HaLow service from configured roles and checks both service activity and a
control-socket PONG. A failed restart prevents a successful apply marker and
retains a rollback snapshot for retry.

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

The form validates all changed fields before local writes or mesh publication;
unchanged provisioned values do not block unrelated edits. Receivers use the
same allowed keys, byte lengths, enums, and IPv4 allocation-capacity checks.
`manet_eud_ap.py` preserves the `-xxxx` node suffix, so an AP base SSID may use
at most 27 UTF-8 bytes. It writes mesh.conf and hostapd atomically per file,
restarts hostapd, and restores both files if activation fails. Telemetry reads
the actual configured broadcast SSID. Measurement session paths use a bounded
label alphabet, reject dot/parent and symlink paths, and generate random result
filenames; peer display names remain JSON data.

**mesh-config-rollback.sh**

The safety net for dangerous changes. A wrong mesh key takes the mesh down, and
with it the only way to push a correction, so each node has to be able to undo
the change on its own, with no help from the network.

`arm` counts distinct `orig_address` values from
`batctl meshif bat0 originators_json`, snapshots `/etc/mesh.conf` and the
supplicant configs, and sets a deadline (default 300 s, `MANET_ROLLBACK_GRACE`).
The JSON query has a five-second timeout. Selected routes count, duplicate
paths count once, and command/JSON failures are errors rather than zero peers.
The snapshot is built in a private temporary directory and published only
after every copy and the state write succeeds. An already armed trial cannot
be overwritten or have its deadline reset by another `arm`.

The config receiver requires a successful `arm` before a dangerous apply.
A missing helper, timeout or nonzero exit leaves the settings untouched and
the activation unconsumed, so a later manager cycle can retry while the
message is valid. Only an explicit boolean `no_rollback: true` bypasses this
gate; safe changes and unchanged dangerous settings do not need it.

`check` runs every node-manager cycle and is a no-op until the deadline passes,
then commits if at least one peer is visible or restores and restarts the
supplicants if none is visible or the query fails. State lives in `/var/lib`
because a dangerous apply can end in a reboot. A persistent `restoring` marker
ensures an incomplete restore is retried even if a peer becomes visible in
the meantime. Failed file restores or service restarts retain the snapshot.

A node that had no peers before the change commits rather than rolling back:
on a solo bench node "the mesh did not come back" cannot be told apart from
"there was never anyone there". This requires a successful empty baseline
query; an unreadable or incomplete state cannot select the solo-node branch.

---

## Updates

`auto_update=` set to a true value. See
[networkd-dispatcher/README.md](../MANET/networkd-dispatcher/README.md).

GitHub Releases now supplies metadata and packages together. Stable selection
reads `manet-release.json` through the Latest release URL; development explicitly
selects the newest published MANET release by publication timestamp and ID.
Fixed tag URLs bind the manifest, archive and checksum to one release. A source
push does not trigger updates. Publication checks all six archive versions,
uploads to a draft, verifies GitHub asset digests, then publishes. Routine builds
are prereleases; explicit promotion sets stable and Latest. Cleanup retains the
newest three prereleases and every stable release.

The updater checks both the manifest digest/size and the checksum sidecar before
its existing archive validation. A newer installed development version is left
alone by stable checks unless `--allow-downgrade` is supplied. Channel selection
is per invocation; Ethernet-triggered automatic runs always select stable.
See [release tooling](../MANET/releases/README.md).

---

## Provisioning

**manet-ap-guard.sh**

Decides, for one interface, whether a mesh supplicant may start on it right
now. Installed as an `ExecCondition` on `wpa_supplicant@.service` through
`/etc/systemd/system/wpa_supplicant@.service.d/10-manet-ap-guard.conf`, so it
applies to every caller that starts or restarts a mesh supplicant.

The AP candidate may serve mesh when a wired EUD takes priority in auto mode.
The transition helper publishes its active mesh role before starting the
supplicant. This guard requires that membership and an inactive hostapd; a
missing mesh role refuses startup even while hostapd is still preparing.
Starting a mesh supplicant against an AP otherwise fails with -95 and can
leave hostapd active over a dead BSS.

Exits 0 to allow non-AP radios or a released candidate with an active mesh role,
and 1 to skip a radio reserved for AP.

`have_package_network` is checked before each apt phase, so "no network" is
recorded once before attempting package downloads.

This exists because a node reached the field with none of its packages
installed: its Ethernet was unplugged part-way through provisioning, every apt
call failed silently behind `|| true`, and the script still touched
`/var/lib/radio-setup.done`. Nothing on the node said anything was wrong.

The log is opened with `tee -a`, not `tee`. It used to truncate per run, which
destroyed the history of the run that went wrong, and two overlapping runs
interleaved into an unreadable file.

Interface renames reuse `radio-setup-run-once.service`, which stays enabled
until setup succeeds. A separate rename service previously started alongside
it after reboot, racing package installs and sharing the failure file. Setup
now takes a nonblocking process lock before opening its log or clearing state.
The lock belongs to the `flock` supervisor and is closed in its child, so
background descendants cannot keep it held after setup exits. Duplicate calls
return without modifying the active run. A requested rename reboot exits setup
immediately rather than falling through to the completion checks.

`manet-radio-names.service` runs after coldplug udev processing, before
`sysinit.target` and `network-pre.target`. MAC-keyed `.link` rules can fail
with `File exists` when two radios must exchange occupied `wlan` names.
The helper reads only the generated `10-wlan*.link` pins, validates the whole
exchange, moves displaced radios to temporary names, then assigns their final
names. It retries the udev add events so device units become available to the
supplicants. It refuses to move an active or enslaved radio or displace an
unpinned interface. Missing hardware is reported without preventing other
radios from being named. No pin files on first boot means no work; setup
enables the service for subsequent boots. The packaged enable symlink also
covers tools updates, taking effect at the next boot.

`manet-os-cleanup.py` runs once during setup on Raspberry Pi OS Trixie after the
MANET kernel is running. The Pi first-boot package list installs runtime libraries
instead of development headers, and setup installs `gpsd-tools` instead of the
GUI `gpsd-clients` dependency tree. Cleanup explicitly removes unused appliance
packages and their orphaned dependencies, treating recommendations as optional
for this transaction. Required recommendations such as ALSA profiles,
rfkill, regulatory data and NSS modules are protected alongside core runtime
packages and libraries used by the prebuilt radio tools. Kernel images and radio
firmware are protected too.

A dry run pins those runtime roots in a temporary copy of APT's extended state;
it does not alter live auto/manual selections. Apply saves the package inventory,
manual selections and plan, pins the real runtime roots, validates the plan again,
then purges. Plans that remove protected packages or install/upgrade anything are
rejected. The stock swap file is deleted only after checking its signature, that
no swap is active, and that no loop device references it. A success marker prevents
later setup runs from undoing operator additions. Cleanup failure is recorded as a
provisioning failure, not silently marked complete.

On the two CM4 Trixie bench nodes, cleanup reduced root filesystem usage from
5.3/5.4 GiB to 1.8/1.9 GiB. Most savings came from the unused 2 GiB swap file,
about 1.1 GiB of packages, and the APT cache. Both rebooted with healthy mesh,
AP, DNS, dashboard and internet access; the second rejoined with Ethernet
unplugged. Native tool dependencies and a Lyra encode/RTP/decode pipeline were
checked. This verifies existing installations; the revised first-boot package
list still needs a fresh-image hardware test.

Profile 2 adds runtime cleanup through the same setup/manual entry point. A
profile 1 marker does not skip it; a successful profile 2 marker preserves later
operator additions unless `--force` is explicit. Preview reads the enabled system
and global user unit lists and active user managers without changing them. Apply
saves inventories, per-unit reasons and each attempted command before execution;
failures leave the previous marker. Global user masks are followed by reload/stop
in current user managers through their private systemd sockets, so stopping the
user bus does not require terminating logins. System D-Bus is never masked.

| Unit/package | Profile 2 verdict and consumer audit |
| --- | --- |
| Global user `pulseaudio.service` / `.socket`, `rtkit-daemon.service`; `pulseaudio`, `pulseaudio-utils`, `rtkit` | Stop/mask and purge packages. `mesh-voice.py` explicitly uses `alsasrc`/`alsasink` on the USB ALSA device. `button-monitor.sh` invokes `led-info.sh`, which only drives LEDs. No runtime `pactl`, `paplay` or Pulse sink/source consumer exists. `mpg123` appears only in package policy/templates; retain this useful ALSA-capable field command. |
| `dbus-user-session`, **user** `dbus.service` / `.socket` | Keep the package; stop/mask global user activation. The CM4 removal preview showed purging it also removes `gstreamer1.0-plugins-good` through libsoup/glib-networking/dconf, so it is protected by KEEP. No MANET service uses a session bus: Syncthing runs as `syncthing@radio.service` under the system manager. Keep system `dbus`/`dbus-daemon`, PAM/logind and user managers. Operators needing session-bus applications can unmask these two user units explicitly. |
| `cron.service` | Disable/stop/mask only if no user crontabs, custom table entries, or executable jobs without a recognized systemd early-exit guard exist. No repo/provisioning cron consumer exists. Retain `cron` tools; log custom consumers for review. |
| `man-db.timer`, `dpkg-db-backup.timer` | Disable/stop/mask optional indexing/backup schedules; retain `man`, `mandb` and dpkg. Their commands remain usable manually. |
| `e2scrub_all.timer`, `e2scrub_reap.service` | Disable/stop/mask on the raw-partition appliance. Preserve both if LVM tools or device-mapper LVM volumes exist. Keep `e2fsprogs` and boot fsck. [Debian's e2scrub manual](https://manpages.debian.org/trixie/e2fsprogs/e2scrub_all.8.en.html) describes the LVM requirement. |
| Global `wpa_supplicant.service` | Disable/stop/mask its unused D-Bus instance. Keep `wpasupplicant`, `wpa_supplicant@wlanX`, the s1g services and USB-uplink instance management used by radio setup/uplink tools. No wildcard masks. |
| `polkit.service` / `polkitd` | Keep. Our UI, `networkctl`/`resolvectl`/`hostnamectl` callers run as root, but the OS networkd daemon does not. DHCP uplinks retain default hostname handling; networkd requests transient hostnames and may request the product UUID from hostnamed. The upstream [DHCP path](https://raw.githubusercontent.com/systemd/systemd/v257/src/network/networkd-dhcp4.c) and [hostnamed authorization](https://raw.githubusercontent.com/systemd/systemd/v257/src/hostname/hostnamed.c) make blanket removal inappropriate. No MANET network configuration is changed to avoid this dependency. |
| User `gpg-agent*`, `dirmngr`, `keyboxd`, `ssh-agent` sockets | Keep unmasked: no recurring polling job, and masking would break normal user signing/decryption, key retrieval or SSH credentials. Protect installed GnuPG/OpenSSH client packages from autoremove. |
| `keyboard-setup.service`, `console-setup.service` | Disable/stop/mask boot console setup on this headless appliance; retain configuration/tools for manual recovery. No runtime/provisioning consumer requires the boot services. |
| `iperf3.service` / `iperf3` | Keep enabled/package protected. `manet_manage.py:run_local_iperf3` connects to peer servers on TCP 5201; both templates enable the daemon and `manet-ui-firewall.sh` restricts it to the mesh. |

Other explicitly enabled provisioning services support mesh, voice, identity,
GPS/battery, LEDs, web UI, Syncthing, DNS, routing and recovery; they remain.
APT/security refresh, logrotate, fstrim, tmpfiles, journald and SSH are also
outside the removal policy. Unknown enabled units are inventoried for review,
never removed by an inverse allowlist. Profile 2 was applied on CM4 after the
`dbus-user-session` correction; netplan/libnm/python3-yaml left through autoremove
(provisioning already disables netplan). Fresh-flash verification remains pending.

### ATAK runtime and package defaults

`manet-atak.service` is core and enabled on every node. An absent or empty
`atak` setting enables it; only `n`, `no`, `0` or `false`, ignoring case and
surrounding whitespace, disables it. The daemon and its pre-start firewall
command share that decision. A disabled start removes stale private tables and
exits successfully; stop/reload-to-disabled also cleans up through `ExecStopPost`.
Use `atak=n` in `/etc/mesh.conf` followed by a service restart to opt out.

Both provisioning templates enable the unit, and `radio-setup.sh` enables and
starts it after the GPS setup. All generated mesh.conf variants document
`atak=y`. The tools, CM4, Rock 3A and RPi5 builders copy both ATAK and positioning
units, with only ATAK added to `multi-user.target.wants`. The tools updater
extracts that enable symlink and reloads systemd; newly enabled units start at
the next boot, or immediately with `systemctl start manet-atak.service`.
`manet-positioning.service` remains disabled and its configuration default off.

The current ATAK admission path requires a verified wired EUD on `end0`/`br0`,
a matching local FDB/neighbor mapping and the owned nftables policy. Android
location services should be off; ATAK receives external GPS CoT on UDP 4349,
ordinary CoT on UDP 4242, and sends SA to the radio's IPv4 address on UDP 4242.
SA admits and pins the phone identity. The radio advertises the contact
`MANET <hostname>` (`atak_callsign` can override it), with its TCP 4242 endpoint
for Send. Select that contact when sending a manual point. The contact is
hidden from the map; the separate external-position feed carries location.
Phone presence, clock and manual-location state are retained under
`/var/lib/manet-atak`; the service never sets the OS clock.

The ATAK daemon now subscribes to route link/address/neighbor (including bridge
FDB) and nftables notifications before its first readback. A cached proof avoids
all four `nft`/`ip`/`bridge` subprocesses on unchanged ticks. Role/generation file
metadata is the cheap additional invalidator; each new TCP stream or UDP peer
forces a full snapshot. Events arriving during a snapshot discard it. Overflow,
truncation or subscription failure closes the service and lets systemd restart
with fresh subscriptions. SIGHUP also invalidates it. The existing output and
input cycle intervals are unchanged, with no extra daemon or polling subprocess.
The kernel firewall remains the packet boundary; asynchronous userspace
notifications cannot make an arbitrary external ruleset flush atomic with I/O.

With no admitted phone, no CoT is sent. Before the first association, the
application does not read GPS, select a position, create warnings or checkpoint
unchanged state. It still checks monitor/jamming file metadata and their
freshness deadlines, durably remembering producer activation/loss so a failed
producer cannot later appear unwired. Inputs are parsed only when metadata
changes or their boot/raw-clock freshness expires. Future timestamps have their
own retry deadline; neither wall-clock steps nor the cache extend a fix's TTL.

After a pinned phone leaves, changed safety inputs, phone presence expiry and
warning deadlines still run. A held manual point or warning history retains the
30-second durable checkpoint to preserve age lower bounds across reboot. Audio
acknowledgements are checked only with pending audio; audio output is rewritten
only on changes. Unchanged idle cycles skip selection, serialization and runtime
JSON writes. `status.json` includes `idle`; its timestamps describe the last
snapshot, not a service heartbeat. Use systemd for liveness and the observation
timestamps to advance a displayed manual age. Admission/path changes are handled
on the existing event cycle, and active output stays at its previous cadence.

Remaining idle work is the existing 0.2-second select wakeup, one-second
role/generation and input metadata checks, stream expiry and netlink handling.
These preserve admission and fail-closed safety without polling subprocesses.

`manet-cpu-sample.py` reads cgroup-v2 `cpu.stat` twice around one timed sleep,
including child-process CPU, and reports CPU seconds per elapsed minute plus
percent of one core. It also reports aggregate busy system CPU for profile
comparisons, without double-counting guest time. Restart/counter resets invalidate
the sample. Use matching phone traffic and uptime conditions for before/after;
the reported old 575 CPU seconds / 8000 elapsed seconds equals 4.3125 CPU s/min
(7.1875% of one core). Slice 16's CM4 result reported in
`review-collab/atak-20261006/claude-017.txt` was about 1.0% of one core
(0.6 CPU s/min), with `path_ready=true`. Slice 17 has no hardware after-sample:
node access was excluded. On the next authorized node run, detach the phone and
use `sudo python3 /usr/local/bin/manet-cpu-sample.py --label slice17-no-phone
--seconds 60`, then repeat after attaching the phone. Offline tests prove four
initial commands and zero more over 300 unchanged cycles; the application also
does no further JSON parsing/writing, durable saves or CoT sends over 300
unchanged never-associated cycles. Tests cover input expiry, faults before first
association, reconnect, pending audio and warning deadlines.

Operator scripts run through `manet-user-scripts.sh`. Per-script completion
markers allow an interrupted run to resume without repeating scripts that finished.

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

## Tests

Tests sit alongside the code they cover and run without hardware or a node:

| File | Covers |
|------|--------|
| `test_halow_plan.py` | The region HaLow plan in `manet_radio.py`: EU capping at 2 MHz and US reaching 8, channel numbers and center frequencies unique within a region and resolving each other, every region/bandwidth pair carrying an operating class, an unknown region falling back to EU, and a channel or bandwidth the region does not have being refused |
| `test_mesh_config.py` | The local/mesh key split in `mesh_config.py`: EUD and AP keys never reaching Alfred, mesh keys still going, only values that differ from `mesh.conf` counting as changes, and `max_euds_per_node` never being written |
| `test_peer_radios.py` | The peer radio chips in `manet_peer_radios.py`: frequency-to-channel conversion, published `INTERFACES_JSON` winning over the registry fallback, the fallback filling in when it is empty, and the channel fields surviving an encode/decode round trip |
| `test_mesh_registry.py` | Real encoder/decoder/registry integration, chunk zero, saved chunks, read failures, and timely publication |
| `test_mesh_ip_startup.py` | Bounded discovery, late/missing peers, failed queries, and allocation barriers |
| `test_mesh_boot_pipeline.py` | Real manager loops, encoded Alfred records and discovery helper with a simulated clock: first-pass discovery, full observation window, failed publication, Syncthing ID caching/retry, and claim publication without the steady-state sleep |
| `test_gateway_startup.py` | Real route manager with simulated networking: late addresses, missing registry, reachability/install retries, bounded fast polling, preservation of local uplinks, stale-route removal and correct primary source |
| `test_mesh_config_rollback.py` | Real rollback script with simulated BATMAN: unique peer counts, solo nodes, bounded recovery, failed queries/backups, interrupted restoration, and the receiver's apply gate |
| `test_mesh_peer_count.py` | JSON counts and real shell callers with simulated BATMAN: empty/single/multiple peers, alternate routes and MAC case, failed queries, bootstrap reset/recovery, quorum return-to-lobby gating, and partition size/beacon/migration failure handling |
| `test_led.py` | Real boot/button displays with fake GPIO holders and BATMAN: direct nodes across radio aliases, multihop exclusion, connected before registry arrival, unknown versus zero, recovery, opt-in hardware, GPIO errors, exclusive ownership, signal cleanup and onboard provisioning verdicts |
| `test_mesh_time_sync.py` | Selected BATMAN routes and canonical identities, safe registry/address parsing, late/missing peers, bounded attempts and six-hour refreshes across restarts/clock steps, correction and source freshness, GPS/uplink transitions, quiet intervals and holdover on source loss, disarming startup steps, unchanged advertisement path and provisioning unit parity |
| `test_acs_agreement.py` | Majority/timeouts, message loss, replay/restart safety, real scoring/activation, failed persistence and radio landing, authenticated straggler recovery |
| `test_acs_bootstrap.py` | Clockless recovery with real encryption, boot/recipient/nonce binding, expiry after restart and slow reads, consume-before-move failure, radio compatibility, traffic bounds, live tourguide response and no unsynchronized timed work |
| `test_acs_connected.py` | Day-old plan recovery over HaLow, actual-channel and freshness checks, one certificate publisher and failover, bounded retries, shared RF scoring after real partitions reconnect, static ACKs, and clockless replies without tourguide hops |
| `test_acs_rendezvous.py` | Absolute rotation, cold anchor waiting, HaLow precedence, unavailable slots and peer queries, restart/retry behavior, authenticated data-channel overlap, selective helper adoption and real tourguide visits without redundant retunes |
| `test_acs_halow_tourguide.py` | Live S1G readiness without peers, failure/recovery and pre-departure cancellation, bounded health probes, readiness in existing encrypted status, fresh peer exclusions and mixed-group guide selection |
| `test_acs.py` | Single/dual-band roles, lobby transitions and channel application, real channel elections, provisioning role selection, canonical tourguide identities and eligibility, shared channel locking, pre-hop comparisons, and real single-band helper encoding/return hops |
| `test_manet_admin.py` | Real encryption, wrong keys, tampering, authenticated ACKs, expiry, replay after restart/rollback/interrupted apply, password rotation, and literal config writes |
| `test_node_update.py` | Real archive staging and installation in a temporary root: checksum/version rejection, unsafe paths and links, download/disk/copy/dependency/service failures, locking, daily throttle and interrupted-install retries |

Run them from the git root, so this directory is on `sys.path`, and from the dev
venv, so the protobuf runtime matches the fleet:

```bash
source ~/.venvs/manet/bin/activate    # bash ../packaging/setup-dev-env.sh
python -m pip install cryptography==43.0.0
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

`carrier.d/50-ethernet-detect` is the canonical hook. The reference `carrier`
delegates to it, and setup no longer overwrites it. The hook retains
`ethernet-autodetect.sh --hotplug` for end0; other interfaces use
`manet-uplink-dispatch.sh carrier`. The packaged Ethernet boot unit and setup
use the same hotplug command. `off` is the same file in both places.

The routable hook queues `manet-auto-update.service` with `--no-block` after
uplink reconciliation. Its wrapper rechecks opt-in and a selected non-mesh
uplink with a default route, then runs the routine updater. No ICMP probe gates
downloads and no download holds up a dispatcher event. The obsolete
`mesh-default-route-fix.service` is disabled and removed by updates; the gateway
route manager owns mesh default routes and preserves service VIPs.

`no-carrier`, `degraded` and `routable` in this directory are three-line wrappers
around `manet-uplink-dispatch.sh <state>`. Nothing installs them, and their
states are covered by the `.d/` entries above.


## Adding or changing a hook

Change the file under `carrier.d/`, `routable.d/`, or `off`, and rebuild the
tarballs; every builder picks the set up from `stage_dispatcher_hooks`, so there
is one copy of each. The two generated hooks used to be heredocs duplicated
across the three install builders, which is how one wrong `grep` came to need
fixing in four places and how the rpi5 copy drifted from the other two.

`node-update.sh` reloads systemd after installing the tools payload; it does not
run `udevadm`. Dispatcher hooks need neither; networkd-dispatcher
reads the directory on each event, so a replaced hook is live immediately.

---

## LEDs

### External harness audit

`led-boot.sh` and `led-info.sh` previously counted text rows in `batctl neighbors`.
Their filter retained a column header and counted multiple radio links as
multiple neighbors, while command failures could become a false zero. Both now
use `mesh-neighbor-count.py`: a five-second-bounded `neighbors_json` query of
direct neighbors, including HaLow and wired mesh links, excluding multihop nodes.
The existing registry's `MAC_ADDRESSES` maps radio MACs to canonical node keys,
so multiple radios to one node produce one blink. Registry entries alone never
establish connectivity. See [batctl's direct-neighbor query](https://github.com/open-mesh-mirror/batctl#batctl-neighbors_json).

The helper returns 0 with an exact count, 1 for an unavailable query, or 3 when
live neighbors exist but their identities cannot yet be resolved. Exit 3 is
distinct from Python/argparse's exit 2 on launch/argument errors. Zero or one
neighbor address needs no registry. Missing, invalid or ambiguous registry
aliases for multiple addresses yield connected-without-a-count, never invented
node totals. The button shows N green blinks for N direct nodes, red for confirmed
zero, amber for an unavailable neighbor query, or three seconds solid green for
connected-without-a-count. Boot retries failed/empty queries and completes its
connected indication as soon as direct neighbors are confirmed, even before
registry identity discovery. ACS/quorum/tourguide originator counting is unchanged.

`manet-led-common.sh` also centralizes the optional harness configuration. An
existing GPIO controller is not evidence of attached LEDs: `/etc/default/manet-led`
must explicitly set `LED_ENABLED=1`. Pin defaults remain provisional. A shared
flock prevents competing boot/button holders; boot yields between half-cycles,
and the button owns its whole count sequence. Each dwell checks the gpioset
holder for failure. EXIT/INT/TERM cleanup kills and reaps it. An explicit off
request is held briefly before normal exit, but the electrical state after GPIO
release still depends on the circuit; no software test proves that it is dark.
See [libgpiod's lifetime contract](https://libgpiod.readthedocs.io/en/master/gpioset.html).

The external boot and button units now live in `MANET/systemd/`, so tools updates
carry the same definitions as installation. Both are ordinary simple services
wanted by `multi-user.target`, with no ordering after that target. The previous
boot unit remained activating indefinitely without peers because it was an
unbounded oneshot; its `DefaultDependencies=no` avoided implicit target ordering.
The button unit, which kept default dependencies, ordered itself after its own
target and conflicted with that target's implicit ordering after wanted services.
`radio-setup.sh` enables the shipped definitions instead of overwriting them.
Onboard LED behavior is independent and unchanged; regression tests cover its
complete/incomplete/running states and legacy completion-marker fallback.

### Provisioning verdict

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
