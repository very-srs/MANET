# Feature list

## Marine

![Crayon drawing of three friends far apart, each with a green radio. Orange dashed lines hop from radio to radio over a hill. A cell tower in the distance has a big red X on it.](../assets/about-crayon.png)

### What your radio can do

Your radio does a lot of jobs by itself. Here are the big ones.

- **Find friends.** Radios find each other and pass messages along.
- **Pick a quiet channel.** Radios agree where to talk, and move together when it gets noisy.
- **Hook up your phone.** Use a cable or the radio's own Wi-Fi.
- **Talk.** Hold the button on your headset and your whole talk group hears you.
- **Show the map.** ATAK on your phone shows where your friends are.
- **Share internet.** One radio with internet can share it with everyone.
- **Keep the same time.** The radios agree on the time, even with no internet.
- **Look inside.** The dashboard shows your radios and how well they hear each other.
- **Get new software.** Radios update themselves, and go back if the update fails.

New radio? Start with [flashing](flash.md).

## User

### How to read this list

This page lists what MANET radios do, grouped by area. Each item links to
the chapter that explains it. Items that are off until you turn them on say
so. The other chapters describe the same features in more detail, with
setup steps and what to expect.

### The mesh

- Radios with the same mesh settings find each other and build the network
  themselves. There is no router, central server, or internet connection
  involved. See [how the mesh works](mesh.md).
- Traffic hops from radio to radio, so two radios out of range of each other
  can still reach each other through the radios between them.
- Radios link over 2.4 GHz Wi-Fi, 5 GHz Wi-Fi, and HaLow. HaLow reaches
  farther and gets through trees and walls better, at lower speed. All three
  carry the same network, and each message takes the best working path.
- When a radio turns off or a link fails, traffic takes another path.
- Mesh links are encrypted with the shared mesh key (WPA3 SAE). The flasher
  can generate a strong key for you.
