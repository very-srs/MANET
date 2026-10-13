# Connect your device

## Marine

![Crayon drawing of a phone connected by a blue Ethernet cable to a green radio, and a separate phone using a wireless link to another green radio.](../assets/connect-crayon.png)

### Phone meets radio

- **Cable:** connect your phone or computer to the radio's Ethernet port with a blue hose.
- **Wi-Fi:** join the radio's wireless access point, if it is enabled.
- **Open:** [http://manet.local/](http://manet.local/) to see status.
- **Change settings:** open [Manage](http://manet.local/manage/) and ask a grown up to help you enter the admin password.

Wi-Fi access depends on how this radio was configured by some nerd. If it is not serving an access point, use Ethernet.

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

The current ATAK integration uses a wired phone connection and additional ATAK settings. Follow [location and ATAK](location.md). Joining the radio's Wi-Fi is not a substitute for the wired admission path.

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
The filter selects discovery protocols, rather than dropping all multicast;
voice and ATAK still need their mesh transports. A released or invalid
allocation withdraws the management-name announcement until a valid local
gateway exists again.

HTTP access is also checked independently of name resolution. Both local
and remote packets can arrive on `br0`, so accepting every request on that
interface would expose management to other radios' clients. The firewall
instead permits localhost and source addresses in this node's DHCP pool;
application checks also require localhost or the mesh/EUD subnet. This is an
address-pool rule, not proof of a particular lease or a cryptographic client
identity. Authentication then decides whether an admitted client may use
management actions. 

### AP ownership during ACS

With `acs=y` and Auto EUD mode, an Ethernet gateway can serve the AP while its other radios keep mesh connectivity. The AP candidate's stored mesh configuration follows the agreed channel plan. When the uplink is removed, it returns to mesh operation. Gateways do not perform Wi-Fi tourguide excursions.


### ATAK boundary

`manet-atak.service` is enabled by default; `atak=n` explicitly opts out. Admission requires the verified wired EUD path, a matching FDB/neighbor mapping, and the owned firewall policy. No admitted phone means no CoT output. `manet-positioning.service` is a separate, disabled-by-default service.

- [Networking and EUD reference](../../../MANET/node_tools/README.md#network-management)
- [Discovery boundary internals](../../../docs/node-tools-internals.md#local-eud-discovery)
- [Web interface internals](../../../docs/node-tools-internals.md#web-interface)
- [Dispatcher hooks](../../../MANET/networkd-dispatcher/README.md)
