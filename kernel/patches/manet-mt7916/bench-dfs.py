#!/usr/bin/env python3
"""Observe automatic two- or three-node DFS preparation, then trigger MAIN radar.

Preinstall the matched kernel/modules and runtime, enable ACS with a 5 GHz
HT20 mesh on every node, and provide RF conditions that make a DFS candidate
win. Rock 3A bench services need MANET_DFS_BENCH=1; shipping defaults stay off.
This script does not invent RF reports or force a clearance. It records actual
preparation, CAC, agreed CSA and fallback. It writes radar_trigger only after
all nodes have transferred the SAME committed plan to their main detectors.
"""
import argparse
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'MANET/node_tools'))
import manet_acs_agreement as protocol

MONITOR = r'''
import atexit, json, pathlib, re, subprocess, threading, time
P=pathlib.Path
lock=threading.Lock()
def emit(row):
 with lock: print(json.dumps(row),flush=True)
def read(path):
 try: return json.loads(P(path).read_text())
 except (OSError,ValueError): return {}
iface=P('/var/lib/mesh_5_if').read_text().strip()
phy=P('/sys/class/net/'+iface+'/phy80211/name').read_text().strip()
hold='/sys/kernel/debug/ieee80211/'+phy+'/background_hold'
def events():
 with subprocess.Popen(['iw','event','-t'],stdout=subprocess.PIPE,text=True) as child:
  atexit.register(child.terminate)
  for line in child.stdout:
   emit(dict(kind='nl80211',at=time.clock_gettime(time.CLOCK_BOOTTIME),event=line.strip()))
threading.Thread(target=events,daemon=True).start()
def journal():
 with subprocess.Popen(['journalctl','-f','-n','0','-o','short-precise',
    '-u','wpa_supplicant@'+iface,'-u','mesh-channel-agreement','-u','manet-dfs-watch'],
    stdout=subprocess.PIPE,text=True) as child:
  atexit.register(child.terminate)
  for line in child.stdout:
   emit(dict(kind='journal',at=time.clock_gettime(time.CLOCK_BOOTTIME),event=line.strip()))
threading.Thread(target=journal,daemon=True).start()
while True:
 state=read('/var/lib/manet-acs/agreement.json')
 output=subprocess.run(['iw','dev',iface,'info'],capture_output=True,text=True,timeout=2).stdout
 freq=re.search(r'channel.*\((\d+) MHz\)',output)
 stations=subprocess.run(['iw','dev',iface,'station','dump'],capture_output=True,text=True,timeout=2).stdout
 try: kernel=json.loads(P(hold).read_text()); kernel_error=None
 except (OSError,ValueError) as exc: kernel={}; kernel_error=str(exc)
 emit(dict(kind='sample',at=time.clock_gettime(time.CLOCK_BOOTTIME),iface=iface,phy=phy,
           own=P('/sys/class/net/br0/address').read_text().strip(),
           frequency=int(freq[1]) if freq else None,kernel=kernel,kernel_error=kernel_error,
           stations=stations,established=stations.count('ESTAB'),status=read('/run/manet-acs-status.json'),
           preparation=state.get('dfs_prepare'),protocol=state.get('protocol'),
           destination=state.get('destination'),cooldown=state.get('dfs_cooldown_until'),
           csa=read('/run/manet-dfs-csa.json')))
 # Debugfs hold reads acquire wiphy/device locks. Keep bench polling sparse;
 # iw event remains continuous and the kernel/CSA records carry event times.
 time.sleep(5)
'''
TRIGGER = r'''
import json,pathlib,time
P=pathlib.Path
iface=P('/var/lib/mesh_5_if').read_text().strip()
phy=P('/sys/class/net/'+iface+'/phy80211/name').read_text().strip()
base=P('/sys/kernel/debug/ieee80211')/phy
hold=json.loads((base/'background_hold').read_text())
assert hold.get('main_active') is True, 'No transferred main detector'
before=time.clock_gettime(time.CLOCK_BOOTTIME)
(base/'mt76/radar_trigger').write_text('1\n')
after=time.clock_gettime(time.CLOCK_BOOTTIME)
print(json.dumps(dict(trigger_before=before,trigger_after=after,phy=phy,value=1)))
'''


