#!/usr/bin/env python3
"""Actual passive poller with counter, MCU, reset and scheduler faults."""
from pathlib import Path
import sys
import os
import subprocess
import tempfile
w = Path(sys.argv[1])
output = Path(sys.argv[2])
head=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <stddef.h>
#include <errno.h>
#include <stdio.h>
typedef uint64_t u64;
typedef uint32_t u32;
#define READ_ONCE(x) (x)
#define module_param_named(a,b,c,d)
#define MODULE_PARM_DESC(a,b)
#define BIT(n) (1U << (n))
#define GENMASK(h,l) (((1U << ((h)+1))-1) & ~((1U << (l))-1))
#define ARRAY_SIZE(x) (sizeof(x)/sizeof((x)[0]))
#define max(a,b) ((a)>(b)?(a):(b))
#define min_t(t,a,b) ((t)(a)<(t)(b)?(t)(a):(t)(b))
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define lockdep_assert_held(p) ((void)(p))
#define MT76_STATE_RUNNING 0
#define MT76_SCANNING 1
#define MT7915_BG_MAX_ELAPSED_US 6000000
static u64 now_us=1000000;
#define ktime_get_boottime() now_us
#define ktime_to_us(v) (v)
static u64 div_u64(u64 n,u32 d) { return n/d; }
static bool test_bit(int bit, unsigned long *v) { return !!(*v & BIT(bit)); }
static void mutex_lock(void *p) {}
static void mutex_unlock(void *p) {}
struct work_struct { int queued; };
struct delayed_work { struct work_struct work; };
static void *system_wq;
static unsigned long msecs_to_jiffies(unsigned int n) { return n; }
static void mod_delayed_work(void *q,struct delayed_work *w, unsigned long delay) { w->work.queued=1; }
static void cancel_delayed_work(struct delayed_work *w) { w->work.queued=0; }
struct wiphy {};
'''+(w/'net/wireless/hold-api.h').read_text()+r'''
struct ieee80211_hw { struct wiphy *wiphy; };
struct mt76_phy { struct ieee80211_hw *hw; struct { u64 cc_tx; } *chan_state; unsigned long state; };
struct mt7915_dev;
struct mt7915_phy { struct mt7915_dev *dev; struct mt76_phy *mt76; };
struct mt7915_ipi_sample { u32 bins[12], tx_assert_us; };
struct mt7915_bg_scan {
 int threshold; u64 elapsed_us; u32 wrap_mask, sample_count, busy_permille, busy_raw_permille;
 struct mt7915_ipi_sample first, last, delta;
};
'''
h=(w/'drivers/net/wireless/mediatek/mt76/mt7915/mt7915.h').read_text()
head+=h[h.index('struct mt7915_hold_monitor {'):h.index('struct mt7915_dev {')]
head+=r'''
struct mt7915_dev {
 struct { int mutex; } mt76;
 struct { bool dirty; } bg_scan;
 struct mt7915_hold_monitor hold;
 struct mt7915_phy *rdd2_phy;
};
static struct manet_hold_token token={.epoch={1,2},.generation=3};
static struct manet_hold_sample latest;
static bool owner=true,ready=true,detector=true;
static int publications,aborts,mcu_error;
static u32 bins[12];
static u64 mib_delay, tx_counter;
bool cfg80211_hold_token_v2(struct wiphy *w, struct manet_hold_token *t) { *t=token; return owner; }
bool cfg80211_hold_sample_v2(struct wiphy *w,const struct manet_hold_token *t,const struct manet_hold_sample *s) {
 assert(!memcmp(t,&token,sizeof(token))); latest=*s; publications++; return owner;
}
static void cfg80211_background_cac_abort(struct wiphy *w) { aborts++; owner=false; }
static bool mt7915_bg_scan_mcu_ready(struct mt7915_dev *d) { return ready; }
static int mt7915_mcu_get_chan_mib_info(struct mt7915_phy *p,bool clear) {
 assert(!clear); now_us+=mib_delay; p->mt76->chan_state->cc_tx=tx_counter; return mcu_error;
}
static u32 mt76_rr(struct mt7915_dev *d,u32 address) {
 if (address==0x831a5c00) return detector;
 assert(address>=0x83121000 && address<0x83121030 && !(address%4));
 return bins[(address-0x83121000)/4];
}
'''
s=(w/'drivers/net/wireless/mediatek/mt76/mt7915/bgscan.c').read_text()
source=s[s.index('static int mt7915_bg_scan_calculate'):s.index('/* Tested cold-start configuration')]
source+=(w/'drivers/net/wireless/mediatek/mt76/mt7915/hold-driver.inc').read_text()
body=r'''
static struct mt7915_dev dev;
static struct mt76_phy mphy;
static struct mt7915_phy phy={.dev=&dev,.mt76=&mphy};
static struct ieee80211_hw hw;
static void init(void) {
 memset(&dev,0,sizeof(dev)); memset(bins,0,sizeof(bins));
 mphy.hw=&hw; static typeof(*mphy.chan_state) state; mphy.chan_state=&state;
 mphy.state=BIT(MT76_STATE_RUNNING); dev.rdd2_phy=&phy;
 owner=ready=detector=true; mt7915_hold_pause=false; publications=aborts=mcu_error=0;
 mib_delay=tx_counter=0; now_us=1000000;
 mt7915_hold_start(&phy);
 assert(dev.hold.active && dev.hold.token.generation==token.generation);
 mt7915_hold_work(&dev.hold.work.work);
 assert(!publications && dev.hold.active);
}
static void tick(u64 us, int bin) {
 now_us+=us;
 bins[bin]=(bins[bin]+us/8) & GENMASK(22,0);
 mt7915_hold_work(&dev.hold.work.work);
}
int main(void) {
 init(); tick(250000,0);
 assert(publications==1 && latest.valid && latest.energy_permille==0 && latest.sample_count==31250);
 tick(250000,3); assert(latest.energy_permille==1000 && latest.valid);
 now_us+=250000; mt7915_hold_work(&dev.hold.work.work);
 assert(aborts==1 && !dev.hold.active); /* frozen histogram */
 init(); mt7915_hold_pause=true; tick(250000,0);
 assert(!publications && !aborts && dev.hold.active);
 owner=false; tick(250000,0); assert(aborts && !dev.hold.active);
 init(); detector=false; tick(250000,0); assert(aborts && !publications);
 init(); mcu_error=-EIO; tick(250000,0); assert(aborts && !publications);
 init(); token.generation++; tick(250000,0); assert(aborts && !publications);
 init(); ready=false; tick(250000,0); assert(aborts && !publications);
 init(); mphy.state=0; tick(250000,0); assert(aborts && !publications);
 init(); dev.bg_scan.dirty=true; tick(250000,0); assert(aborts && !publications);
 init(); tick(MANET_HOLD_MAX_AGE_US,0); assert(aborts && !publications);
 init(); mib_delay=MANET_HOLD_MAX_AGE_US; tick(250000,0); assert(aborts && !publications);
 init(); tx_counter=240000; tick(250000,10);
 assert(publications==1 && !latest.valid && dev.hold.active); /* blanking is not zero occupancy */
 init(); tx_counter=100000; tick(250000,0);
 assert(publications==1 && !latest.valid); /* inconsistent TX/bin10 */
 init(); mib_delay=11000; tick(250000,0);
 assert(publications==1 && !latest.valid && dev.hold.active);
 init(); bins[0]=GENMASK(22,0)-100; dev.hold.previous.bins[0]=bins[0];
 tick(250000,0); assert(latest.valid && latest.sample_count==31250);
 mt7915_hold_stop(&dev); assert(!dev.hold.active && !dev.hold.work.work.queued);
 owner=false; mt7915_hold_start(&phy); assert(!dev.hold.active);
 puts("Passive monitor: cadence, wrap, freeze, token, reset, MCU delay, TX validity and release passed");
}
'''
with tempfile.TemporaryDirectory() as tmp:
 c=Path(tmp)/'monitor.c'; exe=Path(tmp)/'monitor'; c.write_text(head+source+body)
 subprocess.run(['cc','-g','-Wall','-Werror','-fsanitize=address,undefined','-fno-pie','-no-pie',str(c),'-o',str(exe)],check=True)
 subprocess.run([str(exe)],check=True,env=dict(os.environ,ASAN_OPTIONS='detect_leaks=0'))
