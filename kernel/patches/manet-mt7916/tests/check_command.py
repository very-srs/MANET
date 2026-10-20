#!/usr/bin/env python3
"""Check the real ext4E/ext08 wire encoding for the bench chain masks."""
from pathlib import Path
import sys
import os
import subprocess
import tempfile

work = Path(sys.argv[1])
text = (work / 'drivers/net/wireless/mediatek/mt76/mt7915/mcu.c').read_text()
source = text[text.index('int mt7915_mcu_set_chan_info('):text.index('static int mt7915_mcu_set_eeprom_flash(')]
shim = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint8_t u8;
typedef uint16_t __le16;
typedef uint32_t __le32;
#define __packed __attribute__((packed))
#define NL80211_BAND_2GHZ 0
#define NL80211_BAND_5GHZ 1
#define NL80211_BAND_6GHZ 2
#define NL80211_CHAN_WIDTH_80P80 8080
#define IEEE80211_CONF_MONITOR 1
#define IEEE80211_CONF_IDLE 2
#define NL80211_IFTYPE_AP 3
#define CH_SWITCH_NORMAL 0
#define CH_SWITCH_SCAN_BYPASS_DPD 3
#define CH_SWITCH_DFS 5
#define SET_RX_PATH 0x4e
#define CHANNEL_SWITCH 0x08
#define MCU_EXT_CMD(x) (x)
#define hweight16(v) __builtin_popcount((uint16_t)(v))
#define hweight8(v) __builtin_popcount((u8)(v))
#define fls(v) (32 - __builtin_clz(v))
struct ieee80211_channel { int hw_value, band; };
struct cfg80211_chan_def { int center_freq1, center_freq2, width; struct ieee80211_channel *chan; };
struct ieee80211_hw { void *wiphy; struct { int flags; } conf; };
struct mt76_phy { struct cfg80211_chan_def chandef; u8 band_idx, chainmask, antenna_mask; bool offchannel; struct ieee80211_hw *hw; };
struct mt7915_dev { int mt76; u8 chainshift; bool dbdc_support; };
struct mt7915_phy { struct mt7915_dev *dev; struct mt76_phy *mt76; };
static inline u8 mt7915_band_chainshift(struct mt7915_phy *phy) { struct mt7915_dev *dev = phy->dev; if (!dev->dbdc_support) return 0; return phy->mt76->band_idx * dev->chainshift; }
static inline unsigned short mt7915_band_chainmask(struct mt7915_phy *phy) { return phy->mt76->chainmask >> mt7915_band_chainshift(phy); }
static u8 packet[76];
static int command, fail;
static int ieee80211_frequency_to_channel(int f) { return (f - 5000) / 5; }
static int mt76_connac_chan_bw(void *c) { return 0; }
static int mt76_connac_spe_idx(int mask) { assert(mask == 3); return 0; }
static bool cfg80211_reg_can_beacon(void *w, void *c, int t) { return true; }
static int mt76_mcu_send_msg(void *d, int cmd, void *p, unsigned size, bool wait) {
 assert(size == sizeof(packet) && wait);
 memcpy(packet, p, size); command = cmd; return fail;
}
'''
tests = r'''
int main(void) {
 struct mt7915_dev d = { .chainshift = 2, .dbdc_support = true };
 struct ieee80211_channel ch = { .hw_value = 36, .band = NL80211_BAND_5GHZ };
 struct ieee80211_hw hw = {};
 struct mt76_phy m = { .band_idx = 1, .antenna_mask = 3, .hw = &hw,
   .chandef = { .center_freq1 = 5180, .width = 20, .chan = &ch } };
 struct mt7915_phy p = { &d, &m };
 for (unsigned i = 0; i < 2; i++) {
  m.chainmask = i ? 0x0c : 0x1c;
  assert(mt7915_mcu_set_chan_info(&p, SET_RX_PATH) == 0);
  assert(command == 0x4e && packet[0] == 36 && packet[1] == 36);
  assert(packet[3] == (i ? 2 : 3) && packet[4] == (i ? 3 : 7));
  assert(packet[6] == 1 && packet[10] == 1);
  assert(mt7915_mcu_set_chan_info(&p, CHANNEL_SWITCH) == 0);
  assert(command == 0x08 && packet[4] == (i ? 2 : 3));
 }
 m.band_idx = 0; m.chainmask = 3;
 assert(mt7915_mcu_set_chan_info(&p, SET_RX_PATH) == 0);
 assert(packet[3] == 2 && packet[4] == 3 && packet[6] == 0);
 fail = -5;
 assert(mt7915_mcu_set_chan_info(&p, SET_RX_PATH) == -5);
 puts("PASS: ext4E band-local masks, ext08 path counts, band-0 preservation and MCU error propagation");
}
'''
with tempfile.TemporaryDirectory(prefix='reservation-command-') as tmp:
    c = Path(tmp) / 'command.c'; exe = c.with_suffix('')
    c.write_text(shim + source + tests)
    subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                    '-fsanitize=address,undefined', '-fno-pie', '-no-pie',
                    str(c), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True, env={**os.environ, 'ASAN_OPTIONS':'detect_leaks=0'})
