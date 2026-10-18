# MT7916 timing support

These patches provide opt-in timing reports, marked probes, and ACK spatial
extension controls for MANET ranging. The same six patches target both:

- Raspberry Pi CM4: `linux-rpi-6.18` at
  `95b85bebbedcaedfa7ca79116ed38b7376fba412` (6.18.33).
- Rock 3A: `linux-6.18` at
  `7d0a66e4bb9081d75c82ec4957c50034cb0ea449` (6.18).

Apply the complete series from the MANET repository:

```sh
python3 kernel/patches/mt76-ftm/apply.py /path/to/linux-rpi-6.18
# Or, for Rock 3A:
python3 kernel/patches/mt76-ftm/apply.py /path/to/linux-6.18
```

The helper selects the patch directory from an exact kernel HEAD table. Both
entries select this directory's `series`; no per-tree rebase is needed. Unknown
HEADs are rejected. Only paths under `drivers/net/wireless/mediatek/mt76/mt7915/`
are accepted. The helper tests the sequence in a temporary tree before applying
its combined diff. A completely applied series is skipped. A partial or
conflicting series fails without changing source files. Existing unrelated
changes, including the separate txpower edit in `init.c`, are retained.
`series` records patch order. Both board build scripts apply it through
`mt76-bgobs/apply.py` together with the observer series before configuring
the kernel. `build-r3a.sh` first applies the PCIe slot power patch
and `kernel/patches/mt76-txpower/0001-mt7915-channel-power.patch`, skipping each
when already applied. Each timing prefix can be built independently.

Run the helper regression tests from the repository root:

```sh
python3 -m unittest discover -s kernel/patches/mt76-ftm -v
```

The tests read the two local kernel repositories under `kernel-work/real_work/`
and create disposable trees using their base objects. They exercise the actual
series, including apply/skip, partial and conflicting series, unknown bases,
path rejection, and preservation of txpower and PCIe edits. A missing kernel
repository skips that target's tests; the source repositories are not changed.

Patch 0004 adds `MODULE_INFO(manet_timing, "1")`. The firmware selector
checks `modinfo -k "$(uname -r)" -F manet_timing mt7915e` against the installed
module before preparing patched firmware. This tag identifies the timing
build; it does not certify early-boot firmware stability. When extending an
already applied older series, apply the new suffix explicitly before using
the helper; partial-series rejection remains intentional.

For a tree that already has exactly patches 0001 through 0005:

```sh
# Run from the kernel tree; use the absolute path to the new patch.
git apply --check /path/to/0006-qos-null-probes.patch
git apply /path/to/0006-qos-null-probes.patch
```

For a four-patch tree, first check and apply 0005 separately, then 0006.
Then run `apply.py` to verify the complete series is present. Do not remove
earlier patches or bypass the base check.

The root-only MT7916 debugfs controls are `tmr_registers` (band identity and
readback), `tmr_ctrl` (timing enable/role/filter), `tmr_peer` (peer or `off`),
`tmr_mark` (optional skb mark), `tmr_probe` (QoS Null burst), `tmr_spe` (probe SPE index or `off`), `tmr_rate`
(optional fixed rate/width), and `tmr_ack_spe` (response SPE index or `restore`).
The `mt7915:mt7915_rx_tmr` tracepoint supplies opaque report bytes. ACK control
covers all four BSSID response contexts on the selected band; it is not
per-peer. Userspace must serialize use and restore the saved state.

Report capture requires kernel event tracing. The Rock 3A fragment enables
`CONFIG_FTRACE=y` and `CONFIG_ENABLE_DEFAULT_TRACERS=y`; CM4 already enables
event tracing. On Rock 3A, install the rebuilt Image and complete matching
module tree together. Earlier `6.18.0-manet` builds lacked tracing, despite
sharing the same release string. The module tag alone does not prove that
tracing is available; verify the event under tracefs after boot.

No debugfs writes means no probe selection or register writes. The TX path has
one disabled peer check; report tracing uses the kernel tracepoint static key.
The feature does not enable the positioning service or its production radio
capability gate.

## Probe TX status

