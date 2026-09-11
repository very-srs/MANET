# MANET Project

Software for building mesh radios out of single-board computers. Flash a card/emmc,
power the node on, and it finds the others and starts carrying traffic for
them. Nothing assigns it an address, picks its channel, or decides which node
hosts a video stream. The nodes work that out between themselves, and keep
working it out as they move and as the mesh splits and rejoins.

Routing is `batman-adv` at Layer 2, running the BATMAN V algorithm. The radios
are 802.11ax/ac/n on 2.4 and 5 GHz, plus 802.11ah HaLow, which reaches further 
at lower speed.

## What the nodes do on their own

Each node claims a block of IPv4 addresses for its own clients without
colliding with any other node, and IPv6 comes up over SLAAC. Nodes scan the 2.4
and 5 GHz bands, share what they found, and elect a channel together. Every
node computes the same answer from the same data, so there is no coordinator to
lose.  Any node that has Internet access becomes a gateway and shares that access 
with the rest of the nodes.

When a mesh splits in two, a node from each side takes turns hopping to a
common lobby channel to look for the other half, and the smaller partition
moves to rejoin the larger. When links start to degrade, a node says so, and
once more than half the mesh agrees, every node drops to the legacy 802.11
bitrates to keep the links alive.

Services are elected based on connectivity. The best-connected node hosts MediaMTX for
video, another serves time to the rest of the mesh, and if either goes away the
next election moves the service elsewhere. Push-to-talk voice runs across the
mesh from a headset plugged into the node, mixing everyone who is talking.

## Repository layout

All of it sits under `MANET/`.

* `provisioning/` flashes a card/emmc. `additional-scripts/` inside it holds your
  own setup scripts, embedded in the image and run once on the node.
* `node_tools/` is what runs on the node: the orchestrator, the web interface,
  voice, the registry builder, and the script that configures a fresh radio.
* `binaries_arm64/` holds prebuilt `alfred` and `batctl`, and a
  `wpa_supplicant` patched for HaLow.
* `lyra_arm64/` holds the Lyra voice codec plugin and its model weights.
* `systemd/`, `systemd-network/`, `udev/`, `networkd-dispatcher/` and `etc/`
  are the units, network files, and hooks installed onto the node.

## Supported Hardware

| Hardware | Support Level | Notes |
| :--- | :--- | :--- |
| **Compute Module 4 (CM4)** | Functional, primary dev target | Supports 802.11ax + HaLow. |
| **Raspberry Pi 4B** | Briefly tested, lacks pcie hardware | only useful for halow  |
| **Raspberry Pi 5** | Functional, out of date, not a focus | Supports 802.11ax + HaLow. |
| **Radxa Rock 3A** | Functional, not a focus | Supports 802.11ax + HaLow. |

The Pi 5 and Rock 3A both work, but they run too hot for a sealed radio
enclosure, which is the form factor this project targets. They are no longer the
focus of testing. The CM4 is. Expect fixes to land and be verified on CM4
first.

## Getting Started

### 1. Prerequisites
You will need a supported SBC and a Linux or Windows machine to flash from, and Ethernet Internet access for the SBC being flashed. 

See [/provisioning/README.md](MANET/provisioning/README.md) for detailed requirements and download links.

### 2. Provisioning a Node

#### On Windows: download one file

