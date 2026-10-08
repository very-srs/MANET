# MT7916 timing support

These patches provide opt-in timing reports, marked probes and ACK spatial
extension controls for MANET ranging. They target the Raspberry Pi CM4 kernel
`linux-rpi-6.18` at `95b85bebbedcaedfa7ca79116ed38b7376fba412`.

Apply the complete series from the MANET repository:

```sh
python3 kernel/patches/mt76-ftm/apply.py /path/to/linux-rpi-6.18
```

The helper tests the sequence in a temporary tree before applying its combined
diff. A completely applied series is skipped. A partial or conflicting series
fails without changing source files. Existing unrelated changes are retained.
`series` records patch order; the CM4 build script invokes this helper before
configuring the kernel. Each prefix can be built independently.

The root-only MT7916 debugfs controls are `tmr_registers` (band identity and
readback), `tmr_ctrl` (timing enable/role/filter), `tmr_peer` (peer or `off`),
`tmr_mark` (optional skb mark), `tmr_spe` (probe SPE index or `off`), `tmr_rate`
(optional fixed rate/width), and `tmr_ack_spe` (response SPE index or `restore`).
The `mt7915:mt7915_rx_tmr` tracepoint supplies opaque report bytes. ACK control
covers all four BSSID response contexts on the selected band; it is not
per-peer. Userspace must serialize use and restore the saved state.

No debugfs writes means no probe selection or register writes. The TX path has
one disabled peer check; report tracing uses the kernel tracepoint static key.
The feature does not enable the positioning service or its production radio
capability gate.
