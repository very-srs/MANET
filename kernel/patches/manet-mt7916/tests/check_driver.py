#!/usr/bin/env python3
"""Execute real driver RDD ownership code with controlled MCU outcomes."""
import os
from pathlib import Path
import sys
import subprocess
import tempfile

work = Path(sys.argv[1])
source = (work / 'drivers/net/wireless/mediatek/mt76/mt7915/main.c').read_text()
source = source[source.index('static int\nmt7915_set_radar_background'):]
source = source[:source.index('\n#ifdef CONFIG_NET_MEDIATEK_SOC_WED')]
head = r'''
#include <assert.h>
#include <stdbool.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#define MT_BAND1 1
#define NL80211_DFS_UNSET 0
#define IEEE80211_CHAN_RADAR 1
#define WRITE_ONCE(x,v) ((x)=(v))
struct ieee80211_channel { int flags; };
struct cfg80211_chan_def { struct ieee80211_channel *chan; };
struct mt7915_phy;
struct ieee80211_hw { struct mt7915_phy *phy; void *wiphy; };
struct mt76_phy { int band_idx, chainmask; struct ieee80211_hw *hw; };
struct mt7915_dev {
 struct { int mutex, region; } mt76;
 bool background_rx_reserved, rx_probe_failed;
 struct { bool dirty, borrowed, cac_attempted; } bg_scan;
 struct mt7915_phy *rdd2_phy;
 struct cfg80211_chan_def rdd2_chandef;
};
struct mt7915_phy { struct mt7915_dev *dev; struct mt76_phy *mt76; };
static int starts, stops, fail_start, fail_stop, aborts;
static bool mt7915_eeprom_can_reserve_background_radar(struct mt7915_phy *p) { return true; }
static void mt7915_hold_start(struct mt7915_phy *p) {}
static void mt7915_hold_stop(struct mt7915_dev *d) {}
static void mutex_lock(void *p) {}
static void mutex_unlock(void *p) {}
static struct mt7915_phy *mt7915_hw_phy(struct ieee80211_hw *hw) { return hw->phy; }
static void mt7915_bg_scan_stop(struct mt7915_dev *d, int why) { d->bg_scan.dirty=false; }
static bool cfg80211_chandef_valid(struct cfg80211_chan_def *c) { return c->chan != NULL; }
static void cfg80211_background_cac_abort(void *w) { aborts++; }
static int mt7915_mcu_rdd_background_enable(struct mt7915_phy *p, struct cfg80211_chan_def *c) {
 if (c) { starts++; assert(p->dev->rdd2_phy==p); assert(p->dev->rdd2_chandef.chan==c->chan); return fail_start; }
 stops++; return fail_stop;
}
'''
body = r'''
int main(void) {
 struct mt7915_dev dev={.background_rx_reserved=true,.mt76.region=1};
 struct ieee80211_hw hw;
 struct mt76_phy mphy={.band_idx=1,.chainmask=0x0c,.hw=&hw};
 struct mt7915_phy phy={.dev=&dev,.mt76=&mphy};
 struct ieee80211_channel channel={.flags=1};
 struct cfg80211_chan_def ch={.chan=&channel};
 hw.phy=&phy;
 assert(!mt7915_set_radar_background(&hw,&ch));
 assert(starts==1 && stops==0 && dev.rdd2_phy==&phy && dev.bg_scan.cac_attempted);
 dev.mt76.region=0;
 assert(!mt7915_set_radar_background(&hw,NULL));
 assert(stops==1 && !dev.rdd2_phy);
 assert(mt7915_set_radar_background(&hw,&ch)==-EINVAL && starts==1);
 dev.mt76.region=1;
 fail_start=-EIO;
 assert(mt7915_set_radar_background(&hw,&ch)==-EIO);
 assert(aborts==1 && dev.rdd2_phy==&phy);
 fail_stop=-EIO;
 assert(mt7915_set_radar_background(&hw,NULL)==-EIO);
 assert(stops==2 && dev.rdd2_phy==&phy);
 fail_stop=0;
 assert(!mt7915_set_radar_background(&hw,NULL));
 assert(stops==3 && !dev.rdd2_phy);
 fail_start=0; dev.background_rx_reserved=false;
 assert(mt7915_set_radar_background(&hw,&ch)==-EBUSY && starts==2);
 dev.background_rx_reserved=true; dev.bg_scan.dirty=true;
 assert(mt7915_set_radar_background(&hw,&ch)==-EBUSY && starts==2);
 puts("Driver tentative ownership, start/stop failures, one-stop teardown and unset-region stop passed");
}
'''
with tempfile.TemporaryDirectory() as tmp:
    c, exe = Path(tmp) / 'driver.c', Path(tmp) / 'driver'
    c.write_text(head + source + body)
    subprocess.run(['cc', '-g', '-Wall', '-Werror', '-fsanitize=address,undefined',
                    '-fno-pie', '-no-pie', str(c), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True, env=dict(os.environ, ASAN_OPTIONS='detect_leaks=0'))
