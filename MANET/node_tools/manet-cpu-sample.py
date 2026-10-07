#!/usr/bin/env python3
"""One-shot CPU seconds/minute measurement, including a service's children."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time


def snapshot(unit, root=Path('/')):
    output = subprocess.check_output(
        ['systemctl', 'show', unit, '-p', 'ControlGroup', '-p', 'InvocationID',
         '-p', 'ActiveState'], text=True)
    props = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
    group = props.get('ControlGroup', '')
    if props.get('ActiveState') != 'active' or not props.get('InvocationID') or not group.startswith('/') or group == '/':
        raise ValueError('service must be active with its own cgroup')
    counters = dict(line.split() for line in (root / 'sys/fs/cgroup' / group.lstrip('/') / 'cpu.stat').read_text().splitlines())
    # cgroup v2 accounts for nft/ip children as well as the Python process.
    cpu = int(counters['usage_usec']) / 1_000_000
    fields = (root / 'proc/stat').read_text().splitlines()[0].split()
    if fields[0] != 'cpu' or len(fields) < 9:
        raise ValueError('missing aggregate CPU counters')
    ticks = list(map(int, fields[1:9]))  # guest fields already included in user/nice
    busy = (sum(ticks) - ticks[3] - ticks[4]) / os.sysconf('SC_CLK_TCK')
    return {'invocation': props['InvocationID'], 'cgroup': group, 'cpu': cpu,
            'system_busy': busy, 'at': time.monotonic()}


def result(before, after, label):
    elapsed = after['at'] - before['at']
    delta = after['cpu'] - before['cpu']
    busy = after['system_busy'] - before['system_busy']
    if (elapsed <= 0 or delta < 0 or busy < 0 or
            any(before[k] != after[k] for k in ('invocation', 'cgroup'))):
        raise ValueError('service restarted or CPU counters reset; repeat the sample')
    return {'label': label, 'elapsed_s': round(elapsed, 3),
            'service_cpu_s_per_min': round(delta * 60 / elapsed, 4),
            'service_percent_one_core': round(delta * 100 / elapsed, 3),
            'system_busy_cpu_s_per_min': round(busy * 60 / elapsed, 4)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unit', default='manet-atak.service')
    parser.add_argument('--seconds', type=float, default=60)
    parser.add_argument('--label', default='sample')
    args = parser.parse_args()
    if not 1 <= args.seconds <= 3600:
        parser.error('--seconds must be between 1 and 3600')
    before = snapshot(args.unit)
    time.sleep(args.seconds)  # two snapshots, no periodic sampler or daemon
    print(json.dumps(result(before, snapshot(args.unit), args.label), sort_keys=True))


if __name__ == '__main__':
    main()
