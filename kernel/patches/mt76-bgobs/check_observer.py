#!/usr/bin/env python3
"""Execute actual observer functions with mocked kernel scheduling and MCU I/O."""
from pathlib import Path
import os
import json
import re
import subprocess

import sys
work = Path(sys.argv[2])
src = Path(sys.argv[1])

def function(file, name):
    text = (src / file).read_text()
    start = re.search(r'(?m)^(?:[A-Za-z_][A-Za-z_0-9 *]*)?' + re.escape(name) + r'\(', text).start()
    start = text.rfind('\n', 0, start) + 1
    if text[start:].startswith(name):
        start = text.rfind('\n', 0, start - 1) + 1
    # Ignore braces inside strings/comments while extracting real C bodies.
    masked = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//[^\n]*',
                    lambda match: ' ' * len(match[0]), text, flags=re.S)
    body = masked.index('{', start)
    end, depth = body, 1
    while depth:
        end += 1
        depth += (masked[end] == '{') - (masked[end] == '}')
    return text[start:end + 1]

def struct(file, name):
    text = (src / file).read_text()
    start = text.index('struct ' + name + ' {')
    return text[start:text.index(';', text.index('}', start)) + 1]

shim = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
typedef uint8_t u8;
typedef uint32_t u32, __le32;
typedef uint16_t u16, __le16;
typedef uint64_t u64;
#define __packed __attribute__((packed))
#define GENMASK(h,l) ((uint32_t)((~0U >> (31-(h))) & (~0U << (l))))
#define BIT(n) (1U << (n))
#define BIT_ULL(n) (1ULL << (n))
#define max_t(t,a,b) ((t)(a)>(t)(b)?(t)(a):(t)(b))
#define min_t(t,a,b) ((t)(a)<(t)(b)?(t)(a):(t)(b))
#define MT7915_BG_TRACE_MAX 4096
#define MT7915_BG_REG_COUNT 84
#define MT7915_BG_SAVE_COUNT 27
#define MT7915_BG_CASES 64
#define GFP_KERNEL 0
#define kvcalloc(a,b,c) calloc(a,b)
#define kvfree(p) free(p)
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define container_of(p,t,f) ((t *)((char *)(p) - offsetof(t,f)))
#define le32_to_cpu(x) (x)
#define div_u64(a,b) ((a)/(b))
#define div64_u64(a,b) ((a)/(b))
#define hweight8(x) __builtin_popcount((u8)(x))
#define MT_BAND1 1
#define MT7915_IPI_ALL 12
#define MCU_EXT_CMD_RDD_IPI_HIST 0xa3
#define MCU_EXT_CMD_OFFCH_SCAN_CTRL 0x9a
#define MCU_EXT_CMD(x) MCU_EXT_CMD_##x
#define MCU_EXT_QUERY(x) (MCU_EXT_CMD(x) | 0x10000)
#define MT7915_BG_SETTLE_MS 20
#define MT7915_BG_MIN_MS 100
#define MT7915_BG_MAX_MS 5000
#define MT7915_BG_MAX_ELAPSED_US 6000000
#define CH_SWITCH_BACKGROUND_SCAN_START 6
#define CH_SWITCH_BACKGROUND_SCAN_RUNNING 7
#define CH_SWITCH_BACKGROUND_SCAN_STOP 8
enum { MT7915_BG_IDLE, MT7915_BG_SETTLE, MT7915_BG_DWELL };
enum { MT76_MCU_RESET, MT76_STATE_RUNNING, MT76_SCANNING, MT76_RESET };
enum { NL80211_CHAN_WIDTH_20=20, NL80211_CHAN_WIDTH_40=40,
       NL80211_CHAN_WIDTH_80=80, NL80211_CHAN_WIDTH_160=160 };
