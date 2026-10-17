# Guide coverage and source ownership

Use this map when behavior changes. Each outcome chapter has Marine, User
and Developer views. Current source and evidence establish behavior; old
handoffs and the feature roadmap are context. Distinguish implemented,
packaged and hardware-verified behavior.

| Area | Outcome chapters | Canonical reference |
|---|---|---|
| Purpose, architecture and boards | [Overview](content/overview.md), [hardware](content/hardware.md) | [Project README](../../README.md) |
| Windows/Linux imaging and setup | [Flash](content/flash.md), [first boot](content/first-boot.md) | [Provisioning](../../MANET/provisioning/README.md), [internals](../provisioning-internals.md) |
| Enumeration, firmware, kernel and overlays | [Hardware](content/hardware.md), [development](content/development.md) | [Kernel record](../kernel-6.18-morse-port.md), [firmware](../node-tools-internals.md#mt7916-firmware-selection) |
| EUD/AP, DHCP, DNS and discovery | [Connections](content/connect.md) | [Discovery](../node-tools-internals.md#local-eud-discovery), [dispatcher](../../MANET/networkd-dispatcher/README.md) |
| BATMAN, ACS, tourguides and limp mode | [Mesh](content/mesh.md), [radios](content/radios.md) | [Agreement/recovery](../node-tools-internals.md#channel-agreement-and-recovery) |
| IPv4 claims, conflicts, IPv6 and names | [Addresses](content/addresses.md) | [Allocation](../node-tools-internals.md#ipv4-allocation-and-collision-recovery) |
| Uplinks, gateways, NAT and tethers | [Internet](content/internet.md) | [Gateway selection](../node-tools-internals.md#gateway-choice-and-uplink-speed) |
| Alfred, identity, freshness and status | [Dashboard](content/dashboard.md), [automation](content/automation.md) | [Data management](../node-tools-internals.md#data-management) |
| Authentication, ACK/apply/rollback | [Settings](content/settings.md), [radios](content/radios.md) | [Configuration](../node-tools-internals.md#mesh-configuration-push) |
| PTT, talk groups, codecs and audio | [Voice](content/voice.md) | [Voice](../node-tools-internals.md#voice), [Lyra](../../MANET/lyra_arm64/README.md) |
| MediaMTX, Mumble, VIPs and Syncthing | [Services](content/services.md) | [Elections](../node-tools-internals.md#service-elections), [file sync](../../MANET/node_tools/README.md#file-synchronization) |
| GPS/NTS/chrony and peer time | [Time](content/time.md) | [Time internals](../node-tools-internals.md#time-synchronization), [controller](../../MANET/node_tools/mesh-time-sync.py) |
| ATAK, CoT, manual points and GNSS | [Location](content/location.md) | [ATAK](../node-tools-internals.md#atak-runtime-and-package-defaults), [phone selection](../../MANET/node_tools/manet_phone.py) |
| Experimental ranging/position solving | [Location](content/location.md) | [Positioning](../../MANET/node_tools/manet-positioning.py), [tool map](../node-tools-map.md#location-and-experimental-positioning) |
| Measurements, exports and limits | [Measurements](content/measurements.md), [storage](content/storage.md) | [Management](../../MANET/node_tools/manet_manage.py), [storage](../node-tools-internals.md#bounded-logs-and-measurements) |
| LEDs, GPIO, power and shutdown | [Power](content/power.md) | [LED internals](../node-tools-internals.md#leds), [hardware](../../MANET/node_tools/README.md#hardware-support) |
| Cloning and SSH recovery | [Power](content/power.md), [troubleshooting](content/troubleshooting.md) | [Recovery](../../MANET/node_tools/README.md#recovery--identity) |
| Logs, retained state and OS cleanup | [Storage](content/storage.md) | [Retention](../node-tools-internals.md#bounded-logs-and-measurements), [cleanup](../../MANET/node_tools/manet-os-cleanup.py) |
| Operator scripts and failures | [Customization](content/customization.md) | [Additional scripts](../../MANET/provisioning/additional-scripts/README.md) |
| Payload scope, activation/recovery | [Updates](content/updates.md) | [Field update policy](../node-tools-internals.md#field-update-policy), [packages](../../MANET/install_packages/README.md) |
| Builds, tests, versions and releases | [Development](content/development.md) | [Contributing](../../CONTRIBUTING.md), [packaging](../../MANET/packaging/README.md), [kernel builds](../../kernel/build/README.md), [releases](../../MANET/releases/README.md), [tests](../node-tools-internals.md#tests), [binaries](../../MANET/binaries_arm64/README.md) |
| Runtime ownership and tool inventory | [Automation](content/automation.md) | [Tool map](../node-tools-map.md), [orchestration](../node-tools-internals.md#core-orchestration) |
| Documentation and publication | [Maintenance](README.md) | [Topics](topics.json), [builder](build.py), [prompts](assets/prompts.json) |

## Boundaries to preserve

- CM4 is the verification target. Other boards do not inherit its results.
  A complete verified assembly specification and external GPIO harness
  wiring are not yet provided.
- Ranging/positioning remains experimental and default-off. The production
  helper/capture boundary needs implementation and hardware evidence.
- Update archives can carry any component. Kernel boot-set activation and
  recovery still need implementation and validation; that is not an archive
  category restriction.
- Clock qualification is not an absolute UTC accuracy bound. Long-duration
  drift/source-loss measurements remain separate hardware evidence.
- Service VIP movement and database synchronization do not guarantee live
  session continuity or lossless partition merges.
- A checkout includes package builders and kernel recipes. Downloaded source
  trees and generated overlays remain local. Document pinned inputs and
  setup commands so contributors can create them without private files.

Add new evidence to the owning reference and revise affected guide tiers.
Add future areas here even when no new tool filename is introduced. Keep
common operations in outcome chapters, concise file roles in the tool map,
and algorithms in Developer. Cross-link new chapters from the topics that
introduce them.

The build checks local page/heading/asset links, all three tiers and tool
inventory. Browser review checks navigation, small screens, offline and
no-JavaScript behavior and illustrations. Neither check proves hardware
behavior or that prose still agrees with changed implementation.
