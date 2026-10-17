# Paired CM4 release stack and Rock 3A bench modules

This directory combines the pinned cfg80211 and MT7916 features into one
application transaction. The `series` file orders three component series:

- cfg80211: upstream background-CAC fixes, opt-in DFS follower AP, continuous
  CAC hold v2 and token-bound transfer to the main detector.
- mt76: FTM patches 0001 through 0006, channel-power policy, all three
  background-observer/reservation patches, continuous hold v2, and the
  reservation-v2 runtime transition that was benched separately, passive DFS
  observations and main-detector transfer.
- mac80211: mesh CSA precedence retention and DFS handling after a local CSA.

The preceding feature patches are preserved. Hold is split into cfg80211 and driver
parts without changing its implementation. Runtime transition is rebased
onto the hold stack with the same diagnostic-v2 protocol and restrictions.
The follower feature has no separate mt7915 change; it is cfg80211-only.

`apply.py` supports the exact source revisions in
[`kernel/build/sources.json`](../../build/sources.json). It reconstructs and
checks the entire result before modifying source. It accepts a clean pinned
tree or the exact complete series. Partial stacks, local changes to affected
files, unknown revisions and mismatched private headers must not be combined
by independently loading modules. Unrelated source changes are preserved.

## Build and package

Use a separate `MANET_KERNEL_WORK` workspace, prepared using the tracked
source recipes, to avoid changing another experiment's sources or Morse
objects. From the repository root, with the development venv activated:

```sh
export MANET_KERNEL_WORK=/absolute/path/to/separate-work
python3 kernel/build/prepare-sources.py --board cm4
git -C "$MANET_KERNEL_WORK/real_work/morse-driver-1.16" submodule update --init --recursive
BUILD=/absolute/path/to/build-cm4 LOCALVERSION=-r3 JOBS=7 \
  bash kernel/patches/manet-mt7916/build-cm4.sh
bash kernel/patches/manet-mt7916/package-cm4.sh \
  /absolute/path/to/build-cm4 /absolute/path/to/cm4-overlay /absolute/path/to/output
```

The new release is `6.18.33-manet-r3`. The build includes all configured
modules and fresh Morse SPI/USB modules. Packaging includes every compiled
Pi overlay, `overlay_map.dtb`, the matched upstream `overlays/README`, the
Morse overlay, module dependencies, firmware and BCFs. The existing runtime
payload validator checks the manifest and archive bounds. MT7916 WM selection
remains the runtime package's responsibility; no MT7916 firmware is placed
inside the Morse firmware namespace.

The Morse rate-control submodule is pinned by its parent gitlink at
`cf45ca7520663b91ba1f57b5e04542d17aa226a3`. The build checks its presence
and revision before compiling the kernel. The general source-preparation
helper currently does not initialize this nested dependency itself.

The stock `kernel/build/build-cm4.sh` still applies the older feature set.
Use this directory's build wrapper for this combined stack. A subsequent
field candidate must use another unique release identifier.

For the Rock 3A bench, use a disposable source snapshot containing the pinned
`net/wireless`, `net/mac80211` and `drivers/net/wireless/mediatek/mt76` trees, backed by that
revision's Git objects. Supply the prepared build that matches the bench's
existing Image, configuration and symbol table:

```sh
bash kernel/patches/manet-mt7916/build-r3a-pair.sh \
  /absolute/path/to/r3a-source /absolute/path/to/prepared-build /absolute/path/to/pair
```

This produces the matched `cfg80211.ko`, `mac80211.ko` and `mt7915e.ko` for
`6.18.0-manet`, plus `verification.json` and `SHA256SUMS`. It does not install
them. Rock 3A has no module CRC checking in its pinned configuration; correct
symbol names and vermagic alone do not establish arbitrary ABI compatibility.
Preserve the recorded prepared-build identity.

## Host contract and defaults

`cfg80211 dfs_follow_ap_v1=1` enables follower admission. It defaults off and
also requires the paired hostapd option `dfs_follow_station=1`. Follower AP
operation is limited to the existing associated BSS-STA, same-wiphy, 20 MHz
protocol. The upstream station owns DFS detection in that mode.

`mt7915e background_radar_2t2r` remains false at load. Set it to 1 only for
fixed boot reservation. For runtime policy, leave it false and use the
root-only `phyN/mt76/rx_transition_probe` debugfs file. Write `2` to reserve
the receiver and `3` to release it. Hold `/run/manet-bgobs-phyN.lock` while
coordinating reservation, observation and CAC. Status retains schema 1 and
`diagnostic:true`; `manet_rx_transition=diagnostic-v2` identifies this ABI.