def recovery_pending(hosts, latest, fallback, expected_peers, now, trigger):
    """Explain missing post-trigger evidence, including partial peer recovery."""
    pending = []
    for host in hosts:
        sample = latest.get(host, {})
        received = sample.get('received', float('-inf'))
        if received < trigger or now - received > 15:
            pending.append(f'{host}: no fresh post-radar sample')
        elif sample.get('frequency') != fallback:
            pending.append(f'{host}: frequency={sample.get("frequency")}, expected {fallback}')
        elif sample.get('established', 0) < expected_peers[host]:
            pending.append(f'{host}: peering={sample.get("established", 0)}/{expected_peers[host]} established')
    return pending


def check_health(host, row):
    if row['kind'] == 'journal' and any(marker in row['event'] for marker in (
            'AP-DISABLED', '->DISABLED', 'interface state ENABLED->DFS',
            'MESH-GROUP-REMOVED', 'CTRL-EVENT-DISCONNECTED')):
        raise RuntimeError(f'{host}: mesh BSS interrupted during DFS bench: {row["event"]}')


def check_recovery_deadline(hosts, latest, fallback, expected_peers, now, trigger,
                            timeout, summary):
    if trigger is None or now - trigger < timeout:
        return
    pending = recovery_pending(hosts, latest, fallback, expected_peers, now, trigger)
    if 'trigger_to_block_tx_ack_seconds' not in summary:
        pending.append('no matching block-TX CSA acknowledgement')
    if summary.get('radar_cause') != 'main-radar':
        pending.append('no matching main-radar latch')
    raise TimeoutError(f'Radar fallback/peering did not recover within {timeout}s: ' +
                       '; '.join(pending or ['recovery did not remain stable for 10s']))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('hosts',nargs='*',default=['cm4','r3a','r3a.2'])
    parser.add_argument('--detector',default='cm4')
    parser.add_argument('--timeout',type=int,default=1800)
    parser.add_argument('--recovery-timeout',type=int,default=60)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--observe-only',action='store_true')
    args=parser.parse_args()
    if len(args.hosts) not in (2,3) or len(set(args.hosts))!=len(args.hosts) or args.detector not in args.hosts:
        parser.error('supply two or three distinct hosts and select one as detector')
    if args.timeout <= 0 or args.recovery_timeout < 20:
        parser.error('timeout must be positive and recovery-timeout at least 20 seconds')
    args.output.mkdir(parents=True,exist_ok=False)
    messages=queue.Queue()
    children=[]
    def collect(host):
        child=subprocess.Popen(['ssh','-o','BatchMode=yes',host,'sudo','python3','-u','-'],
                               stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
        children.append(child)
        child.stdin.write(MONITOR); child.stdin.close()
        for line in child.stdout:
            try: messages.put((host,time.monotonic(),json.loads(line)))
            except ValueError: pass
        messages.put((host,time.monotonic(),dict(kind='error',exit=child.wait())))
    streams={h:(args.output/(h+'.jsonl')).open('w') for h in args.hosts}
    for host in args.hosts:
        threading.Thread(target=collect,args=(host,),daemon=True).start()
    latest={}; started=time.monotonic(); trigger=None; fallback=None; landed={}
    expected_peers={}; healthy_since=None
    summary={'sample_interval_seconds': 5, 'recovery_timeout_seconds': args.recovery_timeout}
    try:
        while time.monotonic()-started<args.timeout:
            check_recovery_deadline(args.hosts, latest, fallback, expected_peers,
                                    time.monotonic(), trigger, args.recovery_timeout, summary)
            try: host,received,row=messages.get(timeout=1)
            except queue.Empty: continue
            streams[host].write(json.dumps(dict(received=received,**row))+'\n')
            streams[host].flush()
            if row['kind']=='error': raise RuntimeError(f'{host} monitor exited: {row}')
            check_health(host, row)
            if row['kind']!='sample': continue
            latest[host]=dict(row,received=received)
            if trigger is None and len(latest)==len(args.hosts):
                plans=[]
                for h,x in latest.items():
                    destination=x.get('destination') or {}
                    plan=destination.get('plan',{})
                    k=x['kernel']
                    if (time.monotonic()-x['received']>15 or not x.get('established') or
                        not k.get('main_active') or 'dfs' not in plan or
                        not protocol.valid_commit(destination.get('commit'),plan) or
                        x['frequency']!=plan['channels']['5'] or
                        [k.get('main_epoch'),k.get('main_generation')] != plan['dfs']['holds'].get(x['own'])):
                        break
                    plans.append(plan)
                if len(plans)==len(args.hosts) and all(p==plans[0] for p in plans):
                    fallback=plans[0]['dfs']['fallback']['5']
                    summary['plan']=plans[0]
                    if args.observe_only: break
                    expected_peers={h:x['established'] for h,x in latest.items()}
                    summary['expected_peers']=expected_peers
                    trigger=time.monotonic()
                    result=subprocess.run(['ssh','-o','BatchMode=yes',args.detector,
                                           'sudo','python3','-'],input=TRIGGER,
                                          check=True,capture_output=True,text=True,timeout=10)
                    summary['trigger']=json.loads(result.stdout)
                    summary['trigger_controller']=trigger
                    print('MAIN radar triggered with value 1; awaiting block-TX CSA and all fallbacks',flush=True)
            if trigger is not None and received >= trigger:
                if row['frequency']==fallback and row.get('established',0)>=expected_peers[host]:
                    landed.setdefault(host,received-trigger)
                if host==args.detector:
                    at=summary['trigger']['trigger_before']
                    csa=row.get('csa') or {}
                    if csa.get('requested_boottime',0)>=at:
                        summary['trigger_to_block_tx_request_seconds']=csa['requested_boottime']-at
                    if csa.get('acknowledged_boottime',0)>=at:
                        summary['trigger_to_block_tx_ack_seconds']=csa['acknowledged_boottime']-at
                    radar=row['kernel']
                    if radar.get('radar_boottime_us',0)/1e6>=at:
                        summary['trigger_to_radar_latch_seconds']=radar['radar_boottime_us']/1e6-at
                        summary['radar_cause']=radar.get('radar_cause')
                healthy = not recovery_pending(args.hosts, latest, fallback, expected_peers,
                                                time.monotonic(), trigger)
                if not healthy:
                    healthy_since=None
                elif healthy_since is None:
                    healthy_since=time.monotonic()
                if (healthy_since is not None and time.monotonic()-healthy_since>=10
                        and summary.get('radar_cause')=='main-radar'
                        and 'trigger_to_block_tx_ack_seconds' in summary):
                    summary['fallback_observed_seconds']=landed
                    summary['fallback_controller_upper_bound_seconds']=max(landed.values())
                    break
        else:
            raise TimeoutError('No complete automatic DFS plan/fallback within deadline; inspect captures')
        if trigger is not None and 'trigger_to_block_tx_ack_seconds' not in summary:
            raise RuntimeError('Fallback observed but no matching block-TX CSA acknowledgement')
        summary['result']='passed'
        (args.output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
        print(json.dumps(summary,indent=2))
    except Exception as exc:
        summary.update(result='failed', reason=str(exc))
        (args.output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
        raise
    finally:
        for child in children:
            child.terminate()
        for stream in streams.values(): stream.close()


if __name__=='__main__':
    main()
