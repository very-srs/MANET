#!/usr/bin/env python3
"""Execute the actual retune ordering with main-RDD and firmware failures."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
source = (Path(sys.argv[1]) / 'drivers/net/wireless/mediatek/mt76/mt7915/main.c').read_text()
source = source[source.index('int mt7915_set_channel('):source.index('static int mt7915_set_key(')]
head = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <errno.h>
#define MT_DFS_STATE_UNKNOWN 0
#define MT_DFS_STATE_ACTIVE 3
#define MT7915_WATCHDOG_TIME 100
#define MCU_EXT_CMD(x) 0
struct manet_hold_token { int generation; };
struct cfg80211_chan_def { int frequency; };
struct ieee80211_hw { void *wiphy; };
struct mt76_phy { void *priv; struct ieee80211_hw *hw; bool radar_enabled; int dfs_state, band_idx, mac_work; struct cfg80211_chan_def chandef; };
struct mt7915_phy;
struct mt7915_dev { struct mt7915_phy *rdd2_phy; struct cfg80211_chan_def rdd2_chandef; struct {struct mt7915_phy *phy;} bg_scan; bool cal; };
struct mt7915_phy { struct mt7915_dev *dev; struct mt76_phy *mt76; int noise; };
static int sequence, retuned, started, transferred, aborted, quiet, channel_error, detector_error;
static bool token_valid=true, transfer_valid=true;
static bool cfg80211_chandef_identical(struct cfg80211_chan_def *a, struct cfg80211_chan_def *b) {return a->frequency==b->frequency;}
static bool cfg80211_hold_token_v2(void *w, struct manet_hold_token *t) {t->generation=7; return token_valid;}
static void cfg80211_hold_main_stop_v3(void *w) {}
static void cfg80211_background_cac_abort(void *w) {aborted=++sequence;}
static int mt7915_bg_scan_stop(struct mt7915_dev *d,int reason) {return 0;}
static int mt7915_mcu_apply_tx_dpd(struct mt7915_phy *p) {return 0;}
static int mt7915_mcu_set_chan_info(struct mt7915_phy *p,int cmd) {retuned=++sequence; return channel_error;}
static void mt7915_mac_set_timing(struct mt7915_phy *p) {}
static int mt7915_dfs_init_radar_detector(struct mt7915_phy *p) {started=++sequence; if(!detector_error)p->mt76->dfs_state=MT_DFS_STATE_ACTIVE; return detector_error;}
static bool cfg80211_hold_transfer_v3(void *w,struct cfg80211_chan_def *c,struct manet_hold_token *t) {assert(t->generation==7); transferred=++sequence; return transfer_valid;}
static void ieee80211_stop_queues(struct ieee80211_hw *h) {quiet++;}
static int mt7915_mcu_set_mac(struct mt7915_dev *d,int band,bool tx,bool rx) {assert(!tx && !rx); quiet++; return 0;}
static void mt7915_mac_cca_stats_reset(struct mt7915_phy *p) {}
static void mt7915_mac_reset_counters(struct mt7915_phy *p) {}
static bool mt76_testmode_enabled(struct mt76_phy *p) {return false;}
static void ieee80211_queue_delayed_work(struct ieee80211_hw *h,int *w,int t) {}
'''
body = r'''
static void reset(void) {sequence=retuned=started=transferred=aborted=quiet=channel_error=detector_error=0; token_valid=transfer_valid=true;}
int main(void) {
 struct ieee80211_hw hw={0}; struct mt76_phy m={.hw=&hw,.radar_enabled=true,.chandef.frequency=5260};
 struct mt7915_dev d={.rdd2_chandef.frequency=5260}; struct mt7915_phy p={.dev=&d,.mt76=&m}; m.priv=&p; d.rdd2_phy=&p;
 assert(!mt7915_set_channel(&m));
 assert(retuned && started>retuned && transferred>started && !aborted && !quiet);
 reset(); detector_error=-EIO; m.dfs_state=0;
 assert(mt7915_set_channel(&m)==-EIO); assert(!transferred && quiet==2 && aborted>started);
 reset(); channel_error=-EIO;
 assert(mt7915_set_channel(&m)==-EIO); assert(!started && !transferred && quiet==2 && aborted>retuned);
 reset(); transfer_valid=false;
 assert(mt7915_set_channel(&m)==-EACCES); assert(quiet==2 && aborted>transferred);
 reset(); d.rdd2_chandef.frequency=5280;
 assert(!mt7915_set_channel(&m)); assert(aborted && aborted<retuned && !transferred);
 puts("Main retune retains exact held detector until RDD start; failure inhibits TX; unrelated retune revokes");
}
'''
with tempfile.TemporaryDirectory() as temp:
    c, exe = Path(temp)/'transfer.c', Path(temp)/'transfer'
    c.write_text(head+source+body)
    subprocess.run(['cc','-g','-Wall','-Werror','-Wno-unused-function','-fsanitize=address,undefined','-fno-pie','-no-pie',str(c),'-o',str(exe)],check=True)
    subprocess.run([str(exe)],check=True,env=dict(os.environ,ASAN_OPTIONS='detect_leaks=0'))
