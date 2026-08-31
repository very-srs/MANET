# Networkd-dispatcher scripts

This directory contains the scripts that are triggered by networkd-dispatcher
when an ethernet or USB tether interface changes state.

### 1. carrier
* activated when an interface gets carrier
* calls manet-uplink-dispatch.sh to decide gateway or wired EUD behavior

### 2. off
* cleanup script that returns the node to a baseline when no ethernet connection is present

### 3. no-carrier / degraded
* same cleanup path as off for partially removed links

### 4. routable
* activated when networkd reports the interface as routable
* reconciles gateway/NAT state after DHCP or route changes

## What is actually active on a node

Only the contents of `/etc/networkd-dispatcher/<state>.d/` are executed. Those
come from `carrier.d/` and `routable.d/` in this directory, plus `off`
installed three times as `50-gateway-disable` for the `off`, `no-carrier` and
`degraded` states.

| Repo path | Lands as | Active |
|---|---|---|
| `carrier.d/50-ethernet-detect` | `/etc/networkd-dispatcher/carrier.d/50-ethernet-detect` | **yes** |
| `routable.d/50-manet-uplink` | `/etc/networkd-dispatcher/routable.d/50-manet-uplink` | **yes** |
| `off` | `…/{off,no-carrier,degraded}.d/50-gateway-disable` | **yes** |
| `carrier`, `off` | `/root/networkd-dispatcher/` | no, reference copies |

Note that `carrier` and `carrier.d/50-ethernet-detect` are **different
scripts**. The reference copy calls `ethernet-autodetect.sh --hotplug`; the
active hook calls `manet-uplink-dispatch.sh carrier`. Only the latter is
executed.

Every builder stages this set through `stage_dispatcher_hooks` in
`MANET/packaging/lib-dispatcher.sh`, for the tools tarball as well as the
install tarballs, so that a corrected hook can be delivered by an
over-the-air update rather than requiring a reflash.
