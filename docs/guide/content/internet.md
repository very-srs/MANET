# Share an internet connection

## Marine

![Crayon drawing of an internet router cabled to one radio, which shares a wireless path with two other radios and a phone.](../assets/internet-crayon.png)

### One way out can help the others

1. Give one radio a working internet connection.
2. Let the radios connect to each other.
3. Your friends can all download internet pictures through this shared connection.

No internet? The radios can still talk to each other. If the way out changes,
you may have to start your RedTube download again.

## User

### Internet and mesh are different connections

The mesh connects nearby radios to one another. An **uplink** connects a
radio to the internet. A radio sharing an uplink becomes a **gateway** for
the mesh. Local voice and local mesh services do not need an internet
connection once the radios are set up.

You can provide an uplink through a supported Ethernet connection, USB
tether/modem, or USB Wi-Fi adapter. Ethernet detects whether a router offers
network settings. The radio then checks whether the connection actually
reaches the internet: an HTTP request that must come back with a 204 answer.
A sign-in page fails that check. A router being present does not guarantee
that its internet service is working.

For a USB Wi-Fi uplink, open **Manage → Uplink**, enter that external
network's credentials and follow the management apply process. These are
credentials for the internet connection, separate from the mesh key and the
access-point password used by your own phone.

### Recognize a working path

Look at gateway and route information on the [dashboard](dashboard.md).
Then try the application you actually need. A working local dashboard
proves that your device can reach its radio; it does not prove that the
mesh has internet. A network with a sign-in page may also provide an address
while failing the radio's internet check.

More than one radio can offer internet. MANET considers both the path across
the mesh and the advertised uplink capacity. It avoids changing gateways
for small, brief improvements. If the current path fails, it can select an
available replacement. If a radio's own Ethernet uplink stops working, that
radio stops being a gateway and its traffic goes out through another gateway
on the mesh.

A gateway change can break an existing download, call or other internet
session because traffic now leaves through a different connection. Reconnect
the application after the new route settles. No replacement is possible if
none of the reachable radios has a working uplink.

### Speed and data use

A fast router cannot make a slow mesh path faster. Several hops, shared
radio airtime and other users can all limit the result. The displayed
uplink figure is also different from an end-to-end speed test on your phone.

Wired Ethernet uplinks use a small download to estimate available capacity.
The download only sets the advertised figure. A slow or filtered connection
still counts as internet. It is advertised as slow, not dropped.
Phone tethers, cellular interfaces and Wi-Fi uplinks use an assumed capacity
instead of that download test. Internet use by connected people still uses
the upstream connection's data allowance.

## Developer

### Role changes reconcile the whole forwarding path

An Ethernet DHCP response classifies a possible upstream network; it is
not the final internet verdict. The active dispatcher reconciles interface
role, bridge membership, local addressing, route/NAT policy and BATMAN
gateway advertisement. Client-facing Ethernet belongs to `br0`; an upstream
LAN must not remain bridged into the client mesh after promotion. USB
hotplug handlers feed the corresponding uplink lifecycle.

Gateway status also affects Auto EUD/AP policy, so loss of an uplink can
change whether the candidate Wi-Fi interface serves clients or mesh peers.
These are related transitions, not independent scripts choosing permanent
roles. See [Ethernet role ownership](../../../MANET/networkd-dispatcher/README.md)
and [client connections](connect.md).

### Selecting a Linux default route

BATMAN's gateway selection steers its DHCP behavior; it does not by itself
install the Linux default route required here. The route manager evaluates
the advertised gateways and scores each as the smaller of its BATMAN path
throughput and advertised download bandwidth. A 100 Mbit/s uplink reached
over a 6 Mbit/s path scores 6; a 20 Mbit/s uplink reached over a 40 Mbit/s
path scores 20.

A voluntary replacement must be at least 1.5 times the current score and
at least 2 Mbit/s better, continuously for sixty seconds. A five-minute
hold after a switch limits churn, including after the first selection.
The current gateway disappearing, or failing two consecutive reachability
checks, permits replacement without waiting for an improvement interval.
An existing `br0` default route identifies the incumbent after a controller
restart. Only a metric-100 default route on the selected uplink interface is
preserved. A promoted direct uplink uses metric 100, the mesh route 500, and
networkd's own DHCP default 1024, which never wins.

This hysteresis trades some instantaneous performance for session stability:
a new egress gateway can change the NAT/public address and break transport
connections. These figures are selection policy, not a failover latency
guarantee under every service scheduling or link condition.

### Advertised capacity and name resolution

Ethernet qualification is the HTTP 204 check. A separate 5 MB HTTPS download
then estimates capacity. The measurement excludes DNS/TCP/TLS setup and
divides bytes by transfer time after the first byte. A partial transfer of at
least 250 kB counts. TCP startup can still under-read a fast connection, and a
link with no usable measurement is advertised as 1 Mbit/s down and 0.2 up. The result is reused
while the interface, address and router remain the same. Demotion invalidates
it. Failed tests are rate-limited rather than repeated each loop.

Recognized tether/cellular drivers and Wi-Fi uplinks avoid that download
and advertise 10/2 Mbit/s after their connectivity probe. For measured wired
uplinks, upload is an assumed fifth of download, not an upload test. Selection
uses download. Consequently these advertisements are bounded inputs to a
route heuristic, not a promise of symmetric throughput.

DNS follows qualification. A promoted uplink's DHCP resolvers become the
node's resolvers. An unqualified or demoted Ethernet lease never supplies DNS,
so the node resolves through the public fallback over the mesh. A lone node on
a LAN that only allows its own resolver qualifies with a lookup bound to that
link, then uses that resolver once promoted. Local forwarding avoids a
localhost loop. Gateway selection does not replicate an upstream
router's private DNS namespace across every radio. If IP connectivity works
but a private hostname fails, investigate DNS separately from the mesh route.
The [gateway and speed reference](../../../docs/node-tools-internals.md#gateway-choice-and-uplink-speed)
contains the measurement lifecycle and implementation links.
