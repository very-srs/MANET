#!/usr/bin/env python3
"""Exercise actual main-RDD start and band-specific radar dispatch code."""
from pathlib import Path
import subprocess
import tempfile
import sys
import os
root=Path(sys.argv[1])/'drivers/net/wireless/mediatek/mt76'
def function(file, marker):
    text=(root/file).read_text();start=text.index(marker);opening=text.index('{',start)
    depth=1;end=opening+1
    while depth:
        depth+=(text[end]=='{')-(text[end]=='}');end+=1
    return text[start:end]
state=function('mac80211.c','enum mt76_dfs_state mt76_phy_dfs_state(')
init=function('mt7915/mac.c','int mt7915_dfs_init_radar_detector(')
radar=function('mt7915/mcu.c','mt7915_mcu_rx_radar_detected(')
head=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#define NL80211_DFS_UNSET 0
#define MT76_SCANNING 1
#define IEEE80211_CONF_MONITOR 1
#define IEEE80211_CHAN_RADAR 1
#define NL80211_IFTYPE_AP 3
#define MT_RDD_IDX_BAND0 0
#define MT_RDD_IDX_BAND1 1
#define MT_RDD_IDX_BACKGROUND 2
#define MT_BAND1 1
#define MT7975_ONE_ADIE 1
#define MT7976_ONE_ADIE 2
#define READ_ONCE(x) (x)
#define GFP_ATOMIC 0
#define RDD_CAC_END 10
#define RDD_NORMAL_START 11
#define RDD_SET_WF_ANT 12
#define dev_err(...) ((void)0)
typedef uint32_t u32;
enum mt76_dfs_state {MT_DFS_STATE_UNKNOWN=-1,MT_DFS_STATE_DISABLED,MT_DFS_STATE_CAC,MT_DFS_STATE_ACTIVE};
struct channel {int flags;};
struct chandef {struct channel *chan;};
struct hw {void *wiphy;struct {int flags;} conf;};
#define ieee80211_hw hw
struct mt76_dev;
struct mt76_phy {struct hw *hw;struct mt76_dev *dev;unsigned long state;bool radar_enabled;enum mt76_dfs_state dfs_state;struct chandef chandef;};
struct mt76_dev {int region;struct mt76_phy phy,*phys[2];void *dev;};
struct mt7915_dev;
struct mt7915_phy {struct mt76_phy *mt76;struct mt7915_dev *dev;};
struct mt7915_dev {struct mt76_dev mt76;struct mt7915_phy *rdd2_phy;struct chandef rdd2_chandef;struct {bool dirty,borrowed;} bg_scan;int hw_pattern;bool dbdc_support;};
struct sk_buff {void *data;};
struct mt7915_mcu_rdd_report {int rdd_idx;};
static int starts,ends,stops,main_events,bg_events;
static bool available=true;
static struct hw *detected;
static bool test_bit(int bit,unsigned long *state) {return *state & (1UL<<bit);}
static bool cfg80211_reg_can_beacon(void *w,struct chandef *c,int type) {return available;}
static int mt7915_get_rdd_idx(struct mt7915_phy *p,bool bg) {return bg?2:1;}
static int mt7915_dfs_init_radar_specs(struct mt7915_phy *p) {return 0;}
static int mt7915_dfs_start_radar_detector(struct mt7915_phy *p) {starts++;return 0;}
static void mt7915_dfs_stop_radar_detector(struct mt7915_phy *p) {stops++;}
static int mt76_connac_mcu_rdd_cmd(struct mt76_dev *d,int cmd,int idx,int a,int b) {assert(idx==1);if(cmd==RDD_CAC_END)ends++;return 0;}
static bool is_mt7915(struct mt76_dev *d) {return false;}
static bool is_mt7986(struct mt76_dev *d) {return false;}
static int mt7915_check_adie(struct mt7915_dev *d,bool second) {return 0;}
static void cfg80211_background_radar_event(void *w,struct chandef *c,int gfp) {bg_events++;}
static void ieee80211_radar_detected(struct hw *h,void *ch) {detected=h;main_events++;}
'''
body=r'''
int main(void) {
 struct channel ch={.flags=IEEE80211_CHAN_RADAR};struct hw hw={0};
 struct mt7915_dev d={.mt76.region=1};
 struct mt76_phy m={.hw=&hw,.dev=&d.mt76,.radar_enabled=true,.dfs_state=MT_DFS_STATE_UNKNOWN,.chandef.chan=&ch};
 struct mt7915_phy p={.mt76=&m,.dev=&d}; d.mt76.phys[1]=&m;
 assert(mt76_phy_dfs_state(&m)==MT_DFS_STATE_ACTIVE);
 assert(!mt7915_dfs_init_radar_detector(&p));
 assert(starts==1 && ends==1 && m.dfs_state==MT_DFS_STATE_ACTIVE);
 struct mt7915_mcu_rdd_report event={.rdd_idx=MT_RDD_IDX_BAND1};struct sk_buff skb={.data=&event};
 mt7915_mcu_rx_radar_detected(&d,&skb);
 assert(main_events==1 && !bg_events && detected==&hw);
 event.rdd_idx=MT_RDD_IDX_BACKGROUND; d.rdd2_phy=&p;
 mt7915_mcu_rx_radar_detected(&d,&skb);
 assert(main_events==1 && bg_events==1);
 d.bg_scan.dirty=true;mt7915_mcu_rx_radar_detected(&d,&skb);assert(bg_events==1);
 event.rdd_idx=MT_RDD_IDX_BAND1;mt7915_mcu_rx_radar_detected(&d,&skb);assert(main_events==2);
 m.radar_enabled=false;assert(mt76_phy_dfs_state(&m)==MT_DFS_STATE_DISABLED);
 m.radar_enabled=true;available=false;assert(mt76_phy_dfs_state(&m)==MT_DFS_STATE_CAC);
 puts("AVAILABLE radar-enabled mesh starts main band RDD and CAC_END; band-1 radar uses mac80211; RDD2 remains distinct");
}
'''
with tempfile.TemporaryDirectory() as temp:
    c,exe=Path(temp)/'main.c',Path(temp)/'main'
    c.write_text(head+state+'\n'+init+'\nstatic void\n'+radar+'\n'+body)
    subprocess.run(['cc','-Wall','-Werror','-fsanitize=address,undefined','-fno-pie','-no-pie',str(c),'-o',str(exe)],check=True)
    subprocess.run([str(exe)],check=True,env=dict(os.environ,ASAN_OPTIONS='detect_leaks=0'))
