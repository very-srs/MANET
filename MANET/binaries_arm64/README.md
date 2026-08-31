### PRE-COMPILED BINARIES

This directory contains pre-compiled binaries used for the mesh network nodes.

Alfred and batctl are compiled from the open-mesh sources rather than installed from apt, which provides an older version. The S1G WPA binaries are compiled from Morse Micro's HaLow-enabled hostapd sources. They are committed here as binaries so that nodes do not have to build them at first boot.

These files are automatically injected into the OS image by the provisioning scripts. They are archived here for manual recovery, debugging, or custom deployments if needed.

---

### FILE LIST

- `alfred`
  The Almighty Lightweight Fact Remote Exchange Daemon. Used to distribute information (like hostnames or sensor data) across the batman-adv mesh network without requiring a central server.

- `batctl`
  The B.A.T.M.A.N. (Better Approach To Mobile Adhoc Networking) routing protocol utility.

- `wpa_supplicant_s1g`
  A modified version of the WPA Supplicant supporting 802.11ah (Wi-Fi HaLow / Sub-1 GHz). Handles key negotiation and authentication for the long-range HaLow radios.

- `wpa_cli_s1g`
  The command-line client for interacting with the S1G supplicant. Used to check status, scan for networks, and configure the HaLow connection manually.