#define NL80211_BAND_5GHZ 1
#define NL80211_DFS_UNAVAILABLE 1
#define IEEE80211_CHAN_DISABLED 1
#define IEEE80211_CHAN_RADAR 2
#define IEEE80211_CHAN_NO_IR 4
#define NL80211_DFS_UNSET 0
struct seq_file { void *private; FILE *out; };
static void seq_printf(struct seq_file *s, const char *fmt, ...) {
    va_list ap; va_start(ap,fmt); vfprintf(s->out,fmt,ap); va_end(ap);
}
static void seq_puts(struct seq_file *s, const char *str) { fputs(str,s->out); }
struct work_struct { bool pending, running; };
struct delayed_work { struct work_struct work; unsigned ms; };
struct ieee80211_channel { int center_freq, hw_value, band, dfs_state, flags; };
struct cfg80211_chan_def { struct ieee80211_channel *chan; int width, center_freq1; };
struct wiphy { int dummy; };
struct ieee80211_hw { struct wiphy *wiphy; };
struct mt76_channel_state { u64 cc_tx; };
struct mt76_phy {
    struct mt76_channel_state *chan_state;
    int band_idx; unsigned long state; bool offchannel; u8 antenna_mask, chainmask;
    struct cfg80211_chan_def chandef; struct ieee80211_hw *hw;
};
struct mt76_dev { int mutex; int region; struct mt76_phy phy, *phys[2]; };
struct mt7915_phy { struct mt7915_dev *dev; struct mt76_phy *mt76; };
'''

shim += '\n'.join(struct('mt7915.h', x) for x in ('mt7915_ipi_sample', 'mt7915_bg_scan'))
shim += '\n'.join(struct('mcu.h', x) for x in ('mt7915_mcu_ipi_req', 'mt7915_mcu_ipi_reply', 'mt7915_mcu_background_chain_ctrl'))
shim += r'''
struct mt7915_dev {
    struct mt76_dev mt76; struct mt76_phy mphy;
    struct { bool restart, hw_full_reset; } recovery;
    struct mt7915_bg_scan bg_scan; struct mt7915_phy *rdd2_phy;
    bool background_rx_reserved; unsigned hw_pattern;
    struct cfg80211_chan_def rdd2_chandef;
};
static struct ieee80211_channel chans[60];
static u64 now_us;
static unsigned send_count, fail_send, queries, reads;
static int mib_error;
static u64 tx_counter;
static int mt7915_mcu_get_chan_mib_info(struct mt7915_phy *p, bool sw) {
    p->mt76->chan_state->cc_tx=tx_counter; return mib_error;
}
static int query_error;
static bool ignore_reset, simulate_fw_changes;
static unsigned writes, fail_write;
static bool drop_all_writes;
static struct { u32 reg, value; } regs[128];
static unsigned nregs;
static u32 *reg_value(u32 reg) {
    for(unsigned i=0;i<nregs;i++) if(regs[i].reg==reg) return &regs[i].value;
    assert(nregs<ARRAY_SIZE(regs)); regs[nregs].reg=reg; regs[nregs].value=0x10001180;
    return &regs[nregs++].value;
}
static size_t reply_len = 56;
static struct mt7915_mcu_ipi_reply wire;
static struct { int cmd; unsigned char data[64]; size_t len; } sends[512];
static void mutex_lock(int *m) { assert(!*m); *m = 1; }
static void mutex_unlock(int *m) { assert(*m); *m = 0; }
#define lockdep_assert_held(m) assert(*(m))
static bool test_bit(int bit, const unsigned long *state) { return *state & (1UL<<bit); }
static bool work_busy(struct work_struct *w) { return w->pending || w->running; }
static void cancel_delayed_work(struct delayed_work *w) { w->work.pending = false; }
static void cancel_delayed_work_sync(struct delayed_work *w) { assert(!w->work.running); w->work.pending = false; }
#define INIT_DELAYED_WORK(w,f) memset(w, 0, sizeof(*(w)))
#define system_wq NULL
#define msecs_to_jiffies(x) (x)
static void mod_delayed_work(void *q, struct delayed_work *w, unsigned ms) {
    (void)q; w->work.pending = true; w->ms = ms;
}
#define ktime_to_us(x) (x)
#define ktime_get_boottime() now_us
static struct ieee80211_channel *ieee80211_get_channel(struct wiphy *w, unsigned f) {
    (void)w;
    for (size_t i=0; i<ARRAY_SIZE(chans); i++) if (chans[i].center_freq == (int)f) return &chans[i];
    return NULL;
}
static int ieee80211_frequency_to_channel(int f) { return (f-5000)/5; }
static bool cfg80211_chandef_valid(const struct cfg80211_chan_def *c) {
    return c->chan && c->chan->center_freq >= c->center_freq1-c->width/2+10 &&
        c->chan->center_freq < c->center_freq1+c->width/2 &&
        (c->chan->center_freq-(c->center_freq1-c->width/2+10)) % 20 == 0;
}
static bool cfg80211_chandef_usable(struct wiphy *w, struct cfg80211_chan_def *c, int flag) {
    for(int f=c->center_freq1-c->width/2+10; f<c->center_freq1+c->width/2; f+=20) {
        struct ieee80211_channel *ch=ieee80211_get_channel(w,f);
        if(!ch || ch->flags & flag) return false;
    }
    return true;
}
static bool cfg80211_chandef_identical(const struct cfg80211_chan_def *a, const struct cfg80211_chan_def *b) {
    return a->chan==b->chan && a->width==b->width && a->center_freq1==b->center_freq1;
}
static int cfg80211_chandef_get_width(const struct cfg80211_chan_def *c) { return c->width; }
static int mt76_connac_chan_bw(struct cfg80211_chan_def *c) {
    switch(c->width) { case 20:return 0; case 40:return 1; case 80:return 2; case 160:return 3; }
    abort();
}
static int mt76_mcu_send_msg(struct mt76_dev *d, int cmd, const void *p, size_t n, bool wait) {
    (void)d; assert(!wait); assert(n<=64 && send_count<ARRAY_SIZE(sends));
    sends[send_count].cmd=cmd; sends[send_count].len=n; memcpy(sends[send_count].data,p,n);
    send_count++;
    if(cmd==0x9a && simulate_fw_changes) {
        const struct mt7915_mcu_background_chain_ctrl *req=p;
        if(req->scan_mode==1) {
            *reg_value(0x831a5df0)=(*reg_value(0x831a5df0)&~0xf)|9;
            *reg_value(0x831ada20)=(*reg_value(0x831ada20)&~0xf00)|0x900;
        } else if(!req->scan_mode) {
            *reg_value(0x831ad218)=0;
            *reg_value(0x831a5018)&=~1;
            *reg_value(0x831a5c00)&=~1;
            *reg_value(0x831a5de0)=0;
            *reg_value(0x831a5de8)&=~0x1fffff;
        }
    }
    if(cmd==0xa3 && ((const u8 *)p)[0]==1 && !ignore_reset) {
        memset(wire.bins,0,sizeof(wire.bins)); wire.tx_assert_us=0;
    }
    return send_count==fail_send ? -EIO : 0;
}
struct sk_buff { unsigned char *data; size_t len; };
#define MCU_EVENT_EXT 0xed
#define MCU_CMD_EXT_CID 0xed
#define MCU_EXT_EVENT_WA_TX_STAT 0x74
#define MCU_CMD_PATCH_SEM_CONTROL 0x10
#define MCU_EXT_CMD_THERMAL_CTRL 0x2c
#define MCU_CMD(x) MCU_CMD_##x
#define dev_err(...) ((void)0)
#define wake_up(...) ((void)0)
#define queue_work(...) ((void)0)
struct mt76_connac2_mcu_rxd {
    __le32 rxd[6]; __le16 len, pkt_type_id;
    u8 eid, seq, option, rsv, ext_eid, rsv1[2], s2d_index;
};
static bool test_and_set_bit(int bit, unsigned long *state) {
    bool old=test_bit(bit,state); *state |= 1UL<<bit; return old;
}
static void skb_pull(struct sk_buff *skb, size_t n) {
    assert(skb->len>=n); skb->data+=n; skb->len-=n;
}
static u32 mt76_rr(struct mt7915_dev *d, u32 reg) {
    assert(d->mt76.mutex && d->bg_scan.dirty);
    reads++;
    if(reg>=0x83121000 && reg<=0x8312102c) return wire.bins[(reg-0x83121000)/4];
    return *reg_value(reg);
}
static void mt76_wr(struct mt7915_dev *d, u32 reg, u32 value) {
    assert(d->mt76.mutex && d->bg_scan.dirty && !d->bg_scan.borrowed);
    writes++;
    if(!drop_all_writes && writes!=fail_write) *reg_value(reg)=value;
}
#define READ_ONCE(x) (x)
#define MT_RDD_IDX_BAND0 0
#define MT_RDD_IDX_BAND1 1
#define MT_RDD_IDX_BACKGROUND 2
#define MT7975_ONE_ADIE 1
#define MT7976_ONE_ADIE 2
#define GFP_ATOMIC 0
struct mt7915_mcu_rdd_report { u8 rdd_idx; };
static unsigned main_radar, background_radar;
static unsigned mt7915_check_adie(struct mt7915_dev *d, bool b) { return 0; }
static bool is_mt7986(struct mt76_dev *d) { return false; }
static void cfg80211_background_radar_event(struct wiphy *w, struct cfg80211_chan_def *c, int f) { background_radar++; }
static void ieee80211_radar_detected(struct ieee80211_hw *h, void *p) { main_radar++; }
static struct mt7915_phy *current_phy;
static struct mt7915_phy *mt7915_hw_phy(struct ieee80211_hw *hw) { (void)hw; return current_phy; }
static unsigned cac_calls;
static int mt7915_mcu_rdd_background_enable(struct mt7915_phy *p, struct cfg80211_chan_def *c) {
    assert(p->dev->mt76.mutex && !p->dev->bg_scan.dirty); (void)c; cac_calls++; return 0;
}
static int mt7915_mcu_parse_response(struct mt76_dev *, int, struct sk_buff *, int);
static int mt76_mcu_send_and_get_msg(struct mt76_dev *d, int cmd, const void *p,
                                    size_t n, bool wait, struct sk_buff **out) {
    assert(wait && cmd==MCU_EXT_QUERY(RDD_IPI_HIST));
    const struct mt7915_mcu_ipi_req *req=p;
    assert(n==20 && req->index<=15 && req->band<=1 && !req->set_val);
    assert(!req->idle_threshold && !req->idle_max && !req->idle_duration && !req->idle_cmd);
    queries++;
    if(query_error) return query_error;
    size_t hs=sizeof(struct mt76_connac2_mcu_rxd);
    *out=malloc(sizeof(**out)); (*out)->data=calloc(1, hs+reply_len); (*out)->len=hs+reply_len;
    struct mt76_connac2_mcu_rxd *hdr=(void *)(*out)->data;
    hdr->seq=3; hdr->eid=0xed; hdr->ext_eid=0xa3;
    wire.index=req->index; wire.band=req->band;
    struct mt7915_mcu_ipi_reply response=wire;
    if(req->index<12) response.bins[0]=wire.bins[req->index];
    if(req->index==15) response.bins[0]=wire.tx_assert_us;
    memcpy((*out)->data+hs,&response,reply_len < sizeof(wire) ? reply_len : sizeof(wire));
    assert(!mt7915_mcu_parse_response(d,cmd,*out,3));
    return 0;
}
static void dev_kfree_skb(struct sk_buff *s) {
    free(s->data-sizeof(struct mt76_connac2_mcu_rxd)); free(s);
}

