# Location and ATAK

## Marine

![Crayon drawing of a phone cabled through an adapter to a radio, with a GPS receiver, satellite and a location dot on the phone.](../assets/location-crayon.png)

### Show the phone where you are

Plug your ATAK phone into the radio with its internet hose or join that
radio's Wi-Fi. Stay near the radio. Count to twenty, or, if you can't do that, just
be patient. A GPS signal has to come from outer space.

Several phones? (Several means more than one.) Only one of them can set the group's
location. Open [manet.local](http://manet.local/)
and look under **ATAK PHONES**. Choose a phone there to be in charge.

If the radio has GPS, turn **Android location OFF** in your phone settings. 
In ATAK, turn **external GPS input ON, UDP 4349**. Leave **Default Mesh SA ON**. Your own map and the
team's maps then use the radio's position. Follow the
[phone setup steps](location.md#user-set-up-your-atak-phone).
No good position? You should drag your location marker so that it matches where you
are on the map anyway.

No GPS receiver in the radio? Leave phone location on in that case.

Without ATAK on a connected phone, the radio will still tell your ATAK buddies where
you are.  The team sees the radio's name on the map, but it is just holding that location for a friend. 
When a phone takes over, its dot replaces the radio's marker.

## User

### Set up your ATAK phone

ATAK is a mapping application that can receive information from the radio.
Use a **wired Ethernet connection** or this radio's enabled **Wi-Fi access point**.
Several nearby phones can use one radio, although one EUD per radio is recommended.
Set up the phone to use the radio's position on its own map and every output,
including mesh sharing, direct sharing and a TAK server. Stay near the radio
and coordinate who sets the group's location if needed.

1. Connect the phone through a compatible Ethernet adapter to the radio's
   Ethernet connection, or join the radio's own Wi-Fi network.
2. If the radio has a GPS receiver, turn **Android location OFF** and enable
   ATAK's **external GPS input on UDP 4349**. This makes the radio's GPS your
   phone's position on every output. If the radio has **no GPS receiver**, keep
   **Android location ON** instead. The controlling phone's GPS then supplies
   the team's position.
3. Leave ATAK's built-in **Default Mesh SA** output enabled so the radio can
   discover your phone automatically. No per-radio output entry or IP address
   is needed. Leave the standard CoT input enabled for contacts and chat.
   CoT is the message format ATAK uses.
4. Look for the contact **MANET mesh-XXXX**, using the radio's actual hostname.
   If there is a GPS problem, mark a point on the map and send it to this
   contact with ATAK's Send action. Other ATAK users then see you at that point.

The contact itself is hidden from the map. A separate external-position
feed supplies location, so absence of a contact marker does not prove that
the contact is missing. A custom `atak_callsign` can change its name.

### How the group sees you

With the setup above, ATAK uses the radio position for mesh, direct and
TAK-server sharing. If you forget the settings on a multicast-only network,
the radio has a safety net. It replaces positions in admitted **Default Mesh SA**
reports (239.2.3.1, UDP 6969) toward other radios with its trusted GPS or held
manual point. Your own map and phones on the same radio still get your original
position. This safety net does not replace the setup steps.

A phone seen sending TCP traffic to a TAK server on port 8089 or 8087 is labelled
**TAK server** in **ATAK PHONES**. The kernel excludes its mesh reports from the
native block for ten minutes after its last observed traffic. The panel and
replacement sender learn that change when a report needs replacement or status
changes, reusing a result for up to five seconds. A report already in flight can still carry the previous rewrite
around that transition. Otherwise teammates receiving both streams
could see its dot jump between two positions. TLS is encrypted and the radio
cannot rewrite it. A server on the local EUD side also counts if its traffic
passes through the radio. Nonstandard server ports and paths the radio cannot
observe need the same phone setup.

A radio without a GPS receiver gives no missing-GPS prompt. Without a held
manual point or trusted radio fix, your phone shares its own GPS normally.
The controlling phone's fresh GPS also supplies the radio's status-page position,
labelled **phone GPS (callsign)**. It is never sent back as external GPS. If the
phone disconnects, a radio marker can use that fix only for the remaining life
of its report, at most 75 seconds from receipt. An expired phone position is
withdrawn, so an unnoticed departure may leave no fresh fix for a radio marker.

A radio with nobody attached shares its own position with the team's maps,
under its hostname, such as **mesh-5cc2**, or its configured callsign. It uses a
radio/signal symbol and offers no chat or Send endpoint. When a phone joins,
the radio requests removal of that marker so the phone's own dot takes over.
After the last phone's presence expires, at most 75 seconds after its last
report, the radio marker returns if a usable position remains. It refreshes
about every 30 seconds and sooner after moving five metres. Suspect GPS is
never advertised. The radio uses its held manual point or a still-fresh phone
GPS fallback instead, and stops when neither is available. Lost messages can delay a map change.

### Choose who sets the group's location

When more than one EUD is sharing a radio, open **[manet.local](http://manet.local/)**
and expand the entry for your radio. The **ATAK PHONES**
panel shows each present phone's callsign, **Wired** or **Wi-Fi**, and which one
**Controls location**. Tap **Set location from this phone** beside another phone
if you want it to be in control. No admin password is required. Wait for the list to
refresh and show the new controller.

Your saved choice wins while that phone is present. Otherwise, the only wired
phone wins. If there is no wired phone and the currently chosen controller has
left, the radio randomly chooses a present phone and
keeps it until changed. A phone can remain present for up to 75 seconds after its last position
report. Your saved preference survives a reboot or departure and takes priority
when that phone returns.

Only the controller's sent points, position drags, and manual location selections can set
the radio's manual location. That phone also supplies fallback GPS. Switching
controllers does not erase a held manual point.
Other phones receive the position and GPS warnings. Sending a point from a guest
phone produces a chat naming the controller and explaining how to change it.
When another phone joins or control changes, every present phone gets a chat
naming the controller. Agree with the group before changing it.

### Understand the position you are using

A connected GPS receiver still needs a fresh fix. Indoors, blocked sky, or
receiver trouble can leave it without one. A manual point is a supplied
location, not a fresh satellite measurement. Check its age and the displayed
source before treating it as your current position. A present receiver without
a fix raises a warning after three seconds. Unread warnings repeat about once
a minute, never more often than every 30 seconds. A receiver plugged in later
starts this normal handling. With suspect GPS, a held manual point also replaces
phone positions shared toward the mesh.

Without trusted radio GPS, a point you set corrects a small offset while you stay
nearby. The radio remembers the controlling phone's GPS position when it accepts
the point, or the first fresh phone fix afterward. Staying within about 30 metres
of that starting position keeps the point indefinitely. Fresh phone reports more
than 30 metres away for at least 15 seconds release it. Going more than 200 metres
from the held point releases it immediately. These allowances reduce reactions
to ordinary GPS wandering and single jumps. No fresh phone GPS means the point
stays. On radios without GPS, the phone's track decides whether you moved, without
a spoof monitor to check it. Trusted radio GPS keeps the existing point
confirmation and override behavior.

To release it yourself, use the controlling phone to open this radio's
**ATAK PHONES** panel and tap **Use phone GPS again** beside the held point.
No admin password is needed. Guest phones cannot release it. The radio sends one
quiet chat, stops using the point and asks ATAK to remove the sent marker.
Release itself has no spoken alert. Receiver warnings still work normally.
Deleting the marker in ATAK does not release the point on the radio.

When the radio accepts a point it will hold, it sends one chat explaining how
to release it. This also applies to a held USER location or a drag of your own
position. Open **manet.local in your browser** to use the button, or move on with
fresh phone GPS as described above. The address in ATAK chat is plain text,
not a tappable link. While a sent point is held and the controlling phone is
present, the radio quietly sends the same marker back every minute.
An existing marker keeps its contents, and a locally deleted marker can reappear.
Refreshes stop on release or marker retirement. USER/drag selections receive
the explanation chat without creating a separate map marker.

The radio supplies the external position input after discovering your
phone. If nothing appears on your own map, check
the cable or local Wi-Fi connection, the phone's address and the ATAK settings before
assuming GPS is broken. A healthy mesh between radios does not establish
the phone's local connection.

If a phone still reports its own GPS while the radio has trusted GPS, a separation
over 50 metres shows **radio and phone GPS disagree by N m** beside that phone.
The radio sends one quiet chat per episode, with no spoken alert or repeated chat.
Check your location and setup. The message does not decide which fix is right or
change trust. A new episode can start after the two fresh fixes stay within
35 metres for 30 seconds. With Android location off there is no phone GPS to compare.

The project also contains experimental radio-ranging and position-estimation
work. It is disabled by default and is not a ready replacement for GPS.
Do not assume that seeing neighboring radios means MANET can determine your
position from them. The radio's [clock synchronization](time.md) is a
separate function.

## Developer

### Admission is a local network proof

The supported setup for a radio with GPS is Android location off, ATAK external
GPS input on UDP 4349, and Default Mesh SA left on. ATAK then uses the radio fix
for multicast, unicast and TLS outputs. A radio without GPS hardware instead
needs Android location on, using its controller's phone GPS as the team position.
Mesh rewriting is only a safety net for forgotten settings on multicast-only
networks. TLS cannot be rewritten by the bridge.

The ATAK daemon is enabled by default. An absent/empty `atak` setting means
enabled, while `n`, `no`, `0` or `false` disables it. Phone admission requires
an independently verified local EUD port: wired `end0`, or the active AP named
by `/var/lib/ap_interface`. The AP requires live hostapd identity, matching
configuration, carrier and forwarding br0 membership. Routed Ethernet does not
prevent AP admission. A resolved br0 neighbor's MAC must be learned exclusively
on one admitted local port, never also bat0. Each new TCP stream or UDP peer
requires a fresh proof. Default Mesh SA arrives through a packet socket on
each admitted local EUD port, with a kernel BPF filter for IPv4 UDP
`239.2.3.1:6969` only. No socket joins that multicast group or receives on bat0.
The frame's source MAC must be proven on its actual ingress port before its
SA payload reaches the decoder. The contact endpoint must name its IPv4 source.

The bounded mesh decoder accepts legacy XML and TAK v1's `bf 01 bf` header
followed by protobuf SA. Both produce the same `PhoneSA`, including `how`,
`geopointsrc`, `altsrc` and parent links. Other detail and phone numbers are
discarded from retained phone state. A separate bounded rewrite preserves the
original packet's other fields. Replies remain unicast XML on 4242/4349.
Existing unicast XML input
on 4242 also works on every global br0 IPv4 address, including the local gateway
address used by `manet.local`. The advertised contact stays on the primary.

The bridge firewall allows only the configured local interface names for service
traffic on 4242/4349. Strict readback rejects altered rules. AP renaming requires
an ATAK service restart to replace the allow-list. AP role/config metadata and
hostapd PID/start identity invalidate cached proof without subprocess polling.
SA leases, clocks and UID/IP/MAC/attachment records are bounded by the provisioned
`max_euds_per_node` (0–253, zero meaning one wired-primary phone). Missing/invalid
values log and use eight. Absent rows can be evicted when the table is full.

The daemon subscribes to route/link/address/neighbor/FDB and nftables events
before its initial snapshot. Unchanged ticks reuse a cached proof, avoiding
repeated `ip`, `bridge` and `nft` subprocesses. Events during a snapshot
invalidate it. Subscription failure, overflow or truncation ends the process
so systemd can establish fresh subscriptions. The kernel policy remains
the packet boundary: userspace notifications do not make an arbitrary
external firewall flush atomic with I/O.

### Inputs, output and retained state

GPS data comes through gpsd and the local reader, with per-receiver epochs
and freshness. Replayed epochs cannot keep a fix alive merely by arriving
again. The GNSS reader tracks signal/time anomalies as hints. These are
not proof of spoofing. In particular, an unavailable receiver diagnostic
must stay unknown rather than become a clean bill of health.

The ATAK selection logic combines admitted-phone state, fresh location
inputs, manual points and available monitoring information. Phone presence,
phone clock information, manual controller preference and radio manual-location
state persist under `/var/lib/manet-atak`. Schema 2 explicitly discards old
schema-1 single-pin state. Checksum failures and unknown schemas still stop
startup. The daemon never sets the OS clock. Every present phone gets contact
and radio-selected external position in its own clock domain. Phone GPS fallback
is excluded from that external feed. Only controller gestures and
read receipts can acknowledge the radio's warnings. Guest receipts cannot.

Controller preference is manual-if-present, sole wired, current-if-present,
then stable random. A boot/session-bound JSON producer file carries the local
web choice to the existing event cycle, which rechecks the selected phone's
presence and path. The overview exposes only ATAK enable/running state and
phone UID, callsign, port, controller status, TAK-server marker, GPS disagreement
and selected position/source with independently checked freshness deadlines. Session identity and the full
runtime snapshot stay server-side. The HTTP POST requires JSON, rejects foreign
origins and checks this radio's DHCP source pool. The port-80 `manet_ui` firewall enforces
that same local pool plus loopback. It assumes a trusted team network rather
than proving physical HTTP ingress. The held location and distrust survive
controller switches, and marker deletions still target the original phone.
A stock phone on a CM4's AP with only Default Mesh SA enabled is admitted and
receives the contact and external GPS feed.

The public overview also carries an opaque held-point ID and controller UID for
the local **Use phone GPS again** button. Its JSON POST additionally requires the
request's source IP to match the current controller. The daemon consumes
`release.json` on the existing cycle and rechecks the boot, session, five-second
BOOTTIME envelope, point ID and current controller UID/IP/MAC/path. Only a held
point selected without trusted own GPS can be released this way. The HTTP
source-address trust boundary is the same local team network as controller choice.

For admitted phones, a bridge forward rule blocks unfragmented default mesh SA
only from the verified EUD interface/IP/MAC toward bat0 while rewriting is active.
It excludes source MACs in `tak_phones` in the kernel, even while the daemon is
slow or paused. Rewrite memberships retain their three-second timeout and
one-second renewal. A stopped or crashed daemon cannot renew them, and
ExecStopPost removes the table. Unadmitted senders, local EUD-to-EUD copies,
TAK-server traffic and unicast outputs pass natively. The first discovery report
also passes natively.

Bridge prerouting rules on the configured local EUD ports update the dynamic
`tak_phones` Ethernet-source set for IPv4 or IPv6 TCP destination 8087 or 8089.
Each packet, including established ACKs, refreshes its 600-second kernel timeout.
TCP never reaches the userspace packet sockets. A same-EUD-side server counts
when its traffic crosses the bridge. Detection can precede phone admission and
is bounded to 1024 MACs. Visible TCP ports are required, so encrypted tunnels,
nonstandard ports and traffic switched elsewhere are outside this detector.
Unit restart recreates the set, while a paused daemon does not stop its updates.

Before replacing an admitted SA frame, or on meaningful status changes, the
daemon requests `NFT_MSG_GETSETELEM` on its existing netlink socket and reuses
a completed snapshot for up to five seconds. Idle ticks issue no requests,
including with a present phone. Receipt timestamps and age counters do not create demand.
Replies are bounded to 1024 MACs and 128 KiB, and become authoritative only after
successful multipart completion. New rewrite membership requires fresh data.
Existing leases may renew with stale data because the kernel checks its own
TAK exclusion on every frame. A replacement always requires a fresh snapshot.
One admitted frame per EUD port can wait for the reply, with that capture reader
paused to bound retained bytes. Expiry of that SA produces original forwarding.
A read still outstanding at two seconds stops
the daemon. TCP traffic causes no read or wakeup. Set writes wait for a pending dump to finish so the
daemon does not invalidate its own multipart read. Its own rewrite notifications
are identified by the kernel sender, multicast group, originating socket port
and bounded rewrite-sequence or set identity checks, so renewals keep cached
proof. External or unrecognized notifications still require policy readback.

The kernel exemption and userspace snapshot are asynchronous. If a frame is
captured just before a MAC enters `tak_phones`, a snapshot that already contains
the MAC makes the daemon forward the captured original. The native copy might
already have been blocked, or it might have passed, producing a duplicate.
With the preceding snapshot, a replacement may still be sent around the
transition. The next read removes that MAC from rewriting. Retiring originals
continue until the three-second lease window ends and the queue drains. These
reads do not guarantee an atomic handoff. The documented phone settings provide
one position inside ATAK, including TLS, regardless of that race.

The packet tap replaces lat/lon/HAE/CE/LE and precisionlocation source attributes
with the selected trusted radio GPS or held manual point. Every other protobuf
field retains its encoded bytes, including unknown fields. XML changes only the
relevant attribute values. Source tags use `MANET:<radio name>`. An output-only
packet socket sends on bat0, retaining Ethernet/IP source, UDP ports and other
headers with updated lengths and checksums. Bounded undecodable or unsupported
packets from blocked senders are forwarded unchanged. Budget exhaustion or capture
queue loss suspends the block for three seconds. Capture loss cannot lose
TAK-server detection, since that now runs in the kernel. Failure to send even
the original stops the service and lets the leases expire. This work adds no process or polling loop.

A fresh reader report with no devices suppresses and clears `gps_untrusted`,
including chat and audio. A missing or invalid reader report remains unknown.
Present receivers retain the three-second no-fix debounce and immediate distrust
handling. Reminder deadlines use 60 seconds with an explicit 30-second floor.
The controller's admitted `geopointsrc=GPS` SA can supply fallback geometry only
when no radio fix or held manual point is selected. It cannot supply GNSS trust,
time authority, mesh rewrites or 4349 output. The cache is ephemeral and expires
at the earlier of SA staleness and the 75-second receipt-age limit. If the phone's
path disappears first, the beacon may use the remaining fresh interval, with its
stale field shortened to that same deadline. It never extends the phone fix.

Without selected trusted own GPS, `MANUAL_MOVE_M=30`, `MANUAL_MOVE_S=15` and
`MANUAL_MAX_OFFSET_M=200` bound held-point release using only the controller's
admitted, fresh own GPS. Acceptance saves that GPS anchor, or leaves it pending
until the first fresh fix. Reports within 30 m hold the correction indefinitely.
Distinct fresh reports outside 30 m must span at least 15 seconds continuously.
A return inside, stale GPS, lost controller proof or trusted own GPS resets the
interval. Any fresh report over 200 m from the held geometry releases immediately,
including the first fix after acceptance. These are operational allowances for
phone noise and small manual corrections, not accuracy or spoof guarantees.
The point identity and anchor persist, but movement timing and phone GPS do not.
A controller change resets the anchor and interval. Trusted-own-GPS mark,
override and confirmation rules are unchanged.

Release first reserves normal marker retirement to the original owner. Queue
backpressure keeps the held point. Success clears geometry and rewrite authority
before sending one best-effort controller GeoChat and a journal entry. It adds
no audio or retry job and does not change trust. The triggering SA can therefore
pass with its own GPS instead of being rewritten to the released point.
ATAK handles incoming `t-x-d-d` deletions, but deleting a normally sent marker
on the phone sends nothing back to the radio on 4242, so it cannot release the
radio's held point.
Receive-driven release remains unimplemented.

Newly accepted held selections produce one best-effort GeoChat after selection,
using plain browser instructions. Restart and movement reanchoring do not
regenerate it. For a Send-created point, one bounded checkpoint record preserves
the accepted XML exactly, including UID, timestamps, labels and unknown detail.
`HELD_MARKER_REFRESH_S=60` schedules quiet unicast replay to the present controller
on the existing cycle. A failed send also waits five minutes. Same-boot restart
retains the deadline, while reboot starts a new five-minute interval. Earlier
checkpoints without the original XML cannot reconstruct its full content, so a
new Send is needed to enable refresh. USER/drag selections have no separate
marker payload. Replacement, release, GPS-confirmation retirement and pending
retirement all suppress the old refresh before output. Retirement still targets
the original marker owner.

Disagreement compares each admitted fresh `geopointsrc=GPS` observation with the
selector's trusted own GNSS fix, never its manual or phone fallback. The initial
50 m threshold assumes 10 m of horizontal error for each GPS plus 30 m of local
AP separation. These are operating allowances, not measured error guarantees or
a maximum Wi-Fi range. One journal line and one best-effort GeoChat handoff mark
an episode. There is no audio, retry or trust mutation. Rearming requires fresh
fixes within 35 m continuously for 30 seconds. Missing proof, stale fixes or the
35–50 m band interrupt that timer. Episodes are bounded by the phone table and
ephemeral, so restart begins a new observation history. Agreement cannot restore
trust or clear distrust, since a spoofer could move both fixes together.

An unattended radio uses the same position selector and publishes protocol-0
XML to `239.2.3.1:6969` through br0. Its UID is `<radio-uid>-beacon`, distinct
from the hidden contact, with hostname or `atak_callsign` as its label.
CoT type `a-f-G-U-U-S-R` selects the standard radio/signal symbol, with no team,
role or contact endpoint. Trusted receiver GPS or the held manual fallback
can supply geometry, as can the bounded phone GPS fallback described above.
No selected fix means no position announcement, including
no 0,0 placeholder. Refresh is 30 seconds with 90-second staleness. Five metres
of movement can trigger earlier output, bounded to one attempt per five seconds.
These deadlines use the existing event cycle.

A live local phone lease suppresses the radio-GPS/manual beacon, including during
temporary proof loss. A phone-GPS beacon instead requires no currently proven
local phone and a still-fresh observation. Arrival sends a `t-x-d-d` task with
`__forcedelete` for only the
beacon UID. The public ATAK importer removes that item immediately unless the
receiver disables remote deletion. Three successful UDP handoffs are attempted,
at two-second spacing and at most five attempts. Delivery is unacknowledged.
Staleness gives a fallback whose removal timing depends on receiver settings.
Departure resumes announcements when the final lease ends. A separate br0
multicast socket disables loopback and fragmentation. The bridge output rule
drops locally originated mesh SA on every port except bat0. The separate forward
rule covers the admitted phone rewrites described above. Readback proves policy
and bat0 readiness before
sending. Outgoing packet filtering, local-source exclusion and the person-SA
classifier prevent admission of the radio's own beacon.

Before the first association, the application reads receiver and monitoring
inputs for the relay beacon. Changed inputs, freshness deadlines and due beacon
work run selection, while unchanged cycles skip serialization. With no live SA
lease, network events discard proof. The next due beacon or phone admission
establishes it again, with no separate verification timer.
Passive br0 listeners use a cheap address ioctl. Filtered packet sockets follow
the existing role/link events. Nonmatching packets cause no userspace wakeup.
The next new mesh-SA peer, unicast peer or TCP stream must pass full
firewall/path readback before application admission.
Pending verification makes `path_ready` false.
After a phone leaves, retained manual points and warning history still need expiry and
durable age handling. A runtime status file is the last snapshot, not a
heartbeat. An unchanged timestamp during idle does not mean the service died.
See [ATAK lifecycle and freshness](../../../docs/node-tools-internals.md#atak-runtime-and-package-defaults).

### Experimental ranging has an explicit hardware boundary

The positioning code separates ranging messages/scheduling, the radio
adapter, a position solver and GNSS-versus-range consistency checks. The
solver uses acquisition-time observations and uncertainty models. Repeated
frames do not remove a shared radio boot-offset error. A range-consistent
solution is not proof that satellite signals are authentic: jointly
displaced positions can preserve relative geometry.

The supplied simulation exercises service objects without opening live radio
sockets. The production helper boundary is unavailable until authorization,
mesh-port ingress proof, scoped probes, watchdog behavior and capture
capabilities are provided and verified. `manet-positioning.service` remains
disabled and `positioning` defaults off. Enabling a flag alone does not
complete those dependencies.

`positioning=` controls ranging only. The MT7916 firmware service defaults to
the patched 2024 WM whenever the installed driver has the required timing tag,
even with ranging disabled. Changing this setting neither changes firmware nor
rearms a failed firmware trial. See the
[firmware selection mechanism](../../../docs/node-tools-internals.md#mt7916-firmware-selection)
for the pinned-stock fallback and recovery rules.

For the actual boundaries, read the module descriptions in
[the positioning controller](../../../MANET/node_tools/manet-positioning.py),
[radio adapter](../../../MANET/node_tools/manet_ranging_radio.py),
[solver](../../../MANET/node_tools/manet_locate.py) and
[consistency monitor](../../../MANET/node_tools/manet_spoof.py).
The [tool map](../../node-tools-map.md#location-and-experimental-positioning)
lists the related files. Kernel timing experiments should be evaluated
against their recorded hardware evidence, separately from this user feature.
