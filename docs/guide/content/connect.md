# Connect your device

## Marine

![Crayon drawing of a phone connected by a blue Ethernet cable to a green radio, and a separate phone using a wireless link to another green radio.](../assets/connect-crayon.png)

### Phone meets radio

- **Cable:** connect your phone or computer to the radio's Ethernet port with a blue hose.
- **Wi-Fi:** join the radio's wireless access point, if it is enabled.
- **Open:** [http://manet.local/](http://manet.local/) to see status.
- **Change settings:** open [Manage](http://manet.local/manage/) and ask a grown up to help you enter the admin password.

ATAK can use the cable or this radio's Wi-Fi. Pick the phone that sets the
group's location on [status](http://manet.local/). No admin password is needed.
Radio has GPS? Turn **Android location OFF**, turn **ATAK external GPS input ON,
UDP 4349**, and leave **Default Mesh SA ON**. Radio has no GPS receiver? Keep
**Android location ON** so the phone supplies the position. Follow the
[location setup](location.md#user-set-up-your-atak-phone).

Wi-Fi access depends on how this radio was configured by some nerd. If it is not serving an access point, use Ethernet.

Remember not to go to the dealership nearest the base and finance a Camaro as an E3.
## User

### Choose an available connection

An end-user device, or **EUD**, is the phone or computer attached to a radio. To connect, use the radio's Ethernet connection or its configured Wi-Fi access point. A phone may need a compatible Ethernet adapter.

| Mode | Behavior |
|---|---|
| Wired | Ethernet connects a client, or becomes an uplink if connected to an internet router. |
| Wireless | A local access point serves phones and computers. |
| Auto | The role depends on Ethernet detection. A wired EUD disables the WiFi AP and that 5ghz interface rejoins the mesh.

With automatic channel selection enabled, Auto serves an AP when the radio is a gateway.  If you need a dedicated wireless client connection, select wireless EUD mode when configuring the radio. Wired is needed to get the full benefit of the radio's features.

### Open the local dashboard

From a device connected to this radio, visit **[http://manet.local/](http://manet.local/)**. You can also use the radio's local address. The status page shows topology, node details, and link information without a password.

**[http://manet.local/manage/](http://manet.local/manage/)** opens the management page. Sign in with the admin password chosen during flashing. Management includes radio controls, measurements, configuration, and voice status. This page is password protected because it can make mesh wide changes.

The [dashboard guide](dashboard.md) explains the displays and tabs. See [changing settings](settings.md) for the staging and apply process.

The dashboard is for this radio's own clients. A device on the upstream internet router or attached to another radio is not allowed to open it.

### ATAK phones

ATAK works over the client Ethernet connection or this radio's enabled Wi-Fi
access point. Keep phones near their radio. With a GPS-equipped radio, turn
**Android location OFF**, enable ATAK external GPS input on **UDP 4349**, and leave
**Default Mesh SA ON**. ATAK then shares the radio's position over mesh, direct
connections and TAK servers. With no radio GPS receiver, keep **Android location
ON** instead. The controlling phone supplies GPS and there is no missing-receiver
prompt. Choose the controller in **ATAK PHONES** without the admin password.

The multicast rewrite is a safety net if someone forgets those settings.
A detected TAK-server phone is exempt for ten minutes after its last observed
TCP traffic on 8089 or 8087. TLS cannot be rewritten. The local panel shows
**TAK server** and any radio/phone GPS disagreement. Follow
[location and ATAK](location.md) for setup and detection limits.

## Developer

### Layer 2 topology and address ownership

Each node claims an IPv4 block sized `max_euds_per_node + 2`. Its first address is the primary mesh address on `br0`, its second is the local EUD gateway address, and the remaining addresses form its DHCP pool. `br0` bridges `bat0` (which contans one or more wlan devices, depending on EUD mode). 

These are allocations within one shared IPv4 subnet, not a separate routed
subnet per radio. The [address-selection explanation](overview.md#developer-addresses-selecting-a-block-and-resolving-collisions)
covers how a radio chooses the block and what happens after an overlap.

`ethernet-autodetect.sh` classifies Ethernet as an uplink when DHCP traffic, switch traffic, or external RAs are heard.  The absense of
such traffic determines that this is an EUD. Dispatcher hooks and the AP/mesh role helpers reconcile bridge membership, addressing, gateway advertisement, and access-point ownership. The AP's mesh role is preserved in configs for its possible return to mesh operation.

### From a client connection to a mesh packet

When Ethernet is serving an EUD, or the AP is active, that client-facing
interface joins `br0`. The local DHCP server leases an address from this
radio's block and supplies the radio's second address as the client gateway.
Bridge filtering keeps the DHCP request and reply off `bat0`, so another radio's
DHCP server cannot answer the same local request.

Traffic addressed to another device in the shared subnet crosses the bridge
and BATMAN path. Internet traffic instead follows the client's default
gateway to the radio, then the radio's selected uplink or mesh gateway
route. A working local DHCP does not prove a peer path exists, and a working peer path does not prove that any node
has internet access.

An upstream DHCP offer classifies Ethernet as an uplink, but that is not
proof of usable internet. Uplink reconciliation checks connectivity and
updates routing/NAT and gateway advertisement. When the cable's role changes,
the node must reconcile those together with bridge membership and AP
ownership.

### Management and discovery boundaries

`mesh-status.py` serves public status and authenticated management on port 80.
The application admits localhost and the configured mesh/EUD subnet.
Nftables narrows this to localhost and this node's DHCP pool. Authentication
does not bypass that network boundary.

`manet-dhcp-isolation.py` keeps DHCP and local discovery protocols from spreading over the mesh. `manet.local` resolves to the local EUD gateway address, independently of elected service VIPs. Discovery confinement must preserve intentional mesh transports such as voice and ATAK.

The management name illustrates why this boundary exists. Each node serves
the same name, `manet.local`, but answers with **its own** client-gateway
address through local DNS and mDNS. Discovery packets are blocked at the
mesh port in both directions, including traffic to and from the node itself.
The filter selects discovery protocols, rather than dropping all multicast.
Voice and ATAK still need their mesh transports. A released or invalid
allocation withdraws the management-name announcement until a valid local
gateway exists again.

HTTP access is also checked independently of name resolution. Both local
and remote packets can arrive on `br0`, so accepting every request on that
interface would expose management to other radios' clients. The firewall
instead permits localhost and source addresses in this node's DHCP pool.
Application checks also require localhost or the mesh/EUD subnet. This is an
address-pool rule, not proof of a particular lease or a cryptographic client
identity. Authentication then decides whether an admitted client may use
management actions. 

### AP ownership during ACS

With `acs=y` and Auto EUD mode, an Ethernet gateway can serve the AP while its other radios keep mesh connectivity. The AP candidate's stored mesh configuration follows the agreed channel plan. When the uplink is removed, it returns to mesh operation. Gateways do not perform Wi-Fi tourguide excursions.


### ATAK boundary

`manet-atak.service` is enabled by default. `atak=n` explicitly opts out. Admission independently verifies wired `end0` or the active AP's forwarding
br0 path. A MAC must be learned exclusively on that local EUD port and resolved
on br0. Simultaneous bat0 learning fails. The explicit firewall allow-list uses
`end0` and the AP role name, requiring an ATAK restart after AP renaming. SA
leases form a bounded per-phone table with one controller. Controller choice
uses present manual preference, sole wired phone, present current controller,
then stable random selection. The password-free controller POST checks the local
DHCP source pool and JSON/origin boundary, then hands off through a producer
file for daemon-side presence validation. An unattended radio with a usable fix
can publish its own beacon. `manet-positioning.service` is a separate, disabled-by-default service.

The documented setup is Android location off and ATAK external GPS input on
UDP 4349 for GPS-equipped radios, with Default Mesh SA enabled. A radio without
GPS hardware needs Android location on instead. This selects one position inside
ATAK for all outputs, including unicast and TLS, which the radio cannot rewrite.

Default Mesh SA (`239.2.3.1:6969`) is captured only on local EUD ingress with
filtered packet sockets. TCP never wakes these sockets. Bridge rules instead
refresh `tak_phones` source-MAC entries for IPv4/IPv6 TCP 8087/8089, with a
600-second timeout since the latest packet. The native rewrite block checks
that set directly, including while userspace is paused. The daemon reads it
through bounded netlink requests before SA replacement or on meaningful status
changes, reusing a completed result for five seconds. Idle ticks issue no reads.
One admitted frame per EUD port can wait for a fresh reply, with that reader
paused until completion. A pending read still has a two-second failure deadline.

An output-only bat0 socket sends admitted phone SA with radio-selected positions,
preserving sender identity. Three-second nft leases block their native
EUD-to-bat0 copies, except for kernel-marked server MACs. A captured frame can
race a new kernel insertion, so the preceding snapshot may still produce a
replacement around the transition. After learning the insertion, the daemon
relays potentially intercepted originals until the lease window ends and the
queue drains, which can duplicate native reports. The phone setup keeps all
ATAK outputs consistent independently of this safety net.

The daemon neither joins the multicast group nor receives on bat0. Local copies,
unadmitted senders, TAK-server and unicast traffic remain native. Outgoing copies
forwarded from the mesh are filtered before userspace. The existing permanent
multicast-router setting on bat0 remains unchanged. This receiver adds no
multicast listener advertisement. A bounded v1 protobuf or legacy XML SA decoder
runs only after ingress/MAC/neighbor/firewall proof, and requires the contact
endpoint to match the proven IPv4 source. Unicast 4242 input also accepts the
secondary br0 gateway address. TCP destinations and UDP packet-info destinations
must belong to br0's verified global address list. The radio advertises its
primary address and replies directly to the phone.

- [Networking and EUD reference](../../../MANET/node_tools/README.md#network-management)
- [Discovery boundary internals](../../../docs/node-tools-internals.md#local-eud-discovery)
- [Web interface internals](../../../docs/node-tools-internals.md#web-interface)
- [Dispatcher hooks](../../../MANET/networkd-dispatcher/README.md)
