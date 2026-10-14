# Location and ATAK

## Marine

![Crayon drawing of a phone cabled through an adapter to a radio, with a GPS receiver, satellite and a location dot on the phone.](../assets/location-crayon.png)

### Show the phone where you are

Plug your ATAK phone into the radio with its Ethernet adapter, or join that
radio's Wi-Fi. Stay near the radio. Count to twenty, or, if you can't do that, just
be patient. The signal has to come from outer space.

Several phones? (Several means more than one.) Only one of them can set the group's
location. Open [manet.local](http://manet.local/)
and look under **ATAK PHONES**. Choose a phone there to be in charge.

Set up ATAK once: turn off phone location and turn on the radio's location
input. The [phone settings below](location.md#user-set-up-your-atak-phone) work with every
radio. You do not need to add a radio address.
No good position? You should drag your location marker so that it matches where you
are on the map anyway.

Other people on the network see your dot because your phone tells them where
you are. Leave ATAK's sharing turned on.

Nobody using a radio? If it has a good position, the team sees the radio's name
on the map. When a phone takes over, its dot replaces the radio's marker.

## User

### Set up your ATAK phone

ATAK is a mapping application that can receive information from the radio.
Use a **wired Ethernet connection** or this radio's enabled **Wi-Fi access point**.
Several nearby phones can use one radio, although one EUD per radio is recommended.
All the EUDs connected to a radio will receive the same GPS position,
so stay near the radio and coordinate who sets the group's location if needed.

1. Connect the phone through a compatible Ethernet adapter to the radio's
   Ethernet connection, or join the radio's own Wi-Fi network.
2. Turn off Android location services for this setup.
3. Enable ATAK's external GPS input on **UDP 4349**. Leave its standard CoT
   input enabled. CoT is the message format ATAK uses for these events.
4. Leave ATAK's built-in **Default Mesh SA** output enabled. It lets the radio
   discover your phone automatically. No output entry or radio IP address is
   needed. These settings stay the same when you change radios.
5. Look for the contact **MANET mesh-XXXX**, using the radio's actual hostname.
   If there is a GPS problem, mark a point on the map and send it to this
   contact with ATAK's Send action. Other ATAK users then see you at that point.

The contact itself is hidden from the map. A separate external-position
feed supplies location, so absence of a contact marker does not prove that
the contact is missing. A custom `atak_callsign` can change its name.

### How the group sees you

The radio gives your phone its position. Your phone then shares that position
with every other ATAK user on the mesh through ATAK's built-in **Default Mesh SA**
output (239.2.3.1, UDP 6969). Leave that output enabled. With it off, nobody
else sees you, and this radio cannot discover your phone either. While a phone
is attached, the radio does not appear on the map itself. Your phone's dot is
your position.

A radio with nobody attached shares its own position with the team's maps,
under its hostname, such as **mesh-5cc2**, or its configured callsign. It uses a
radio/signal symbol and offers no chat or Send endpoint. When a phone joins,
the radio requests removal of that marker so the phone's own dot takes over.
After the last phone's presence expires, at most 75 seconds after its last
report, the radio marker returns if a usable position remains. It refreshes
about every 30 seconds and sooner after moving five metres. Suspect GPS is
never advertised. The radio uses its held manual point instead, or stops if
there is none. Lost messages can delay a map change. The new relay marker's
appearance and handoff still need verification on the target Pixel.

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
the radio's location. Switching controllers does not erase a held manual point.
Other phones receive the position and GPS warnings. Sending a point from a guest
phone produces a chat naming the controller and explaining how to change it.
When another phone joins or control changes, every present phone gets a chat
naming the controller. Agree with the group before changing it.

### Understand the position you are using

A connected GPS receiver still needs a fresh fix. Indoors, blocked sky, or
receiver trouble can leave it without one. A manual point is a supplied
location, not a fresh satellite measurement. Check its age and the displayed
source before treating it as your current position.

The radio supplies your phone's location after discovering it. If nothing appears, check
the cable or local Wi-Fi connection, the phone's address and the ATAK settings before
assuming GPS is broken. A healthy mesh between radios does not establish
the phone's local connection.

The project also contains experimental radio-ranging and position-estimation
work. It is disabled by default and is not a ready replacement for GPS.
Do not assume that seeing neighboring radios means MANET can determine your
position from them. The radio's [clock synchronization](time.md) is a
separate function.

## Developer

### Admission is a local network proof

The ATAK daemon is enabled by default. An absent/empty `atak` setting means
enabled, while `n`, `no`, `0` or `false` disables it. Phone admission requires
an independently verified local EUD port: wired `end0`, or the active AP named
by `/var/lib/ap_interface`. The AP requires live hostapd identity, matching
configuration, carrier and forwarding br0 membership. Routed Ethernet does not
prevent AP admission. A resolved br0 neighbor's MAC must be learned exclusively
on one admitted local port, never also bat0. Each new TCP stream or UDP peer
requires a fresh proof. Default Mesh SA arrives through a packet socket on
each admitted local EUD port, with a kernel BPF filter for IPv4 UDP
`239.2.3.1:6969`. No socket joins that multicast group or opens on bat0.
The frame's source MAC must be proven on its actual ingress port before its
SA payload reaches the decoder. The contact endpoint must name its IPv4 source.

The bounded mesh decoder accepts legacy XML and TAK v1's `bf 01 bf` header
followed by protobuf SA. Both produce the same `PhoneSA`, including `how`,
`geopointsrc`, `altsrc` and parent links. Other detail and phone numbers are
discarded. Replies remain unicast XML on 4242/4349. Existing unicast XML input
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
and external position in its own clock domain. Only controller gestures and
read receipts can acknowledge the radio's warnings. Guest receipts cannot.

Controller preference is manual-if-present, sole wired, current-if-present,
then stable random. A boot/session-bound JSON producer file carries the local
web choice to the existing event cycle, which rechecks the selected phone's
presence and path. The overview exposes only ATAK enable/running state and
phone UID, callsign, port and controller status. Session identity and the full
runtime snapshot stay server-side. The HTTP POST requires JSON, rejects foreign
origins and checks this radio's DHCP source pool. The port-80 `manet_ui` firewall enforces
that same local pool plus loopback. It assumes a trusted team network rather
than proving physical HTTP ingress. The held location and distrust survive
controller switches, and marker deletions still target the original phone.
A stock Pixel on a CM4's AP, with only Default Mesh SA enabled, was admitted
and received the contact and external GPS feed. Multiple-phone operation still
needs hardware validation.

Phone SA multicast crosses `br0` and `bat0` like any other ATAK traffic, and
the discovery firewall passes `239.2.3.1:6969`. The radio reads that SA only
from its own EUD ports, so remote phones' SA never reaches its decoder.

An unattended radio uses the same position selector and publishes protocol-0
XML to `239.2.3.1:6969` through br0. Its UID is `<radio-uid>-beacon`, distinct
from the hidden contact, with hostname or `atak_callsign` as its label.
CoT type `a-f-G-U-U-S-R` selects the standard radio/signal symbol, with no team,
role or contact endpoint. Trusted receiver GPS or the held manual fallback
can supply geometry. No selected fix means no position announcement, including
no 0,0 placeholder. Refresh is 30 seconds with 90-second staleness. Five metres
of movement can trigger earlier output, bounded to one attempt per five seconds.
These deadlines use the existing event cycle.

A live local phone lease suppresses the beacon, including during temporary
proof loss. Arrival sends a `t-x-d-d` task with `__forcedelete` for only the
beacon UID. The public ATAK importer removes that item immediately unless the
receiver disables remote deletion. Three successful UDP handoffs are attempted,
at two-second spacing and at most five attempts. Delivery is unacknowledged.
Staleness gives a fallback whose removal timing depends on receiver settings.
Departure resumes announcements when the final lease ends. A separate br0
multicast socket disables loopback and fragmentation. The bridge output rule
drops locally originated mesh SA on every port except bat0, while forwarded
phone SA remains unchanged. Readback proves that rule and bat0 readiness before
sending. Outgoing packet filtering, local-source exclusion and the person-SA
classifier prevent admission of the radio's own beacon. Pixel icon rendering,
multicast deletion and bat0-only delivery remain hardware validation work.

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

For the actual boundaries, read the module descriptions in
[the positioning controller](../../../MANET/node_tools/manet-positioning.py),
[radio adapter](../../../MANET/node_tools/manet_ranging_radio.py),
[solver](../../../MANET/node_tools/manet_locate.py) and
[consistency monitor](../../../MANET/node_tools/manet_spoof.py).
The [tool map](../../node-tools-map.md#location-and-experimental-positioning)
lists the related files. Kernel timing experiments should be evaluated
against their recorded hardware evidence, separately from this user feature.
