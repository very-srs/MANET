"""Mock kernel environment and lifecycle assertions for extracted hold C."""
head = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stddef.h>
#include <errno.h>
typedef int gfp_t;
#define GFP_KERNEL 0
#define EXPORT_SYMBOL(x)
#define GFP_ATOMIC 0
#define NL80211_DFS_USABLE 0
#define NL80211_DFS_UNAVAILABLE 1
#define NL80211_DFS_AVAILABLE 2
#define IEEE80211_DFS_MIN_CAC_TIME_MS 60000U
#define NL80211_CHAN_WIDTH_20 20
#define NL80211_EXT_FEATURE_RADAR_BACKGROUND 0
#define IEEE80211_CHAN_RADAR 1
#define READ_ONCE(x) (x)
#define max(a,b) ((a)>(b)?(a):(b))
#define guard(x) noop
#define lockdep_assert_wiphy(x) ((void)(x))
#define spin_lock_irqsave(x,f) do { (f)=0; } while (0)
#define spin_unlock_irqrestore(x,f) ((void)(f))
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define to_delayed_work(p) container_of(p, struct delayed_work, work)
static unsigned long jiffies=123;
static void *cfg80211_wq;
static int starts, stops, start_error, stop_error, messages, firmware_active;
static unsigned int cac_ms, queued_ms;
struct work_struct { int busy; };
struct delayed_work { struct work_struct work; bool pending; };
struct net_device {};
struct wiphy {};
struct ieee80211_channel { int flags, dfs_state, center_freq; unsigned long dfs_state_entered; };
struct cfg80211_chan_def { struct ieee80211_channel *chan; int width, center_freq1; };
struct wireless_dev { struct wiphy *wiphy; struct net_device *netdev; };
struct cfg80211_registered_device {
 struct wiphy wiphy; struct wireless_dev *background_radar_wdev;
 struct cfg80211_chan_def background_radar_chandef, radar_chandef, cac_done_chandef;
 struct delayed_work background_cac_done_wk;
 struct work_struct background_cac_abort_wk, propagate_radar_detect_wk, propagate_cac_done_wk;
 int background_hold_lock; bool background_hold_enabled, background_hold_active, background_hold_revoked, background_hold_fault;
 uint64_t background_hold_generation;
};
enum nl80211_radar_event { NL80211_RADAR_CAC_STARTED, NL80211_RADAR_CAC_FINISHED, NL80211_RADAR_CAC_ABORTED, NL80211_RADAR_DETECTED };
static void noop(void *p) {}
static struct cfg80211_registered_device *wiphy_to_rdev(struct wiphy *w) { return container_of(w, struct cfg80211_registered_device, wiphy); }
static void trace_cfg80211_radar_event(struct wiphy *w, struct cfg80211_chan_def *c, bool o) {}
static void cfg80211_set_dfs_state(struct wiphy *w, const struct cfg80211_chan_def *c, int state) { c->chan->dfs_state=state; }
static void cfg80211_sched_dfs_chan_update(struct cfg80211_registered_device *r) {}
static bool cancel_delayed_work(struct delayed_work *w) { bool p=w->pending; w->pending=false; return p; }
static int work_busy(struct work_struct *w) { return w->busy; }
static bool delayed_work_pending(struct delayed_work *w) { return w->pending; }
static void queue_work(void *q, struct work_struct *w) { w->busy=1; }
static void queue_delayed_work(void *q, struct delayed_work *w, unsigned int ms) { w->pending=true; queued_ms=ms; }
static unsigned int msecs_to_jiffies(unsigned int ms) { return ms; }
static void nl80211_radar_notify(struct cfg80211_registered_device *r, const struct cfg80211_chan_def *c, enum nl80211_radar_event e, struct net_device *n, gfp_t gfp) { messages++; }
static bool cfg80211_chandef_identical(const struct cfg80211_chan_def *a, const struct cfg80211_chan_def *b) { return a->chan == b->chan && a->width == b->width && a->center_freq1 == b->center_freq1; }
static bool cfg80211_chandef_valid(const struct cfg80211_chan_def *c) { return c->chan != NULL; }
static bool wiphy_ext_feature_isset(struct wiphy *w, int feature) { return true; }
static unsigned int cfg80211_chandef_dfs_cac_time(struct wiphy *w, const struct cfg80211_chan_def *c) { return cac_ms; }
static int rdev_set_radar_background(struct cfg80211_registered_device *r, struct cfg80211_chan_def *c) {
 if (c) { starts++; if (!start_error) firmware_active=1; return start_error; }
 stops++; if (!stop_error) firmware_active=0; return stop_error;
}
void cfg80211_stop_background_radar_detection(struct wireless_dev *w);
'''
body = r'''
static struct cfg80211_registered_device r;
static struct wireless_dev wdev;
static struct ieee80211_channel channel;
static struct cfg80211_chan_def ch;
static void init(int freq) {
 memset(&r,0,sizeof(r)); memset(&wdev,0,sizeof(wdev)); memset(&channel,0,sizeof(channel));
 r.background_hold_enabled=true; wdev.wiphy=&r.wiphy;
 channel.center_freq=freq; channel.flags=IEEE80211_CHAN_RADAR;
 ch=(struct cfg80211_chan_def){ .chan=&channel, .width=20, .center_freq1=freq };
 starts=stops=messages=firmware_active=start_error=stop_error=cac_ms=queued_ms=0;
}
static void done(void) { r.background_cac_done_wk.pending=false; cfg80211_background_cac_done_wk(&r.background_cac_done_wk.work); }
int main(void) {
 init(5260); assert(!cfg80211_start_background_radar_detection(&r,&wdev,&ch));
 assert(queued_ms==60000); done();
 assert(firmware_active && r.background_hold_active && r.background_radar_wdev==&wdev && channel.dfs_state==2 && !stops);
 assert(cfg80211_start_background_radar_detection(&r,&wdev,&ch)==-EBUSY);
 __cfg80211_radar_event(&r.wiphy,&ch,true,0);
 assert(!r.background_hold_active && r.background_hold_revoked && channel.dfs_state==1);
 done(); assert(channel.dfs_state==1);
 cfg80211_background_cac_abort_wk(&r.background_cac_abort_wk);
 assert(!firmware_active && !r.background_radar_wdev && channel.dfs_state==1);
 init(5260); assert(!cfg80211_start_background_radar_detection(&r,&wdev,&ch));
 __cfg80211_radar_event(&r.wiphy,&ch,true,0); done(); assert(channel.dfs_state==1 && !r.background_hold_active);
 init(5600); assert(!cfg80211_start_background_radar_detection(&r,&wdev,&ch)); assert(queued_ms==600000); done();
 cfg80211_background_cac_abort(&r.wiphy); assert(channel.dfs_state==0 && !r.background_hold_active);
 stop_error=-EIO; cfg80211_background_cac_abort_wk(&r.background_cac_abort_wk);
 assert(!r.background_radar_wdev && !r.background_hold_active && r.background_hold_fault);
 assert(cfg80211_start_background_radar_detection(&r,&wdev,&ch)==-EBUSY);
 stop_error=0; cfg80211_stop_background_radar_detection(&wdev); assert(!r.background_radar_wdev && r.background_hold_fault);
 r.background_hold_enabled=false; done(); assert(channel.dfs_state==0);
 init(5260); cac_ms=1000; assert(!cfg80211_start_background_radar_detection(&r,&wdev,&ch)); assert(queued_ms==60000);
 channel.dfs_state=1; done(); assert(channel.dfs_state==1 && !r.background_hold_active);
 init(5260); assert(!cfg80211_start_background_radar_detection(&r,&wdev,&ch));
 __cfg80211_radar_event(&r.wiphy,&ch,false,0); done(); assert(channel.dfs_state==1 && !r.background_hold_active);
 init(5580); assert(!cfg80211_start_background_radar_detection(&r,&wdev,&ch)); assert(queued_ms==60000);
 cfg80211_stop_background_radar_detection(&wdev); done(); assert(channel.dfs_state==0);
 init(5260); start_error=-EIO; assert(cfg80211_start_background_radar_detection(&r,&wdev,&ch)==-EIO);
 assert(!r.background_radar_wdev && !r.background_hold_active && !firmware_active);
 init(5260); channel.dfs_state=1; assert(cfg80211_start_background_radar_detection(&r,&wdev,&ch)==-EINVAL); assert(!starts);
 init(5260); r.background_hold_enabled=false; assert(!cfg80211_start_background_radar_detection(&r,&wdev,&ch)); done();
 assert(!firmware_active && !r.background_radar_wdev && channel.dfs_state==2);
 puts("CAC completion, held owner, radar ordering, cancellation, stop failure, NOP and weather CAC passed");
}
'''
