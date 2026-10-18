#!/usr/bin/env python3
"""Bench-only deterministic DFS selection with real supplicants, CAC and CSA.

Installs temporary /run service overrides, collects a real candidate/fallback
observation and feeds that selection to the normal authenticated agreement.
No congestion, invented occupancy, forged clearance, manual CAC or forced CSA.
The bench override supplies scan requests and a deterministic preference only.
Requires all peers in the connected component to be listed. Existing configured
mesh frequencies must equal --fallback. Use --cleanup after controller loss.
"""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import time
import secrets

WRAPPER = r'''
import fcntl, importlib.util, json, os, pathlib, sys, time
sys.path.insert(0, '/usr/local/bin')
spec=importlib.util.spec_from_file_location('agreement', '/usr/local/bin/mesh-channel-agreement.py')
a=importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
import manet_bgobs as bg
from manet_rx_policy import reconcile_receive_paths
P=pathlib.Path
cfg=json.loads(P('/run/manet-dfs-auto/config.json').read_text())
target, fallback=cfg['target'], cfg['fallback']
original_score, original_status=a.Runtime.score, a.Runtime.status

def status(self, now):
 if now < cfg['expires']:
  # Same local request the normal manager sends after a sweep. Radio stability,
  # clock, cooldown, busy/recovery timers and peer readiness still apply.
  a.private_json_write(self.request_path, dict(round=now//a.protocol.ROUND_SECONDS))
 result=original_status(self, now)
 if now < cfg['expires']:
  result['dfs_candidates']=[f for f in result['dfs_candidates'] if f == target]
 return result

def score(self, view, *, candidates=False, fallback=False):
 if time.time() >= cfg['expires']:
  return original_score(self, view, candidates=candidates, fallback=fallback)
 if fallback:
  sample=getattr(self, '_bench_measurement', None)
  return ({'5':cfg['fallback']}, False) if sample and time.monotonic()-sample < 30 else None
 if not candidates or not all(s and s['dfs_capable'] and target in s['dfs_candidates'] for s in view.values()):
  return None
 if any(s['current'].get('5') != cfg['fallback'] for s in view.values()):
  return None
 iface=self.interfaces()['5'][0]
 observer=bg.observer_path(iface)
 if observer is None: return None
 lock=self.run_dir/('manet-bgobs-'+observer.parents[1].name+'.lock')
 with lock.open('a') as stream:
  fcntl.flock(stream, fcntl.LOCK_EX|fcntl.LOCK_NB)
  if reconcile_receive_paths(observer, True) != 'reserved': return None
 samples=bg.collect_candidates(observer, [target,cfg['fallback']], iface)
 observed={s['channel']:s for s in samples}
 if not all(f in observed for f in (target,cfg['fallback'])): return None
 print('BENCH measured selection '+json.dumps(samples),flush=True)
 self._bench_measurement=time.monotonic()
 return {'5':target}, False

a.Runtime.status=status
a.Runtime.score=score
sys.exit(a.main())
'''
INSTALL = r'''
import json,pathlib,subprocess,sys
P=pathlib.Path
cfg=json.load(sys.stdin)
base=P('/run/manet-dfs-auto')
assert not base.exists(), 'Bench override already present. Use --cleanup first.'
iface=P('/var/lib/mesh_5_if').read_text().strip()
pid=subprocess.check_output(['systemctl','show','--value','-p','MainPID','wpa_supplicant@'+iface],text=True).strip()
exe=P('/proc/'+pid+'/exe').resolve(strict=True)
assert exe == P('/usr/sbin/wpa_supplicant_manet'), 'Live MANET supplicant required'
# Execute the inode of the RUNNING binary, including an old deleted binary.
version=subprocess.check_output(['/proc/'+pid+'/exe','-v'],text=True)
assert '-manet-dfs3' in version, 'Restart into the rebuilt mesh radar/CAC supplicant'
identity=subprocess.check_output(['wpa_cli','-i',iface,'raw','MANET_DFS_STATUS'],text=True).strip()
assert identity in ('MANET_DFS_V3 owner=0','MANET_DFS_V3 owner=1'), identity
subprocess.run(['systemctl','is-active','--quiet','manet-dfs-watch.service'],check=True)
info=subprocess.check_output(['iw','dev',iface,'info'],text=True)
assert 'width: 20 MHz' in info and '('+str(cfg['fallback'])+' MHz)' in info, info
assert P('/run/initial_time_synced').exists(), 'Clock sync required'
base.mkdir(mode=0o700)
(base/'config.json').write_text(json.dumps(cfg))
(base/'mesh-channel-agreement.py').write_text(cfg['wrapper'])
for unit in ('mesh-channel-agreement','manet-dfs-watch'):
 d=P('/run/systemd/system')/(unit+'.service.d'); d.mkdir(parents=True,exist_ok=True)
 text='[Service]\nEnvironment=MANET_DFS_BENCH=1\n'
 if unit=='mesh-channel-agreement':
  text+='ExecStart=\nExecStart=/usr/bin/python3 -u /run/manet-dfs-auto/mesh-channel-agreement.py run\n'
 (d/'zz-manet-dfs-auto.conf').write_text(text)
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','restart','mesh-channel-agreement','manet-dfs-watch'],check=True)
print(json.dumps(dict(interface=iface,pid=pid,supplicant=version.strip(),info=info)))
'''
CLEANUP = r'''
import json,pathlib,shutil,subprocess,sys
P=pathlib.Path
request=json.load(sys.stdin)
config=P('/run/manet-dfs-auto/config.json')
if request.get('token') is not None:
 if not config.exists() or json.loads(config.read_text()).get('token') != request['token']:
  sys.exit(0)
for unit in ('mesh-channel-agreement','manet-dfs-watch'):
 (P('/run/systemd/system')/(unit+'.service.d')/'zz-manet-dfs-auto.conf').unlink(missing_ok=True)
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','restart','mesh-channel-agreement','manet-dfs-watch'],check=True)
shutil.rmtree('/run/manet-dfs-auto',ignore_errors=True)
'''


