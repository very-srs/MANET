#!/usr/bin/env python3
"""Execute FTM probe/status helpers extracted from the combined driver."""
from pathlib import Path
import subprocess
import sys

driver = Path(sys.argv[1]) / 'drivers/net/wireless/mediatek/mt76/mt7915'
output = Path(sys.argv[2])
mac = (driver / 'mac.c').read_text()
start = mac.index('static void mt7915_mac_add_txs_skb(')
status = mac[start:mac.index('\nstatic void mt7915_mac_add_txs(', start)]
flag = next(line for line in mac.splitlines() if line.startswith('#define MT7915_TX_CB_TMR '))
start = mac.index('static bool mt7915_tmr_selected(')
txwi = mac[start:mac.index('\nint mt7915_tx_prepare_skb(', start)]
probe = (driver / 'tmr.c').read_text()
start = probe.index('#define MT7915_TMR_PROBE_SKIPPED ')
helpers = probe[start:probe.index('\nstruct mt7915_tmr_lookup', start)]
start = probe.index('static ssize_t mt7915_tmr_probe_write(')
io = probe[start:probe.index('\nconst struct file_operations', start)]
template = Path(__file__).resolve().parents[2] / 'mt76-ftm/timing_test.c'
harness = template.read_text().replace('/* DRIVER_HELPERS */', flag + '\n' + status)
harness = harness.replace('/* PROBE_HELPERS */', txwi + helpers + io)
source, exe = output / 'timing_test.c', output / 'timing_test'
source.write_text(harness)
subprocess.run(['cc', '-std=gnu11', '-Wall', '-Werror', '-Wno-unused-parameter',
                '-fsanitize=address,undefined', '-fno-pie', '-no-pie',
                '-o', str(exe), str(source)], check=True)
subprocess.run([str(exe)], check=True)