Patch 0005 snapshots the existing peer/mark selection once during
`mt7915_tx_prepare_skb`. The same Boolean sets `MT_TXD3_TIMING_MEASURE` and
the private `MT7915_TX_CB_TMR` bit in `mt76_tx_cb.flags`, after
`mt76_tx_status_skb_add` clears that control block. It does not recheck the
current debugfs configuration or skb mark when status arrives.

Marked frames request individual status and therefore cannot select the
untracked `MT_PACKET_ID_WED` path. TXS WCID/PID resolves the retained skb under
the mt76 status lock. A local version of the connac completion helper keeps
ACK/error, AMPDU status, timeout, and TXFREE completion behavior, but skips
`mt76_connac2_mac_fill_txs` for that saved flag. Thus neither `wcid.rate` nor
the TX mode/MCS/NSS/bandwidth histograms take the probe's fixed rate.
`mt7915_sta_statistics` reads that rate into `sinfo->txrate`, which BATMAN V
can sample. Unmarked frames retain the original behavior, including a fresh
periodic sampling opportunity during a probe burst. Exhausted PID allocation
falls back to `NO_SKB`, whose status is already ignored by this driver.

The driver advertises `HAS_RATE_CONTROL`; rate selection is firmware-owned.
The patch leaves airtime, retry/failure, and byte counters intact. It changes
host reporting, without claiming to change firmware rate-control internals.

## Dedicated QoS Null probes

Patch 0006 adds root-only `tmr_probe` on each MT7916 PHY. Write
`COUNT INTERVAL_US` to schedule COUNT transmission slots to the configured
`tmr_peer` on that PHY's mesh interface. The write is synchronous: it returns
when the burst finishes or fails. A second file descriptor can write `0` to
request cancellation; a new burst returns `EBUSY` until the first writer
exits. Reads show
`running=0|1|2 sent=N skipped=N error=ERRNO`, where 2 means cancellation is
pending. A zero write while idle succeeds without changing the previous result.

Limits are 1..4096 slots, 10000..1000000 microseconds between slots,
and `COUNT * INTERVAL_US <= 120000000`. Sleep pacing is approximate and can
run late under load; there is no catch-up burst. Cancellation and signals are
checked between submissions and at most 20 ms apart during sleeps. An
already queued/on-air frame cannot be recalled. `sent` counts accepted DMA
submissions, not ACKs or timing reports. `skipped` counts failed RTNL trylocks
and slots that find the radio scanning. A skip consumes one slot and uses the
normal delay before the next slot, without retrying or transmitting a
replacement. Completed bursts have
`sent + skipped = COUNT` and `error=0`, including when every slot was skipped.
Both counters reset when a new burst is accepted. Partial failure returns an
error and retains both counters for inspection; never retry the whole write
without first draining and starting a new capture epoch.

The station must be on exactly one active mesh interface on the selected
PHY. Each submission queries the public station API for an ESTAB link with
both local and peer power modes active, then looks up the station by MAC
under RCU. Nonmesh, missing, ambiguous, sleeping, and non-ESTAB peers are
refused. The link query bypasses the driver's optional firmware statistics
read only in the calling task; normal station statistics remain unchanged.
RTNL is acquired with trylock for one submission. Contention skips that slot.
Under the driver mutex, scanning also skips the slot after cancellation,
running, reset, and testmode checks. A stopped radio, reset, or testmode stays
fatal even if scanning is also set. All submission errors, including `EBUSY`
and `ENETDOWN`, still abort the burst. Scanning is checked once without
waiting for it to finish. There is no bounded retry: one immediate attempt
preserves slot pacing without extra RTNL attempts during configuration or
teardown. No station or interface
pointer survives a sleep. Interface removal, radio stop, and firmware reset
request cancellation. There is no background worker to outlive debugfs.

The frame is a 32-byte four-address mesh QoS Null, TID 7 / AC VO, normal ACK,
PM=0, EOSP=1, and RSPI=0. It carries no mesh control or payload and uses no
key or Protected bit. This matches the layout of mac80211's mesh power
management frames. The active-mode admission check avoids changing a
sleeping link's power mode. Direct mt76 DMA queue submission bypasses the
BE software queue and mac80211 aggregation. TXD BA_DISABLE and cleared
HW_AMSDU prohibit aggregation independently of `tmr_spe`; REM_TX_COUNT=1
requests one attempt. The usual optional `tmr_spe` / `tmr_rate` override and
patch 0005's saved completion flag apply. Ordinary skb-mark selection is
suppressed during the burst, then resumes with its previous settings.
Peer, mark, rate, SPE, and timing-control writes return `EBUSY` during a burst.

