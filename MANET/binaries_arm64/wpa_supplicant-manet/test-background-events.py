#!/usr/bin/env python3
"""Compile the actual nl80211 radar decoder from a supplied hostap source tree.

Usage: python3 test-background-events.py /path/to/wpa_supplicant-2.11
Only netlink attribute access, logging and event consumers are replaced.
Both mesh and AP inputs exercise the real decoder, with and without the flag.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

source = Path(sys.argv[1]) / 'src/drivers/driver_nl80211_event.c'
text = source.read_text()
a = text.index('static void nl80211_radar_event(')
b = text.index('\n}\n', a) + 3
function = text[a:b]
header = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "nl80211_copy.h"
#define CONFIG_MESH 1
#define NL80211_DRV_LINK_ID_NA -1
#define MSG_DEBUG 0
#define os_memset memset
#define wpa_printf(...) ((void)0)
struct nlattr { uint32_t value; };
struct wpa_driver_nl80211_data { void *ctx; void *first_bss; int nlmode; };
union wpa_event_data { struct {
 int link_id, freq, ht_enabled, chan_offset, chan_width, cf1, cf2;
} dfs_event; };
enum { EVENT_DFS_RADAR_DETECTED, EVENT_DFS_CAC_FINISHED, EVENT_DFS_CAC_ABORTED,
 EVENT_DFS_NOP_FINISHED, EVENT_DFS_PRE_CAC_EXPIRED, EVENT_DFS_CAC_STARTED };
static int calls, last_event, last_freq;
static uint32_t nla_get_u32(struct nlattr *a) { return a->value; }
static uint8_t nla_get_u8(struct nlattr *a) { return a->value; }
static int nl80211_get_link_id_by_freq(void *bss, int freq) { return -1; }
static int convert2width(int width) { return width; }
static void wpa_supplicant_event(void *ctx, int event, union wpa_event_data *data) {
 calls++; last_event = event; last_freq = data->dfs_event.freq;
}
'''
main = r'''
int main(void) {
 struct nlattr freq = {5300}, event = {0}, flag = {0}, width = {NL80211_CHAN_WIDTH_20};
 struct nlattr *tb[NL80211_ATTR_MAX + 1] = {0};
 struct wpa_driver_nl80211_data drv = {0};
 int types[] = { NL80211_RADAR_CAC_STARTED, NL80211_RADAR_CAC_FINISHED,
   NL80211_RADAR_CAC_ABORTED, NL80211_RADAR_DETECTED,
   NL80211_RADAR_NOP_FINISHED, NL80211_RADAR_PRE_CAC_EXPIRED };
 int mapped[] = { EVENT_DFS_CAC_STARTED, EVENT_DFS_CAC_FINISHED,
   EVENT_DFS_CAC_ABORTED, EVENT_DFS_RADAR_DETECTED,
   EVENT_DFS_NOP_FINISHED, EVENT_DFS_PRE_CAC_EXPIRED };
 tb[NL80211_ATTR_WIPHY_FREQ] = &freq;
 tb[NL80211_ATTR_RADAR_EVENT] = &event;
 tb[NL80211_ATTR_CHANNEL_WIDTH] = &width;
 for (int mesh = 0; mesh < 2; mesh++)
 for (int bg = 0; bg < 2; bg++)
 for (int i = 0; i < 6; i++) {
   drv.nlmode = mesh ? NL80211_IFTYPE_MESH_POINT : NL80211_IFTYPE_AP;
   tb[NL80211_ATTR_RADAR_BACKGROUND] = bg ? &flag : NULL;
   event.value = types[i]; calls = 0;
   nl80211_radar_event(&drv, tb);
   if (mesh && bg && i < 3) {
     if (calls) { fprintf(stderr, "external mesh CAC %d reached foreground state machine\n", types[i]); return 1; }
   } else {
     assert(calls == 1 && last_event == mapped[i] && last_freq == 5300);
   }
 }
 puts("mesh background CAC isolated; foreground CAC, AP and all radar events preserved");
}
'''
with tempfile.TemporaryDirectory(prefix='manet-radar-c-') as tmp:
    c = Path(tmp) / 'radar.c'
    exe = Path(tmp) / 'radar'
    c.write_text(header + function + main)
    subprocess.run([os.environ.get('CC', 'cc'), '-std=c99', '-g', '-Wall', '-Werror',
                    '-fsanitize=address,undefined', '-fno-pie', '-no-pie',
                    '-I', str(source.parent), str(c), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