- Separated groups join back into one mesh when any radio path reconnects
  them. HaLow carries this recovery when it works. Without HaLow, chosen
  radios visit shared meeting channels so the groups can find each other.
  See [rejoining after separation](mesh.md#user-rejoining-after-separation).
- A radio that shuts down normally tells the others it is leaving, so they
  stop counting on it for shared jobs.

### Channels and radar

- With automatic channel selection (ACS), radios share what they measure and
  agree on one Wi-Fi channel plan. Radios connected only by HaLow still take
  part in the same decision. See [choosing channels](mesh.md#user-choosing-channels).
- Each radio watches the channel it is on. The mesh only looks at other
  channels when a radio reports lasting interference, meaning other traffic
  fills the channel at least 60% of the time.
- A radio that starts on a different channel moves to the others' channel as
  soon as HaLow connects it, without waiting for a vote.
- When every measured channel is jammed, the mesh takes the least bad one. If
  most radios report jamming, they drop to slower, sturdier Wi-Fi rates to
  keep their links up. This is limp mode, described in the
  [runtime reference](../../../MANET/node_tools/README.md#channel-selection--jamming-detection).
- Both Wi-Fi meshes use 20 MHz channels, which reach farther. Nearby radios
  carry about 140 to 180 Mbit/s over the 5 GHz mesh at this width.
- When a CM4 checks other channels, its 5 GHz card lends its third antenna as
  a background listener, so the mesh keeps running. See
  [the third antenna](mesh.md#user-the-third-antenna-and-radar-channels).
- The mesh can use quiet radar (DFS) channels. It only does so when every
  connected radio supports them, the channel measures better, every radio has
  checked it for radar, and the group has agreed a backup channel.
- If radar appears, the radio that hears it stops transmitting at once, the
  mesh moves to the backup channel, and it stays off radar channels for
  30 minutes.
- HaLow offers the channels and widths of your region. New US radios start on
  channel 10 at 907 MHz, 2 MHz wide. New EU radios start on channel 1 at
  863.5 MHz, 1 MHz wide.
- The country code you choose when flashing sets the Wi-Fi rules, and the
  matching HaLow region follows from it.

### Phones and computers

- Connect a phone, laptop, or other end user device (EUD) to its radio with an
  Ethernet cable, including a USB Ethernet adapter on a phone, or through the
  radio's own Wi-Fi access point. See [connect your device](connect.md).
- Three connection modes: **Wired**, **Wireless**, and **Auto**. Auto serves
  Wi-Fi until a wired device appears, then returns that radio to the mesh.
  With automatic channel selection on, Auto serves Wi-Fi only on a gateway
  radio. See [choose an available connection](connect.md#user-choose-an-available-connection).
- The access point runs on 5 GHz at low power. Every radio's access point
  uses the base name you choose when flashing plus four characters unique to
  that radio, such as **EUD-5cc2**.
- Each radio gives its own devices their addresses, name lookups, and a local
  gateway. A device's setup requests never leave its radio.
- **[manet.local](http://manet.local/)** always opens the dashboard of the
  radio your device is connected to.
- A device on one radio can reach devices on every other radio by their mesh
  addresses.
- You choose how many devices each radio serves when flashing. The flasher
  shows how many radios and devices your network range can hold.

### Addresses and names

- Radios pick their own addresses from the shared range, with no central
  server. When two groups that chose overlapping addresses meet, the radios
  find the overlap and one of them moves. See [addresses and names](addresses.md).
- Mesh ranges from /16 to /28 are supported.
- IPv6 addresses configure themselves on every radio, using one /64 prefix
  that every radio announces.
- Each radio's name, such as `mesh-1234`, comes from its hardware and stays
  the same when it is reflashed. Every radio knows the other radios' names.
- Changing the shared address range moves every radio, its devices, and the
  shared service addresses to the new range together.
- A storage card copied onto different hardware gets its own name and
  addresses instead of duplicating the original radio. See [power and recovery](power.md).

### Internet sharing

- Any radio with internet automatically becomes a gateway for the mesh. Local
  mesh traffic, voice, and services keep working without internet. See
  [share an internet connection](internet.md).
- A radio can get internet from Ethernet to a router, a USB Ethernet adapter, a
  USB phone tether or cellular modem, or one of its built-in Wi-Fi radios.
- Ethernet works out by itself whether it leads to a router or to your device.
- A connection only counts as internet after a real check succeeds. A hotel
  sign-in page or a router with no service fails that check.
- With several working connections, a radio prefers Ethernet, then a phone
  tether or modem, then Wi-Fi, and switches to the next one if its connection
  fails.
- **Manage → Uplink** scans for nearby Wi-Fi and connects to an external Wi-Fi router or
  hotspot. It supports WPA2 and WPA3 personal passwords. See
  [use nearby Wi-Fi](internet.md#user-use-nearby-wi-fi).
- Each radio remembers up to 32 working Wi-Fi networks and their passwords,
  on that radio only. **Forget** removes one.
- The scan list hides the mesh's own phone hotspots, which lead back into the
  mesh and cannot supply internet.
- With a 5 GHz Wi-Fi uplink, the radio's own phone hotspot shares that radio
  and follows the external network's channel.
- When several radios share internet, each radio picks a gateway from the
  mesh path and the advertised uplink speed. It ignores small, brief
  improvements so the route does not keep changing.
- Ethernet uplinks measure their speed with a small download. Tethers, modems,
  and Wi-Fi use an assumed figure.
- Devices on your own private network, such as a ZeroTier network, can reach
  the radios and their devices through the gateway. See
  [reach the mesh from your own network](internet.md#user-reach-the-mesh-from-your-own-network).

### Push to talk voice

- Talk over the mesh with a headset and push to talk (PTT) button plugged into
  the radio. Both flashers turn voice on by default. See [push to talk](voice.md).
- Voice uses a CM108-family USB audio device, intended for OpenVLM boards. An
  electret microphone works directly. A dynamic microphone needs a preamp.
- Radios offer 32 talk groups. Each radio picks its own group on the
  **Voice** tab, like the channel knob on a handheld.
- When two people talk at once, listeners hear both voices mixed.
- The default codec is Lyra, which needs little bandwidth. Opus is the
  alternative. Every radio must use the same codec, so the Voice tab changes
  it across the whole mesh.
- The **GAIN** card sets receive gain from 0 to 24 dB with a limiter, sets the
  headset volume, and shows how loud your microphone is without transmitting.
- A high-pass filter cleans up transmitted audio by default. A band-pass
  filter and a three-band equalizer are available in the radio's
  [voice settings](../../../MANET/node_tools/README.md#transmit-shaping).
- Voice gets the highest priority on the radio links.
- An optional half-duplex mode refuses your PTT while someone else is talking.
- The Voice tab reports audio health, and a stalled audio path restarts by
  itself.
- Voice works with no internet connection.

### Location and ATAK

- Every radio runs an ATAK phone service by default. See
  [location and ATAK](location.md).
- Phones connect by Ethernet or the radio's Wi-Fi. Several phones can share
  one radio.
- A radio with a GPS receiver feeds its position to ATAK as external GPS
  input. With the phone set up as the chapter describes, everything ATAK
  shares uses the radio's fix.
- The radio finds ATAK phones on its own from ATAK's standard **Default Mesh
  SA** output. There is no per-radio phone setup or IP address to enter.
- ATAK shows the radio as a contact, **MANET mesh-XXXX**, for manually sending it a location
  point when GPS is bad. A custom callsign can replace the name.
- When several phones share a radio, anyone can choose which phone controls
  location from the status page, without the admin password. See
  [choose who sets the group's location](location.md#user-choose-who-sets-the-groups-location).
- A radio without GPS uses the controlling phone's GPS and shows it on the
  status page.
- A point you send corrects a bad GPS position. The radio holds it until you
  move away from it or release it from the status page.
- A safety net replaces the position in phone reports sent to other radios
  with the radio's trusted position, for phones set up without external GPS.
  Phones using a TAK server are left alone.
- A radio with nobody attached still shows on the team's map under its
  hostname or callsign, with a radio symbol.
- The radio warns in ATAK when its GPS has no fix, never shares a position it
  suspects is wrong, and reports when the radio's and phone's GPS disagree by
  more than 50 meters.

### Shared services

- MediaMTX hosts media streams on one radio chosen by the mesh, at a fixed
  service address. It is off by default. See [shared services](services.md).
- Mumble hosts a voice server for Mumble apps the same way. It is off by
  default and separate from push to talk.
- Syncthing keeps a shared folder in step between radios, including Mumble's
  saved accounts and channels.
- The mesh chooses each service's host from how well connected the radios
  are, keeps the current host unless another is clearly better, and moves the
  service when its host leaves. The old host keeps serving until the new one
  is ready.
- The service address stays the same when the host changes, so apps can
  reconnect to the same address.

### Time

- Radios take time from a GPS receiver, from internet time servers with
  authenticated time (NTS), or from other radios. See [time and GPS clocks](time.md).
- With no GPS or internet anywhere, the radios agree on one clock a few minutes
  after power-up.
- A radio that had GPS or internet time is preferred over one that only
  learned the time from other radios.
- When two groups meet, the smaller group takes the larger group's time. A
  group with GPS or internet time always wins.
- The time service sleeps between checks. It wakes when a radio joins, when
  GPS or internet time appears, once a day, or when another radio asks.
- A clock jump of more than 10 seconds cancels settings changes that were sent
  but not yet applied, and restarts any channel vote.
- Timed changes wait for the first verified clock. Ordinary mesh traffic does
  not.

### Dashboard and measurements

- The status page at [manet.local](http://manet.local/) needs no password. It
  shows the mesh map, every radio's addresses, uptime, radio links, attached
  devices, hosted services, GPS, and this radio's MANET version. See
  [read the dashboard](dashboard.md).
- The status page works on a phone and refreshes about every 15 seconds.
- Anyone can label their radio with an operator name or callsign from the
  status page.
- The **ATAK PHONES** panel lists this radio's phones and which one controls
  location.
- The **Mesh recovery** panel explains the observed and agreed channels,
  acknowledgements, recovery holds, and the last verified clock source.
- Power and throttling warnings appear on the status page and the SSH login
  banner.
- The password-protected management page has tabs for topology, radio
  settings, measurements, saved sessions, Wi-Fi uplink, voice, and node
  settings.
- **Measure** runs TCP and UDP throughput, jitter, packet loss, and ping tests
  between radios, in either direction. Throughput tests run for 5 to 300
  seconds. See [measure a connection](measurements.md).
- Results are saved as labeled sessions and download as JSON or CSV. The
  radio never deletes them to make room. A full storage budget stops new tests
  until you remove old sessions.

### Settings and administration

- One admin password covers the whole mesh. Radios accept commands from each
  other only when they carry that password, and joining the mesh alone does
  not allow changes. See [change settings](settings.md).
- Local settings, such as connection mode and the hotspot password, apply at
  once on your radio only.
- Shared settings are staged on every radio, each radio acknowledges them,
  and all radios apply them together about 60 seconds after **Apply Now**.
  **Force Apply** skips the acknowledgement wait.
- After a change to the mesh name, key, or address range, a radio that loses
  all its peers restores its previous settings after five minutes.
- An apply interrupted by a restart finishes by itself from a saved record.
  An old command cannot be replayed to change settings again.
- **Radio config** turns interfaces on and off, sets Wi-Fi channels when ACS
  is off, sets HaLow channels, and changes Wi-Fi transmit power until the next
  restart. See [radio controls](radios.md).
- Management logins last up to 48 hours, and end at logout, reboot, or an
  admin password change.

### Security

- The dashboard and management page answer only devices attached to that
  radio. The router's network and other radios' devices cannot open them.
- Setup logs leave out passwords and keys.
- Background services run with restricted permissions.
- Settings commands between radios are encrypted. Settings that belong to one
  radio, such as its hotspot password, are never sent to the others.

### Flashing and first boot

- One small launcher for Windows or Linux downloads the matching flasher. See
  [flash a radio](flash.md).
- The Windows flasher is a step-by-step GUI that checks for the tools it
  needs and offers to install them. A console version is also available.
- The Linux flasher checks for its tools, offers to install them on Debian,
  Ubuntu, and Fedora, and fetches a working Raspberry Pi Imager if the system
  one is too old. It can write a batch of cards in one run.
- Supported targets are CM4 eMMC (through `rpiboot`), SD cards for Raspberry
  Pi boards, and Rock 3A.
- One form covers the mesh settings, region, connection mode, services, SSH
  account, and admin password. Saved settings work in both flashers.
- Your own setup scripts are checked and built into the image.
- Flashing installs the current stable release by default.
  `--development` selects the newest published build instead.
- First boot needs Ethernet with internet and steady power, and takes about
  ten minutes. See [the first boot](first-boot.md).
- Before installing anything, first boot checks that it can reach what it
  needs, for up to five minutes, and verifies every download.
- On the Waveshare carrier board, the onboard LEDs show whether setup
  finished. The SSH login banner reports the same result and names anything
  that failed.
- A failed setup retries after a reboot without reflashing.

### Updates

- Radios update from the stable GitHub release and verify each package before
  installing it. See [update a radio](updates.md).
- `sudo node-update.sh` updates a radio by hand.
- Both flashers turn automatic updates on by default, and **Node config** can
  turn them off. Radios check when their internet or a mesh gateway becomes
  available, after the clock first syncs, and once a day. Radios sharing one
  gateway spread their downloads out.
- Updates can carry kernels, drivers, firmware, and configuration changes, so
  a radio sealed in an enclosure is updated in place.
- A CM4 tries a new kernel on one restart, checks itself, and returns to its
  old kernel if the check fails. Kernel updates can be switched off per radio.
  See [new kernels on CM4 radios](updates.md#user-new-kernels-on-cm4-radios).
- An interrupted update resumes after a reboot, without internet.
- A radio that fails to install the same release three times goes back to its
  last good release, and a newer release can replace a stuck one.
- Each update runs the updater from the release being installed, so updater
  fixes take effect on that same update.
- `--development` tries the newest published build, and `--allow-downgrade`
  returns to an older stable release.
- Operating system package upgrades cannot replace the MANET kernel.

### Power, lights, and recovery

- The onboard LEDs show the setup result at every boot. See
  [power and shutdown](power.md).
- An optional external LED and button blink once for each radio in direct
  range.
- Supported boards report under-voltage and throttling, including events
  earlier in the same boot.
- The CPU scales with demand. A CM4 also has a speed limit to keep a sealed
  enclosure cool.
- `sudo poweroff` shuts a radio down cleanly and tells the mesh it is leaving.
- Logs survive a reboot and rotate so they do not fill the storage.
- A recovery service keeps SSH reachable on a radio with no screen or
  keyboard.

### Your own setup

- Setup scripts you add when flashing run once after setup finishes. The SSH
  banner reports their results. See [add your own setup](customization.md).
- Unfinished scripts resume after a power loss, and finished ones do not
  repeat.
- Each radio is a normal Debian 13 computer, so you can add software as you
  would on any Linux system.

### Hardware

- The Raspberry Pi Compute Module 4 on a Waveshare carrier board is the
  primary hardware. Rock 3A and Raspberry Pi 5 also work. A Raspberry Pi 4B
  supports HaLow only. See [hardware and accessories](hardware.md).
- Wi-Fi comes from a MediaTek MT7916 dual-band Wi-Fi 6 card. Radios run
  patched card firmware and fall back to the stock firmware if it fails.
- HaLow comes from a Morse Micro MM6108 SPI card for the 902 to 928 MHz band,
  or an MM8108 USB adapter, which also covers the EU 863 to 870 MHz band.
- A GPS receiver supplies position and time.
- An OpenVLM board connects the headset and PTT button.

### Experimental

- The project contains radio ranging and position estimation between radios.
  It is off by default and is not a replacement for GPS. See
  [understand the position you are using](location.md#user-understand-the-position-you-are-using).

## Developer

### Where each feature lives

This tier maps each feature area to the mechanism that implements it, the
chapter that explains that mechanism, and the source that owns it. The User
list is the inventory of behavior. The [node-tool map](../../node-tools-map.md)
lists every runtime file, and the [coverage map](../coverage.md) lists the
canonical reference for each area.

| Area | Mechanism | Explained in | Main source |
|---|---|---|---|
| Forwarding | 802.11s mesh links on the MT7916 and Morse HaLow radios join batman-adv (BATMAN V) in `bat0`. The `br0` bridge joins `bat0` to the local EUD side. | [Runtime architecture](overview.md#developer-runtime-architecture) | [batman-if-setup.sh](../../../MANET/node_tools/batman-if-setup.sh), [manet-mesh-supplicant.py](../../../MANET/node_tools/manet-mesh-supplicant.py) |
| Peer knowledge | Protobuf identity and telemetry records travel over Alfred on `br0`. Each node joins them into a local registry and ages entries on its own boot clock. | [How a node learns about its peers](overview.md#developer-how-a-node-learns-about-its-peers) | [encoder.py](../../../MANET/node_tools/encoder.py), [manet_registry_builder.py](../../../MANET/node_tools/manet_registry_builder.py) |
| Addresses | Nodes claim IPv4 blocks in their Alfred identity after an observation window, exclude overlapping ranges, and settle conflicts by MAC. | [Discover, claim, reconcile](addresses.md#developer-discover-claim-reconcile) | [mesh-ip-manager.sh](../../../MANET/node_tools/mesh-ip-manager.sh), [mesh-ip-startup.py](../../../MANET/node_tools/mesh-ip-startup.py) |
| Channels | The lowest ready node in the connected BATMAN component coordinates. A strict majority of the frozen membership commits a plan over encrypted Alfred. Home-channel RF trouble opens scanning, and a three-round hold-down limits preference moves. | [One connected component, one channel decision](mesh.md#developer-one-connected-component-one-channel-decision) | [mesh-channel-agreement.py](../../../MANET/node_tools/mesh-channel-agreement.py), [channel-election.sh](../../../MANET/node_tools/channel-election.sh) |
| DFS and third chain | A reserved MT7916 receive chain measures 5 GHz candidates and runs background CAC. Unanimous leases admit a DFS channel, and a guardian evacuates to the agreed fallback on radar. | [One connected component, one channel decision](mesh.md#developer-one-connected-component-one-channel-decision) | [manet_bgobs.py](../../../MANET/node_tools/manet_bgobs.py), [manet_dfs.py](../../../MANET/node_tools/manet_dfs.py), [manet_dfs_safety.py](../../../MANET/node_tools/manet_dfs_safety.py) |
| Partitions and limp mode | Working HaLow carries recovery. Without it, an elected tourguide visits a clock-derived rendezvous rotation. The quorum checker returns isolated nodes to discovery, and limp mode drops to legacy rates under mesh-wide jamming. | [Recovery and local role constraints](mesh.md#developer-recovery-and-local-role-constraints) | [tourguide-manager.sh](../../../MANET/node_tools/tourguide-manager.sh), [manet_rendezvous.py](../../../MANET/node_tools/manet_rendezvous.py), [limp-mode-manager.sh](../../../MANET/node_tools/limp-mode-manager.sh) |
| Client access | Ethernet is classified as uplink or client. DHCP and local discovery stay inside the bridge boundary, and the web firewall admits only the local pool. | [Layer 2 topology and address ownership](connect.md#developer-layer-2-topology-and-address-ownership) | [ethernet-autodetect.sh](../../../MANET/node_tools/ethernet-autodetect.sh), [manet-dhcp-isolation.py](../../../MANET/node_tools/manet-dhcp-isolation.py), [manet-ui-firewall.sh](../../../MANET/node_tools/manet-ui-firewall.sh) |
| Internet | Uplinks qualify by an HTTP 204 probe and rank by kind. Gateways advertise measured or assumed capacity, and each node picks a gateway with hysteresis and keeps its own default route. | [Selecting a Linux default route](internet.md#developer-selecting-a-linux-default-route) | [manet-uplink-dispatch.sh](../../../MANET/node_tools/manet-uplink-dispatch.sh), [gateway-route-manager.sh](../../../MANET/node_tools/gateway-route-manager.sh), [manet_wifi_uplink.py](../../../MANET/node_tools/manet_wifi_uplink.py) |
| Settings | Password-authenticated, encrypted Alfred commands with replay protection carry staged changes. Authenticated acknowledgements gate a scheduled activation. A durable journal resumes jobs, and a local trial rolls back a change that strands the node. | [Transaction authority and lifecycle](settings.md#developer-transaction-authority-and-lifecycle) | [manet_admin.py](../../../MANET/node_tools/manet_admin.py), [mesh-config-sync.py](../../../MANET/node_tools/mesh-config-sync.py), [manet_config_journal.py](../../../MANET/node_tools/manet_config_journal.py) |
| Voice | GStreamer pipelines send Lyra or Opus as multicast RTP, one port per talk group. Presence beacons prepare receivers before speech, and each talker gets its own decode path into a mixer. | [Pipeline and transport](voice.md#developer-pipeline-and-transport) | [mesh-voice.py](../../../MANET/node_tools/mesh-voice.py) |
| ATAK | A phone is admitted when its mesh SA or unicast traffic arrives from this radio's local side. The service feeds external GPS, exchanges CoT, and maintains a kernel rule that rewrites positions in outgoing mesh SA. | [Admission is a local network proof](location.md#developer-admission-is-a-local-network-proof) | [manet-atak.py](../../../MANET/node_tools/manet-atak.py), [manet_phone.py](../../../MANET/node_tools/manet_phone.py), [manet_cot.py](../../../MANET/node_tools/manet_cot.py) |
| Services | Hosts are ranked by mean BATMAN throughput with incumbent bias and deterministic ties. Winners claim reserved virtual addresses, and the old host serves until it sees the new claim. | [Ranking and stable ownership](services.md#developer-ranking-and-stable-ownership) | [mesh-service-election.py](../../../MANET/node_tools/mesh-service-election.py), [manet_service_handover.py](../../../MANET/node_tools/manet_service_handover.py) |
| Time | One controller owns chrony and its sources: GPS, NTS, and peers. It runs orphan mode on demand when no source is in reach. A merge step resolves meeting groups, and clock epochs void timed commands after a jump. | [One controller owns chrony](time.md#developer-one-controller-owns-chrony) | [mesh-time-sync.py](../../../MANET/node_tools/mesh-time-sync.py), [manet_clock.py](../../../MANET/node_tools/manet_clock.py) |
| Dashboard and measurements | One web service serves the public status APIs and the session-protected management interface, under a nonce-based content security policy. Measurement jobs run locally against admission and storage budgets. | [Local radio status page](dashboard.md#developer-local-radio-status-page) | [mesh-status.py](../../../MANET/node_tools/mesh-status.py), [manet_manage.py](../../../MANET/node_tools/manet_manage.py) |
| Provisioning | The flashers render a first-boot script pinned to one release. First boot checks its resources, installs the archive, cleans the base image, and records a verdict for the LEDs and banner. | [Provisioning has distinct stages](first-boot.md#developer-provisioning-has-distinct-stages) | [firstrun.sh.template](../../../MANET/provisioning/firstrun.sh.template), [radio-setup.sh](../../../MANET/node_tools/radio-setup.sh) |
| Updates | The updater selects a release, verifies the manifest and archive, hands off to a newer release's updater, and installs with durable recovery state. A CM4 kernel payload runs as a watchdog-guarded trial boot. | [Installation and recovery](updates.md#developer-installation-and-recovery) | [node-update.py](../../../MANET/node_tools/node-update.py), [manet_release.py](../../../MANET/node_tools/manet_release.py), [manet_kernel_update.py](../../../MANET/node_tools/manet_kernel_update.py) |
| Power and identity | The provisioning verdict drives the LEDs. A shutdown tombstone withdraws service eligibility, and clone detection resets identity on new hardware. | [Provisioning state and connectivity are separate producers](power.md#developer-provisioning-state-and-connectivity-are-separate-producers) | [manet-led-status.sh](../../../MANET/node_tools/manet-led-status.sh), [mesh-shutdown.sh](../../../MANET/node_tools/mesh-shutdown.sh), [mesh-clone-identity.sh](../../../MANET/node_tools/mesh-clone-identity.sh) |
| Positioning (experimental) | Ranging, solving, and GNSS consistency checks exist behind a disabled service and an explicit hardware boundary. | [Experimental ranging has an explicit hardware boundary](location.md#developer-experimental-ranging-has-an-explicit-hardware-boundary) | [manet-positioning.py](../../../MANET/node_tools/manet-positioning.py) |

### Switches and defaults

Most features have no switch. The ones below are set in `/etc/mesh.conf`
unless noted. Shared keys travel through the settings transaction and must
agree across the mesh. Local keys stay on one radio. The
[runtime reference](../../../MANET/node_tools/README.md) documents the
full key set, including the voice tuning keys.

| Key | Default | Scope | Controls |
|---|---|---|---|
| `eud` | Chosen when flashing, starting at `wired` | Local | Wired, Wireless, or Auto client connection. |
| `acs` | `y`, except Wireless mode, which forces `n` | Shared | Automatic channel selection or fixed channel plans. |
| `regulatory_domain` | Chosen when flashing | Shared | Wi-Fi country rules and the derived HaLow region. Radio changes need a reboot. |
| `ipv4_network` | Chosen when flashing | Shared | The mesh range, /16 to /28. |
| `max_euds_per_node` | Chosen when flashing | Fixed | Block width and DHCP pool size. |
| `mtx`, `mumble` | `n` | Shared | MediaMTX or Mumble hosting. Enable it on every radio, because a radio with it off can still win the election. |
| `auto_update` | `y` in the flashers | Shared | Automatic update checks. |
| `voice` | `y` in the flashers | Local | The push to talk daemon. `voice_channel` starts at 1. |
| `voice_codec` | `lyra` | Mesh-wide | Change it from the Voice tab, never by hand. |
| `atak` | `y` | Local | The ATAK phone service. `atak=n` is the only off switch. |
| `positioning` | Off | Local | Experimental ranging only. It does not select MT7916 firmware. |
| `uplink_routed` | Unset | Gateway | Private ranges the gateway forwards without NAT. |
| `operator_name` | Empty | Local | The label set from the status page. |
| `enabled` in `/etc/manet-kernel-update.conf` | `y` on CM4 | Local | CM4 kernel field updates. Tools updates continue with `n`. |

### Board boundaries

The CM4 is the verification target, and some features depend on it. The
third receive chain observer, background CAC, and DFS channel use need the
CM4's MT7916 with the patched driver and firmware. Kernel field updates run
only on the CM4, because the Rock 3A bootcount and reset path is unverified.
A Rock 3A takes its kernel from the install archive when flashed, and its
tools archive carries no kernel. RPi5 and Rock 3A radios still join DFS
decisions as unsupported members, which keeps a mixed mesh on regular
channels. See [hardware](hardware.md#developer-kernel-firmware-and-userspace-form-one-bring-up-chain)
for the bring-up chain on each board.
