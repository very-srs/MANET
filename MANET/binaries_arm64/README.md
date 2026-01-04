### PRE-COMPILED BINARIES

This directory contains pre-compiled binaries used for the mesh network nodes.

Alfred and Batman are compiled from the open-mesh sources rather than pulled from apt to get a newer version.  The s1g wpa binaries are compiled from Morse Micro's halow enabled hostapd sources.  I add them as a binary here to avoid having to build them at first boot on the mesh nodes.

These files are automatically injected into the OS image by the provisioning scripts. They are archived here for manual recovery, debugging, or custom deployments if needed.

---

### FILE LIST

- `alfred`
  The Almighty Lightweight Fact Remote Exchange Daemon. Used to distribute information (like hostnames or sensor data) across the batman-adv mesh network without requiring a central server.

- `batman`
  The B.A.T.M.A.N. (Better Approach To Mobile Adhoc Networking) routing protocol utility.

- `wpa_supplicant_s1g`
  A modified version of the WPA Supplicant supporting 802.11ah (Wi-Fi HaLow / Sub-1 GHz). Handles key negotiation and authentication for the long-range HaLow radios.

- `wpa_cli_s1g`
  The command-line client for interacting with the S1G supplicant. Used to check status, scan for networks, and configure the HaLow connection manually.
