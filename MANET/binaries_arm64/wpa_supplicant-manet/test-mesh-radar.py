#!/usr/bin/env python3
"""Exercise actual hostap radar, NOP and CSA callbacks with isolated dependencies.

Usage: test-mesh-radar.py /path/to/patched/wpa_supplicant-2.11
The harness replaces hardware, channel lookup and BSS lifecycle consumers.
It cannot prove RF stop latency or on-radio SAE continuity.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(sys.argv[1])
def function(path, name):
    source = (root / path).read_text()
    start = source.index(name + '(')
    start = source.rfind('\n', 0, start) + 1
    end = source.index('\n}\n', start) + 3
    return source[start:end]

header = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#define CONFIG_MESH 1
#define NEED_AP_MLME 1
#define MSG_INFO 0
#define wpa_msg(...) ((void)0)
#define wpa_printf(...) ((void)0)
#define hostapd_logger(...) ((void)0)
typedef uint8_t u8;
typedef uint16_t u16;
enum { HOSTAPD_CHAN_DFS_UNAVAILABLE=1, HOSTAPD_CHAN_DFS_USABLE,
 HAPD_IFACE_ENABLED, HAPD_IFACE_DFS, WPA_DRIVER_FLAGS_DFS_OFFLOAD=16 };
enum { CHAN_WIDTH_20_NOHT, CHAN_WIDTH_20, CHAN_WIDTH_40, CHAN_WIDTH_80,
 CHAN_WIDTH_80P80, CHAN_WIDTH_160, CHAN_WIDTH_320 };
enum { CONF_OPER_CHWIDTH_USE_HT, CONF_OPER_CHWIDTH_80MHZ,
 CONF_OPER_CHWIDTH_80P80MHZ, CONF_OPER_CHWIDTH_160MHZ, CONF_OPER_CHWIDTH_320MHZ };
enum { HOSTAPD_MODE_IEEE80211A, NUM_HOSTAPD_MODES,
 CH_SWITCH_VHT_ENABLED=1, CH_SWITCH_VHT_DISABLED=2,
 CH_SWITCH_HE_ENABLED=1, CH_SWITCH_HE_DISABLED=2,
 HT_CAP_INFO_SUPP_CHANNEL_WIDTH_SET=2, VHT_CAP_SUPP_CHAN_WIDTH_MASK=12,
 VHT_CAP_SUPP_CHAN_WIDTH_160MHZ=4, VHT_CAP_SUPP_CHAN_WIDTH_160_80PLUS80MHZ=8 };
struct config { int ieee80211h, ieee80211n, ieee80211ac, ieee80211ax;
 int channel, ch_switch_vht_config, ch_switch_he_config, ch_switch_eht_config;
 int ht_capab, secondary_channel, op_class, vht_capab;
};
struct mode { int mode; };
struct hostapd_iface { struct hostapd_data **bss; struct config *conf;
 void *mconf; int manet_dfs_owner; int drv_flags, state, cac_started, radar_detected, freq;
 struct {int channel;} radar_background;
 struct mode *current_mode; void *hw_features; int num_hw_features; size_t num_bss;
};
struct hostapd_data { void *msg_ctx; struct hostapd_iface *iface;
 struct config *iconf; int csa_in_progress; struct {int freq;} cs_freq_params; };
struct ssid {int frequency;};
struct wpa_supplicant {struct hostapd_iface *ap_iface, *ifmsh;
 int assoc_freq; struct ssid *current_ssid;};
static int state_writes, state_freq, state_value, switches, resets, setup, available;
static int set_dfs_state(struct hostapd_iface *i, int f, int ht, int off, int w,
 int cf1, int cf2, int state) {
 state_writes++; state_freq=f; state_value=state; return 1;
}
static int hostapd_dfs_is_background_event(struct hostapd_iface *i, int f) {return 0;}
static int dfs_are_channels_overlapped(struct hostapd_iface *i, int f, int w, int c1, int c2) {return f==i->freq;}
static int hostapd_dfs_background_start_channel_switch(struct hostapd_iface *i, int f) {return -1;}
static int hostapd_dfs_start_channel_switch(struct hostapd_iface *i) {switches++; return 0;}
static int hostapd_handle_dfs(struct hostapd_iface *i) {setup++; return 0;}
static int dfs_use_radar_background(struct hostapd_iface *i) {return 0;}
static void hostapd_dfs_update_background_chain(struct hostapd_iface *i) {setup++;}
static int hostapd_is_dfs_required(struct hostapd_iface *i) {return i->conf->ieee80211h && i->freq==5260;}
static int hostapd_hw_get_channel(struct hostapd_data *h, int f) {return (f-5000)/5;}
static int hostapd_csa_update_hwmode(struct hostapd_iface *i) {return 0;}
static int ieee80211_freq_to_chan(int f, u8 *c) {*c=(f-5000)/5; return 0;}
static int ieee80211_freq_to_channel_ext(int f, int off, int w, u8 *op, u8 *ch) {*op=115; *ch=(f-5000)/5; return 0;}
static void hostapd_set_oper_chwidth(struct config *c, int w) {}
static void hostapd_set_oper_centr_freq_seg0_idx(struct config *c, int s) {}
static void hostapd_set_oper_centr_freq_seg1_idx(struct config *c, int s) {}
static void hostapd_set_and_check_bw320_offset(struct config *c, int s) {}
static int ieee80211_is_dfs(int f, void *m, int n) {return f==5260;}
static void hostapd_cleanup_cs_params(struct hostapd_data *h) {h->csa_in_progress=0;}
static void ieee802_11_set_beacon(struct hostapd_data *h) {}
static void hostapd_setup_interface_complete(struct hostapd_iface *i, int err) {setup++;}
static int hostapd_is_dfs_chan_available(struct hostapd_iface *i) {return available;}
static void hostapd_disable_iface(struct hostapd_iface *i) {resets++;}
static void hostapd_enable_iface(struct hostapd_iface *i) {resets++;}
static void hostapd_neighbor_set_own_report(struct hostapd_data *h) {}
'''
main = r'''
int main(int argc, char **argv) {
 struct config conf={0}; struct mode mode={HOSTAPD_MODE_IEEE80211A};
 struct hostapd_iface iface={.conf=&conf, .state=HAPD_IFACE_ENABLED,
   .freq=5260, .current_mode=&mode, .num_bss=1};
 struct hostapd_data hapd={.iface=&iface, .iconf=&conf};
 struct hostapd_data *bss[]={&hapd}; iface.bss=bss;
 struct ssid ssid={5260};
 struct wpa_supplicant wpa={.ifmsh=&iface, .current_ssid=&ssid};
 char reply[64];
 iface.mconf=&iface;
 assert(manet_control(&wpa,"MANET_DFS_STATUS",reply,sizeof(reply))>0);
 assert(strcmp(reply,"MANET_DFS_V3 owner=0\n")==0);
 assert(manet_control(&wpa,"MANET_DFS_OWNER 1",reply,sizeof(reply))==3);
 assert(iface.manet_dfs_owner==1);
 assert(manet_control(&wpa,"MANET_DFS_OWNER 0",reply,sizeof(reply))==3);
 assert(iface.manet_dfs_owner==0);
 iface.mconf=NULL;
 assert(manet_control(&wpa,"MANET_DFS_OWNER 1",reply,sizeof(reply))==-1);
 assert(iface.manet_dfs_owner==0);
 /* Mesh entered from non-DFS (11h=0) and mesh started on DFS (11h=1). */
 for (int h=0; h<2; h++) {
  if (argc > 1 && strcmp(argv[1], h ? "radar1" : "radar0")) continue;
  conf.ieee80211h=h; iface.mconf=&iface; iface.manet_dfs_owner=1;
  state_writes=switches=resets=setup=0; iface.radar_detected=false;
  hostapd_dfs_radar_detected(&iface,5260,1,0,CHAN_WIDTH_20,5260,0);
  if (state_writes!=1 || state_freq!=5260 || state_value!=HOSTAPD_CHAN_DFS_UNAVAILABLE ||
      switches || resets || setup || iface.radar_detected) {
   fprintf(stderr,"mesh radar h=%d: NOP writes=%d, switches=%d, resets=%d, radar latch=%d\n",
      h,state_writes,switches,resets,iface.radar_detected); return 1;
  }
  /* Off-channel radar must not trigger a move either. */
  hostapd_dfs_radar_detected(&iface,5300,1,0,CHAN_WIDTH_20,5300,0);
  assert(state_freq==5300 && !switches && !resets);
  hostapd_dfs_nop_finished(&iface,5260,1,0,CHAN_WIDTH_20,5260,0);
  assert(state_value==HOSTAPD_CHAN_DFS_USABLE && !setup && !resets);
 }
 /* Static/unmanaged mesh retains upstream evacuation and CAC checking. */
 iface.mconf=&iface; iface.manet_dfs_owner=0; conf.ieee80211h=1;
 iface.freq=5260; switches=0; iface.radar_detected=false;
 hostapd_dfs_radar_detected(&iface,5260,1,0,CHAN_WIDTH_20,5260,0);
 if (switches!=1 || !iface.radar_detected) {
  fputs("unmanaged mesh lost native radar evacuation\n",stderr); return 1;
 }
 iface.freq=5240; resets=0;
 hostapd_event_ch_switch(&hapd,5260,1,0,CHAN_WIDTH_20,5260,0,0,1);
 assert(resets==2);
 /* Ordinary AP radar and NOP retain the existing state machine. */
 iface.mconf=NULL; conf.ieee80211h=1; iface.freq=5260; switches=0;
 hostapd_dfs_radar_detected(&iface,5260,1,0,CHAN_WIDTH_20,5260,0);
 assert(switches==1 && iface.radar_detected);
 iface.state=HAPD_IFACE_DFS; setup=0;
 hostapd_dfs_nop_finished(&iface,5260,1,0,CHAN_WIDTH_20,5260,0);
 assert(setup==1);
 if (argc > 1 && strcmp(argv[1], "csa")) return 0;
 /* External/peer CSA updates the mesh without trusting stale CAC cache. */
 iface.mconf=&iface; iface.manet_dfs_owner=1; iface.state=HAPD_IFACE_ENABLED;
 for (int dest=5260; dest>=5240; dest-=20) {
  iface.freq=5240; resets=setup=0; available=0;
  for (int done=0; done<2; done++)
   wpas_ap_ch_switch(&wpa,dest,1,0,CHAN_WIDTH_20,dest,0,0,done);
  assert(iface.freq==dest && ssid.frequency==dest && wpa.assoc_freq==dest);
  if (resets || setup) {fputs("peer CSA restarted mesh BSS\n",stderr); return 1;}
 }
 /* The AP still retries CAC when an uncleared DFS channel is selected. */
 iface.mconf=NULL; iface.freq=5240; resets=0;
 hostapd_event_ch_switch(&hapd,5260,1,0,CHAN_WIDTH_20,5260,0,0,1);
 assert(resets==2);
 puts("mesh radar/NOP and external CSA preserve BSS; AP behavior retained");
}
'''
functions = ''.join(function(path, name) for path, name in (
    ('src/ap/dfs.c', 'hostapd_dfs_radar_detected'),
    ('src/ap/dfs.c', 'hostapd_dfs_nop_finished'),
    ('src/ap/drv_callbacks.c', 'hostapd_event_ch_switch'),
    ('wpa_supplicant/ap.c', 'wpas_ap_ch_switch'),
))
# Compile the real per-BSS control dispatch as well as the callbacks.
control_source = (root / 'wpa_supplicant/ctrl_iface.c').read_text()
control_start = control_source.index('#ifdef CONFIG_MESH\n\t} else if (os_strcmp(buf, "MANET_DFS_STATUS")')
control_end = control_source.index('#endif /* CONFIG_MESH */', control_start) + len('#endif /* CONFIG_MESH */')
functions += '\n#define os_strcmp strcmp\n#define os_snprintf snprintf\n'
functions += 'static int manet_control(struct wpa_supplicant *wpa_s, const char *buf, char *reply, size_t reply_size) { int reply_len=3; if (0) {\n'
functions += control_source[control_start:control_end] + '\n} return reply_len; }\n'
with tempfile.TemporaryDirectory(prefix='manet-mesh-radar-') as tmp:
    c, exe = Path(tmp)/'radar.c', Path(tmp)/'radar'
    c.write_text(header + functions + main)
    subprocess.run([os.environ.get('CC','cc'), '-std=c99', '-g', '-Wall', '-Werror',
                    '-fsanitize=address,undefined', '-fno-pie', '-no-pie',
                    str(c), '-o', str(exe)], check=True)
    failed = []
    for case in ('radar0', 'radar1', 'csa'):
        result = subprocess.run([str(exe), case])
        if result.returncode:
            failed.append(case)
    if failed:
        raise SystemExit('Failed cases: ' + ', '.join(failed))