Download
**[Flash a Radio.cmd](https://raw.githubusercontent.com/very-srs/MANET/main/MANET/provisioning/Flash%20a%20Radio.cmd)** 
and double-click it.

There is nothing else to download. The
launcher fetches what it needs, and the GUI offers to install `rpi-imager` and, for a
CM4, `rpiboot` if this computer does not already have them. On first run `Flash a Radio.cmd` makes itself a
`MANET Flasher` folder beside where you put it and moves itself in, so your saved settings and
your own setup scripts stay in one place.

Windows shows a security warning the first time, because the file came from the internet.
Choose **Run**, then **Yes** when Windows asks for Administrator: writing to a card needs
it.

![Choosing the board on the first page of the flasher](docs/images/provisioning/flasher-1-board.png)

Six pages, in order: pick the board, check this computer for needed software, enter the mesh settings,
review any setup scripts of your own, choose the card to flash, and read the summary back before
anything is written.

Every page is shown in
[Windows: step by step](MANET/provisioning/README.md#windows-step-by-step).

> The older console script, `windows.ps1`, still works and does exactly the same thing.
> The GUI is a front end over it, so both produce an identical image.

#### On Linux: download one file

Download
**[flash-a-radio.sh](https://raw.githubusercontent.com/very-srs/MANET/main/MANET/provisioning/flash-a-radio.sh)**
and run it:

```bash
chmod +x flash-a-radio.sh
./flash-a-radio.sh
```

There is nothing else to download. The script fetches what it needs, and offers to
install any missing software with apt or dnf. On other distributions it prints the exact
command for your package manager instead of running it. On first run `flash-a-radio.sh`
makes itself a `manet-flasher` folder beside where you put it and moves itself in, so
your saved settings and your own setup scripts stay in one place.

Run it as yourself, not with `sudo`. It asks for your password at the points where it
needs to write to the card.

#### What you will be asked, on either host

* **EUD Connection**: Wired, Wireless (local AP), or Auto.
* **Optional Services**: MediaMTX, mesh voice (Mumble is untested).
* **Mesh Security**: SSID and SAE Password.
* **Network Settings**: CIDR blocks and addressing.

Settings can be named and saved, allowing you to load a saved config and flash many radios 
with the same configuration, rather than manually entering this into every node one by one.  
A saved configuration works in both flashers, so a mesh can be built from a mix of Windows and Linux machines.

*(Optional)* Place site-specific setup scripts in `additional-scripts/`. They
are sanity checked before anything is written to the card, embedded in the image, and run
**once as root on the node** after the node is configured for the first time: for static routes, organization SSH
keys, or additional custom configurations. See
[Additional setup scripts](MANET/provisioning/additional-scripts/README.md).

### 3. First Boot
Insert the storage media into the node and power it on. The `firstrun.sh` script will, over the course of a few reboots:
1.  Disable default setup wizards.
2.  Wait for internet connectivity (via Ethernet) to download the latest kernel and tools.
3.  Install necessary packages (`batctl`, `alfred`, `wpa_supplicant`, etc.).
4.  Configure the radio interfaces.
5.  Leave a working mesh node.
6.  Run any scripts supplied in `additional-scripts/`. Their outcome is reported on
    the SSH login banner. A failure there does not mark the node unprovisioned.

## Web Interface

Each node serves two things on port 80, reachable from a device connected to
that node (Ethernet or its AP), or over an SSH port-forward:

* **`http://<node>/`**: status page. Mesh topology, link throughput, per-node
  health and detail. No password.
* **`http://<node>/manage`**: management UI. Radio control, throughput and ping
  measurement, uplink credentials, and the mesh configuration
  form. Requires the **admin password** chosen at flash time.

Both are restricted to that node's own clients and localhost, not other radios,
not other radios' clients, and not the upstream LAN when the node is acting as a
gateway. Clients of that radio may access this page at `http://manet.local/` or its IP.

### Status page

Live mesh topology with per-link throughput, and a node list showing which radio carries
the best route to each peer.

![Mesh topology on the status page](docs/images/webui/status-topology.png)

Expanding a node gives its addresses, uptime, GPS info, battery state, every network
interface with the role it is playing, connected EUDs, and which services it is hosting.

![Per-node detail card](docs/images/webui/node-detail.png)

### Management UI

**Radio config**: bring each interface up or down and set TX power, per node or across
the whole mesh at once, plus the HaLow channel.

![Radio configuration tab](docs/images/webui/manage-radio-config.png)

**Measure**: run iperf3 and ping between any pair of nodes, in either direction, and
save the results against a labeled session so field tests can be compared later.

![Measurement tab](docs/images/webui/manage-measure.png)

See [Node Tools Documentation](MANET/node_tools/README.md) for the routes,
access-control layers, and what each management tab does.

## Connectivity Modes

A phone or compter reaches the mesh through whichever node it is connected to.
Those are End User Devices, EUDs throughout this documentation, and a node
handles them three ways:

* **Wired.** Over Ethernet. The node bridges the device onto the mesh, or acts
  as a gateway when the cable leads to the internet instead of an EUD.
* **Wireless.** The node runs a 5 GHz access point, separate from the mesh
  backhaul, for clients to join.
* **Auto.** The default. Wireless until an EUD is plugged in, then wired
  takes priority and the wireless access point turns off and that interface rejoins the mesh.

## Documentation
* [Provisioning Guide](MANET/provisioning/README.md)
* [Additional setup scripts](MANET/provisioning/additional-scripts/README.md)
* [Node Tools Documentation](MANET/node_tools/README.md)
* [Binary Details](MANET/binaries_arm64/README.md)
* [Dispatcher Hooks](MANET/networkd-dispatcher/README.md)

