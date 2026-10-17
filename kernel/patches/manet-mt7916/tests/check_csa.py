#!/usr/bin/env python3
"""Compile actual mesh CSA precedence and regulatory-reason branches."""
from pathlib import Path
import subprocess
import sys
import tempfile
import os
root=Path(sys.argv[1])/'net/mac80211'
mesh=(root/'mesh.c').read_text()
start=mesh.index('\tif (cfg80211_chandef_identical(&params.chandef,',mesh.index('ieee80211_mesh_process_chnswitch'))
end=mesh.index('\n\tmcsa_dbg',start+5)
branch=mesh[start:end]
# The first debug call is nested. End after its containing return/brace.
if 'return true;' not in branch:
    end=mesh.index('\n\t}',end)+3
    branch=mesh[start:end]
reasons=[]
for file in ('util.c','mesh.c'):
    text=(root/file).read_text()
    start=text.index('put_unaligned_le16(',text.index('WLAN_EID_CHAN_SWITCH_PARAM_TX_RESTRICT'))
    end=text.index(';',start)+1
    reasons.append(text[start:end])
head=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#define IEEE80211_CHAN_RADAR 8
#define WLAN_REASON_MESH_CHAN 52
#define WLAN_REASON_MESH_CHAN_REGULATORY 53
#define mcsa_dbg(...) ((void)0)
struct channel {int flags;};
struct chandef {int freq; struct channel *chan;};
struct settings {bool block_tx; struct chandef chandef;};
struct data {struct {struct {struct {struct chandef oper;} chanreq;} bss_conf;} vif;};
static bool cfg80211_chandef_identical(struct chandef *a,struct chandef *b) {return a->freq==b->freq;}
struct mesh {int pre_value;};
struct ie {int pre_value;};
static void put_unaligned_le16(int value,int *pos) {*pos=value;}
static bool same(struct data *sdata, struct mesh *ifmsh, bool beacon,struct ie csa_ie,struct settings params) {
'''
code=head+branch+'\nreturn false;\n}\n'
code+='static int reason(struct data *sdata,bool block,bool action) {int result=0,*pos=&result; struct settings settings={.block_tx=block},*csa_settings=&settings; struct {struct settings settings;} value={.settings=settings},*csa=&value; if(action){'+reasons[0]+'}else{'+reasons[1]+'}return result;}\n'
code+=r'''
int main(void) {
 struct channel ch={0}; struct data d={.vif.bss_conf.chanreq.oper={.freq=5260,.chan=&ch}};
 struct mesh m={.pre_value=7}; struct ie ie={.pre_value=11}; struct settings s={.chandef.freq=5260};
 assert(same(&d,&m,true,ie,s) && m.pre_value==11);
 ie.pre_value=3;assert(same(&d,&m,true,ie,s) && m.pre_value==11);
 ie.pre_value=20;assert(same(&d,&m,false,ie,s) && m.pre_value==11);
 s.chandef.freq=5200;assert(!same(&d,&m,true,ie,s) && m.pre_value==11);
 for(int action=0;action<2;action++)for(int dfs=0;dfs<2;dfs++)for(int block=0;block<2;block++) {
  ch.flags=dfs?IEEE80211_CHAN_RADAR:0;
  assert(reason(&d,block,action)==(dfs&&block?WLAN_REASON_MESH_CHAN_REGULATORY:WLAN_REASON_MESH_CHAN));
 }
 puts("Mesh identical-channel precedence and regulatory block-TX reasons passed");
}
'''
with tempfile.TemporaryDirectory() as temp:
    c,exe=Path(temp)/'csa.c',Path(temp)/'csa'
    c.write_text(code)
    subprocess.run(['cc','-Wall','-Werror','-Wno-unused-variable','-fsanitize=address,undefined','-fno-pie','-no-pie',str(c),'-o',str(exe)],check=True)
    subprocess.run([str(exe)],check=True,env=dict(os.environ,ASAN_OPTIONS='detect_leaks=0'))