'''
functions = [function('mcu.c', x) for x in (
    'mt7915_mcu_rx_radar_detected', 'mt7915_mcu_parse_response',
    'mt7915_mcu_background_chain_ctrl', 'mt7915_mcu_ipi_decode',
    'mt7915_mcu_ipi_query_index')]
bgtext = (src / 'bgscan.c').read_text()
a = bgtext.index('static const struct {')
functions += [bgtext[a:bgtext.index('};', a)+2]]
functions += [function('bgscan.c', x) for x in (
    'mt7915_bg_scan_mcu_ready', 'mt7915_bg_save', 'mt7915_bg_write_control',
    'mt7915_bg_quiesce', 'mt7915_bg_restore_controls', 'mt7915_bg_scan_stop',
    'mt7915_bg_scan_calculate', 'mt7915_bg_read_bins', 'mt7915_bg_configure',
    'mt7915_bg_sample', 'mt7915_bg_scan_work', 'mt7915_bg_scan_reset',
    'mt7915_bg_scan_reset_done', 'mt7915_bg_scan_init', 'mt7915_bg_scan_cleanup',
    'mt7915_bg_scan_chandef', 'mt7915_bg_scan_start', 'mt7915_bg_scan_bins',
    'mt7915_bg_scan_show')]
functions += [function('main.c', 'mt7915_set_radar_background')]
# The combined stack adds passive hold callbacks around the same receiver.
# Their fault handling has a separate C harness; keep this observer harness
# usable against both the standalone and paired stack.
if 'mt7915_hold_start' in functions[-1]:
    shim = shim.replace('struct mt7915_dev {',
                        'struct mt7915_dev { struct { struct delayed_work work; } hold;')
    shim += r'''
