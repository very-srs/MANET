#!/usr/bin/env python3
"""Execute actual v2 lifecycle and heartbeat functions with mocked scheduling."""
import os
from pathlib import Path
import sys
import subprocess
import tempfile
w = Path(sys.argv[1])
output = Path(sys.argv[2])
from hold_fixture import head as fixture_head, body as fixture_body
parts = dict(head=fixture_head, body=fixture_body)
head = parts['head']
head = head.replace('typedef int gfp_t;', '''typedef uint64_t u64;
typedef uint32_t u32;
typedef int gfp_t;
#define EXPORT_SYMBOL_GPL(x)
static uint64_t now_us=1000000, random_serial;
#define ktime_get_boottime() now_us
#define ktime_to_us(v) (v)
static void get_random_bytes(void *p, size_t n) { memset(p, ++random_serial, n); }
static void *system_wq;
struct wiphy;
''' + (w/'net/wireless/hold-api.h').read_text())
start = (w/'net/wireless/core.h').read_text()
extra = start[start.index('\tstruct manet_hold_token background_hold_token'):start.index('\tstruct wireless_dev *background_radar_wdev;')]
head = head.replace('uint64_t background_hold_generation;', 'uint64_t background_hold_generation;\n' + extra)
head += '\n#define mod_delayed_work queue_delayed_work\n'
head = head.replace('struct wiphy {};', 'struct wiphy { int mtx; };')
head += r'''
#include <stdarg.h>
#define U64_MAX UINT64_MAX
#define ARRAY_SIZE(x) (sizeof(x)/sizeof((x)[0]))
struct seq_file { struct wiphy *private; char buf[4096]; size_t len; };
static int mutex_trylock(void *p) { return 1; }
static void mutex_unlock(void *p) {}
static void seq_printf(struct seq_file *s,const char *fmt,...) {
 va_list ap; va_start(ap,fmt); s->len+=vsnprintf(s->buf+s->len,sizeof(s->buf)-s->len,fmt,ap); va_end(ap);
 assert(s->len<sizeof(s->buf));
}
static void seq_puts(struct seq_file *s,const char *text) { seq_printf(s,"%s",text); }
''' 
s=(w/'net/wireless/mlme.c').read_text()
source=s[s.index('static void cfg80211_background_hold_revoke'):s.index('void cfg80211_cac_event(')]
source+=s[s.index('static void\n__cfg80211_background_cac_event'):s.index('int cfg80211_assoc_ml_reconf')]
# The Pi stable tree adds an unrelated main-link MLO CAC helper between the
# background helpers. This harness exercises the background lifecycle only.
if 'void cfg80211_stop_radar_detection(' in source:
 source = (source[:source.index('void cfg80211_stop_radar_detection(')] +
           source[source.index('void cfg80211_stop_background_radar_detection('):])
source=source.replace('#include "hold-monitor.inc"', (w/'net/wireless/hold-monitor.inc').read_text().replace('#include <linux/random.h>', ''))
status=(w/'net/wireless/hold-debugfs.inc').read_text()
source += status[:status.index('static int background_hold_open')]
body=parts['body']
body=body.replace('token=r.background_hold_token; done();', 'token=r.background_hold_token; done();\n struct seq_file snapshot={.private=&r.wiphy};\n assert(!background_hold_show(&snapshot,NULL)); puts(snapshot.buf);')
body=body.replace('assert(r.background_hold_radar_sequence==1);', 'assert(r.background_hold_radar_sequence==1);\n snapshot.len=0; assert(!background_hold_show(&snapshot,NULL)); puts(snapshot.buf);')
body=body.replace('static void done(void) {', '''static void heartbeat(void) {
 struct manet_hold_sample sample={.at_us=now_us,.sequence=r.background_hold_sample.sequence+1,.valid=true};
 cfg80211_hold_sample_v2(&r.wiphy,&r.background_hold_token,&sample);
}
static void done(void) { heartbeat();''')
body=body.replace('puts("CAC completion', '''
 /* No first sample is never clearance. */
 init(5260); assert(!cfg80211_start_background_radar_detection(&r,&wdev,&ch));
 r.background_cac_done_wk.pending=false;
 cfg80211_background_cac_done_wk(&r.background_cac_done_wk.work);
 assert(!r.background_hold_active && r.background_hold_revoked && channel.dfs_state==0);
 /* Deadline withdraws availability without driver or user-space activity. */
 init(5260); assert(!cfg80211_start_background_radar_detection(&r,&wdev,&ch));
 done(); assert(r.background_hold_active);
 now_us+=MANET_HOLD_MAX_AGE_US;
 assert(!background_hold_fresh(&r,now_us));
 cfg80211_background_hold_watch(&r.background_hold_watch.work);
 assert(r.background_hold_revoked && !r.background_hold_active && channel.dfs_state==0);
 assert(!strcmp(r.background_hold_reason,"heartbeat-expired"));
 now_us++; heartbeat(); assert(!r.background_hold_active);
 /* A late sample cannot resurrect even if the watchdog has not run. */
 init(5260); assert(!cfg80211_start_background_radar_detection(&r,&wdev,&ch));
 now_us+=MANET_HOLD_MAX_AGE_US; heartbeat();
 assert(r.background_hold_revoked);
 /* Wrong epoch/generation and out-of-order samples are fenced. */
 init(5260); assert(!cfg80211_start_background_radar_detection(&r,&wdev,&ch));
 struct manet_hold_token token=r.background_hold_token;
 struct manet_hold_sample sample={.at_us=now_us,.sequence=1,.valid=true};
 token.generation++;
 assert(!cfg80211_hold_sample_v2(&r.wiphy,&token,&sample));
 token=r.background_hold_token; token.epoch[0]++;
 assert(!cfg80211_hold_sample_v2(&r.wiphy,&token,&sample));
 heartbeat(); heartbeat(); assert(r.background_hold_revoked);
 /* Radar remains latched with its own token after stop and a new start. */
 init(5260); assert(!cfg80211_start_background_radar_detection(&r,&wdev,&ch));
 token=r.background_hold_token; done();
 __cfg80211_radar_event(&r.wiphy,&ch,true,0);
 assert(r.background_hold_radar_sequence==1);
 assert(!memcmp(&token,&r.background_hold_radar_token,sizeof(token)));
 assert(!strcmp(r.background_hold_radar_cause,"background-radar"));
 cfg80211_background_cac_abort_wk(&r.background_cac_abort_wk);
 r.background_cac_abort_wk.busy=0; channel.dfs_state=0;
 assert(!cfg80211_start_background_radar_detection(&r,&wdev,&ch));
 assert(memcmp(&token,&r.background_hold_token,sizeof(token)));
 assert(r.background_hold_token.generation==token.generation+1);
 assert(r.background_hold_radar_sequence==1 && r.background_hold_radar_token.generation==token.generation);
 sample.at_us=++now_us;
 assert(!cfg80211_hold_sample_v2(&r.wiphy,&token,&sample));
 /* Main radar on a different channel revokes but is not attributed to candidate. */
 struct ieee80211_channel other={.center_freq=5500,.flags=1};
 struct cfg80211_chan_def different={.chan=&other,.width=20,.center_freq1=5500};
 __cfg80211_radar_event(&r.wiphy,&different,false,0);
 assert(r.background_hold_radar_sequence==1 && r.background_hold_revoked);
 puts("V2 heartbeat deadline, stale publications, epoch restart, radar latch; CAC completion''')
