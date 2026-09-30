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

## When the interface becomes routable

Once DHCP has finished and a route exists, gateway and NAT state are
reconciled. Carrier alone does not mean the interface can reach anything, which
is why this is a separate step.

After reconciliation, an opted-in node queues `manet-auto-update.service`
without waiting for it. This oneshot rechecks `auto_update` and the selected
uplink's IPv4 default route before running the updater. It excludes mesh
bridges and does not require ICMP access. Systemd serializes concurrent starts.
There is no cron job or timer; routable events trigger automatic checks.
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

In auto mode, a wired EUD takes priority over the wireless AP. The detector
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