#define WRITE_ONCE(x,v) ((x)=(v))
static void mt7915_hold_start(struct mt7915_phy *p) {}
static void mt7915_hold_stop(struct mt7915_dev *d) {}
static void cfg80211_background_cac_abort(struct wiphy *w) {}
'''
tests = r'''
static void fire(struct mt7915_dev *d) {
    struct delayed_work *w=&d->bg_scan.work;
    assert(w->work.pending); now_us+=(u64)w->ms*1000;
    w->work.pending=false; w->work.running=true;
    mt7915_bg_scan_work(&w->work); w->work.running=false;
}
int main(void) {
    assert(sizeof(struct mt7915_mcu_ipi_req)==20 && sizeof(wire)==56);
    struct mt7915_ipi_sample sample={};
    wire.index=12; wire.band=1; wire.bins[3]=0x12345678; wire.tx_assert_us=123;
    for(size_t n=0; n<56; n++) {
        unsigned char *shortbuf=malloc(n ? n : 1);
        memcpy(shortbuf,&wire,n);
        assert(mt7915_mcu_ipi_decode(shortbuf,n,12,1,&sample)==-EMSGSIZE);
        free(shortbuf);
    }
    assert(mt7915_mcu_ipi_decode(&wire,57,12,1,&sample)==-EMSGSIZE);
    assert(mt7915_mcu_ipi_decode(&wire,56,12,0,&sample)==-EPROTO);
    wire.index=11; assert(mt7915_mcu_ipi_decode(&wire,56,12,1,&sample)==-EPROTO);
    wire.index=12; assert(!mt7915_mcu_ipi_decode(&wire,56,12,1,&sample));
    assert(sample.bins[3]==0x12345678 && sample.tx_assert_us==123);
    memset(&wire,0,sizeof(wire)); wire.index=12; wire.band=1;

    struct mt7915_dev d={.background_rx_reserved=true};
    struct wiphy w={}; struct ieee80211_hw hw={&w};
    struct mt76_channel_state channel={};
    struct mt76_phy m={.chan_state=&channel,.band_idx=1,.state=1UL<<MT76_STATE_RUNNING,.antenna_mask=3,.hw=&hw};
    struct mt7915_phy p={&d,&m};
    struct mt7915_bg_scan *s=&d.bg_scan;
    struct seq_file output={.private=&p, .out=fopen(getenv("OBSERVER_JSON_PATH"),"w")};
    assert(output.out);
    struct { struct mt76_connac2_mcu_rxd hdr; unsigned char payload[56]; } packet={};
    struct sk_buff skb={};
    for(size_t n=0; n<sizeof(packet.hdr); n++) {
        unsigned char *tiny=malloc(n ? n : 1);
        skb=(struct sk_buff){tiny,n};
        assert(mt7915_mcu_parse_response(&d.mt76,MCU_EXT_QUERY(RDD_IPI_HIST),&skb,3)==-EPROTO);
        free(tiny);
    }
    packet.hdr.seq=2; skb=(struct sk_buff){(void *)&packet,sizeof(packet)};
    assert(mt7915_mcu_parse_response(&d.mt76,MCU_EXT_QUERY(RDD_IPI_HIST),&skb,3)==-EAGAIN);
    packet.hdr.seq=3;
    assert(mt7915_mcu_parse_response(&d.mt76,MCU_EXT_QUERY(RDD_IPI_HIST),&skb,3)==-EPROTO);
    packet.hdr.eid=0xed; packet.hdr.ext_eid=0xa3;
    assert(!mt7915_mcu_parse_response(&d.mt76,MCU_EXT_QUERY(RDD_IPI_HIST),&skb,3));
    assert(skb.len==56 && skb.data==packet.payload);
    for(size_t i=0; i<ARRAY_SIZE(chans); i++) {
        chans[i].center_freq=5180+i*10; chans[i].hw_value=(chans[i].center_freq-5000)/5;
        chans[i].band=1;
    }
    m.chandef=(struct cfg80211_chan_def){.chan=&chans[0],.width=80,.center_freq1=5210};
    mt7915_bg_scan_init(&d);
    mutex_lock(&d.mt76.mutex);
    d.rdd2_phy=&p;
    assert(mt7915_bg_scan_start(&p,5500,80,5530,250,2)==-EBUSY && !send_count);
    d.rdd2_phy=NULL;
    assert(mt7915_bg_scan_start(&p,5500,80,5530,99,2)==-EINVAL);
    assert(mt7915_bg_scan_start(&p,5500,80,5530,5001,2)==-EINVAL);
    assert(mt7915_bg_scan_start(&p,5500,80,5530,250,11)==-EINVAL);
    assert(mt7915_bg_scan_start(&p,5580,80,5610,250,2)==-EPERM);
    assert(mt7915_bg_scan_start(&p,5500,160,5570,250,2)==-EPERM);
    assert(mt7915_bg_scan_start(&p,5500,80,5520,250,2)==-EINVAL);
    chans[32].dfs_state=NL80211_DFS_UNAVAILABLE;
    assert(mt7915_bg_scan_start(&p,5500,80,5530,250,2)==-EPERM);
    chans[32].dfs_state=0;
    d.mt76.region=1; current_phy=&p;
    assert(!mt7915_bg_scan_start(&p,5500,80,5530,250,2));
    assert(s->dirty && s->phase==MT7915_BG_SETTLE && sends[0].len==16);
    const unsigned char start[]={36,42,2,0,0,100,106,2,0,0,1,1,2,1,0,0};
    assert(sends[0].cmd==0x9a && !memcmp(sends[0].data,start,16));
    assert(mt7915_bg_scan_start(&p,5180,20,5180,250,2)==-EBUSY);
    mutex_unlock(&d.mt76.mutex);
    fire(&d); assert(s->phase==MT7915_BG_DWELL && writes==25);
    assert((*reg_value(0x831200c4)&0xf701)==0x3401);
    assert((*reg_value(0x831200d0)&0x0f000000)==0x0c000000);
    assert(!s->cac_primed && s->configuration==34);
    assert(!(*reg_value(0x831a5018)&1));
    assert(!(*reg_value(0x831a5004)&0x80));
    assert(!(*reg_value(0x83180038)&0xc00));
    assert(*reg_value(0x831a5000)==s->saved[11]);
    assert(*reg_value(0x831a5de0)==0x104 && *reg_value(0x831a5de4)==0x104);
    assert((*reg_value(0x831a5de8)&0x1fffff)==2);
    assert((*reg_value(0x831a5dec)&0x1fffff)==2);
    wire.bins[0]=25000; wire.bins[3]=6250; wire.bins[11]=0;
    tx_counter=50000;
    fire(&d);
    assert(!s->dirty && !s->phase && s->valid && !s->error && !s->changed);
    assert(s->elapsed_us==250000 && s->sample_count==31250 && s->busy_permille==200);
    assert(s->tx_first_us==0 && s->tx_last_us==50000);
    assert(!mt7915_bg_scan_show(&output,NULL));
    const unsigned char stop[]={36,42,2,2,3,0,0,0,0,0,0,1,2,1,0,0};
    assert(sends[1].len==16 && !memcmp(sends[1].data,stop,16));
    for(unsigned i=0;i<MT7915_BG_SAVE_COUNT;i++)
        assert(*reg_value(mt7915_bg_controls[i].address)==s->saved[i]);

    // Histograms are differences; no fabricated free-run clock or TX subtraction.
    s->last.bins[11]=123; assert(!mt7915_bg_scan_calculate(s));
    s->last.bins[0]=GENMASK(22,0); assert(mt7915_bg_scan_calculate(s)==-ERANGE);
    s->last.bins[0]=25000; s->first.bins[0]=25001;
    assert(mt7915_bg_scan_calculate(s)==-ERANGE); // reset/wrap
    s->first=s->last; assert(mt7915_bg_scan_calculate(s)==-ERANGE); // frozen
    memset(&s->first,0,sizeof(s->first)); s->last.bins[0]=0; s->last.bins[3]=10;
    assert(mt7915_bg_scan_calculate(s)==-ERANGE); // inadequate population
    s->last.bins[0]=25000; s->last.bins[3]=6250; s->last.tx_assert_us=UINT32_MAX;
    assert(!mt7915_bg_scan_calculate(s) && s->busy_permille==200);
    s->last.bins[3]=0; assert(!mt7915_bg_scan_calculate(s) && s->busy_permille==0);
    s->last.bins[10]=6250;
    assert(!mt7915_bg_scan_calculate(s) && s->busy_permille==0 && s->busy_raw_permille==200);
    s->last.bins[10]=0;

    mutex_lock(&d.mt76.mutex);
    assert(!mt7915_bg_scan_start(&p,5500,20,5500,250,2));
    fail_send=send_count+1;
    assert(mt7915_bg_scan_stop(&d,-ECANCELED)==-EIO);
    assert(s->dirty && !s->phase && !s->valid && s->restore_error==-EIO);
    assert(mt7915_bg_scan_start(&p,5500,20,5500,250,2)==-EBUSY);
    fail_send=0; assert(!mt7915_bg_scan_stop(&d,0) && !s->dirty);
    s->work.work.running=true;
    assert(mt7915_bg_scan_start(&p,5500,20,5500,250,2)==-EBUSY);
    s->work.work.running=false;
    fail_send=send_count+1;
    assert(mt7915_bg_scan_start(&p,5500,20,5500,250,2)==-EIO && !s->dirty);
    fail_send=0;
    assert(!mt7915_bg_scan_start(&p,5500,20,5500,250,2));
    mutex_unlock(&d.mt76.mutex); fire(&d);
    mutex_lock(&d.mt76.mutex);
    drop_all_writes=true;
    assert(mt7915_bg_scan_stop(&d,-ECANCELED)==-EIO && s->dirty && s->changed);
    drop_all_writes=false; assert(!mt7915_bg_scan_stop(&d,0) && !s->changed);
    // A failed configuration write still restores all fields already touched.
    for (unsigned failure=1; failure<=25; failure++) {
        // A dropped write is observable only if it would change the field.
        for(unsigned i=0;i<nregs;i++) regs[i].value=0x10001180;
        *reg_value(0x83180038)|=0xc00;
        *reg_value(0x83180004)=0;
        *reg_value(0x83180008)=0;
        *reg_value(0x831ada2c)^=0x20000000;
        *reg_value(0x831ada20)=0;
        *reg_value(0x831a2130)=0;
        *reg_value(0x831a5df0)=0;
        *reg_value(0x83120108)=0x3f000000;
        *reg_value(0x831ada20)=0x70000;
        *reg_value(0x831a5df0)=0x3ff00070;
        assert(!mt7915_bg_scan_start(&p,5500,20,5500,250,2));
        fail_write=writes+failure;
        mutex_unlock(&d.mt76.mutex); fire(&d); mutex_lock(&d.mt76.mutex);
        if(s->error!=-EIO) fprintf(stderr,"injection %u: error %d phase %u\n",failure,s->error,s->phase);
        assert(!s->dirty && !s->valid && s->error==-EIO && !s->changed);
        for(unsigned i=0;i<MT7915_BG_SAVE_COUNT;i++)
            assert(*reg_value(mt7915_bg_controls[i].address)==s->saved[i]);
        fail_write=0;
    }
    assert(!mt7915_bg_scan_start(&p,5500,20,5500,250,2));
    // A failed synchronization reply retains the reservation, even after STOP.
    query_error=-ETIMEDOUT;
    assert(mt7915_bg_scan_stop(&d,-ECANCELED)==-ETIMEDOUT && s->dirty);
    query_error=0; assert(!mt7915_bg_scan_stop(&d,0));
    // START must be synchronized before any host configuration writes.
    assert(!mt7915_bg_scan_start(&p,5500,20,5500,250,2));
    unsigned untouched=writes;
    query_error=-ETIMEDOUT;
    mutex_unlock(&d.mt76.mutex); fire(&d); mutex_lock(&d.mt76.mutex);
    assert(s->dirty && !s->valid && s->changed && writes==untouched);
    assert(s->error==-ETIMEDOUT && s->restore_error==-ETIMEDOUT);
    query_error=0; assert(!mt7915_bg_scan_stop(&d,0));
    assert(!mt7915_bg_scan_start(&p,5500,20,5500,250,2));
    unsigned before=send_count, wbefore=writes;
    d.mphy.state=1UL<<MT76_MCU_RESET;
    assert(mt7915_bg_scan_reset(&d));
    assert(send_count==before && writes==wbefore && s->dirty && !s->valid);
    assert(mt7915_bg_scan_stop(&d,-EIO)==-EAGAIN && send_count==before);
    mt7915_bg_scan_reset_done(&d);
    assert(!s->dirty && !s->changed && !s->controls_saved && !s->phy);
    d.mphy.state=0;
    // Main channel/scanning changes cancel a running observer and restore controls.
    assert(!mt7915_bg_scan_start(&p,5500,20,5500,250,2));
    mutex_unlock(&d.mt76.mutex); fire(&d);
    m.state |= 1UL<<MT76_SCANNING; fire(&d);
    assert(!s->dirty && !s->valid && s->error==-ECANCELED);
    m.state &= ~(1UL<<MT76_SCANNING);
    // Survey failures cannot produce a usable observer measurement.
    for (unsigned phase=0;phase<2;phase++) {
        mutex_lock(&d.mt76.mutex);
        assert(!mt7915_bg_scan_start(&p,5500,20,5500,250,2));
        mutex_unlock(&d.mt76.mutex);
        if (phase) fire(&d);
        mib_error=-ETIMEDOUT; fire(&d); mib_error=0;
        assert(!s->valid && !s->dirty && s->error==-ETIMEDOUT);
    }
    // Actual failed bench capture: a valid modulo-23 interval, not a stopped sampler.
    const u32 wrap_first[]={8272163,320,767,40035,263152,165,209,138,170,299,26717};
    const u32 wrap_last[]={10537,323,769,40036,263154,167,214,140,172,299,27117};
    memcpy(s->first.bins,wrap_first,sizeof(wrap_first));
    memcpy(s->last.bins,wrap_last,sizeof(wrap_last));
    s->elapsed_us=1019196; s->threshold=2;
    assert(!mt7915_bg_scan_calculate(s));
    assert(s->sample_count==127401 && s->wrap_mask==1 && s->delta.bins[0]==126982);
    assert(s->busy_permille==0 && s->busy_raw_permille==4);
    // No periodic clear: 200 successive one-second intervals cross multiple boundaries.
    memset(&s->first,0,sizeof(s->first)); memset(&s->last,0,sizeof(s->last));
    s->elapsed_us=1000000;
    unsigned wraps=0;
    for(unsigned window=0;window<200;window++) {
        s->first=s->last;
        s->last.bins[0]=(s->first.bins[0]+124500)&GENMASK(22,0);
        s->last.bins[10]=(s->first.bins[10]+500)&GENMASK(22,0);
        assert(!mt7915_bg_scan_calculate(s));
        assert(s->sample_count==125000 && !s->busy_permille && s->busy_raw_permille==4);
        wraps+=!!s->wrap_mask;
    }
    assert(wraps==2);
    s->elapsed_us=6000001; assert(mt7915_bg_scan_calculate(s)==-ERANGE);
    // The maximum low-23 value is a valid reading, not itself proof of saturation.
    memset(&s->first,0,sizeof(s->first)); memset(&s->last,0,sizeof(s->last));
    s->elapsed_us=1000000;
    s->last.bins[0]=GENMASK(22,0); s->first.bins[0]=s->last.bins[0]-125000;
    assert(!mt7915_bg_scan_calculate(s) && !s->wrap_mask);
    s->first=s->last; assert(mt7915_bg_scan_calculate(s)==-ERANGE); // stuck maximum
    s->last.bins[0]=124999;
    assert(!mt7915_bg_scan_calculate(s) && s->wrap_mask==1);
    s->first.bins[0]=500000; s->last.bins[0]=100;
    assert(mt7915_bg_scan_calculate(s)==-ERANGE); // reset-like population is implausible
    // A completed CAC must leave evidence even without a probe-cac tap.
    assert(!s->cac_attempted);
    struct cfg80211_chan_def cac_definition={.chan=&chans[32],.width=20,.center_freq1=5500};
    chans[32].flags=IEEE80211_CHAN_RADAR;
    assert(!mt7915_set_radar_background(&hw,&cac_definition));
    assert(s->cac_attempted);
    assert(!mt7915_set_radar_background(&hw,NULL) && s->cac_attempted);
    mutex_lock(&d.mt76.mutex);
    mt7915_bg_scan_reset_done(&d);
    mutex_unlock(&d.mt76.mutex);
    assert(s->cac_attempted);
    // Real radar callback: standalone RDD2 reports are dropped even on failed cleanup.
    struct mt7915_mcu_rdd_report radar={.rdd_idx=2};
    struct sk_buff radar_skb={.data=(void *)&radar,.len=sizeof(radar)};
    d.mt76.phy.hw=&hw; d.mt76.phys[1]=&m; d.rdd2_phy=&p;
    s->dirty=true; s->borrowed=false;
    mt7915_mcu_rx_radar_detected(&d,&radar_skb);
    assert(!background_radar && !d.hw_pattern);
    radar.rdd_idx=0; mt7915_mcu_rx_radar_detected(&d,&radar_skb);
    radar.rdd_idx=1; mt7915_mcu_rx_radar_detected(&d,&radar_skb);
    assert(main_radar==2 && d.hw_pattern==2);
    s->borrowed=true; radar.rdd_idx=2;
    mt7915_mcu_rx_radar_detected(&d,&radar_skb);
    assert(background_radar==1 && d.hw_pattern==3);
    s->dirty=false; s->borrowed=false; d.rdd2_phy=NULL;
    mt7915_mcu_rx_radar_detected(&d,&radar_skb);
    assert(background_radar==1);
    mt7915_bg_scan_cleanup(&d); assert(s->shutdown && !s->dirty && !s->work.work.pending);
    mutex_lock(&d.mt76.mutex);
    assert(mt7915_bg_scan_start(&p,5500,20,5500,250,2)==-ESHUTDOWN);
    mutex_unlock(&d.mt76.mutex);
    fclose(output.out);
    puts("observer: counter validity, raw replies, masked writes, restoration and CAC lifecycle pass");
    return 0;
}
'''
c = work / 'observer-host.c'
c.write_text(shim + '\n' + '\n'.join(functions) + tests)
exe = work / 'observer-host'
subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                '-Wno-sign-compare', '-Wno-unused-function',
                '-Wno-unused-parameter', '-fsanitize=address,undefined',
                '-g', '-o', str(exe), str(c)], check=True)
json_path = work / 'observer-output.jsonl'
subprocess.run([str(exe)], check=True, env={**os.environ,
    'ASAN_OPTIONS': 'detect_leaks=0', 'OBSERVER_JSON_PATH': str(json_path)})
result = json.loads(json_path.read_text())
assert result['schema'] == 1 and result['rx_reserved']
assert result['valid'] and result['energy_permille'] == 200
assert result['tx_first_us'] == 0 and result['tx_last_us'] == 50000
