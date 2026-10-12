# MT7916 timing support

These patches provide opt-in timing reports, marked probes and ACK spatial
extension controls for MANET ranging. The same five patches target both:

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
`series` records patch order; both build scripts invoke this helper before
configuring the kernel. `build-r3a.sh` first applies the PCIe slot power patch
and `kernel/patches/mt76-txpower/0001-mt7915-channel-power.patch`, skipping each
when already applied. Each timing prefix can be built independently.

Run the helper regression tests from the repository root:

```sh
python3 -m unittest discover -s kernel/patches/mt76-ftm -v
```

The tests read the two local kernel repositories under `kernel-work/real_work/`
and create disposable trees using their base objects. They exercise the actual
series, including apply/skip, partial and conflicting series, unknown bases,
path rejection and preservation of txpower and PCIe edits. A missing kernel
repository skips that target's tests; the source repositories are not changed.

Patch 0004 adds `MODULE_INFO(manet_timing, "1")`. The firmware selector
checks `modinfo -k "$(uname -r)" -F manet_timing mt7915e` against the installed
module before preparing patched firmware. This tag identifies the timing
build; it does not certify early-boot firmware stability. When extending an
already applied older series, apply the new suffix explicitly before using
the helper; partial-series rejection remains intentional.

For a tree that already has exactly patches 0001 through 0004:

```sh
# Run from the kernel tree; use the absolute path to the new patch.
git apply --check /path/to/0005-probe-tx-status.patch
git apply /path/to/0005-probe-tx-status.patch
```

Then run `apply.py` to verify the complete series is present. Do not remove
earlier patches or bypass the base check.

The root-only MT7916 debugfs controls are `tmr_registers` (band identity and
readback), `tmr_ctrl` (timing enable/role/filter), `tmr_peer` (peer or `off`),
`tmr_mark` (optional skb mark), `tmr_spe` (probe SPE index or `off`), `tmr_rate`
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
ACK/error, AMPDU status, timeout and TXFREE completion behavior, but skips
`mt76_connac2_mac_fill_txs` for that saved flag. Thus neither `wcid.rate` nor
the TX mode/MCS/NSS/bandwidth histograms take the probe's fixed rate.
`mt7915_sta_statistics` reads that rate into `sinfo->txrate`, which BATMAN V
can sample. Unmarked frames retain the original behavior, including a fresh
periodic sampling opportunity during a probe burst. Exhausted PID allocation
falls back to `NO_SKB`, whose status is already ignored by this driver.

The driver advertises `HAS_RATE_CONTROL`; rate selection is firmware-owned.
The patch leaves airtime, retry/failure and byte counters intact. It changes
host reporting, without claiming to change firmware rate-control internals.

## Known limitation: responder capture under load

Responder diagnostics capture matching data frames from every peer, so busy
links can overwhelm probe pairing. The processed report retains only two
transmitter MAC bytes and has no verified station identifier for a full peer
match. Firmware advances its correction accumulator before host delivery,
including for frames a host filter would discard. Dropping those reports and
using `STEP * report index` would therefore bias the corrected timestamps.
Keep all reports in their original order for current diagnostics; this still
does not recover reports lost before host delivery.

A safe restriction needs one of two firmware changes:

- Filter the full transmitter address before correction and accumulator
  advancement, with an acknowledged control, defined reset/drain boundaries
  and correct timing-lock release for rejected frames.
- Export full peer identity, role/band, reset epoch and the exact accumulator
  value used for each report. Update the decoders and pairing to use that
  value before filtering, so missing or discarded reports do not shift later
  corrections.

Peer filtering still admits ordinary traffic from the selected peer. Probe
filtering also needs a verified over-air identifier or classifier; the local
`TIMING_MEASURE` descriptor bit is not transmitted. Diagnostic responder
sessions remain available; the production ranging capability gate stays
closed until capture association and correction accounting are verified.

## Module-only rebuild compatibility

These changes touch only mt7915 source and no shared structures or exports.
Use the existing configured build directories and `make ... modules`, then
replace only `mt7915e.ko.xz` in staging and regenerate both SBC overlays.
Keep the existing Image and complete dependency module set.

Compare the build configuration, generated headers, Image, full
`Module.symvers` and module vermagic before and after building. CM4 uses
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