body=body.replace('token=r.background_hold_token; done();', 'token=r.background_hold_token; done();\n struct seq_file snapshot={.private=&r.wiphy};\n assert(!background_hold_show(&snapshot,NULL)); puts(snapshot.buf);')
body=body.replace('assert(r.background_hold_radar_sequence==1);', 'assert(r.background_hold_radar_sequence==1);\n snapshot.len=0; assert(!background_hold_show(&snapshot,NULL)); puts(snapshot.buf);')
body = body.replace(' puts("V2 heartbeat', r'''
 /* Transfer only the live exact token after main detection. */
 init(5260); assert(!cfg80211_start_background_radar_detection(&r,&wdev,&ch));
 token=r.background_hold_token;
 assert(!cfg80211_hold_transfer_v3(&r.wiphy,&ch,&token));
 done(); token.generation++;
 assert(!cfg80211_hold_transfer_v3(&r.wiphy,&ch,&token));
 token=r.background_hold_token;
 assert(!cfg80211_hold_transfer_v3(&r.wiphy,&different,&token));
 assert(cfg80211_hold_transfer_v3(&r.wiphy,&ch,&token));
 assert(channel.dfs_state==NL80211_DFS_AVAILABLE && r.hold_main_active);
 cfg80211_background_cac_abort_wk(&r.background_cac_abort_wk);
 assert(!r.background_radar_wdev && !firmware_active);
 assert(channel.dfs_state==NL80211_DFS_AVAILABLE && r.hold_main_active);
 __cfg80211_radar_event(&r.wiphy,&ch,false,0);
 assert(!strcmp(r.background_hold_radar_cause,"main-radar"));
 assert(!memcmp(&token,&r.background_hold_radar_token,sizeof(token)));
 assert(!r.hold_main_active && channel.dfs_state==NL80211_DFS_UNAVAILABLE);
 cfg80211_hold_main_stop_v3(&r.wiphy);
 assert(channel.dfs_state==NL80211_DFS_UNAVAILABLE);
 init(5260); assert(!cfg80211_start_background_radar_detection(&r,&wdev,&ch));
 done(); token=r.background_hold_token; now_us+=MANET_HOLD_MAX_AGE_US;
 assert(!cfg80211_hold_transfer_v3(&r.wiphy,&ch,&token));
 puts("V2 heartbeat''')
with tempfile.TemporaryDirectory() as tmp:
 c=Path(tmp)/'hold.c'; exe=Path(tmp)/'hold'
 c.write_text(head+source+body)
 subprocess.run(['cc','-g','-Wall','-Werror','-Wno-unused-function','-fsanitize=address,undefined','-fno-pie','-no-pie',str(c),'-o',str(exe)],check=True)
 result=subprocess.run([str(exe)],check=True,text=True,stdout=subprocess.PIPE,env=dict(os.environ,ASAN_OPTIONS='detect_leaks=0'))
 import json
 lines=result.stdout.splitlines()
 records=[json.loads(line) for line in lines if line.startswith('{')]
 assert len(records)==2
 live, radar=records
 assert live['schema']==2 and live['live'] and live['receiver_live']
 assert live['epoch']==live['occupancy']['epoch'] and len(live['epoch'])==32
 assert radar['radar_sequence']==1 and radar['radar_epoch']==live['epoch']
 assert not radar['live'] and not radar['occupancy']['valid']
 (output/'status-examples.json').write_text(json.dumps(records,indent=2)+'\n')
 print(lines[-1])
 print('Actual debugfs JSON parsed: heartbeat, occupancy and radar tokens agree')
