# Location and ATAK

## Marine

![Crayon drawing of a phone cabled through an adapter to a radio, with a GPS receiver, satellite and a location dot on the phone.](../assets/location-crayon.png)

### Show the phone where you are

Plug your ATAK phone into the radio with its Ethernet adapter. Give the
radio's GPS receiver a chance to find you.

The phone needs the right ATAK settings to use the radio's position.
No good position? A dot on an old map is not proof of where you are now.

## User

### Set up the wired phone

ATAK is a mapping application that can receive information from the radio.
The current MANET integration requires a **wired Ethernet phone connection**.
A phone on the local Wi-Fi access point does not meet its admission rules.

1. Connect the phone through a compatible Ethernet adapter to the radio's
   client Ethernet connection.
2. Turn off Android location services for this setup.
3. Configure ATAK to receive external GPS CoT on **UDP 4349** and ordinary
   CoT on **UDP 4242**. CoT is the message format ATAK uses for these events.
4. Configure ATAK's situational-awareness output to the radio's IPv4 address
   on **UDP 4242**. That traffic lets the radio associate the attached phone.
5. Look for the contact **MANET mesh-XXXX**, using the radio's actual hostname.
   To send a manual point to the radio, select this contact with ATAK's Send
   action. Its Send endpoint uses **TCP 4242**.

The contact itself is hidden from the map. A separate external-position
feed supplies location, so absence of a contact marker does not prove that
the contact is missing. A custom `atak_callsign` can change its name.

### Understand the position you are using

A connected GPS receiver still needs a fresh fix. Indoors, blocked sky or
receiver trouble can leave it without one. A manual point is a supplied
location, not a fresh satellite measurement; check its age and the displayed
source before treating it as your current position.

The radio sends no CoT until a phone is admitted. If nothing appears, check
the wired connection, the phone's address and the ATAK settings before
assuming GPS is broken. A healthy mesh between radios does not establish
the phone's local connection.

The project also contains experimental radio-ranging and position-estimation
work. It is disabled by default and is not a ready replacement for GPS.
Do not assume that seeing neighboring radios means MANET can determine your
position from them. The radio's [clock synchronization](time.md) is a
separate function.

## Developer

### Admission is a local network proof

The ATAK daemon is enabled by default; an absent/empty `atak` setting means
enabled, while `n`, `no`, `0` or `false` disables it. The current path requires
a verified wired EUD on `end0`/`br0`, matching local FDB and neighbor state,
and the daemon's owned nftables policy. Incoming SA associates and pins the
phone identity. Each new TCP stream or UDP peer requires a fresh proof.

The daemon subscribes to route/link/address/neighbor/FDB and nftables events
before its initial snapshot. Unchanged ticks reuse a cached proof, avoiding
repeated `ip`, `bridge` and `nft` subprocesses. Events during a snapshot
invalidate it; subscription failure, overflow or truncation ends the process
so systemd can establish fresh subscriptions. The kernel policy remains
the packet boundary: userspace notifications do not make an arbitrary
external firewall flush atomic with I/O.

### Inputs, output and retained state

GPS data comes through gpsd and the local reader, with per-receiver epochs
and freshness. Replayed epochs cannot keep a fix alive merely by arriving
again. The GNSS reader tracks signal/time anomalies as hints; these are
not proof of spoofing. In particular, an unavailable receiver diagnostic
must stay unknown rather than become a clean bill of health.

The ATAK selection logic combines admitted-phone state, fresh location
inputs, manual points and available monitoring information. Phone presence,
phone clock information and manual-location state persist under
`/var/lib/manet-atak`; the daemon never sets the OS clock. Its external
position feed and ordinary contact/CoT messages are separate outputs.

With no admitted phone, no CoT is emitted. Before the first association,
the application skips GPS selection and unchanged state serialization while
still tracking monitoring-input freshness and producer loss. After a phone
leaves, retained manual points and warning history still need expiry and
durable age handling. A runtime status file is the last snapshot, not a
heartbeat; an unchanged timestamp during idle does not mean the service died.
See [ATAK lifecycle and freshness](../../../docs/node-tools-internals.md#atak-runtime-and-package-defaults).

### Experimental ranging has an explicit hardware boundary

The positioning code separates ranging messages/scheduling, the radio
adapter, a position solver and GNSS-versus-range consistency checks. The
solver uses acquisition-time observations and uncertainty models; repeated
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
