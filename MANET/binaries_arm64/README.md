# Prebuilt binaries (aarch64)

These are committed as binaries so a node does not have to build them at first
boot. `alfred` and `batctl` are built from the open-mesh sources, which are
newer than what apt ships. The S1G supplicant and its client are built from
Morse Micro's HaLow-enabled hostapd sources.

The install tarball stages all of them into `/usr/sbin`, so a node picks them
up when first-boot provisioning extracts it. The tools tarball does not carry
them, so a new binary reaches a node through a reflash or a copy by hand.

## The files

- `alfred`

  The Almighty Lightweight Fact Remote Exchange Daemon. Distributes information
  such as hostnames and sensor data across the batman-adv mesh, with no central
  server.

- `batctl`

  The control and debug utility for B.A.T.M.A.N. Advanced.

- `wpa_supplicant_s1g`

  A WPA supplicant with 802.11ah support, handling key negotiation and
  authentication for the HaLow radios.

- `wpa_cli_s1g`

  The command-line client for that supplicant. Checks status, scans for
  networks, and configures a HaLow connection by hand.

- `openvlm`

  Reads, writes and validates the EEPROM on an OpenVLM USB audio board
  (C-Media CM108B) over USB HID. A board ships with a blank EEPROM, and until
  it is programmed the chip ignores every volume and analog setting, so voice
  on a node with a fresh board needs this tool once. Statically linked Go, with
  no runtime dependencies.

  The settings that matter for a headset with an external mic preamp are
  `mic-boost false`, `adc-init-volume 0` and `aa-init-volume -23`. The last is
  the analog sidetone, which howls against a preamp at anything higher.

  The EEPROM is read only at chip power-on, so writing it needs a physical
  unplug and replug afterward. Detaching the device logically with
  `echo 0 > .../authorized` does not drop VBUS and has no effect.
