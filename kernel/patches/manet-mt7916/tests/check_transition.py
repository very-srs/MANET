#!/usr/bin/env python3
"""Execute the actual bench transition with faulted MCU calls, without radios."""
from pathlib import Path
import sys
import subprocess
import os
import tempfile
work = Path(sys.argv[1])
source = (work / 'drivers/net/wireless/mediatek/mt76/mt7915/rxprobe.inc').read_text().split('static int mt7915_rx_probe_show')[0]
head = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <errno.h>
#include <string.h>
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint64_t u64;
#define MT_EE_WIFI_CONF 0x190
#define MT_EE_WIFI_CONF_STREAM_NUM 0xe0
#define u8_get_bits(v,m) (((v) & (m)) >> __builtin_ctz(m))
#define MT_BAND1 1
#define MT76_STATE_RUNNING 0
#define MT76_RESET 1
#define MT76_SCANNING 2
#define NL80211_CHAN_WIDTH_20 20
#define NL80211_BAND_5GHZ 1
#define IEEE80211_CHAN_RADAR 1
#define IEEE80211_CHAN_DISABLED 2
#define IEEE80211_CHAN_NO_IR 4
#define NL80211_IFTYPE_MESH_POINT 1
#define IEEE80211_IFACE_ITER_NORMAL 0
#define MCU_EXT_CMD(x) (x)
#define SET_RX_PATH 0x4e
#define hweight8(v) __builtin_popcount((u8)(v))
#define ffs(v) __builtin_ffs(v)
#define GENMASK(h,l) (((1U << ((h) + 1)) - 1) & ~((1U << (l)) - 1))
#define mutex_lock(...) ((void)0)
#define mutex_unlock(...) ((void)0)
#define mt76_set_stream_caps(...) ((void)0)
#define mt7915_set_stream_vht_txbf_caps(...) ((void)0)
#define mt7915_set_stream_he_caps(...) ((void)0)
#define is_mt7981(...) false
#define mt7915_hw_phy(hw) ((hw)->phy)
#define mt7915_hw_dev(hw) ((hw)->phy->dev)
#define lockdep_assert_held(x) ((void)(x))
struct ieee80211_vif { int type; };
struct ieee80211_channel { int band, flags; };
struct cfg80211_chan_def { int width; struct ieee80211_channel *chan; };
struct mt76_phy { unsigned long state; bool offchannel; int band_idx, antenna_mask; u16 chainmask; struct cfg80211_chan_def chandef; void *hw; };
struct mt7915_dev { struct { int mutex; struct { u8 *data; } eeprom; } mt76; int chainshift, chainmask; bool ibf, background_rx_reserved, rx_probe_failed; void *rdd2_phy; struct { bool dirty; } bg_scan; u32 rx_probe_count; u64 rx_probe_at_us, rx_probe_pause_us; int rx_probe_error, rx_probe_restore_error; };
struct mt7915_phy { struct mt7915_dev *dev; struct mt76_phy *mt76; };
struct wiphy { u32 available_antennas_tx; };
struct ieee80211_hw { struct wiphy *wiphy; struct mt7915_phy *phy; };
static int calls, failures, mask_seen[8], mode, chip = 1, stopped;
static u64 now = 100000000000;
static u64 ktime_get_boottime_ns(void) { now += 100000; return now; }
static bool is_mt7916(void *dev) { return chip; }
static bool test_bit(int bit, unsigned long *s) { return (*s & (1UL << bit)) != 0; }
static bool cfg80211_chandef_valid(struct cfg80211_chan_def *c) { return c->chan != NULL; }
static int result(void) { return failures & (1 << calls++) ? -EIO : 0; }
static int mt7915_mcu_set_mac(struct mt7915_dev *d, int b, bool tx, bool rx) { assert(b == 1 && tx == rx); return result(); }
static int mt7915_mcu_set_chan_info(struct mt7915_phy *p, int cmd) { assert(cmd == 0x4e); mask_seen[calls] = p->mt76->chainmask; return result(); }
static void ieee80211_stop_queues(void *hw) { stopped = 1; }
static void ieee80211_wake_queues(void *hw) { stopped = 0; }
static void ieee80211_iterate_active_interfaces_atomic(void *hw, int flags, void (*f)(void *, u8 *, struct ieee80211_vif *), void *data) { struct ieee80211_vif vif = { .type = mode }; f(data, NULL, &vif); }
'''
main_source = (work / 'drivers/net/wireless/mediatek/mt76/mt7915/main.c').read_text()
start = main_source.index('static int\nmt7915_set_antenna(')
end = main_source.index('static void mt7915_sta_statistics(', start)
source += main_source[start:end]
body = r''' 
int main(void) {
 u8 ee[4096] = { [0x191] = 0x5b, [0x193] = 0x4c };
 struct mt7915_dev dev;
 struct mt76_phy mphy;
 struct ieee80211_channel ch;
 struct mt7915_phy phy = { .dev = &dev, .mt76 = &mphy };
 for (int failure = 0; failure < 16; failure++) {
  memset(&dev, 0, sizeof(dev)); memset(&mphy, 0, sizeof(mphy)); memset(&ch, 0, sizeof(ch));
  dev.mt76.eeprom.data = ee; dev.chainshift = 2; dev.chainmask = 0x1f;
  mphy.state = 1; mphy.band_idx = 1; mphy.antenna_mask = 3; mphy.chainmask = 0x1c;
  mphy.chandef.width = 20; mphy.chandef.chan = &ch; ch.band = 1;
  mode = 1; failures = failure; calls = 0; stopped = 0;
  int ret = mt7915_rx_probe_transition(&phy, true, false);
  assert(mphy.antenna_mask == 3);
  if (dev.rx_probe_failed) { assert(stopped && !dev.background_rx_reserved && ret); }
  else if (ret) { assert(!stopped && mphy.chainmask == 0x1c && !dev.background_rx_reserved); }
  else {
   assert(!stopped && mphy.chainmask == 0x0c && dev.background_rx_reserved);
   assert(mt7915_rx_probe_transition(&phy, false, false) == -EAGAIN);
   now += 60000000000ULL; failures = 0;
   assert(!mt7915_rx_probe_transition(&phy, false, false));
   assert(mphy.chainmask == 0x1c && !dev.background_rx_reserved && !stopped);
  }
 }
 memset(&dev, 0, sizeof(dev)); dev.mt76.eeprom.data = ee; dev.chainshift = 2; dev.chainmask = 0x1f;
 mphy.chainmask = 0x1c; calls = 0; failures = 0;
 dev.rdd2_phy = &dev; assert(mt7915_rx_probe_transition(&phy, true, false) == -EBUSY); dev.rdd2_phy = NULL;
 dev.bg_scan.dirty = true; assert(mt7915_rx_probe_transition(&phy, true, false) == -EBUSY); dev.bg_scan.dirty = false;
 dev.ibf = true; assert(mt7915_rx_probe_transition(&phy, true, false) == -EOPNOTSUPP); dev.ibf = false;
 mode = 2; assert(mt7915_rx_probe_transition(&phy, true, false) == -EOPNOTSUPP); mode = 1;
 ch.flags = 1; assert(mt7915_rx_probe_transition(&phy, true, false) == -EPERM); ch.flags = 0;
 mphy.antenna_mask = 7; assert(mt7915_rx_probe_transition(&phy, true, false) == -EOPNOTSUPP); mphy.antenna_mask = 3;
 mphy.state = 1 | (1 << MT76_SCANNING); assert(mt7915_rx_probe_transition(&phy, true, false) == -EBUSY);
 mphy.state = 1;
 ee[0x191] = 0xd2; assert(mt7915_rx_probe_transition(&phy, true, false) == -EOPNOTSUPP);
 ee[0x191] = 0x52; assert(mt7915_rx_probe_transition(&phy, true, false) == -EOPNOTSUPP);
 ee[0x191] = 0x5b; ee[0x193] = 0x20; assert(mt7915_rx_probe_transition(&phy, true, false) == -EOPNOTSUPP);
 ee[0x193] = 0x4c;
 dev.background_rx_reserved = true; assert(mt7915_rx_probe_transition(&phy, true, false) == -EINVAL);
 dev.background_rx_reserved = false;
 mphy.chainmask = 0x0c; assert(mt7915_rx_probe_transition(&phy, true, false) == -EINVAL);
 assert(calls == 0);
 struct wiphy wiphy = { .available_antennas_tx = 3 };
 struct ieee80211_hw hw = { &wiphy, &phy };
 dev.rx_probe_count = 1; dev.background_rx_reserved = true;
 mphy.chainmask = 0x0c; mphy.antenna_mask = 3;
 assert(mt7915_set_antenna(&hw, 0, 3, 3) == -EBUSY);
 assert(mt7915_set_antenna(&hw, 0, 1, 1) == -EBUSY);
 assert(mphy.chainmask == 0x0c && mphy.antenna_mask == 3);
 dev.rx_probe_count = 0; dev.background_rx_reserved = false;
 assert(mt7915_set_antenna(&hw, 0, 3, 3) == 0 && mphy.chainmask == 0x1c);
 puts("Transition gates, command failures, rollback, rate limit, masks and queue ownership passed");
}
'''
with tempfile.TemporaryDirectory() as tmp:
 c = Path(tmp) / 'transition.c'; exe = Path(tmp) / 'transition'
 c.write_text(head + source + body)
 subprocess.run(['cc', '-g', '-Wall', '-Werror', '-Wno-unused-function', '-fsanitize=address,undefined', '-fno-pie', '-no-pie', str(c), '-o', str(exe)], check=True)
 subprocess.run([str(exe)], check=True, env=dict(os.environ, ASAN_OPTIONS='detect_leaks=0'))
