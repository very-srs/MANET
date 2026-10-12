# Prebuilt binaries (aarch64)

These are committed as binaries so a node does not have to build them at first
boot. `alfred` and `batctl` are built from the open-mesh sources, which are
newer than what apt ships. The S1G supplicant and its client are built from
Morse Micro's HaLow-enabled hostapd sources.

Install tarballs stage the shipped binaries into `/usr/sbin`. Tools tarballs
also carry `wpa_supplicant_manet` and `wpa_cli_manet` when present, so a normal
update can deliver Wi-Fi mesh fixes. The HaLow candidate build is excluded
from every archive and needs separate bench testing before activation.

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

## Wi-Fi mesh supplicant

`wpa_supplicant-manet/` contains the hostap 2.11 build configuration and patch
series. The output names are `wpa_supplicant_manet` and `wpa_cli_manet`.
Debian upgrades do not replace these MANET-owned binaries.

The build enables `CONFIG_IEEE80211AX` alongside mesh, SAE, 802.11n and
802.11ac. Debian's 2:2.10-24 build omits AX, which prevents reliable HE
capability exchange during mesh peering. The patches remove random extra
inactivity delay for established mesh peers, bound unsuccessful SAE attempts,
repair missing kernel stations, and report SAE steps and status at INFO.
Patch 0006 keeps the mesh on its configured primary channel. Upstream moves
a joining node's primary when other networks share its 40 or 80 MHz block,
which leaves that node unable to peer with the rest of the mesh.

Mesh configs use the global setting `mesh_max_inactivity=10`. The patched
Wi-Fi build frees an established peer when kernel inactivity reaches ten
seconds. Received beacons refresh activity, so an idle peer in range remains
connected. There is no AP null-data poll or disassociation grace period on
this mesh path. A failed station query retains the peer unless the driver
explicitly reports ENOENT. Normal AP inactivity handling is unchanged.

An unsuccessful SAE attempt is freed after three or four seconds, including
local setup failures. Existing SAE retransmissions operate during that
window. One second of random jitter separates synchronized retries; the next
beacon after cleanup can start a fresh exchange. The SAE timeout never sets
PLINK_BLOCKED. INFO messages include the peer, exchange step, state, received
status and local result where applicable. Status -1 means no received status
is available. Keys, passwords and commit material are not logged.

Every `wpa_supplicant@.service` instance uses `manet-mesh-supplicant.py` through
`20-manet-binary.conf`. This covers radio setup, BATMAN attachment, channel
changes, lobby visits and AP-to-mesh transitions. The existing AP guard still
runs first. When the MANET executable is absent, the wrapper logs a WARNING
to the journal and executes `/usr/sbin/wpa_supplicant`. That fallback lacks
the HE and recovery changes. USB client uplinks retain their separate Debian
unit: they do not need these mesh patches or a reduced dependency build.

The tools updater adds missing inactivity settings to live, lobby and HaLow
mesh configs under the channel-change lock, reloads systemd, and restarts
active Wi-Fi mesh instances. Lock contention leaves the update pending.
Explicitly configured values are preserved. The wrapper also prepares the
live and lobby files on each start, including files restored by rollback.
Fresh setup and AP-to-mesh generation write the setting directly. Frequency
and credential edits preserve this global field.

### Native Debian 13 build

On the arm64 build node, install the build dependencies:

```sh
sudo apt install build-essential pkg-config libnl-3-dev libnl-genl-3-dev \
    libssl-dev curl patch git
bash MANET/binaries_arm64/wpa_supplicant-manet/build.sh /tmp/manet-wifi
```

The script downloads `https://w1.fi/releases/wpa_supplicant-2.11.tar.gz`,
checks the SHA-256 in `source.sha256`, applies `patches/series` without fuzz,
and builds both programs into the requested output directory. It does not
install into the running system. Set `WPA_TARBALL=/path/to/archive` for an
offline build and `JOBS=2` to limit parallel compilation. The tarball digest
is `912ea06f74e30a8e36fbb68064d6cdff218d8d591db0fc5d75dee6c81ac7fc0a`.

The configuration derives from Debian 2:2.10-24. It removes D-Bus, PC/SC,
smartcard, readline and MACsec dependencies, and uses OpenSSL's default
cipher policy. Runtime dependencies are libc, libnl-3, libnl-genl-3, libssl
and libcrypto. Use the node's native build to retain its glibc/OpenSSL ABI.

After hardware verification, place both output files directly under
`MANET/binaries_arm64/` and rebuild the install and tools archives. A builder
warns if a Wi-Fi binary is missing. Check `systemctl cat
wpa_supplicant@wlan0` and the journal for binary selection on the node.
`wpa_cli_manet` is available for diagnosis; existing scripts may keep using
Debian's compatible `wpa_cli` control client.

### HaLow candidate

`wpa_supplicant-s1g-manet/` builds Morse hostap tag 1.16.4, pinned to commit
`a1bc239be2caef2dfd0a45757d030ab0c589387d`. It follows the Morse OpenWrt
recipe with mesh enabled, carries its feature-indication patch and header,
and removes OpenWrt ubus and optional desktop dependencies. The hostapd
feature-indication hunk is rebased to the tag's DPP initialization context.
The resulting candidate has the same SAE timeout, stale-station repair and
INFO diagnostics as the Wi-Fi build.

```sh
bash MANET/binaries_arm64/wpa_supplicant-s1g-manet/build.sh /tmp/manet-s1g
```

The script verifies the pinned commit and the SHA-256 of its deterministic
`git archive`. For an offline build, set `MORSE_TARBALL` to an archive made
with `git archive --format=tar --prefix=hostap-1.16.4/` at that commit. The
expected digest is in `source.sha256`.

Outputs are named `wpa_supplicant_s1g_candidate` and `wpa_cli_s1g_candidate`.
No unit or builder selects these names. Complete Wi-Fi bench testing before
installing a HaLow candidate on a bench node. The shipped S1G binaries stay
separate from these outputs.

HaLow configs explicitly retain `mesh_max_inactivity=300`, and the candidate
retains upstream inactivity polling and jitter. This conservative setting
avoids increasing established-link key teardown frequency before testing:
Morse firmware can wedge during key removal and require a hard power cycle.
`batman-enslave-watch.sh` continues to enforce `mesh_plink_timeout=0`.
The faster cleanup of failed SAE entries still needs a HaLow soak test.
Tune the global S1G setting only after testing both traffic and idle beacons.

### Validation

`0005-mesh-recovery-tests.patch` adds hwsim cases for repeated wrong-key SAE,
local setup failure and recovery after `iw station del`. The Wi-Fi series
also tests beacon-only retention and silent-peer expiry. Run these in the
upstream hwsim environment with its testing build configuration; the release
tarball does not include the complete hwsim harness. Passing a native build
and version check does not establish RF, HE rates or firmware stability.
