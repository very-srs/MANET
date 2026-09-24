> **Everything you need is in the provisioning directory**
>
> The files in this repository are under active development. They frequently contain breaking bugs, untested changes, or active debugging.
>
> When you flash a device it will pull down the most recent code automatically.

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
- [x] MediaMTX Server
- [x] Automatic gateway selection
- [x] Zero-conf IP addressing
- [x] Tri-band Mesh (802.11ah, 802.11ax 2.4/5)
- [x] Status page, open to connected clients
- [x] Management UI behind the install-time admin password
- [x] Mesh-wide configuration change, staged over Alfred with a per-node ACK
- [x] Supply-voltage and throttling reported on the status page and login banner
- [x] Region-aware HaLow channel and bandwidth selection
- [x] Over-the-air tools update

#### In Testing
- [ ] Mumble Server - server launches, migration not tested
- [ ] Automatic channel selection: one agreed plan across all connected radios,
      bounded majority acknowledgements and straggler recovery. Automated tests
      pass; CM4 bench validation is waiting for hardware setup.
- [ ] Partition healing: HaLow-assisted recovery, rotating Wi-Fi lobby fallback
      and reconciliation when isolated groups reconnect. CM4 bench validation
      is pending. Limp mode also remains in testing.
- [ ] In-mesh time synchronization: verified GPS/Internet sources, registry
      discovery and periodic client refreshes. Drift and source transitions
      still need CM4 bench measurement.
- [ ] Push-to-talk voice over the mesh (Lyra codec). Verified one way between two
      nodes at 0% loss. A two-way test needs a second OpenVLM board. A headset with a
      dynamic microphone needs an external mic preamp ahead of the OpenVLM
- [ ] Operator setup scripts run once at first boot. Only basic test scripts have been tested.
- [ ] Self-rollback after a mesh key or SSID change
- [ ] External LED/button connectivity indication: direct-neighbor counts,
      deduplicated across radios, with separate disconnected/unavailable states.
      Software tests pass; pin wiring and electrical behavior remain unverified.
- [ ] Enclosure design

#### Future Work
- [ ] Further reduction in network traffic
- [ ] Physical interaction (buttons, knobs). The existing button and LED scripts
      require explicit harness configuration and are disabled by default while
      pin wiring is unfinished. Further controls are planned as
      a dual rotary encoder for power, talk group and headset volume. Talk-group
      switching already works from a script: write `voice_channel` to `/etc/mesh.conf`
      and send `mesh-voice` a SIGHUP, which retunes in place instead of restarting