To admit only QoS Null on the responder, the existing `tmr_ctrl` already
accepts this command, with no ABI change:

```sh
printf '1 1 2 0 0x32 3 3 3 8 10\n' > tmr_ctrl
```

The type/subtype byte is `(subtype << 2) | type`: QoS Data 0x22, Data 0x02,
QoS Null 0x32, Null 0x12, and Action 0x34. The vendor uses reserved type 3 for
unused slots. RMAC TMR_PA packs four six-bit match slots in bits 0..23,
with separate category checks in bits 24..27. They are alternative matches,
not a mask on one combined value. Category=0 avoids parsing a nonexistent
category/action body in the Null frame. The firmware programs the selected
band; filter readback is available in `tmr_registers`.

## Capture limits and hardware validation

The subtype filter rejects ordinary Data/QoS Data before timing processing,
including data from the same peer. It does not select a full transmitter MAC.
QoS Null is also used for real mesh power management and may arrive from
other peers. Keep all responder rows in their original order. The processed
report still has only two transmitter MAC bytes and firmware still advances
its correction accumulator before host delivery. Host filtering or compacting
indices remains unsafe; tracefs loss=0 does not establish lossless firmware
report delivery. The production capability gate remains closed.

Firmware and vendor register evidence establish a per-band timing lock and
one shared software correction context. They support capture contention
under bulk traffic. The inspected report builder does not iterate over
A-MPDU subframes or export independent timestamps for them. The exact
subframe chosen by hardware and the extent of bulk-traffic starvation must
still be measured. Existing marked probes with SPE enabled already request
BA_DISABLE, so aggregation of those probes is not an established cause.

For idle, same-peer bulk, and other-peer bulk runs, test both the narrow and
wide fixed-rate profiles. Require `sent=N`, `marked=N`, initiator reports=N,
no trace loss, ACK success, and stable ordinary station rates/BATMAN routes.
Inspect an over-air capture for subtype, four addresses, no Protected bit,
normal ACK, no retries, and no aggregation. Test SAE/PMF mesh links as well as
open links. mac80211 consumes valid mesh Null frames before ordinary data
unencrypted checks and without counting them as dropped packets. Hardware
ACK/report behavior on a secured peer still needs bench confirmation.
Exercise cancellation, overlapping writes, peer removal, interface stop,
firmware recovery, and module unload while a burst is active.

The standalone bench tools support `PROBE=null`; their default remains ping.
Pairing uses the same report ABI and needs no subtype-specific decoder change.
It remains a diagnostic estimator with the same loss/accumulator assumptions.

## Module-only rebuild compatibility

These changes touch only mt7915 source and no shared structures or exports.
Use the existing configured build directories and `make ... modules`, then
replace only `mt7915e.ko.xz` in staging and regenerate both SBC overlays.
Keep the existing Image and complete dependency module set.

Compare the build configuration, generated headers, Image, full
`Module.symvers`, and module vermagic before and after building. CM4 uses
MODVERSIONS; every imported symbol CRC must match its existing symbol table.
Rock 3A has MODVERSIONS disabled, so matching vermagic alone is insufficient:
it must be the existing tracing-enabled build, with all imports present in
that unchanged build's symbol table. A node still on the older non-tracing
Rock 3A Image requires the full kernel replacement described above.

Offline builds cannot prove which Image is running remotely. Check node
Image/module identity and the trace event before a module-only replacement.
After installation, verify ordinary station rate/throughput stays stable
during a marked burst, ACKs and timing reports continue, unmarked traffic
still updates the rate, and disabling controls while TX status is pending
does not reintroduce a probe-rate sample. Repeat with OFDM 6 Mb/s and the
80 MHz fixed-rate probe setting, and inspect BATMAN next-hop stability.
