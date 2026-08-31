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

Only what is in `/etc/networkd-dispatcher/<state>.d/` runs. That comes from
`carrier.d/` and `routable.d/` here, plus `off` installed three times as
`50-gateway-disable` for the `off`, `no-carrier` and `degraded` states.

| Repo path | Lands as | Active |
|---|---|---|
| `carrier.d/50-ethernet-detect` | `/etc/networkd-dispatcher/carrier.d/50-ethernet-detect` | **yes** |
| `routable.d/50-manet-uplink` | `/etc/networkd-dispatcher/routable.d/50-manet-uplink` | **yes** |
| `off` | `…/{off,no-carrier,degraded}.d/50-gateway-disable` | **yes** |
| `carrier`, `off` | `/root/networkd-dispatcher/` | no — reference copies |

`carrier` and the active `carrier.d/50-ethernet-detect` are **different scripts**
that have both existed for a while: the reference copy calls
`ethernet-autodetect.sh --hotplug`, the active one calls
`manet-uplink-dispatch.sh carrier`. Only the second one runs.

Every builder stages this set through `stage_dispatcher_hooks` in
`MANET/packaging/lib-dispatcher.sh` — install **and** tools tarballs alike, so a
hook fix can go out over the air. The two `.d` hooks used to be heredocs
duplicated across the three install builders, which is why the `auto_update`
gate had to be corrected in four places, and why the rpi5 copy had drifted.
