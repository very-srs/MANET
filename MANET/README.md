> **Everything you need is in the provisioning directory**
>
> The files in this repository are under active development. They frequently contain breaking bugs, untested changes, or active debugging.
>
> When you flash a device, it downloads the current stable release automatically.
>
> For hardware, flashing, operation, and troubleshooting, see the [field guide](https://very-srs.github.io/MANET/).

---

### Feature Roadmap

Verification happens on the CM4. A feature under **Working** has run on
hardware. One under **In Testing** has code written, may have been bench tested, but has not yet been
proven enough to be "finished".

#### Working
- [x] Wireless EUD
- [x] Wired EUD
- [x] Auto EUD
- [x] EUD multicast over mesh
- [x] Host lookup without DNS
- [x] MediaMTX server
- [x] Automatic gateway selection
- [x] Zero-conf IP addressing
- [x] Tri-band mesh (802.11ah HaLow, 802.11ax 2.4 and 5 GHz)
- [x] HaLow on a USB dongle (MM8108) or an SPI card (MM6108)
- [x] Status page, open to connected clients
- [x] Management UI behind the install-time admin password
- [x] Mesh-wide configuration change, staged over Alfred with a per-node ACK
- [x] Supply voltage and throttling reported on the status page and login banner
- [x] Region-aware HaLow channel and bandwidth selection
- [x] Over-the-air updates from GitHub releases, including the kernel, modules,
      and firmware on CM4
- [x] Automatic channel selection: one agreed Wi-Fi channel plan across all
      connected radios
- [x] 5 GHz radar (DFS) channels: the third receive chain checks a channel for
      radar before the mesh moves onto it, and the mesh agrees a fallback
      channel to move to if radar appears
- [x] Built-in Wi-Fi as an internet uplink, picked from a scan list in the web UI
- [x] ATAK on a connected phone: radio markers on the map, the radio as the
      phone's GPS source, and chat with the radio. On by default.

#### In Testing
- [ ] Mumble server: the server launches, migration is not tested
- [ ] Partition healing: HaLow-assisted recovery, rotating Wi-Fi lobby fallback,
      and reconciliation when isolated groups reconnect. Limp mode also remains
      in testing.
- [ ] Radar handling on a hand-set DFS channel, with automatic channel selection off
- [ ] In-mesh time synchronization: verified GPS/Internet sources, registry
      discovery, and periodic client refreshes. Drift and source transitions
      still need CM4 bench measurement.
- [ ] ATAK with several phones on one radio, across several radios, and with a
      TAK server
- [ ] Push-to-talk voice over the mesh (Lyra codec). Verified one way between two
      nodes at 0% loss. A two-way test needs a second OpenVLM board. A headset with a
      dynamic microphone needs an external mic preamp ahead of the OpenVLM.
- [ ] Operator setup scripts run once at first boot. Only basic test scripts have been tested.
- [ ] Self-rollback after a mesh key or SSID change
- [ ] External LED/button connectivity indication: direct-neighbor counts,
      deduplicated across radios, with separate disconnected/unavailable states.
      Software tests pass. Pin wiring and electrical behavior remain unverified.
- [ ] The Raven carrier board, which BuildsByShane is making for this project,
      and an enclosure for it

#### Future Work
- [ ] Further reduction in network traffic
- [ ] Positions without GPS, from Wi-Fi ranging (FTM) between radios. Ranging
      works on the MT7916, and the positioning protocol is in design.
- [ ] Physical interaction (buttons, knobs). The existing button and LED scripts
      require explicit harness configuration and are disabled by default while
      pin wiring is unfinished. Further controls are planned as
      a dual rotary encoder for power, talk group, and headset volume. Talk-group
      switching already works from a script: write `voice_channel` to `/etc/mesh.conf`
      and send `mesh-voice` a SIGHUP, which retunes in place instead of restarting
