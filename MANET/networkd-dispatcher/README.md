# Networkd-dispatcher hooks

networkd-dispatcher runs a script when systemd-networkd reports an interface
state change. This directory holds the hooks a MANET node uses to decide what a
cable means — an upstream uplink, a wired EUD port, or nothing — and to tear that
decision down when the cable goes away.

## What actually runs on a node

**Only the contents of `/etc/networkd-dispatcher/<state>.d/` are executed.**
A file named for the state, sitting directly in `/etc/networkd-dispatcher/`, is
not run by anything. Every builder stages this set through
`stage_dispatcher_hooks` in `MANET/packaging/lib-dispatcher.sh`, for the tools
tarball as well as the three install tarballs, so a corrected hook can be
delivered over the air instead of needing a reflash.

| Repo path | Installed as | Runs |
|---|---|---|
| `carrier.d/50-ethernet-detect` | `/etc/networkd-dispatcher/carrier.d/50-ethernet-detect` | **yes**, on carrier |
| `routable.d/50-manet-uplink` | `/etc/networkd-dispatcher/routable.d/50-manet-uplink` | **yes**, on routable |
| `off` | `…/off.d/50-gateway-disable`, and the same file again in `no-carrier.d/` and `degraded.d/` | **yes**, on all three states |
| `carrier` | `/root/networkd-dispatcher/carrier` | no — reference copy |
| `off` | `/root/networkd-dispatcher/off` | no — reference copy |
| `no-carrier`, `degraded`, `routable` | nothing installs them | **no** |

### carrier.d/50-ethernet-detect

Two jobs, in order:

1. `manet-uplink-dispatch.sh carrier "$IFACE"` — decides whether the interface
   that just got carrier is an uplink or a wired EUD port, and reconciles state
   to match.
2. **The only trigger for over-the-air updates.** When `/etc/mesh.conf` has
   `auto_update=` set to a true value and a ping out through `$IFACE` succeeds,
   it runs `node-update.sh --routine`. There is no cron job and no timer; a node
   checks for a new release when Ethernet comes up, and at no other time.

The `auto_update` test is `^auto_update=(y|yes|1|true)` case-insensitively. It
tested for `=1` until 2026-08-31, a value no writer has ever produced — both
flashers write `y`/`n`, the web UI writes `'y':'n'`, and `mesh-config-sync.py`
validates the key as exactly `y` or `n` — so `auto_update=y` did nothing on
every node from the day the hook was written. The pattern is liberal about a
hand-edited conf and cannot match `n`/`no`/`0`/`false`.

Note this hook does **not** filter on interface name. The reference copy under
`/root` returns early unless `$IFACE` is `end0`; the installed hook hands every
interface to `manet-uplink-dispatch.sh`, which does its own classification and
so also covers USB Ethernet and tethers.

### routable.d/50-manet-uplink

`manet-uplink-dispatch.sh routable "$IFACE"`. Reconciles gateway and NAT state
once DHCP has finished and a route exists — carrier alone does not mean the
interface can reach anything.

### off.d / no-carrier.d / degraded.d — 50-gateway-disable

One script installed three times, because all three states mean the same thing
to a node: the cable is gone or the link is only half up. It returns `end0` to
baseline directly rather than calling `manet-uplink-dispatch.sh` — stops
`dnsmasq`, flushes the interface, removes the generated `.network` files and
restores the default DHCP one, drops the wired-EUD dnsmasq config, and reverts
gateway and NTP state if this node held either.

It reloads networkd and reconfigures **only** `end0`. A full
`systemctl restart systemd-networkd` is deliberately avoided: reconfiguring
`wlan0`/`wlan2` on the way past kicks them out of `bat0`.

It returns early unless `$IFACE` is `end0`, so a wireless or USB interface
losing carrier does not run the wired teardown.

## Two scripts called `carrier`

`carrier` and `carrier.d/50-ethernet-detect` are **different scripts** and only
the second one runs. The reference copy calls
`ethernet-autodetect.sh --hotplug`; the installed hook calls
`manet-uplink-dispatch.sh carrier`. `off` is the same file in both places.

`no-carrier`, `degraded` and `routable` in this directory are three-line wrappers
around `manet-uplink-dispatch.sh <state>`. Nothing installs them, and their
states are covered by the `.d/` entries above.

## Adding or changing a hook

Change the file under `carrier.d/`, `routable.d/`, or `off`, and rebuild the
tarballs — every builder picks the set up from `stage_dispatcher_hooks`, so there
is one copy of each. The two generated hooks used to be heredocs duplicated
across the three install builders, which is how one wrong `grep` came to need
fixing in four places and how the rpi5 copy drifted from the other two.

`node-update.sh` extracts the tools tarball and nothing else; it does not run
`daemon-reload` or `udevadm`. Dispatcher hooks need neither — networkd-dispatcher
reads the directory on each event, so a replaced hook is live immediately.
