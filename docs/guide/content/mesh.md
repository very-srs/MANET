# How the mesh works

## Marine

![Crayon drawing of four radios with an orange route crossed out at a failed link and a purple alternative route through another radio.](../assets/mesh-crayon.png)

### Pass it along

Radios pass traffic through other radios. If a link breaks, another available path can carry it.

- **Wi-Fi:** faster links nearby.
- **HaLow:** longer reach, lower speed.
- **No path at all?** Move back into range or restore a working link.

The radio can find another route. It cannot invent one across a gap with no usable signal.

Radios keep listening to the channel they are using. If interference stays, they look for a better one.

The extra 5 GHz listening mode is still being tested. Leave it off.

A radio borrowing house Wi-Fi has fewer radios left to talk to its friends.

## User

### More than a direct connection

Two radios do not need to hear each other directly if other mesh radios provide a usable path between them. MANET uses the available links to carry traffic across those intermediate nodes.

Wi-Fi and HaLow can connect the same group of nodes. Wi-Fi favors higher throughput; HaLow provides another path at lower speed and typically longer reach. Neither promises a fixed distance through every building or landscape.

### Choosing channels

With automatic channel selection enabled, radios share observations and agree on a Wi-Fi channel plan. Radios still connected through HaLow belong to the same connected mesh, even when their Wi-Fi links are temporarily disconnected.

Each radio measures the channel it is on. It only looks at other channels when it, or another radio in the connected mesh, reports lasting interference: the channel busy with other people's traffic at least 60% of the time, which is where mesh speed starts to drop. A healthy band never leaves its channel to look around. After a restart the first measurement takes a full listening window, about three minutes, so a restart alone never moves the channel.

A healthy channel also stays put through short load swings. A better channel has to win three three-minute rounds in a row before the mesh moves. A channel that is blocked, unmeasured or overloaded is left at once. A failed or expired scan is left out instead of being shared as new data, and when the connected mesh has no valid measurements for a band, that band keeps its current channel. Recovery visits that look for separated radios still happen.

Time synchronization is needed for scheduled channel decisions. A cold radio can recover contact before its clock is synchronized, but timed decisions wait until GPS or network time has been verified.

Keep the optional background receiver disabled. Earlier testing found that
enabling it stopped the main 5 GHz link from connecting. A kernel fix is
ready for further testing, but normal traffic and background listening must
work together before this option is ready for use. The default remains off.

