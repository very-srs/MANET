#!/usr/bin/env python3
"""Compile the actual 0018 power controller with mocked kernel/MCU plumbing."""
from pathlib import Path
import subprocess
import sys
import tempfile

HERE = Path(sys.argv[1]) / 'drivers/net/wireless/mediatek/mt76/mt7915'


def block(src, start):
    a = src.index(start)
    b = src.index('{', a)
    e, n = b + 1, 1
    while n:
        n += (src[e] == '{') - (src[e] == '}')
        e += 1
    return src[a:e]


head = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <stddef.h>
#include <string.h>
typedef uint8_t u8;typedef int8_t s8;typedef uint16_t u16;typedef int16_t s16;typedef uint32_t u32;typedef uint32_t __le32;
#define HZ 100
#define U16_MAX UINT16_MAX
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x,v) ((x)=(v))
#define BIT(n) (1u<<(n))
#define GENMASK(h,l) ((~0u<<(l))&(~0u>>(31-(h))))
#define FIELD_GET(mask,v) (((v)&(mask))>>__builtin_ctz(mask))
#define le32_to_cpu(v) (v)
#define ARRAY_SIZE(x) (sizeof(x)/sizeof((x)[0]))
#define container_of(p,t,m) ((t*)((char*)(p)-offsetof(t,m)))
#define min(a,b) ((a)<(b)?(a):(b))
#define max(a,b) ((a)>(b)?(a):(b))
#define min_t(t,a,b) min((t)(a),(t)(b))
#define max_t(t,a,b) max((t)(a),(t)(b))
#define clamp_t(t,x,a,b) min_t(t,max_t(t,x,a),b)
#define time_after(a,b) ((long)((b)-(a))<0)
#define time_before(a,b) time_after(b,a)
#define hweight8(x) __builtin_popcount((u8)(x))
#define hweight16(x) __builtin_popcount((u16)(x))
#define lockdep_assert_held(x) ((void)0)
#define spin_lock_bh(x) ((void)0)
#define spin_unlock_bh(x) ((void)0)
#define module_param_named(...)
#define MODULE_PARM_DESC(...)
#define dev_warn_ratelimited(...) ((void)0)
#define MT7915_WTBL_STA 288
#define MT_PACKET_ID_FIRST 3
#define MT_PHY_TYPE_HE_SU 8
#define MT_PHY_TYPE_HE_EXT_SU 9
#define MT7915_TX_PATHS_SINGLE 2
#define NL80211_TX_POWER_AUTOMATIC 0
#define NL80211_TX_POWER_LIMITED 1
#define NL80211_IFTYPE_MESH_POINT 7
#define IEEE80211_TX_CTL_RATE_CTRL_PROBE BIT(0)
#define IEEE80211_TX_CTL_NO_ACK BIT(1)
#define IEEE80211_TX_CTL_HW_80211_ENCAP BIT(2)
static unsigned long jiffies=100*HZ;
struct ewma_avg_signal {unsigned long internal;};
static unsigned long ewma_avg_signal_read(struct ewma_avg_signal*p){return p->internal>>10;}
static void ewma_avg_signal_init(struct ewma_avg_signal*p){p->internal=0;}
static void ewma_avg_signal_add(struct ewma_avg_signal*p,int x){p->internal=p->internal?((p->internal*7+x*1024ul)/8):x*1024ul;}
struct mt7915_phy;
struct mt7915_vif {struct mt7915_phy*phy;};
struct ieee80211_vif {int type;struct mt7915_vif drv_priv[1];};
struct mt76_tx_cb {unsigned long jiffies;u16 wcid;u8 pktid,flags;u32 rate_generation;};
struct ieee80211_tx_info {u32 flags;};
struct ieee80211_hdr {u16 frame_control;};
struct sk_buff {u8 data[64];struct ieee80211_tx_info info;struct mt76_tx_cb cb;};
#define IEEE80211_SKB_CB(s) (&(s)->info)
static struct mt76_tx_cb*mt76_tx_skb_cb(struct sk_buff*s){return &s->cb;}
static bool ieee80211_is_data_present(u16 fc){return (fc&0x4c)==8;}
'''
# TX rate and TX status fields from mt76_connac2_mac.h, identical on both
# pinned kernels; the test tree holds only the files the series touches.
head += r'''
#define MT_TX_RATE_STBC			BIT(13)
#define MT_TX_RATE_NSS			GENMASK(12, 10)
#define MT_TX_RATE_MODE			GENMASK(9, 6)
#define MT_TX_RATE_SU_EXT_TONE		BIT(5)
#define MT_TX_RATE_DCM			BIT(4)
#define MT_TX_RATE_IDX			GENMASK(5, 0)
#define MT_TXS0_FIXED_RATE		BIT(31)
#define MT_TXS0_BW			GENMASK(30, 29)
#define MT_TXS0_TID			GENMASK(28, 26)
#define MT_TXS0_AMPDU			BIT(25)
#define MT_TXS0_TXS_FORMAT		GENMASK(24, 23)
#define MT_TXS0_BA_ERROR		BIT(22)
#define MT_TXS0_PS_FLAG			BIT(21)
#define MT_TXS0_TXOP_TIMEOUT		BIT(20)
#define MT_TXS0_BIP_ERROR		BIT(19)
#define MT_TXS0_QUEUE_TIMEOUT		BIT(18)
#define MT_TXS0_RTS_TIMEOUT		BIT(17)
#define MT_TXS0_ACK_TIMEOUT		BIT(16)
#define MT_TXS0_ACK_ERROR_MASK		GENMASK(18, 16)
#define MT_TXS0_TX_STATUS_HOST		BIT(15)
#define MT_TXS0_TX_STATUS_MCU		BIT(14)
#define MT_TXS0_TX_RATE			GENMASK(13, 0)
'''
head += block((HERE / 'mt7915.h').read_text(), 'struct mt7915_peer_power {') + ';\n'
head += r'''
struct mt7915_sta {struct {bool sta,sta_disabled;u16 idx;} wcid;struct mt7915_vif*vif;u8 tx_paths;struct ewma_avg_signal path_signal;unsigned long path_signal_jiffies;u16 path_signal_fc,path_signal_index;bool path_signal_by_index;struct mt7915_peer_power power;};
struct ieee80211_sta {u8 addr[6];struct {u8 rx_nss,bandwidth;struct {bool has_he;} he_cap;struct {int type,power;} txpwr;} deflink;struct mt7915_sta drv_priv[1];};
struct mt76_phy {u8 antenna_mask;u16 chainmask;int txpower_cur;void*hw;struct {u16 center_freq1;} chandef;};
struct mt7915_dev {bool peer_spe;struct {int sta_poll_lock,mutex;void*dev;} mt76;};
struct mt7915_phy {struct mt7915_dev*dev;struct mt76_phy*mt76;u16 power_cursor;bool power_active;};
static struct ieee80211_sta *registry[20];static int count,scans,lookups;
static s16 firmware[MT7915_WTBL_STA];static int commands,fail,minimum_calls;
static bool is_mt7916(void*p){return true;}
static int mt76_tx_power_path_delta(int n){return n==3?4:2;}
static int mt7915_mcu_set_txpower_frame_min(struct mt7915_phy*p,int n){minimum_calls++;return 0;}
static int mt7915_mcu_set_txpower_limit(struct mt7915_phy*p,struct ieee80211_vif*v,struct ieee80211_sta*s,s16 limit){commands++;if(fail){fail=0;return -EIO;}firmware[s->drv_priv[0].wcid.idx]=limit;return 0;}
static int mt7915_mcu_set_txpower_frame(struct mt7915_phy*p,struct ieee80211_vif*v,struct ieee80211_sta*s,s16 limit){return mt7915_mcu_set_txpower_limit(p,v,s,s->deflink.txpwr.type?limit:-1);}
static void ieee80211_iterate_stations_atomic(void*h,void(*fn)(void*,struct ieee80211_sta*),void*p){scans++;for(int i=0;i<count;i++)fn(p,registry[i]);}
static struct ieee80211_sta*ieee80211_find_sta_by_ifaddr(void*h,const u8*a,const u8*l){lookups++;for(int i=0;i<count;i++)if(!memcmp(a,registry[i]->addr,6))return registry[i];return NULL;}
'''
head += r'''
struct mt76_rx_status {u8 chains;int chain_signal[4];};
static int mt76_rx_signal(u8 chains,int *signal){return signal[0];}
#define MT7915_TX_PATHS_SAMPLE_AGE (10*HZ)
static bool tx_paths_auto=true;
extern bool mt7915_txpower_auto;
#define ether_addr_equal(a,b) (!memcmp(a,b,6))
'''
rx = (HERE / 'mac.c').read_text()
head += block(rx, 'static void\nmt7915_mac_sample_path_signal(') + '\n'
head += block(rx, 'struct mt7915_basic_signal {') + ';\n'
head += block(rx, 'static bool\nmt7915_mac_sample_basic_peer(') + '\n'
head += block(rx, 'static void mt7915_mac_sample_basic(') + '\n'
source = (HERE / 'power.c').read_text()
source = source[source.index('bool mt7915_txpower_auto'):]
tests = r'''
static int input_signal=-34;
static void beacon(struct ieee80211_sta*s,int signal){struct mt7915_sta*m=s->drv_priv;m->path_signal.internal=(-signal)*1024ul;m->path_signal_jiffies=jiffies;}
static void raw_status(struct mt7915_dev*d,struct ieee80211_sta*s,u32 word){struct sk_buff skb={.data={8},.cb.pktid=3};u32 raw[8]={word,0};mt7915_power_tx_prepare(d,s,&skb);mt7915_power_tx_status(d,s,&skb,raw);}
static void status(struct mt7915_dev*d,struct ieee80211_sta*s,int mcs,bool ack){raw_status(d,s,(8<<6)|(1<<10)|mcs|(ack?0:BIT(16)));}
static void step(struct mt7915_phy*p,struct ieee80211_sta*s,int mcs,bool ack){jiffies+=HZ/4;beacon(s,input_signal);status(p->dev,s,mcs,ack);mt7915_power_work(p);}
static void run(struct mt7915_phy*p,struct ieee80211_sta*s,int secs,int mcs){for(int i=0;i<secs*4;i++)step(p,s,mcs,true);}
static void dither(struct mt7915_phy*p,struct ieee80211_sta*s,int secs){for(int i=0;i<secs*4;i++)step(p,s,10+(i%2),i%20!=0);}
static void light(struct mt7915_phy*p,struct ieee80211_sta*s,int secs,int ticks){for(int i=0;i<secs*4;i++){jiffies+=HZ/4;beacon(s,input_signal);if((i+1)%ticks==0)status(p->dev,s,11,true);mt7915_power_work(p);}}
static void reset(struct mt7915_phy*p,struct ieee80211_sta*s){mt7915_power_init(p->dev,s);mt7915_power_work(p);}
int main(void){
 struct mt7915_dev d={.peer_spe=true};struct mt76_phy radio={.antenna_mask=3,.chainmask=0x1c,.txpower_cur=42,.chandef.center_freq1=5180};
 struct mt7915_phy phy={.dev=&d,.mt76=&radio};struct ieee80211_vif v={.type=7,.drv_priv[0].phy=&phy};
 struct ieee80211_sta a={.addr={1},.deflink={.rx_nss=2,.he_cap.has_he=true},.drv_priv[0]={.vif=v.drv_priv,.wcid={.sta=true,.idx=1}}};
 struct mt7915_peer_power*p=&a.drv_priv[0].power;registry[0]=&a;count=1;mt7915_power_init(&d,&a);
 assert(sizeof(struct mt76_tx_cb)<=16);
 assert(mt7915_txpower_auto); // shipped default
 mt7915_txpower_auto=false;mt7915_power_work(&phy);assert(!commands&&!scans); // off: no scan
 mt7915_txpower_auto=true;mt7915_power_work(&phy);assert(firmware[1]==-1&&p->ready);
 dither(&phy,&a,180);assert(p->limit==15&&firmware[1]==15&&!p->backoff);
 assert(p->baseline_mcs==10*256+128&&p->baseline_per==5);
 // One mode outlier is a ratio too; it must not turn into zero mean MCS.
 raw_status(&d,&a,(1<<10)|10);dither(&phy,&a,10);assert(p->limit==15&&!p->backoff);
 // Clean pre-cap ACK history must tolerate occasional post-cap errors too.
 reset(&phy,&a);for(int sample=0;sample<48;sample++)step(&phy,&a,10+(sample%2),true);
 assert(p->limit==15&&p->baseline_per==0);
 dither(&phy,&a,60);assert(p->limit==15&&!p->backoff);
 puts("PASS: 10/11 dither, occasional ACK errors and a mode outlier retain the cap");
 run(&phy,&a,10,9);assert(p->limit==-1&&p->backoff==300);
 unsigned long retry=p->retry_at;
 // Relearn an uncapped dither baseline, but do not retry before the deadline.
 int i=0;while(time_before(jiffies+HZ/4,retry)){int sample=i++;step(&phy,&a,10+(sample%2),sample%20!=0);assert(p->limit==-1);}
 for(i=0;i<4&&p->limit<0;i++)step(&phy,&a,10+(i%2),true);
 assert(p->limit==15&&p->backoff==300);
 run(&phy,&a,10,8);assert(p->limit==-1&&p->backoff==600);
 struct mt7915_peer_power retry_probe={0};
 int delays[]={300,600,1200,2400,3600,3600};
 for(i=0;i<6;i++){mt7915_power_backoff(&retry_probe,jiffies);assert(retry_probe.backoff==delays[i]&&retry_probe.retry_at==jiffies+delays[i]*HZ);}
 puts("PASS: sustained loss clears, unchanged signal retries at five minutes and backs off to one hour");
 reset(&phy,&a);run(&phy,&a,12,11);assert(p->limit==15);
 for(i=0;i<40;i++)step(&phy,&a,11,i%2);assert(p->limit==-1&&p->backoff==300);
 puts("PASS: sustained error-ratio increase clears instead of a single-error trip");
 reset(&phy,&a);run(&phy,&a,11,11);assert(p->limit==15);
 status(&d,&a,10,true);mt7915_power_work(&phy);assert(p->limit==15);
 run(&phy,&a,2,11);assert(p->limit==15&&!p->backoff);
 run(&phy,&a,10,10);assert(p->limit==-1&&p->backoff==300);

 reset(&phy,&a);run(&phy,&a,12,8);assert(p->limit==15&&p->baseline_mcs==8*256);
 puts("PASS: stable MCS8 is eligible without an absolute MCS floor");
 reset(&phy,&a);
 for(i=0;i<8;i++){run(&phy,&a,5,i%2?11:8);assert(p->limit==-1);}
 puts("PASS: unstable uncapped windows cannot qualify a cap");
 reset(&phy,&a);run(&phy,&a,12,11);assert(p->limit==15);
 input_signal=-43;step(&phy,&a,11,true);assert(p->limit==19);
 input_signal=-50;beacon(&a,input_signal);mt7915_power_work(&phy);assert(p->limit==-1);
 input_signal=-34;reset(&phy,&a);run(&phy,&a,12,11);assert(p->limit==15);
 jiffies+=3*HZ;beacon(&a,input_signal);mt7915_power_work(&phy);assert(p->limit==-1&&!p->backoff);
 run(&phy,&a,5,11);assert(p->limit==-1);run(&phy,&a,6,11);assert(p->limit==15);
 // One completion per second needs a longer observation, not a penalty.
 light(&phy,&a,60,4);assert(p->limit==15&&!p->backoff);
 run(&phy,&a,20,11);assert(p->limit==15&&!p->backoff);
 puts("PASS: load to one-per-second traffic to load retains the cap without backoff");
 reset(&phy,&a);run(&phy,&a,10,11);light(&phy,&a,6,4);assert(p->limit==15&&!p->qualified);
 input_signal=-43;beacon(&a,input_signal);mt7915_power_work(&phy);assert(p->limit==19&&!p->backoff);
 input_signal=-50;beacon(&a,input_signal);mt7915_power_work(&phy);assert(p->limit==-1&&!p->backoff);input_signal=-34;
 puts("PASS: signal weakening raises power during an unfinished sparse window");
 reset(&phy,&a);run(&phy,&a,10,11);
 for(i=0;i<16;i++){jiffies+=HZ;beacon(&a,input_signal);status(&d,&a,9,true);mt7915_power_work(&phy);if(i<15)assert(p->limit==15&&!p->backoff);}
 assert(p->limit==-1&&p->backoff==300);
 puts("PASS: a completed sparse window still detects sustained rate loss");

 reset(&phy,&a);light(&phy,&a,34,4);assert(p->limit==15&&!p->backoff);
 puts("PASS: two sparse uncapped windows qualify once each reaches sixteen samples");
 // At one completion per two seconds, only fifteen fit in thirty seconds.
 // Keep the existing history; lack of samples must neither double it nor
 // schedule another cooldown, even at the timeout boundary.
 reset(&phy,&a);run(&phy,&a,10,11);assert(p->limit==15);
 p->backoff=600;p->retry_at=jiffies-HZ;retry=p->retry_at;
 light(&phy,&a,29,8);assert(p->limit==15&&p->backoff==600);
 light(&phy,&a,1,4);assert(p->limit==-1&&p->backoff==600&&p->retry_at==retry);
 run(&phy,&a,11,11);assert(p->limit==15&&p->backoff==600);
 puts("PASS: thirty-second sparse timeout clears without changing history or delaying requalification");
 // Decay must be earned while capped, not by old uncapped uptime.
 reset(&phy,&a);input_signal=-50;p->backoff=3600;p->retry_at=jiffies-HZ;
 run(&phy,&a,620,11);assert(p->limit==-1&&p->backoff==3600);
 input_signal=-34;step(&phy,&a,11,true);assert(p->limit==15);
 run(&phy,&a,599,11);assert(p->limit==15&&p->backoff==3600);
 run(&phy,&a,1,11);assert(p->limit==15&&!p->backoff&&!p->retry_at);
 run(&phy,&a,10,9);assert(p->limit==-1&&p->backoff==300);
 puts("PASS: ten capped good minutes reset backoff; a later real trip starts at five minutes");
 // An unfinished sparse window does not confirm recovery at ten minutes.
 struct mt7915_peer_power sparse_probe={.base=23,.limit=15,.backoff=3600,.samples=5,
  .sum_mcs=55,.baseline_mcs=2816,.changed=jiffies-POWER_RECOVER,.window_start=jiffies-POWER_WINDOW,.qualified=true};
 unsigned long start=sparse_probe.window_start;
 assert(mt7915_power_window(&sparse_probe,jiffies)==POWER_KEEP);
 assert(sparse_probe.samples==5&&sparse_probe.window_start==start&&!sparse_probe.qualified&&sparse_probe.backoff==3600);
 sparse_probe.samples=16;sparse_probe.sum_mcs=176;
 assert(mt7915_power_window(&sparse_probe,jiffies)==POWER_KEEP&&!sparse_probe.backoff);
 puts("PASS: incomplete windows block deeper caps and cannot earn backoff decay");
 reset(&phy,&a);
 for(i=0;i<80;i++){jiffies+=HZ/2;beacon(&a,input_signal);raw_status(&d,&a,(i%5?0:8<<6)|(1<<10)|8);mt7915_power_work(&phy);}
 assert(p->limit==-1&&!p->stable&&!p->backoff);
 puts("PASS: mostly mismatched-format background statuses remain ineligible");
 reset(&phy,&a);run(&phy,&a,12,11);
 struct sk_buff old={.data={8},.cb.pktid=3};u32 raw[8]={(8<<6)|(1<<10)|11};
 mt7915_power_tx_prepare(&d,&a,&old);u32 gen=old.cb.rate_generation;
 mt7915_power_invalidate(&d,&a);mt7915_power_work(&phy);assert(p->generation!=gen);
 mt7915_power_tx_status(&d,&a,&old,raw);assert(!p->samples&&!p->qualified);
 puts("PASS: pre-apply and old-generation status cannot qualify a new window");
 a.deflink.txpwr.type=1;a.deflink.txpwr.power=15;mt7915_power_init(&d,&a);
 assert(!mt7915_power_reapply(&d,&v,&a)&&firmware[1]==15);
 int prev=commands;run(&phy,&a,40,11);assert(commands==prev&&firmware[1]==15);
 a.deflink.txpwr.type=0;mt7915_power_init(&d,&a);assert(!mt7915_power_reapply(&d,&v,&a));
 mt7915_power_work(&phy);run(&phy,&a,12,11);assert(p->limit==15);
 mt7915_txpower_auto=false;for(i=0;i<3;i++)mt7915_power_work(&phy);assert(firmware[1]==-1&&!phy.power_active);
 prev=scans;for(i=0;i<100;i++)mt7915_power_work(&phy);assert(scans==prev);
 // Even an uncapped peer outside the last disable batch can receive an
 // RA invalidation later. That needs no firmware clear or worker scan.
 p->ready=true;p->limit=-1;p->dirty=false;mt7915_power_invalidate(&d,&a);
 assert(!p->ready&&!p->dirty);mt7915_power_work(&phy);assert(scans==prev);
 puts("PASS: manual limit wins, AUTO hands back, disabled cleanup drains then scans stop");
 mt7915_txpower_auto=true;reset(&phy,&a);run(&phy,&a,12,11);
 fail=1;mt7915_power_invalidate(&d,&a);mt7915_power_work(&phy);assert(p->dirty&&p->limit==-1&&!p->ready&&p->backoff==300);
 retry=p->retry_at;fail=1;mt7915_power_work(&phy);assert(p->dirty&&p->retry_at==retry&&p->backoff==300);
 mt7915_power_work(&phy);assert(!p->dirty&&firmware[1]==-1&&p->retry_at==retry);
 puts("PASS: MCU failure retries a clear without perpetually extending cooldown");
 reset(&phy,&a);run(&phy,&a,12,11);
 radio.chainmask=0x0c;mt7915_power_work(&phy);assert(p->limit==-1&&p->chains==0x0c&&!p->stable);
 radio.chainmask=0x1c;
 puts("PASS: topology change clears and starts a fresh full-power baseline");
 struct ieee80211_sta b=a;b.addr[0]=2;b.drv_priv[0].wcid.idx=2;registry[1]=&b;count=2;
 mt7915_power_init(&d,&a);mt7915_power_init(&d,&b);mt7915_power_work(&phy);
 for(i=0;i<240;i++){jiffies+=HZ/4;beacon(&a,-45);beacon(&b,-34);status(&d,&a,11,true);status(&d,&b,11,true);mt7915_power_work(&phy);}
 assert(firmware[1]==21&&firmware[2]==15);
 for(i=0;i<40;i++){jiffies+=HZ/4;beacon(&a,-45);beacon(&b,-34);status(&d,&a,11,true);status(&d,&b,9,true);mt7915_power_work(&phy);}
 assert(firmware[1]==21&&firmware[2]==-1);
 d.peer_spe=false;mt7915_power_work(&phy);assert(firmware[1]==-1);d.peer_spe=true;
 puts("PASS: independent caps, local sustained rollback and MANET-WM gate");
 struct mt7915_peer_power probe={.base=23,.limit=-1,.qualified=true,.last_txs=jiffies,.window_start=jiffies};
 assert(mt7915_power_decide(&probe,jiffies,true,true,-48)==POWER_NONE);
 assert(mt7915_power_decide(&probe,jiffies,true,true,-46)==21);
 assert(mt7915_power_decide(&probe,jiffies,true,true,-44)==19);
 assert(mt7915_power_decide(&probe,jiffies,true,true,-34)==15);
 probe.base=30;assert(mt7915_power_decide(&probe,jiffies,true,true,-34)==22);probe.base=23;
 probe.limit=21;assert(mt7915_power_decide(&probe,jiffies,true,true,-44)==POWER_NONE);
 assert(mt7915_power_decide(&probe,jiffies,true,true,-42)==19);
 probe.limit=19;assert(mt7915_power_decide(&probe,jiffies,true,true,-45)==21);
 puts("PASS: unchanged cap bins, PHY-power basis and weakening/deepening hysteresis");
 struct mt76_rx_status rx={.chains=3,.chain_signal={-41}};
 a.drv_priv[0].path_signal_jiffies=0;b.drv_priv[0].path_signal_jiffies=0;
 struct mt7915_basic_signal sample={.phy=&phy,.status=&rx,.addr=a.addr,.fc=0x80,.index=1,.by_index=true};
 assert(mt7915_mac_sample_basic_peer(&sample,a.drv_priv));
 assert(a.drv_priv[0].path_signal_jiffies==jiffies&&a.drv_priv[0].path_signal_fc==0x80&&a.drv_priv[0].path_signal_by_index);
 rx.chain_signal[0]=-80;unsigned long stamp=a.drv_priv[0].path_signal_jiffies;
 mt7915_mac_sample_basic_peer(&sample,a.drv_priv);assert(a.drv_priv[0].path_signal_jiffies==stamp&&ewma_avg_signal_read(&a.drv_priv[0].path_signal)==41);
 jiffies+=11*HZ;mt7915_mac_sample_basic_peer(&sample,a.drv_priv);assert(ewma_avg_signal_read(&a.drv_priv[0].path_signal)==80);
 jiffies+=HZ;rx.chain_signal[0]=0;mt7915_mac_sample_basic_peer(&sample,a.drv_priv);assert(a.drv_priv[0].path_signal_jiffies==jiffies-HZ);
 rx.chain_signal[0]=-40;
 struct mt7915_sta group={.vif=v.drv_priv};assert(!mt7915_mac_sample_basic_peer(&sample,&group));
 prev=scans;int before=lookups;sample.by_index=false;mt7915_mac_sample_basic(&sample);
 assert(!a.drv_priv[0].path_signal_by_index&&a.drv_priv[0].path_signal_jiffies==jiffies&&lookups==before+1&&scans==prev);
 sample.addr=b.addr;mt7915_mac_sample_basic(&sample);assert(b.drv_priv[0].path_signal_jiffies==jiffies);
 b.drv_priv[0].wcid.sta=false;jiffies+=HZ;mt7915_mac_sample_basic(&sample);assert(b.drv_priv[0].path_signal_jiffies==jiffies-HZ);
 b.drv_priv[0].wcid.sta=true;v.type=0;mt7915_mac_sample_basic(&sample);assert(b.drv_priv[0].path_signal_jiffies==jiffies-HZ);v.type=7;
 struct mt7915_phy other_phy=phy;sample.phy=&other_phy;mt7915_mac_sample_basic(&sample);assert(b.drv_priv[0].path_signal_jiffies==jiffies-HZ);sample.phy=&phy;
 tx_paths_auto=false;mt7915_txpower_auto=false;sample.addr=a.addr;mt7915_mac_sample_basic(&sample);assert(a.drv_priv[0].path_signal_jiffies==jiffies-HZ);
 tx_paths_auto=true;mt7915_mac_sample_basic(&sample);assert(a.drv_priv[0].path_signal_jiffies==jiffies);
 mt7915_txpower_auto=true;
 puts("PASS: actual sampler slot/expiry/shared gate and hash fallback retain PHY/vif/active checks");
 // Disable must drain caps beyond the first batch and retry failed clears.
 struct ieee80211_sta many[17];count=17;phy.power_cursor=0;
 for(i=0;i<count;i++){many[i]=a;many[i].drv_priv[0].wcid.idx=i+1;mt7915_power_init(&d,&many[i]);many[i].drv_priv[0].power.limit=15;many[i].drv_priv[0].power.dirty=false;firmware[i+1]=15;}
 for(i=0;i<count;i++)registry[i]=&many[(i+1)%count];
 phy.power_active=true;mt7915_txpower_auto=false;fail=1;
 for(i=0;i<10&&phy.power_active;i++)mt7915_power_work(&phy);
 assert(!phy.power_active);for(i=0;i<count;i++)assert(firmware[i+1]==-1&&!many[i].drv_priv[0].power.dirty);
 prev=scans;for(i=0;i<100;i++)mt7915_power_work(&phy);assert(scans==prev);
 puts("PASS: fair bounded cleanup drains every WCID including failed clears before disabled fast path");
}
'''

with tempfile.TemporaryDirectory(prefix='mt7915-power-') as tmp:
    c = Path(tmp) / 'power_test.c'
    exe = Path(tmp) / 'power_test'
    c.write_text(head + source + tests)
    subprocess.run(['cc', '-O2', '-fsanitize=undefined', str(c), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)

# Integration placement: the original raw index is captured before remapping;
# all frame-rejecting returns precede the validated metric block.
fill = block(rx, 'static int\nmt7915_mac_fill_rx(')
assert fill.index('mt76_wcid_ptr(dev, idx)') < fill.index('mt7915_rx_get_wcid(dev, idx, unicast)')
assert 'ieee80211_iterate_stations_atomic' not in fill
assert 'ieee80211_find_sta_by_ifaddr' in block(rx, 'static void mt7915_mac_sample_basic(')
assert 'basic = !unicast' in fill  # translated multicast-to-unicast is data-power limited
tail = fill[fill.index('if (rxv && dev->peer_spe &&'):]
assert 'RX_FLAG_FAILED_FCS_CRC' in tail and 'RX_FLAG_ONLY_MONITOR' in tail
assert 'return -' not in tail and 'return ret' not in tail and 'return err' not in tail
mac_h = HERE.parent / 'mt76_connac2_mac.h'
if mac_h.exists():  # absent from the series-only test tree
    assert 'MT_RXD6_TA_LO' in mac_h.read_text() and 'MT_RXD7_TA_HI' in mac_h.read_text()
print('PASS: raw-index-before-remap, Group-4 TA definitions and validated RX placement')

# Exercise the actual manual/automatic API split and emitted offset table.
packer_head = r'''
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
typedef uint8_t u8;typedef int8_t s8;typedef uint16_t u16;typedef int16_t s16;typedef uint16_t __le16;typedef uint32_t u32;
#define __packed __attribute__((packed))
#define S8_MAX 127
#define NL80211_TX_POWER_AUTOMATIC 0
#define MT7915_SKU_RATE_NUM 161
#define TX_POWER_LIMIT_FRAME 17
#define MCU_EXT_CMD(x) 0
#define cpu_to_le16(x) (x)
#define ARRAY_SIZE(x) (sizeof(x)/sizeof((x)[0]))
#define DIV_ROUND_UP(n,d) (((n)+(d)-1)/(d))
enum {SKU_CCK,SKU_OFDM,SKU_HT_BW20,SKU_HT_BW40,SKU_VHT_BW20,SKU_HE_RU26,SKU_HE_RU242};
static const u8 mt7915_sku_group_len[]={4,8,8,8,12,12,12};
struct mt7915_sta {struct {u16 idx;} wcid;};
struct ieee80211_sta {struct {struct {bool ht_supported;} ht_cap;struct {bool vht_supported;} vht_cap;struct {bool has_he;} he_cap;struct {int type;} txpwr;} deflink;struct mt7915_sta drv_priv[1];};
struct ieee80211_vif {};
struct mt76_phy {int txpower_cur;u8 band_idx;};struct mt7915_dev {int mt76;};struct mt7915_phy {struct mt7915_dev*dev;struct mt76_phy*mt76;};
static u8 packet[64];static int queries,sends;
static int mt7915_mcu_get_txpower_sku(struct mt7915_phy*p,s8*b,int n){queries++;memset(b,42,n);return 0;}
static int mt76_get_power_bound(struct mt76_phy*p,int n){return n*2-4;}
static int mt76_mcu_send_msg(int*d,int cmd,void*p,int n,bool wait){sends++;memcpy(packet,p,n);return 0;}
'''
packer = (HERE / 'mcu.c').read_text()
packer = (block(packer, 'int mt7915_mcu_set_txpower_limit(') + '\n' +
          block(packer, 'int mt7915_mcu_set_txpower_frame('))
packer_tests = r'''
int main(void){struct mt7915_dev d={0};struct mt76_phy r={.txpower_cur=42,.band_idx=1};struct mt7915_phy phy={&d,&r};struct ieee80211_vif v={};struct ieee80211_sta s={.deflink={.ht_cap.ht_supported=true,.vht_cap.vht_supported=true,.he_cap.has_he=true},.drv_priv[0].wcid.idx=7};
 assert(!mt7915_mcu_set_txpower_limit(&phy,&v,&s,-1)&&!queries&&sends==1);
 for(int i=8;i<56;i++)assert(!packet[i]); // all-zero clear, no SKU dependency
 assert(mt7915_mcu_set_txpower_limit(&phy,&v,&s,-2)==-EINVAL);
 assert(!mt7915_mcu_set_txpower_limit(&phy,&v,&s,15)&&queries==1);
 for(int i=8;i<56;i++)assert((s8)packet[i]==-8);
 s.deflink.txpwr.type=1;assert(mt7915_mcu_set_txpower_frame(&phy,&v,&s,-1)==-EINVAL);
 s.deflink.txpwr.type=0;assert(!mt7915_mcu_set_txpower_frame(&phy,&v,&s,0));
 for(int i=8;i<56;i++)assert(!packet[i]);
 puts("PASS: actual packer clears without SKU query, emits nonpositive limits, preserves manual validation");}
'''
with tempfile.TemporaryDirectory(prefix='mt7915-power-packet-') as tmp:
    c, exe = Path(tmp) / 'packet.c', Path(tmp) / 'packet'
    c.write_text(packer_head + packer + packer_tests)
    subprocess.run(['cc', '-O2', '-fsanitize=undefined', str(c), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