Background radar is advertised at wiphy registration for native MT7916
band-1 EEPROM that can use this runtime transition: chain shift 2, device
mask 0x1f, antenna mask 3 (two NSS), resolved EEPROM byte 0x5b and two streams,
without implicit beamforming. The debugfs transition must be compiled in.
This capability does not require an already-running mesh at registration;
the transition still enforces its live restrictions. The existing boot
reservation and `mediatek,disable-radar-background` device-tree opt-out are
preserved.

A CAC start before runtime reservation returns EBUSY without issuing firmware
commands or changing ownership. The host takes the observer lock, requests
`2`, verifies reserved mask 0x0c, enables the hold, then starts background CAC.
An EAGAIN or EBUSY reservation defers CAC. A failed transition remains latched
and cannot start CAC; teardown is still allowed. No new path performs an
implicit reservation or bypasses the transition's rate limit.

Runtime switching preserves two NSS and changes three RX paths to two RX
paths plus the observer. It is restricted to native EEPROM, a running 20 MHz
non-DFS mesh-only 5 GHz PHY, with no implicit beamforming, scanning, reset,
RDD owner or dirty observer. AP/STA roles return EOPNOTSUPP. Competing owners
return EBUSY. Attempts are rate-limited to one per 60 seconds with EAGAIN;
same-state requests return success after ownership checks. A failed rollback
or MAC restart keeps queues stopped and requires module reload. Do not use
module reload as routine automatic recovery on a live mesh.

Write `enable` to the wiphy's `background_hold` before starting background
CAC. The hold remains disabled by default and reports schema 2 with
`experimental:true`. `background_hold_pause` defaults false and is a timeout
test control. Both modules retain `manet_cac_hold=experimental-v2` and the
paired private exports `cfg80211_hold_token_v2` and `cfg80211_hold_sample_v2`.
The driver and cfg80211 copies of `hold-api.h` must match exactly.

Hold occupancy still needs the host's stricter rejection of local TX >= 10
percent. The older kernel validity flag alone is not sufficient for scoring.

## DFS mesh execution

The release adds `cfg80211_hold_transfer_v3` and
`cfg80211_hold_main_stop_v3` to the paired private API. The loaded driver
exposes read-only `dfs_mesh_v1=Y`; cfg80211 includes `dfs_mesh_v1:true` in its
schema-2 status. The host requires both, CM4 policy and live PHY DFS flags.
A schema-2 hold alone is still insufficient. Rock 3A bench services can opt in
with `MANET_DFS_BENCH=1`; normal Rock 3A and RPi5 behavior remains off.

mac80211 already propagates radar requirements for mesh CSA. The source path
is `nl80211_channel_switch` -> `params.radar_required` ->
`ieee80211_link_reserve_chanctx` -> `chanctx.conf.radar_enabled` ->
`ieee80211_emulate_switch_vif_chanctx` -> `hw.conf.radar_enabled` ->
`mt76_update_channel` -> `mt76_phy_dfs_state` ->
`mt7915_dfs_init_radar_detector`. An AVAILABLE DFS channel selects ACTIVE,
starts the main-band RDD and sends CAC_END. MT7916 band 1 uses its main RDD,
not the dedicated background RDD2. Main reports go through
`mt7915_mcu_rx_radar_detected` -> `ieee80211_radar_detected` ->
`cfg80211_radar_event` -> `nl80211_radar_notify`.

The host initiates DFS mesh CSA with HANDLE_DFS. The series preserves RDD2
through a retune onto its exact cleared chandef. After main RDD startup, the
driver submits the saved epoch/generation to cfg80211. Transfer checks an
active hold, fresh heartbeat, identical chandef and AVAILABLE state under the
hold lock. It saves a separate main token before scheduling RDD2 teardown.
Revocation preserves AVAILABLE under that main owner, and the existing
`cfg80211_any_wiphy_oper_chan` rule prevents later pre-CAC expiry while the
mesh operates there. No temporary USABLE transition is accepted. A transfer
failure stops queues and the main MAC. The new main fields retain the token
after background teardown, allowing a later `main-radar` latch. Main stop,
retune, regulatory change and firmware recovery invalidate main ownership.

