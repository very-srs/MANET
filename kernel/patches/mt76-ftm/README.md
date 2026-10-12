# MT7916 timing support

These patches provide opt-in timing reports, marked probes and ACK spatial
extension controls for MANET ranging. The same four patches target both:

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

The final patch adds `MODULE_INFO(manet_timing, "1")`. The firmware selector
checks `modinfo -k "$(uname -r)" -F manet_timing mt7915e` against the installed
module before preparing patched firmware. This tag identifies the timing
build; it does not certify early-boot firmware stability. When extending an
already applied older series, apply the new suffix explicitly before using
the helper; partial-series rejection remains intentional.

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
