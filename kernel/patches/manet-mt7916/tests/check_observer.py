#!/usr/bin/env python3
"""Reuse the observer harness with the combined driver's additional fixtures."""
from pathlib import Path

source = Path(__file__).resolve().parents[2] / 'mt76-bgobs/check_observer.py'
text = source.read_text()
# Extend only the mock device and its EEPROM predicate. The harness continues
# to extract and execute the unmodified driver functions from the supplied tree.
# Native EEPROM eligibility is exercised separately by check_background.py.
old = 'bool background_rx_reserved; unsigned hw_pattern;'
assert text.count(old) == 1
text = text.replace(old, 'bool background_rx_reserved, rx_probe_failed; unsigned hw_pattern;')
old = 'static struct ieee80211_channel chans[60];'
assert text.count(old) == 1
text = text.replace(old, '''
static bool mt7915_eeprom_can_reserve_background_radar(struct mt7915_phy *p) {
    return true;
}
''' + old)
old = '.antenna_mask=3,.hw=&hw'
assert text.count(old) == 1
text = text.replace(old, '.antenna_mask=3,.chainmask=0x0c,.hw=&hw')
exec(compile(text, str(source), 'exec'))
