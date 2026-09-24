# Networkd-dispatcher hooks

networkd-dispatcher runs a script when systemd-networkd reports that an
interface changed state. These hooks are how a node decides what a cable
plugged into it means, and how it undoes that decision when the cable is pulled.

## When a cable gets carrier

The node works out whether the interface is an upstream uplink or a wired EUD
port, and configures itself to match. See
[Connectivity Modes](../../README.md#connectivity-modes) for what each of those
means.

This is also the only trigger for over-the-air updates. When `/etc/mesh.conf`
has `auto_update=` set to a true value and a ping out through the interface
succeeds, the node checks for a new release. There is no cron job and no timer,
so a node checks only when Ethernet comes up.

The `auto_update` test accepts `y`, `yes`, `1` or `true`, in any case, and
matches none of `n`, `no`, `0` or `false`.

Interface name is not filtered here, so USB Ethernet and phone tethers are
covered along with the built-in port.

## When the interface becomes routable

Once DHCP has finished and a route exists, gateway and NAT state are
reconciled. Carrier alone does not mean the interface can reach anything, which
is why this is a separate step.

The time service observes the selected uplink and starts internet time
synchronization through it. Only a verified chrony source sets the NTP flag
carried in normal Alfred telemetry; becoming a gateway alone does not set it.

## When carrier is lost

Losing carrier, or a link that only ever comes half up, returns `end0` to its
baseline. `dnsmasq` stops, the interface is flushed, the generated `.network`
files are replaced with the default DHCP one, the wired-EUD dnsmasq config is
dropped, and gateway and internet-NTP state are reverted if this node held
either. The hook leaves chrony control to the time service, which preserves a
usable local GPS source when the internet uplink disappears.

Only `end0` is reconfigured. A wireless or USB interface losing carrier does
not run the wired teardown.
