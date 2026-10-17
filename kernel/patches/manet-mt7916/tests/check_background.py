#!/usr/bin/env python3
"""Run the real registration, reservation and CAC callback on native EEPROM."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(sys.argv[1]) / 'drivers/net/wireless/mediatek/mt76/mt7915'


def function(file, marker):
    text = (root / file).read_text()
    start = text.index(marker)
    opening = text.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end] + '\n'


# Extract the actual registration predicate, including the DT opt-out.
init = (root / 'init.c').read_text()
end = init.index('NL80211_EXT_FEATURE_RADAR_BACKGROUND);')
start = init.rfind('\n\tif (', 0, end)
registration = init[start:end + len('NL80211_EXT_FEATURE_RADAR_BACKGROUND);')]
eeprom = function('eeprom.c', 'bool\nmt7915_eeprom_has_background_radar(')
marker = 'bool mt7915_eeprom_can_reserve_background_radar('
if marker in (root / 'eeprom.c').read_text():
    eeprom += function('eeprom.c', marker)
transition = (root / 'rxprobe.inc').read_text().split('static int mt7915_rx_probe_show')[0]
callback = function('main.c', 'static int\nmt7915_set_radar_background(')

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
#define MT_EE_WIFI_CONF0_BAND_SEL 0xc0
#define MT_EE_WIFI_CONF0_TX_PATH 0x38
#define MT_EE_WIFI_CONF0_RX_PATH 0x07
#define MT_EE_V2_BAND_SEL_5GHZ 1
#define MT7915_EEPROM_BLOCK_SIZE 16
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
#define NL80211_DFS_UNSET 0
#define NL80211_EXT_FEATURE_RADAR_BACKGROUND 0
#define MCU_EXT_CMD(x) (x)
#define SET_RX_PATH 0x4e
#define WRITE_ONCE(x,v) ((x)=(v))
#define IS_ENABLED(x) (debugfs_enabled)
#define lockdep_assert_held(x) assert(*(x) == 1)
struct wiphy { bool feature; };
struct mt7915_phy;
struct ieee80211_hw { struct wiphy *wiphy; struct mt7915_phy *phy; };
struct ieee80211_vif { int type; };
struct ieee80211_channel { int band, flags; };
struct cfg80211_chan_def { int width; struct ieee80211_channel *chan; };
struct device { bool *of_node; };
struct mt76_dev { int mutex, region; struct { u8 *data; } eeprom; struct device *dev; };
struct mt76_phy { unsigned long state; bool offchannel; int band_idx, antenna_mask;
 u16 chainmask; struct cfg80211_chan_def chandef; struct ieee80211_hw *hw; };
struct mt7915_dev { struct mt76_dev mt76; int chainshift, chainmask;
 bool ibf, background_rx_reserved, rx_probe_failed;
 struct mt7915_phy *rdd2_phy; struct cfg80211_chan_def rdd2_chandef;
 struct { bool dirty, borrowed, cac_attempted; } bg_scan;
 u32 rx_probe_count; u64 rx_probe_at_us, rx_probe_pause_us;
 int rx_probe_error, rx_probe_restore_error; };
struct mt7915_phy { struct mt7915_dev *dev; struct mt76_phy *mt76; };
static int chip = 0x7906, mode = NL80211_IFTYPE_MESH_POINT, starts, stops, holds;
static bool debugfs_enabled = true;
static u64 now = 100000000000;
static u64 ktime_get_boottime_ns(void) { now += 100000; return now; }
static bool is_mt7916(struct mt76_dev *d) { return chip == 0x7906; }
static int mt76_chip(struct mt76_dev *d) { return chip; }
static bool test_bit(int bit, unsigned long *s) { return (*s & (1UL << bit)) != 0; }
static bool cfg80211_chandef_valid(struct cfg80211_chan_def *c) { return c->chan != NULL; }
static int mt7915_mcu_set_mac(struct mt7915_dev *d, int b, bool tx, bool rx) { return 0; }
static int mt7915_mcu_set_chan_info(struct mt7915_phy *p, int cmd) { return 0; }
static void ieee80211_stop_queues(void *hw) {}
static void ieee80211_wake_queues(void *hw) {}
static void ieee80211_iterate_active_interfaces_atomic(void *hw, int flags,
 void (*f)(void *, u8 *, struct ieee80211_vif *), void *data) {
 struct ieee80211_vif vif = { .type = mode }; f(data, NULL, &vif);
}
static int mt7915_mcu_get_eeprom(struct mt7915_dev *d, int offset, u8 *buf) {
 memcpy(buf, d->mt76.eeprom.data + (offset & ~15), 16); return 0;
}
static bool of_property_read_bool(bool *node, const char *name) {
 assert(!strcmp(name,"mediatek,disable-radar-background")); return *node;
}
static void wiphy_ext_feature_set(struct wiphy *w, int feature) { w->feature = true; }
static void mutex_lock(int *mutex) { assert(!*mutex); *mutex = 1; }
static void mutex_unlock(int *mutex) { assert(*mutex == 1); *mutex = 0; }
static struct mt7915_phy *mt7915_hw_phy(struct ieee80211_hw *hw) { return hw->phy; }
static void mt7915_hold_start(struct mt7915_phy *p) { holds++; }
static void mt7915_hold_stop(struct mt7915_dev *d) {}
static void mt7915_bg_scan_stop(struct mt7915_dev *d, int why) { d->bg_scan.dirty = false; }
static void cfg80211_background_cac_abort(struct wiphy *w) { assert(false); }
static int mt7915_mcu_rdd_background_enable(struct mt7915_phy *p, struct cfg80211_chan_def *c) {
 assert(p->dev->mt76.mutex == 1);
 if (c) {
  assert(p->dev->background_rx_reserved && p->mt76->chainmask == 0x0c);
  assert(p->dev->rdd2_phy == p); starts++;
 } else stops++;
 return 0;
}
'''
wrapper = '''
static void advertise(struct mt7915_phy *phy) {
 struct mt7915_dev *dev = phy->dev;
 struct mt76_dev *mdev = &dev->mt76;
 struct mt76_phy *mphy = phy->mt76;
 struct wiphy *wiphy = mphy->hw->wiphy;
 wiphy->feature = false;
''' + registration + '\n}\n'
body = r'''
static int reserve(struct mt7915_phy *p, bool wanted) {
 mutex_lock(&p->dev->mt76.mutex);
 int result = mt7915_rx_probe_transition(p, wanted, false);
 mutex_unlock(&p->dev->mt76.mutex);
 return result;
}
/* Model nl80211's advertised-feature gate before dispatch to the driver. */
static int cac(struct ieee80211_hw *hw, struct cfg80211_chan_def *c) {
 if (!hw->wiphy->feature) return -EOPNOTSUPP;
 return mt7915_set_radar_background(hw, c);
}
int main(int argc, char **argv) {
 u8 ee[4096] = { [0x191] = 0x5b, [0x193] = 0x4c };
 bool disabled = false;
 struct device device = { .of_node = &disabled };
 struct mt7915_dev d = { .mt76 = { .region=1, .eeprom.data=ee, .dev=&device },
  .chainshift=2, .chainmask=0x1f };
 struct wiphy w = {0}; struct ieee80211_hw hw = { .wiphy=&w };
 struct ieee80211_channel home = { .band=1 }, target = { .band=1, .flags=1 };
 struct mt76_phy m = { .band_idx=1, .antenna_mask=3, .chainmask=0x1c,
  .hw=&hw, .chandef={ .width=20, .chan=&home } };
 struct mt7915_phy p = { .dev=&d, .mt76=&m }; hw.phy=&p;
 struct cfg80211_chan_def ch = { .width=20, .chan=&target };
 /* Registration happens before the PHY is running. */
 advertise(&p);
 m.state = 1UL << MT76_STATE_RUNNING;
 assert(argc == 2);
 if (!strcmp(argv[1], "feature")) {
  assert(w.feature);
  disabled=true; advertise(&p); assert(!w.feature); disabled=false;
  m.band_idx=0; advertise(&p); assert(!w.feature); m.band_idx=1;
  chip=0x7986; advertise(&p); assert(!w.feature); chip=0x7906;
  m.antenna_mask=7; advertise(&p); assert(!w.feature); m.antenna_mask=3;
  d.chainshift=1; advertise(&p); assert(!w.feature); d.chainshift=2;
  d.chainmask=0xf; advertise(&p); assert(!w.feature); d.chainmask=0x1f;
  d.ibf=true; advertise(&p); assert(!w.feature); d.ibf=false;
  ee[0x193]=0x20; advertise(&p); assert(!w.feature); ee[0x193]=0x4c;
  ee[0x191]=0xdb; advertise(&p); assert(!w.feature); ee[0x191]=0x5b;
  debugfs_enabled=false; advertise(&p); assert(!w.feature); debugfs_enabled=true;
  /* Existing boot-reservation advertisement and its band restriction survive. */
  d.background_rx_reserved=true; ee[0x191]=0x52; advertise(&p); assert(w.feature);
  m.band_idx=0; advertise(&p); assert(!w.feature);
 } else if (!strcmp(argv[1], "cac")) {
  assert(!reserve(&p, true));
  assert(d.background_rx_reserved && m.chainmask == 0x0c);
  assert(!cac(&hw, &ch));
  assert(starts == 1 && holds == 1 && d.rdd2_phy == &p);
  assert(reserve(&p, false) == -EBUSY);
  assert(!mt7915_set_radar_background(&hw, NULL));
  assert(stops == 1 && !d.rdd2_phy);
  assert(reserve(&p, false) == -EAGAIN);
  now += 60000000000ULL;
  home.flags=IEEE80211_CHAN_RADAR;
  assert(reserve(&p, false) == -EPERM);
  assert(d.background_rx_reserved && m.chainmask == 0x0c);
  home.flags=0;
  assert(!reserve(&p, false));
  assert(!d.background_rx_reserved && m.chainmask == 0x1c);
 } else {
  assert(!strcmp(argv[1], "guards"));
  assert(mt7915_set_radar_background(&hw, &ch) == -EBUSY);
  assert(!starts && !d.rdd2_phy && !d.bg_scan.cac_attempted && !d.rx_probe_count);
  assert(!reserve(&p, true));
  d.rx_probe_failed=true;
  assert(mt7915_set_radar_background(&hw, &ch) == -EBUSY);
  assert(reserve(&p, false) == -EBUSY);
  assert(!starts && !d.rdd2_phy);
  assert(!mt7915_set_radar_background(&hw, NULL));
  assert(d.rx_probe_failed && d.background_rx_reserved);
  d.rx_probe_failed=false; m.chainmask=0x1c;
  assert(mt7915_set_radar_background(&hw, &ch) == -EBUSY);
  m.chainmask=0x0c; d.bg_scan.dirty=true;
  assert(mt7915_set_radar_background(&hw, &ch) == -EBUSY);
  d.bg_scan.dirty=false; m.band_idx=0;
  assert(mt7915_set_radar_background(&hw, &ch) == -EOPNOTSUPP);
  assert(!starts);
 }
 printf("Runtime background CAC %s checks passed\n", argv[1]);
}
'''
with tempfile.TemporaryDirectory() as temp:
    c, exe = Path(temp) / 'background.c', Path(temp) / 'background'
    c.write_text(head + eeprom + wrapper + transition + callback + body)
    subprocess.run(['cc', '-g', '-Wall', '-Werror', '-fsanitize=address,undefined',
                    '-fno-pie', '-no-pie', str(c), '-o', str(exe)], check=True)
    results = [subprocess.run([str(exe), mode], env=dict(os.environ, ASAN_OPTIONS='detect_leaks=0'))
               for mode in ('feature', 'cac', 'guards')]
    sys.exit(any(result.returncode for result in results))
