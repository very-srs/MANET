# How the mesh works

## Marine

![Crayon drawing of four radios with an orange route crossed out at a failed link and a purple alternative route through another radio.](../assets/mesh-crayon.png)

### Pass it along

Radios pass traffic through other radios. If a link breaks, another available path can carry it.

- **Wi-Fi:** faster links nearby.
- **HaLow:** longer reach, lower speed.
- **No path at all?** Move back into range or restore a working link.

The radio can find another route. It cannot invent one across a gap with no usable signal.

Radios keep listening to the channel they are using. If other people's Wi-Fi
keeps it busy, they look for a better channel together. After following their friends to a new channel, they stay
there for a while before choosing again.

A radio borrowing house Wi-Fi for internet access has fewer radios left to talk to its friends.

## User

### More than a direct connection

Two radios do not need to hear each other directly if other mesh radios provide a usable path between them. MANET uses the available links to carry traffic across those intermediate nodes.

Wi-Fi and HaLow can connect the same group of nodes. Wi-Fi favors higher throughput. HaLow provides another path at lower speed and typically longer reach. Neither promises a fixed distance through every building or landscape.

### Choosing channels

Both Wi-Fi meshes use 20 MHz channels. A narrow channel reaches farther and
lets a CM4 check radar channels in the background. Between nearby radios the
5 GHz mesh carries about 140 to 180 Mbit/s at this width. Actual speed depends
on signal, interference, and the route. Updating an existing radio applies the
narrow channels without reflashing.

With automatic channel selection enabled, radios share what they measure and
agree on one Wi-Fi channel plan. Radios still connected through HaLow belong to
the same mesh for this decision, even while their Wi-Fi links are down. A radio
that starts on a different Wi-Fi channel moves to the one the others use as
soon as HaLow connects it, without waiting for a vote.

Each radio measures the channel it is on. It only looks at other channels when
it, or another radio in the mesh, reports lasting interference. That means the
channel is busy with other people's traffic at least 60% of the time, which is
where mesh speed starts to drop. A healthy channel is never abandoned just to
look around. After a restart the first measurement takes about three minutes,
so a restart alone never moves the channel.

A better channel has to win three rounds in a row, three minutes apart, before
the mesh moves. A channel measured as blocked or overloaded can be left as soon
as a measured alternative exists. A missing reading is not treated as
interference. The radios keep their current channel until they have a real
comparison.

A radio that follows another radio's channel change saves the new channel and
waits with the rest of the group before choosing again. It does not drift back
to its old channel.

Scheduled channel moves need a synchronized clock. A radio that just powered up
can rejoin before its clock is set, but timed moves wait for GPS or network time.

### The third antenna and radar channels

The CM4's 5 GHz radio normally receives with all three antennas. While the radio
has lasting interference, or while it is checking a radar channel, it lends one
antenna to a background listener. That listener measures how busy other
channels are without interrupting the mesh. Download speed over 5 GHz can drop
by up to a third while the antenna is lent out. It returns when the radio no
longer needs it. Rock 3A and RPi5 radios do not use this feature.

Radar channels, called DFS channels, are shared with weather and airport radar.
They are often quiet, which makes them useful in crowded places. The mesh only
uses one when:

1. **Every connected radio supports it.** One radio without support keeps the
   whole group on regular channels.
2. **It measures better than the regular choices.** A radar channel without a
   real measurement cannot win.
3. **Every radio has checked it for radar.** The check takes one minute, or ten
   minutes on weather radar channels, and the mesh keeps running meanwhile.
4. **The group has agreed on a regular backup channel.**

If radar appears, the radio that hears it stops transmitting at once and
switches to the backup. The other radios follow its announcement, or the
message passed through 2.4 GHz and HaLow. The mesh then reconnects on the
backup by itself and stays off radar channels for 30 minutes.

Radar protection stays on while a radio changes settings or installs an update.
If a radio cannot keep listening for radar, it leaves the radar channel first.
A readout that could be the radio's own traffic is never treated as a clear
channel.

