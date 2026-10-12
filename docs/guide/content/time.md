# Time and GPS clocks

## Marine

![Crayon drawing of a satellite and three radios with matching clocks, with an arrow showing time shared between radios.](../assets/time-crayon.png)

### Get the clocks to agree

A radio can learn the time from GPS or the internet. Other radios can ask
it for the time.

The radios need matching clocks to do some jobs together. If they are still
waiting for time, give one a working GPS fix or internet connection.

Knowing the time does not tell a radio where it is.

## User

### Why time matters

The radios schedule some changes for a shared moment, including coordinated
channel and configuration changes. If their clocks disagree, one could
change too early or reject another's message. MANET therefore verifies a
time source before enabling timed control.

A GPS receiver with a usable fix can supply time. A radio with its own
internet uplink can obtain time from public time servers. Other radios can
obtain time from a reachable mesh radio that advertises a qualified source.
They do not all need separate internet connections.

### What to look for

The dashboard's **Mesh recovery** details show the last verified clock
source and whether time is ready. GPS details show whether a receiver has
a position fix. A receiver being plugged in is not the same as a usable
fix, and a gateway advertisement is not proof that a time check succeeded.

At startup, allow the source to become ready. If no source is available,
provide GPS reception or a working uplink and wait for the check. The radio
can still search for peers while timed control is gated. It does not invent
a reliable time just because another radio is nearby.

After an initial successful check, losing the source does not immediately
stop the mesh. The radio continues on its own clock and tries to refresh
time later. The longer it goes without a source, the less confidence you
should place in clock agreement. Rebooting starts a new qualification cycle.

### Time is separate from location

A radio can learn time from a neighbor without learning its own position.
The ATAK integration has separate rules for location and phone-provided
information. It does not set the radio's system clock from a phone.
See [location and ATAK](location.md).

## Developer

### One controller owns chrony

The time controller, launched by `one-shot-time-sync.service`, is the sole
runtime owner of chrony's configuration and lifecycle. Local source roles
are checked every fifteen seconds. This ownership prevents a finishing mesh
client attempt or a departing uplink handler from stopping a newly available
GPS time source.

A recent GPS fix enables the SHM refclock. A direct uplink enables configured
internet servers with Network Time Security (NTS), with acquisition bound
to the uplink interface. Ordinary mesh clients receive neither unconditional
internet sources nor a fabricated local-stratum fallback. Only qualified
source profiles serve time to the configured mesh networks; idle/client
profiles do not advertise themselves as sources.

Qualification checks chrony command success, a real reference, stratum 1–15,
synchronized leap state, at most 0.1 seconds remaining system-clock
correction, and a recent selected source with nonzero reach. Internet
qualification also requires negotiated NTS authentication. The 0.1-second
condition measures remaining correction, **not absolute UTC accuracy**.

### Peer selection uses evidence independent of the clock being repaired

The controller joins advertised time-server identities to BATMAN radio
aliases, accepts only unambiguous IPv4 host addresses inside the mesh CIDR,
and requires a selected route with positive throughput and last-seen age
no greater than thirty seconds. It ranks by throughput, with a deterministic
MAC tie-break, excluding self and shutting-down nodes.

Peer wall-clock timestamps cannot establish bootstrap freshness when the
local wall clock is wrong. Live kernel route age and an actual NTP sample
from the selected peer provide the relevant evidence. A cached service
advertisement is only a candidate: it can outlive a lost source until the
next regular Alfred telemetry publication.

No usable candidate causes a thirty-second discovery retry. A chosen peer
gets a ninety-second attempt; failure excludes it for five minutes so
another can be tried. Monotonic deadlines and exclusions persist across
service restarts in `/run/mesh-time-client.json`, without granting extra
attempt time. Success must refer to the selected address and a sample from
the current attempt.

### Initial synchronization, refresh and holdover

Successful initial synchronization creates `/run/initial_time_synced`.
For a mesh client, chrony then stops and the quiet profile remains. The
next refresh is due after six hours plus a per-boot stagger of zero to ten
minutes. Source nodes keep chrony available; local role checks can promote
a client when GPS or an uplink appears.

Initial acquisition may step the clock. After the first verified success,
the controller disarms startup stepping and subsequent corrections slew.
This avoids ordinary refreshes jumping backward through the timestamps used
for channel/admin replay checks. A large correction may not settle within a
bounded attempt; it is not forced by stepping.

Failure after initial success retains the boot-ready marker but withdraws
source-serving flags and keeps retrying. A marker is evidence of a past
success, not current accuracy. `/run` state disappears on reboot. Missing
refresh state cannot grant another six-hour delay merely because the marker
exists. The [time reference](../../../docs/node-tools-internals.md#time-synchronization)
describes remaining drift and hardware-validation limits; the
[mesh guide](mesh.md#developer-one-connected-component-one-channel-decision)
explains why scheduled channel activation depends on these clocks.
