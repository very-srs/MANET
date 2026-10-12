# Addresses and names

## Marine

![Crayon radios joined by wireless paths, showing that messages can travel between them.](../assets/mesh-crayon.png)

### Every radio needs a place to get mail

Your radio picks some addresses. It keeps one for itself and gives one to
your phone. Other radios pick their own addresses.

If two radios pick the same space, one picks again. A phone may need to
reconnect while that happens.

Use **[manet.local](http://manet.local/)** to find the radio your phone is
plugged into.

## User

### What an address does

An IP address is a number used to deliver information to a device. MANET
assigns these automatically; you normally leave your phone or computer set
to obtain its address automatically.

Each radio reserves a small group of addresses from the mesh's shared
range. It uses one as its own mesh address, one as the local gateway for
attached devices, and the rest for those devices. The gateway is where a
phone sends traffic that needs to leave its immediate network.

The number of attached devices allowed per radio is chosen during flashing.
If all those places are occupied, another device cannot simply obtain a
fresh lease. Check that capacity before diagnosing a connected device with
no address as a wireless failure.

### Use the name for the radio beside you

**[http://manet.local/](http://manet.local/)** opens the dashboard of the
radio serving your attached device. Every radio uses this same convenient
local name. It is not a name for one permanent central server.

The radio also has a hostname such as `mesh-1234` and a mesh address, visible
in its status details. These identify it to the rest of the system. Record
the hostname when reporting a fault, so another person knows which radio
you mean. If the convenient name fails, try the local gateway address shown
in your phone or computer's network details.

### Why an address might change

There is no central machine handing blocks to radios. Each radio listens
for existing claims and tries to choose free space. Two groups that were
apart can pick overlapping addresses. When they meet, the radios resolve
the overlap and one changes its block. Simultaneous startup can also require
this repair.

An address change can interrupt a connection. If your device retains its
old network details, disconnect and reconnect it so it can ask again. Avoid
building a permanent configuration around a particular radio's current
number unless you account for reallocation.

Some [shared services](services.md) have reserved service addresses that
can move between hosts. Those are separate from a radio's own address.
Changing the shared mesh address range affects all of them; follow
[settings](settings.md) rather than editing one radio independently.

## Developer

### One shared subnet, several address roles

The allocation unit is a contiguous range inside `ipv4_network`, not a
subnet routed behind each radio. For client capacity E, block width is
W = E + 2. With H as the first usable host address and chunk index K,
the primary address is H + 5 + K × W. The next address is the local client
gateway and the remaining E addresses are the DHCP pool. Five usable hosts
at the beginning are reserved for shared services.

For `10.30.0.0/24` and E = 1, chunk zero uses `.6` for the node, `.7` for
its client gateway and `.8` for its client. Chunk one starts at `.9`.
Capacity is floor((254 - 5) / 3) = 83 complete blocks. Different per-node
client capacities mean that equality of chunk indices is insufficient for
collision detection: the allocator must compare absolute start/end ranges.

### Discover, claim, reconcile

IPv6 and Alfred readiness bootstrap discovery before final IPv4 allocation.
The allocator publishes local identity and telemetry, observes a ten-second
Alfred synchronization window, and allows up to twenty seconds from that
window's original start when visible peers still lack records. Failure to
query local state postpones selection instead of declaring the network empty.

The occupied set comes from peer identity claims in the local registry. A
remembered allocation is reusable only if it remains free under current
observations. Otherwise the allocator randomly chooses an available range,
applies its addresses and DHCP pool, and publishes the claim. Periodic
reconciliation repairs races and partition merges. For overlapping claims,
the higher MAC keeps its allocation and the lower MAC releases and reselects.

Thus random choice avoids predictable contention but does not prove
uniqueness. Convergence depends on exchange of fresh claims. Unchanged
telemetry does not renew observation age; stale peers eventually stop
reserving space. A returning node can therefore find its old allocation
occupied. The [allocation reference](../../../docs/node-tools-internals.md#ipv4-allocation-and-collision-recovery)
describes expiry, mixed-width collisions and exhaustion behavior in full.

### Names follow ownership and discovery boundaries

`manet.local` points to this node's client-gateway address. DHCP and local
DNS/mDNS discovery are confined at the mesh bridge boundary so a different
radio cannot answer a local client's setup request. Allocation changes
therefore require synchronized updates to addresses, DHCP, discovery and
the management firewall's allowed source pool.

Peer hostname mappings are a separate registry-derived local hosts view.
The hosts updater compares content before replacing it; it does not probe
each peer's HTTP server. A known name or cached record establishes identity,
not present reachability. Live BATMAN routes answer the latter question.

IPv6 also carries control/bootstrap traffic and reserved service VIPs.
Service helpers derive stable `/128` addresses from the configured mesh
prefix, while moving ownership to the elected host. Neither IPv6 bootstrap
nor a service VIP eliminates the IPv4 allocation and client-isolation work.
See [client packet paths](connect.md#developer-layer-2-topology-and-address-ownership)
and [service ownership](services.md#developer-ranking-and-stable-ownership).