Using a built-in radio as an internet connection removes that band from the
mesh on this radio. The remaining radios keep their mesh roles. See
[Wi-Fi uplinks](internet.md#user-use-nearby-wi-fi).

### Rejoining after separation

HaLow provides a recovery path when it is working. Where HaLow is unavailable, selected radios visit rendezvous channels to help separated groups meet again. Recovery takes time and still requires a usable radio path.

If a node has internet access, it can become a gateway. Nodes can choose among available gateways using the mesh path and uplink performance. Shared services also elect hosts and can move when a host disappears.

Check the status dashboard for actual peers, routes, and link information. A completed setup or a powered-on radio does not guarantee a connected mesh.

## Developer

### Routing and control use different mechanisms

BATMAN V routes at Layer 2 over the attached mesh interfaces. Alfred replicates identity, telemetry, and control records. The local registry is a view derived from that data. Identity uses type 67 and volatile telemetry type 68. Reachable BATMAN membership must be distinguished from stale registry entries.

`node-manager-acs.sh` schedules home-channel RF measurement, gated candidate scans, coordination requests, tourguide work, and service elections while maintaining status and addressing. A scan only counts if it completed. A channel visited for less than 25 ms is left out, 5 GHz busy time needs three readings, and observations older than three minutes are not published. The static manager retains the status, address, and service work without automatic Wi-Fi channel selection. `node-manager-select.sh` chooses the runtime symlink from `acs=` before service start.

### One connected component, one channel decision

Wi-Fi mesh configs enforce 20 MHz with `max_oper_chwidth=0` and
`disable_ht40=1` in each network block, keeping HE/VHT rates enabled.
`ht40=0` alone does not prevent 40 MHz. The setup and
AP-to-mesh generators use both required keys. The updater migrates live and
lobby copies under the channel lock before restarting active Wi-Fi mesh
services. The start wrapper also enforces the width when an inactive radio
returns to mesh. HaLow and client uplinks keep their own width settings.
ACS and observer candidates are 20 MHz primaries. DFS CAC and CSA use the
primary as the center frequency, matching the runtime third-chain switch's
20 MHz requirement. See
[mesh supplicant recovery](../../../docs/node-tools-internals.md#mesh-supplicant-selection-and-recovery)
for migration details.

`mesh-channel-agreement.py` coordinates a proposal for the connected BATMAN component across **all** radios. Wi-Fi groups connected through HaLow are not separate voting domains. A failed membership query does not mean a solo node.

The coordinator freezes participants and collects ACKs for 60 seconds. A strict majority allows commitment with activation 30 seconds later. Missing ACKs do not shrink the denominator. Static members only ACK compatible plans. Timed agreement waits for the initial verified clock synchronization.

The membership is assembled from reachable BATMAN originators mapped to
canonical node identities. Several paths to one radio count as one member.
A missing identity or failed query delays the decision. The lowest ready
ACS identity coordinates the round and scores the component's shared scan
observations. Receivers check whether they can follow the proposed band plan,
rather than independently choosing a new winner from their own observations.
That matters because two radios with different interference reports could
otherwise make different, individually reasonable channel choices.

The coordinator waits for enough fresh ready participants before scoring and
checks compatible voters afterward. Unknown and unready nodes stay in the
quorum denominator. Rejected rounds do not advance the preference count.
This reduces futile proposals without guaranteeing identical leadership views.

An authenticated destination is adopted even after a peer CSA has already
landed the radio. Adoption saves the config and moving-plan recovery hold once
per plan identity. A repeated advertisement or same-boot restart cannot renew
it, and a reboot ends it.

A searching radio connected to peers, over HaLow or any other path, with no
agreed plan in reach tunes each band to the channel most of those peers
already use, as published in their status. Equal groups keep the channel of
the group holding the lowest MAC. It never joins a DFS channel this way, and a
normal round then optimizes. Joining peers that are all still searching keeps
it searching with them. A connected searcher also repairs a radio that
drifted from its configured channel. Config repair alone never sends a second CSA. A committed ordinary plan
that lands first by peer CSA survives only on the same boot, at its exact
target and within its activation grace. DFS still needs live detector tokens.
Ordinary and DFS transition logs include quorum, readiness, hold time, and an
explicit expiry reason. Adoption logs capture old/new live and configured MHz.

The coordinator keeps a per-band count of consecutive preference wins against
the incumbent (any challenger counts) and moves on the third. The count
starts over after a reboot or a coordinator change. Receivers still do not
re-score. A measured veto or overload can bypass the preference wait. Missing
home telemetry on an allowed data channel keeps that incumbent in use.

Each radio measures its home channel every three-minute cycle from survey and
station counter deltas. Windows shorter than 60 seconds are rejected.
Foreign airtime is busy time minus own transmit time minus summed peer
receive time. Overheard unicast between two other mesh nodes still counts as
foreign. Counter resets, channel or interface changes, and restarts start a new
baseline. Total busy time stays in telemetry, and foreign airtime scores the
incumbent.

A per-band trouble flag sets after two windows at or above 60% foreign or
-80 dBm noise, and clears after three windows below 50% and -85 dBm. The 60%
limit comes from a controlled interferer on a two-node 2.4 GHz mesh: measured
foreign airtime tracked the interferer's airtime linearly, and throughput lost
about 20% by 60% foreign, about 60% by 75%, and 70 to 75% above 80%.
Off-channel and on-channel readings of one channel agreed within a point, so
the mesh's own traffic is not counted as foreign. The noise limit is still
provisional, because the noise floor did not move under Wi-Fi interference.
The flag rides in the home channel report. A local flag, or a fresh one from
any member of the connected view (including over HaLow), opens candidate
scanning on that band. Reports from unreachable nodes cannot open it.

With the gate open, 2.4 GHz visits each candidate once for 250 ms on its own
channel and takes occupancy and noise from the survey counters, with no BSS
scan. 5 GHz uses the reserved third-chain observer when its ABI is present.
Otherwise it uses the main-radio scan and short-sample warmup behind the same
gate, with BSS counts on both incumbent and candidates. With no measured
alternative the channel holds without urgency. Once candidates arrive, a
disqualified incumbent can move at once, bypassing the three-round hold-down.
An allowed non-lobby incumbent missing home telemetry stays put until a valid
comparison arrives. There is no missing-data timeout that authorizes a move.
Lobby, tourguide, and discovery scans are not gated. See
[channel scoring](../../../docs/node-tools-internals.md#channel-scoring).

`manet_rx_policy.py` reconciles local trouble and hold ownership under the
observer lock. It reads the kernel's transition timestamp, preserves its
60 second reversal limit, and defers EBUSY/EAGAIN without cancelling another
owner. The default is native three-path receive operation. CM4 requests
`rx_transition_probe` transitions only when available. The transition interface
rejects AP/STA, scan/reset, non-HT20, and DFS main-channel operation. Rock 3A
and RPi5 are excluded from this release policy.
Static mode clears the local trouble request and retries release while still
respecting a live hold and the transition interval.

The schema-2 reader preserves kernel epochs across host restarts and checks
the receiver heartbeat against boot time. The matched release adds a loaded
driver capability and a token-bound transfer to main radar detection. Live
PHY flags must identify DFS channels outside a radar non-occupancy period,
and every connected participant must advertise support for the candidate.
The parser accepts both integer and decimal MHz output from `iw`. The
coordinator exports the common DFS set to the scorer, which admits only valid
measured occupancy for those channels. Noise or BSS counts alone cannot win.
One fresh connected observer report is sufficient to score the candidate.
Passive scoring selects a candidate and non-DFS runner-up.
An authenticated preparation intention coordinates background CAC on all
nodes. A completed CAC keeps its receiver live until the unanimous timed plan
executes by mesh CSA, with HANDLE_DFS supplied by the host.
The intention freezes both frequencies until completion or invalidation.
Fallback scoring does not reset the main preference counter. Missing peer
observations preserve an unexpired preparation but cannot authorize a move.
EBUSY on the shared capability/hold snapshot leaves ordinary ACS status
publication working, with no DFS clearance or capability grant for that pass.
Pending watcher rechecks and unexpired preparations wait for a fresh read.
A saved DFS promise keeps its original apply window and cannot use a stale
clearance lease.
Preparation and pending DFS plans refresh and read clearance every one-second
pass, with another authenticated read before deciding and at activation.
This leaves room for Alfred's ten-second synchronization inside the 15-second
lease. An otherwise matching expired peer observation can wait up to five
seconds for a fresh one, within the original apply window. That wait grants no
vote, commitment, or channel move. Local clearance loss and radar still cancel
immediately.

The MANET Wi-Fi supplicant requires patches 0007 and 0008. Patch 0007 filters background CAC
STARTED, FINISHED, and ABORTED for mesh interfaces before the foreground BSS
state machine can change state. Foreground CAC, AP operation, and all radar
reports still reach the supplicant. Patch 0008 marks radar channels unavailable
for an ACS-owned BSS without invoking hostapd's mesh channel picker or
restarting the BSS. A volatile per-BSS opt-in defaults off, so static and
unmanaged meshes retain native evacuation. External
mesh CSA also preserves the BSS when its foreground CAC cache is stale.
The kernel enforces NOP, and MANET owns the agreed fallback.
Capability requires a compatible running supplicant, a ready guardian, and
the paired kernel. A new file copied over an old process is insufficient.
The package builder rejects a missing or incompatible DFS binary. Explicit
non-DFS packages retain Debian fallback and cannot advertise DFS support.
The operating contract has its own same-boot record, independent of ordinary
agreements. A 2.4 GHz-only move cannot erase it. Only verified departure or
verified replacement retires it. Radar without a contract stops an externally
owned radio. The watcher checks the running BSS: explicit native ownership
retains native evacuation, while unknown ownership still requires fencing.
The guardian survives manager stops and config applies. Before replacing its
code, updates fence DFS interfaces and stop their supplicants. They resume
on the saved fallback only after the new guardian reports ready. A failed
installer leaves the interface stopped and the update pending.
The watcher subscribes to nl80211 and file
notifications with no polling timer. It requests evacuation from a matching
kernel radar notification before reading debugfs. A busy read requests one
fresh snapshot from the agreement daemon, and a file event resumes delivery.

The main RDD starts before the background detector is retired. cfg80211 keeps
the channel AVAILABLE and preserves the token for later `main-radar` reports.
The independent watcher issues a block-TX CSA to the agreed fallback before
publishing over Alfred. Peers follow the first arriving CSA or authenticated
report. A follower already switching to the agreed fallback keeps its BSS
when the kernel confirms that the matching switch has blocked transmit queues.
The watcher binds that evidence to the current boot, contract, and interface.
It retains the original deadline across daemon restarts and repeated events,
and verifies the live channel before completing recovery. Missing evidence
still requires fencing. A historical radar latch is superseded only by a
newer live receiver on the same frequency with an exact local contract binding.
Fresh radar notifications still fence immediately. The daemon allows ten seconds for an accepted three-beacon CSA with
TX blocked and preserves a peer's completed move. Only a failed or expired
CSA needs interface-down recovery and a full supplicant restart on the safe
configuration. Radar starts a 30-minute cooldown. Rescoring can refresh the fallback
in a new unanimous plan. Peer loss alone never authorizes a DFS exit.
The receiver stays reserved during DFS main operation because the
path-count transition rejects radar channels. It can return to three paths
after leaving DFS. See the [combined kernel series](../../../kernel/patches/manet-mt7916/README.md)
for source tracing, the transfer API, and the two- or three-node test
scripts.

Heavy local TX biases the observer's
correction upward, so candidate windows with at least 10% survey TX are
omitted. The limit keeps low-TX readings and bounds that error. Raw bins and survey TX
remain available for investigation. Uncertain samples do not become clean
channel scores. This affects candidate measurement, while the trouble flag
continues to use the separate long home survey. See the
[kernel series](../../../kernel/patches/mt76-bgobs/README.md).

| Stage | What becomes fixed | What stops progress |
|---|---|---|
| Discover and propose | Participant list, proposed channels, plan hash, and activation time | Incomplete membership or no ready coordinator |
| Vote | A node's vote binds the complete plan and its boot session; durable state prevents a different vote after a process restart | Unsupported channels, incompatible static settings, or a different membership view |
| Commit | After the 60-second deadline, the coordinator attests a strict majority of the original participants | Too few ACKs; a missing voter is still counted |
| Activate | Nodes apply at the agreed time, 30 seconds after that deadline | A late commit or failed radio application |

For three participants, two ACKs suffice. For two participants, both are
needed. A receiver must accept the commit before activation minus five
seconds and applies within a five-second grace window. Thus a packet that
arrives very late cannot trigger an arbitrarily late switch. A topology
change cannot simply erase a missing voter to make a stalled proposal pass.
The [agreement protocol](../../../docs/node-tools-internals.md#channel-agreement-and-recovery)
also describes round timing and membership expansion.

At application time the node serializes changes against local tourguide
work, reconfigures changed radios, and checks their actual frequencies. An
unchanged plan is not logged as applied.
A service reporting that it is running does not prove that the radio landed
on the selected channel. Failed landings use bounded repair attempts. The
commit itself is an authenticated coordinator statement, not independent
cryptographic signatures from every voter. Nodes sharing the admin password
are within the same administrative trust domain.

Encrypted Alfred type 74 carries agreement and current-plan recovery. Type 75 carries authenticated rendezvous helpers. Types 76 and 77 provide nonce-bound cold recovery before a node can validate wall-clock freshness. Cold recovery does not set the clock or enable scheduled elections.

### Recovery and local role constraints

A working HaLow interface suppresses Wi-Fi tourguide excursions. Without HaLow, the shared rendezvous schedule rotates conventional Wi-Fi bands and anchors. Tourguide and channel application use a shared local lock. An AP-owning gateway retains the agreed mesh configuration for the AP candidate's later return to mesh mode.

Channel moves are coordinated, but unreliable delivery can leave stragglers. Recovery and reconciliation are part of the protocol. The design is not an atomic switch across every disconnected radio.

For example, a node may miss the final commit but remain reachable through
HaLow. A reachable peer that is actually operating the committed plan can
advertise fresh authenticated evidence of that destination, allowing the
straggler to join it. An old cached announcement alone is insufficient.
If separate islands made incompatible plans and then reconnect, neither the
newest timestamp nor an arbitrary plan identifier chooses the winner: the
joined component runs a shared agreement round.

A cold node cannot use ordinary timestamp freshness before its clock is
trusted. Instead it sends a random challenge tied to its boot. A reply must
answer that challenge within its local lifetime. That authorizes the narrow
recovery operation without making the reply a time source or granting
permission to apply configuration changes. Without any surviving link,
rendezvous visits create opportunities for this exchange. They cannot repair
physical lack of radio coverage. See
[recovery across radios](../../../docs/node-tools-internals.md#recovery-and-reconciliation-across-all-radios)
and [cold-node recovery](../../../docs/node-tools-internals.md#clock-readiness-and-cold-node-recovery).

A built-in Wi-Fi radio reserved as an internet client is excluded from ACS,
channel agreement, and drift repair. Its remaining radios still participate in
the same component's decisions. A shared 5 GHz EUD AP follows the upstream's
channel locally. See [Wi-Fi uplinks](internet.md#user-use-nearby-wi-fi).

### Choosing an internet gateway

An internet path is limited by both the mesh route to a gateway and that
gateway's uplink. The route manager scores each candidate using the lower
of its BATMAN path throughput and advertised download bandwidth. A path
capable of 40 Mbit/s leading to a 5 Mbit/s uplink therefore scores 5 Mbit/s.
A 12 Mbit/s path to a 50 Mbit/s uplink scores 12 Mbit/s.

The node installs an IP default route through the selected gateway's mesh
address. BATMAN's Layer 2 gateway selection alone does not install that
route. To avoid repeatedly breaking internet sessions, a healthy incumbent
is replaced only when a challenger is at least 1.5 times faster and
2 Mbit/s better for 60 seconds, outside a five-minute switch hold. Loss of
the current gateway or two failed reachability checks permits failover.
[Gateway selection](../../../docs/node-tools-internals.md#gateway-choice-and-uplink-speed)
explains measurement, restart behavior, and retry timing.

### Shared services and control

Service elections rank eligible, fresh candidates by connectivity, use deterministic ties and incumbent preference, and reconcile after partitions rejoin. Administrative configuration is staged and acknowledged over encrypted Alfred messages, with scheduled activation and local rollback protection for dangerous changes. AP credentials stay local. HTTP is not the peer control transport.

- [Agreement and recovery details](../../../MANET/node_tools/README.md#channel-selection--jamming-detection)
- [Rendezvous schedule](../../../MANET/node_tools/README.md#discovery--partition-healing)
- [Protocol internals and failure cases](../../../docs/node-tools-internals.md#elections-channels-and-healing)
- [Mesh configuration and rollback](../../../MANET/node_tools/README.md#mesh-configuration-push)