The observer can now listen passively on enabled DFS channels. It still
rejects NOP channels and never treats passive energy as CAC completion.
Automatic host preparation uses authenticated Alfred and the connected
participant set, followed by a unanimous plan with live holds and a measured
non-DFS fallback. Every participant initiates the agreed-time mesh CSA. The
identical-chandef beacon path records incoming precedence so later relays do
not reuse stale precedence. A block-TX exit from a DFS channel carries the
regulatory reason in mesh actions and beacons, allowing peers to mark the old
channel unavailable and latch the event before Alfred arrives.

On main radar, the independent host watcher requests one-beacon block-TX CSA
before persistence, agreement processing or Alfred publication. Failed CSA
uses interface-down recovery. The main detector follows the standard kernel
radar path; the userspace watcher is not a bound on stop latency if the host
is stalled. No hardware latency or RF sensitivity claim follows from the
source tests or compilation.

Run the three-node acceptance capture after installing this complete stack
and current tools, with every node in HT20 5 GHz mesh mode, ACS and synchronized
time. Start at 5180 MHz and create measured interference so a DFS candidate
wins normal scoring. The script does not fabricate reports or bypass CAC:

```sh
python3 kernel/patches/manet-mt7916/bench-dfs.py cm4 r3a r3a.2 \
  --detector cm4 --output /absolute/path/to/new-capture
```

It waits for the same committed DFS plan and active transferred main token on
all three nodes, then writes **1** to the detecting PHY's `mt76/radar_trigger`.
Value **2** triggers the background detector and is not this test. Per-node
JSONL records preparation, CAC/hold state, actual frequencies and nl80211
events. `summary.json` reports trigger-to-main-latch and trigger-to-block-TX
ACK on the detecting node's boot clock, plus controller-observed fallback
latencies for every node. The latter include polling and SSH latency. The
script times out if the real scoring never selects DFS. A separate monitor
or traffic capture is needed for over-the-air TX cessation and loss.

The complete path remains a hardware acceptance candidate until this bench
passes. Keep all three Rock 3A modules together with their pinned Image and
configuration. Do not replace only cfg80211 or only the driver.

## DFS-only regulatory database

`restore-dfs.py` uses wireless-regdb's pinned encoder and the historical
revision whose non-world frequency rules exactly match the shipped database.
It verifies that the existing `db.txt` reproduces the shipped binary, adds
only DFS tags, and independently decodes both binaries to compare countries,
regions, frequencies, bandwidths, powers, CAC values and WMM records.

```sh
python3 kernel/patches/manet-mt7916/restore-dfs.py \
  --upstream /path/to/wireless-regdb \
  --source MANET/root/db.txt --shipped MANET/root/regulatory.db \
  --output /path/to/regdb-output
```

`db.txt.diff` is the full source diff. Its original comment describes the
historical flattening; the candidate changes only rule flags. The two custom
world-domain DFS ranges retain their existing bounds. The output is unsigned,
as the shipped database is. The CM4 build explicitly disables signed-regdb
enforcement. The pinned Rock 3A build enables it and must retain its existing
trusted database for bench use; copying this unsigned database does not make
that kernel accept it. No new signing key is introduced.

## Verification

```sh
python3 -m unittest discover -s kernel/patches/manet-mt7916 -v
```

The suite applies and reverses the complete series on both exact kernel
bases, checks idempotence, and rejects partial/conflicting application without
source writes. C harnesses extract the actual combined functions and exercise
FTM status/probes, EEPROM initialization, reservation, observer restoration,
follower admission/revocation, CAC hold, heartbeat expiry, stale tokens,
radar attribution, runtime transition rollback and firmware-command packing.
AddressSanitizer and UndefinedBehaviorSanitizer run on these host harnesses.
The background-CAC harness also checks actual registration with native EEPROM,
the reservation-to-CAC sequence, pre-reservation EBUSY, failed-state rejection,
and release refusal while the main channel is DFS. It reproduces the missing
feature and post-reservation CAC failures on r2. The observer harness adapter
only extends its mock device fields for this combined stack. A host integration
test exercises the existing reservation policy and preparation methods together,
including deferred CAC and retention until both background and main owners end.

`check-pair.py` verifies the real ELF imports and exported symbol tables,
private-header equality, capability tags, release names and every available
module-version CRC. It accepts `--extra-symvers` for the externally built
Rock 3A private exports. This complements source tests and full compilation;
it is not a hardware or kernel-concurrency test.
