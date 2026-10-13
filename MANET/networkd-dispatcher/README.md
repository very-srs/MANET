# Networkd-dispatcher hooks

networkd-dispatcher runs a script when systemd-networkd reports that an
interface changed state. These hooks are how a node decides what a cable
plugged into it means, and how it undoes that decision when the cable is pulled.

## When a cable gets carrier

The node works out whether the interface is an upstream uplink or a wired EUD
port, and configures itself to match. See
[Connectivity Modes](../../README.md#connectivity-modes) for what each of those
means.

For `end0`, the carrier hook runs `ethernet-autodetect.sh --hotplug`, matching
the boot service. Other interfaces enter uplink reconciliation through
`manet-uplink-dispatch.sh`. Carrier detection does not download updates.

While it waits for an address on `end0`, the node listens to what the other
end sends. A lease means an upstream network. A single device asking for an
address is treated as a wired EUD after about 5 seconds. Router
advertisements, switch protocol frames (STP, LLDP, CDP) or traffic from more
than one device mean a network is attached. If that network gives the node no
address within 20 seconds, the port is left unbridged and the node does not
serve DHCP onto it. A capture reaching 1 MiB is also treated conservatively
as a network; capture storage and process lifetime are bounded. A completely
silent cable is treated as a wired EUD after
20 seconds. To force a wired EUD anyway, run
`ethernet-autodetect.sh --mode wired-eud`.

Detection on `end0` runs once per physical connection. Bridging or reconfiguring
the port makes networkd report carrier again, and those repeat events are
ignored while the link itself has not changed. Unplugging the cable, swapping it
to another device, or power-cycling the device at the other end starts a fresh
detection. If you move the cable during the 20-second detection, the node
detects again once the current run finishes.

## When the interface becomes routable

Once DHCP has finished and a route exists, gateway and NAT state are
reconciled. Carrier alone does not mean the interface can reach anything, which
is why this is a separate step.

An Ethernet uplink becomes a gateway only after it downloads a speed test of
at most 5 MB over HTTPS, and the measured download speed is what it announces
to the mesh. A captive portal or a network that blocks the test leaves the node
a mesh client; it is rechecked when the node manager next publishes its
status, at most 3 minutes later. Phone tethers, cellular modems and Wi-Fi
uplinks are not tested and announce the default 10/2 Mbit/s.

After reconciliation, an opted-in node queues `manet-auto-update.service`
without waiting for it. This oneshot rechecks `auto_update` and accepts either
the selected uplink's IPv4 default route or a next-hop default on `br0` from
the mesh gateway route manager. It still excludes loopback, BATMAN devices and
other bridges, and does not require ICMP access. Systemd serializes concurrent
starts, and the updater's flock excludes concurrent manual updates too.
Without `/run/initial_time_synced`, it logs that the check is deferred and exits
successfully. The time service creates this marker after verified GPS, NTS or
qualified peer synchronization. `manet-auto-update.path` watches for it and
queues another check once per boot, including if the marker already exists
when the path unit starts. The clock trigger also checks opt-in and the route;
if no route exists yet, a later routable event or mesh route acquisition checks
again. The gateway route manager queues a check only after successfully gaining
a default route where none existed, not on route refreshes or gateway switches.
Mesh-path checks wait a random 0–900 seconds, then recheck opt-in and route
availability. Direct-uplink checks do not add this delay.

`manet-auto-update.timer` also queues the same service daily with up to two hours
of random delay. It catches missed calendar events after downtime. The updater
still skips routine checks within 24 hours of a successful check or installation;
the timer does not override that limit. Path and timer ship enabled and are
started by setup and updates. With automatic updates disabled, the wrapper exits
successfully before looking up routes or waiting.
The opt-in accepts `y`, `yes`, `1` or `true`, case-insensitively.

The time service observes the selected uplink and starts internet time
synchronization through it. Only a verified chrony source sets the NTP flag
carried in normal Alfred telemetry; becoming a gateway alone does not set it.

## When carrier is lost

Losing carrier returns `end0` to its baseline. A degraded event with carrier
still present leaves its role intact, including a wired-EUD bridge port. `dnsmasq` stops, the interface is flushed, the generated `.network`
files are replaced with the default DHCP one, the wired-EUD dnsmasq config is
dropped, and gateway and internet-NTP state are reverted if this node held
either. The hook leaves chrony control to the time service, which preserves a
usable local GPS source when the internet uplink disappears.

Only `end0` is reconfigured. A wireless or USB interface losing carrier does
not run the wired teardown.

## AP and wired-EUD transitions

In auto mode, a wired EUD takes priority over the wireless AP. With
`acs=y`, the AP also requires gateway state. hostapd's start check refuses
the AP on a node without it. The off hook returns the radio to the mesh when
end0 was the last uplink. A USB uplink that is still live keeps the node a
gateway: the hook then keeps gateway state and NAT, cleans up only end0, and
serves the AP. The detector
and periodic uplink reconciliation call `manet_ap_mesh.py mesh`, which restores
a mesh-capable AP candidate's original band and current mesh configuration.
AP-only hardware is stopped instead. Returning to AP uses hostapd's preparation
helper to withdraw mesh roles and detach bat0 before changing interface type.

Reconciliation recognizes carrier-bearing br0 ports as wired EUDs: it never
DHCP-probes, detaches or flushes them. A USB uplink can remain the gateway while
end0 serves a wired EUD. The detector, off hook and dispatcher serialize policy
changes; a pending detector waits with a bound, while reconciliation skips a
busy pass and retries later. Unplug events recheck carrier after taking the
lock so queued stale events cannot undo a new cable connection.