Using a built-in radio as an internet connection removes that band from the
mesh on this node. The remaining radios keep their mesh roles. See
[Wi-Fi uplinks](internet.md#user-use-nearby-wi-fi).

### Rejoining after separation

HaLow provides a recovery path when it is working. Where HaLow is unavailable, selected radios visit rendezvous channels to help separated groups meet again. Recovery takes time and still requires a usable radio path.

If a node has internet access, it can become a gateway. Nodes can choose among available gateways using the mesh path and uplink performance. Shared services also elect hosts and can move when a host disappears.

Check the status dashboard for actual peers, routes, and link information. A completed setup or a powered-on radio does not guarantee a connected mesh.

## Developer

### Routing and control use different mechanisms

BATMAN V routes at Layer 2 over the attached mesh interfaces. Alfred replicates identity, telemetry, and control records; the local registry is a view derived from that data. Identity uses type 67 and volatile telemetry type 68. Reachable BATMAN membership must be distinguished from stale registry entries.

`node-manager-acs.sh` schedules home-channel RF measurement, gated candidate scans, coordination requests, tourguide work, and service elections while maintaining status and addressing. A scan only counts if it completed. A channel visited for less than 25 ms is left out, 5 GHz busy time needs three readings, and observations older than three minutes are not published. The static manager retains the status, address, and service work without automatic Wi-Fi channel selection. `node-manager-select.sh` chooses the runtime symlink from `acs=` before service start.

### One connected component, one channel decision

`mesh-channel-agreement.py` coordinates a proposal for the connected BATMAN component across **all** radios. Wi-Fi groups connected through HaLow are not separate voting domains. A failed membership query does not mean a solo node.

The coordinator freezes participants and collects ACKs for 60 seconds. A strict majority allows commitment with activation 30 seconds later. Missing ACKs do not shrink the denominator. Static members only ACK compatible plans. Timed agreement waits for the initial verified clock synchronization.

The membership is assembled from reachable BATMAN originators mapped to
canonical node identities. Several paths to one radio count as one member;
a missing identity or failed query delays the decision. The lowest ready
ACS identity coordinates the round and scores the component's shared scan
observations. Receivers check whether they can follow the proposed band plan,
rather than independently choosing a new winner from their own observations.
That matters because two radios with different interference reports could
otherwise make different, individually reasonable channel choices.

The coordinator keeps a per-band count of consecutive preference wins against
the incumbent (any challenger counts) and moves on the third. The count
starts over after a reboot or a coordinator change. Receivers still do not
re-score. A vetoed, unmeasured or overloaded incumbent is not held.

Each radio measures its home channel every three-minute cycle from survey and
station counter deltas. Windows shorter than 60 seconds are rejected.
Foreign airtime is busy time minus own transmit time minus summed peer
receive time. Overheard unicast between two other mesh nodes still counts as
foreign. Counter resets, channel or interface changes and restarts start a new
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
scan. 5 GHz keeps its main-radio scan and short-sample warmup behind the same
gate, with BSS counts on both incumbent and candidates. With no measured
alternative the channel holds without urgency. Once candidates arrive, a
disqualified incumbent can move at once, bypassing the three-round hold-down.
Lobby, tourguide and discovery scans are not gated. See
[channel scoring](../../../docs/node-tools-internals.md#channel-scoring).

The optional MT7916 third-chain observer remains a bench feature with
`mt7916_background_rx=n` by default. The original reservation broke main-link
peering on two Rock 3A nodes. The kernel fix finalizes the band-1 EEPROM
selection before firmware upload and aligns the legacy OFDM path count with
2T2R operation. Host tests verify the uploaded bytes, but simultaneous main
traffic and observer response still need RF acceptance. Runtime chain policy
and DFS activation remain unimplemented. See the
[kernel series](../../../kernel/patches/mt76-bgobs/README.md).

| Stage | What becomes fixed | What stops progress |
|---|---|---|
| Discover and propose | Participant list, proposed channels, plan hash, and activation time | Incomplete membership or no ready coordinator |
| Vote | A node's vote binds the complete plan and its boot session; durable state prevents a different vote after a process restart | Unsupported channels, incompatible static settings, or a different membership view |
| Commit | After the 60-second deadline, the coordinator attests a strict majority of the original participants | Too few ACKs; a missing voter is still counted |
| Activate | Nodes apply at the agreed time, 30 seconds after that deadline | A late commit or failed radio application |

For three participants, two ACKs suffice; for two participants, both are
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
cryptographic signatures from every voter; nodes sharing the admin password
are within the same administrative trust domain.

Encrypted Alfred type 74 carries agreement and current-plan recovery. Type 75 carries authenticated rendezvous helpers. Types 76 and 77 provide nonce-bound cold recovery before a node can validate wall-clock freshness. Cold recovery does not set the clock or enable scheduled elections.

### Recovery and local role constraints

A working HaLow interface suppresses Wi-Fi tourguide excursions. Without HaLow, the shared rendezvous schedule rotates conventional Wi-Fi bands and anchors. Tourguide and channel application use a shared local lock. An AP-owning gateway retains the agreed mesh configuration for the AP candidate's later return to mesh mode.

Channel moves are coordinated, but unreliable delivery can leave stragglers. Recovery and reconciliation are part of the protocol; the design is not an atomic switch across every disconnected radio.

For example, a node may miss the final commit but remain reachable through
HaLow. A reachable peer that is actually operating the committed plan can
advertise fresh authenticated evidence of that destination, allowing the
straggler to join it. An old cached announcement alone is insufficient.
If separate islands made incompatible plans and then reconnect, neither the
newest timestamp nor an arbitrary plan identifier chooses the winner: the
joined component runs a shared agreement round.

A cold node cannot use ordinary timestamp freshness before its clock is
trusted. Instead it sends a random challenge tied to its boot; a reply must
answer that challenge within its local lifetime. That authorizes the narrow
recovery operation without making the reply a time source or granting
permission to apply configuration changes. Without any surviving link,
rendezvous visits create opportunities for this exchange; they cannot repair
physical lack of radio coverage. See
[recovery across radios](../../../docs/node-tools-internals.md#recovery-and-reconciliation-across-all-radios)
and [cold-node recovery](../../../docs/node-tools-internals.md#clock-readiness-and-cold-node-recovery).

A built-in Wi-Fi radio reserved as an internet client is excluded from ACS,
channel agreement and drift repair. Its remaining radios still participate in
the same component's decisions. A shared 5 GHz EUD AP follows the upstream's
channel locally. See [Wi-Fi uplinks](internet.md#user-use-nearby-wi-fi).

### Choosing an internet gateway

An internet path is limited by both the mesh route to a gateway and that
gateway's uplink. The route manager scores each candidate using the lower
of its BATMAN path throughput and advertised download bandwidth. A path
capable of 40 Mbit/s leading to a 5 Mbit/s uplink therefore scores 5 Mbit/s;
a 12 Mbit/s path to a 50 Mbit/s uplink scores 12 Mbit/s.

The node installs an IP default route through the selected gateway's mesh
address. BATMAN's Layer 2 gateway selection alone does not install that
route. To avoid repeatedly breaking internet sessions, a healthy incumbent
is replaced only when a challenger is at least 1.5 times faster and
2 Mbit/s better for 60 seconds, outside a five-minute switch hold. Loss of
the current gateway or two failed reachability checks permits failover.
[Gateway selection](../../../docs/node-tools-internals.md#gateway-choice-and-uplink-speed)
explains measurement, restart behavior, and retry timing.

### Shared services and control

Service elections rank eligible, fresh candidates by connectivity, use deterministic ties and incumbent preference, and reconcile after partitions rejoin. Administrative configuration is staged and acknowledged over encrypted Alfred messages, with scheduled activation and local rollback protection for dangerous changes. AP credentials stay local; HTTP is not the peer control transport.

- [Agreement and recovery details](../../../MANET/node_tools/README.md#channel-selection--jamming-detection)
- [Rendezvous schedule](../../../MANET/node_tools/README.md#discovery--partition-healing)
- [Protocol internals and failure cases](../../../docs/node-tools-internals.md#elections-channels-and-healing)
- [Mesh configuration and rollback](../../../MANET/node_tools/README.md#mesh-configuration-push)
