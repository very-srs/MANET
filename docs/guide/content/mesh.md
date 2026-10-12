# How the mesh works

## Marine

![Crayon drawing of four radios with an orange route crossed out at a failed link and a purple alternative route through another radio.](../assets/mesh-crayon.png)

### Pass it along

Radios pass traffic through other radios. If a link breaks, another available path can carry it.

- **Wi-Fi:** faster links nearby.
- **HaLow:** longer reach, lower speed.
- **No path at all?** Move back into range or restore a working link.

The radio can find another route. It cannot invent one across a gap with no usable signal.

## User

### More than a direct connection

Two radios do not need to hear each other directly if other mesh radios provide a usable path between them. MANET uses the available links to carry traffic across those intermediate nodes.

Wi-Fi and HaLow can connect the same group of nodes. Wi-Fi favors higher throughput; HaLow provides another path at lower speed and typically longer reach. Neither promises a fixed distance through every building or landscape.

### Choosing channels

With automatic channel selection enabled, radios share observations and agree on a Wi-Fi channel plan. Radios still connected through HaLow belong to the same connected mesh, even when their Wi-Fi links are temporarily disconnected.

Time synchronization is needed for scheduled channel decisions. A cold radio can recover contact before its clock is synchronized, but timed decisions wait until GPS or network time has been verified.

### Rejoining after separation

HaLow provides a recovery path when it is working. Where HaLow is unavailable, selected radios visit rendezvous channels to help separated groups meet again. Recovery takes time and still requires a usable radio path.

If a node has internet access, it can become a gateway. Nodes can choose among available gateways using the mesh path and uplink performance. Shared services also elect hosts and can move when a host disappears.

Check the status dashboard for actual peers, routes, and link information. A completed setup or a powered-on radio does not guarantee a connected mesh.

## Developer

### Routing and control use different mechanisms

BATMAN V routes at Layer 2 over the attached mesh interfaces. Alfred replicates identity, telemetry, and control records; the local registry is a view derived from that data. Identity uses type 67 and volatile telemetry type 68. Reachable BATMAN membership must be distinguished from stale registry entries.

`node-manager-acs.sh` schedules scanning, coordination requests, tourguide work, and service elections while maintaining status and addressing. The static manager retains the status, address, and service work without automatic Wi-Fi channel selection. `node-manager-select.sh` chooses the runtime symlink from `acs=` before service start.

### One connected component, one channel decision

`mesh-channel-agreement.py` coordinates a proposal for the connected BATMAN component across **all** radios. Wi-Fi groups connected through HaLow are not separate voting domains. A failed membership query does not mean a solo node.

The coordinator freezes participants and collects ACKs for 60 seconds. A strict majority allows commitment with activation 30 seconds later. Missing ACKs do not shrink the denominator. Static members only ACK compatible plans. Timed agreement waits for the initial verified clock synchronization.

Encrypted Alfred type 74 carries agreement and current-plan recovery. Type 75 carries authenticated rendezvous helpers. Types 76 and 77 provide nonce-bound cold recovery before a node can validate wall-clock freshness. Cold recovery does not set the clock or enable scheduled elections.

### Recovery and local role constraints

A working HaLow interface suppresses Wi-Fi tourguide excursions. Without HaLow, the shared rendezvous schedule rotates conventional Wi-Fi bands and anchors. Tourguide and channel application use a shared local lock. An AP-owning gateway retains the agreed mesh configuration for the AP candidate's later return to mesh mode.

Channel moves are coordinated, but unreliable delivery can leave stragglers. Recovery and reconciliation are part of the protocol; the design is not an atomic switch across every disconnected radio.

### Shared services and control

Service elections rank eligible, fresh candidates by connectivity, use deterministic ties and incumbent preference, and reconcile after partitions rejoin. Administrative configuration is staged and acknowledged over encrypted Alfred messages, with scheduled activation and local rollback protection for dangerous changes. AP credentials stay local; HTTP is not the peer control transport.

- [Agreement and recovery details](../../../MANET/node_tools/README.md#channel-selection--jamming-detection)
- [Rendezvous schedule](../../../MANET/node_tools/README.md#discovery--partition-healing)
- [Protocol internals and failure cases](../../../docs/node-tools-internals.md#elections-channels-and-healing)
- [Mesh configuration and rollback](../../../MANET/node_tools/README.md#mesh-configuration-push)
