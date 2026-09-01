> **Everything you need is in the provisioning directory**
>
> The files in this repository are under active development. They frequently contain breaking bugs, untested changes, or active debugging.
>
> When you flash a device it will pull down the most recent code automatically.

---

### Feature Roadmap

Verification happens on the CM4. A feature under **Working** has run on
hardware; one under **In Testing** is complete in the tree but has not yet been
proven on a bench pair.

#### ✅ Working
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

#### 🧪 In Testing
- [ ] Mumble Server - server launches, migration not tested
- [ ] Automatic channel selection
- [ ] Partition healing (tourguide) and limp mode
- [ ] In mesh NTP
- [ ] Push-to-talk voice over the mesh (Lyra codec) — needs a mic preamp for a dynamic-mic headset
- [ ] Operator setup scripts run once at first boot — the node-side runner is verified, the flash-time half needs a reflash
- [ ] Over-the-air tools update on Ethernet carrier — works manually, testing on carrier
- [ ] Self-rollback after a mesh key or SSID change

#### 📅 Future Work
- [ ] Further reduction in network traffic
- [ ] Enclosure selection
- [ ] Physical interaction (buttons, knobs) — the button and LED scripts are in
      the tree but exit immediately, since the pin wiring is not finalised
- [ ] Display or status indication