def remote(host, source, data=None):
    # One shell-quoted program argument, and structured data on stdin.
    return subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                           host, 'sudo python3 -c ' + shlex.quote(source)],
                          input=json.dumps(data) if data is not None else '',
                          text=True, capture_output=True, check=True, timeout=45).stdout


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('hosts', nargs='+')
    p.add_argument('--target', type=int, default=5260)
    p.add_argument('--fallback', type=int, default=5240)
    p.add_argument('--detector', default='cm4')
    p.add_argument('--timeout', type=int, default=1800)
    p.add_argument('--recovery-timeout', type=int, default=60)
    p.add_argument('--output', type=Path)
    p.add_argument('--cleanup', action='store_true')
    args = p.parse_args()
    if len(set(args.hosts)) not in (2, 3) or len(set(args.hosts)) != len(args.hosts):
        p.error('supply two or three distinct hosts')
    if args.cleanup:
        for host in args.hosts:
            print(host, remote(host, CLEANUP, {}))
        return
    if args.timeout <= 0 or args.recovery_timeout < 20:
        p.error('timeout must be positive and recovery-timeout at least 20 seconds')
    if args.output is None or args.detector not in args.hosts:
        p.error('--output and a detector among the supplied hosts are required')
    if args.target not in (5260, 5280, 5300, 5320):
        p.error('use a permitted 60-second-CAC target in 5260..5320')
    if args.fallback not in (5200, 5220, 5240, 5745, 5765, 5785, 5805, 5825):
        p.error('fallback must be a supported measured non-DFS primary')
    if args.output.exists():
        p.error('output directory already exists')
    installed = []
    evidence = {}
    token = secrets.token_hex(16)
    try:
        for host in args.hosts:
            installed.append(host)  # Cleanup partial installation too.
            evidence[host] = remote(host, INSTALL, dict(target=args.target,
                fallback=args.fallback, token=token, expires=int(time.time()) + args.timeout,
                wrapper=WRAPPER))
        subprocess.run(['python3', str(Path(__file__).with_name('bench-dfs.py')),
                        *args.hosts, '--detector', args.detector, '--timeout', str(args.timeout),
                        '--recovery-timeout', str(args.recovery_timeout),
                        '--output', str(args.output)], check=True)
    finally:
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output/'bench-preflight.json').write_text(json.dumps(evidence,indent=2)+'\n')
        failures = []
        for host in reversed(installed):
            try:
                remote(host, CLEANUP, dict(token=token))
            except subprocess.SubprocessError as exc:
                failures.append(host)
                print(f'{host}: cleanup failed: {exc}', flush=True)
        if failures:
            raise RuntimeError('Run --cleanup on: ' + ' '.join(failures))


if __name__ == '__main__':
    main()
