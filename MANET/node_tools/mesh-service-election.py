#!/usr/bin/env python3
"""Rank the nodes that may host a mesh service (MediaMTX, Mumble).

    mesh-service-election.py SERVICE REGISTRY

Reads one snapshot of the node registry as data and prints one line:

    WINNER SCORE INCUMBENT

with "-" for an empty field (no eligible node, or no incumbent). Exits 1,
printing nothing, when the registry cannot be read, so a caller never takes
"could not decide" for "nobody is eligible".

Rules, the same for every service and on every node:
- A node is eligible only with a valid MAC, a finite non-negative
  MEAN_THROUGHPUT_MBPS, a NODE_STATE that is not SHUTTING_DOWN or STALE, and
  an OBSERVED_AT_UPTIME (this node's boot clock) at most 300 s old and not in
  the future. The age check is independent of NODE_STATE, so a registry that
  stopped being rebuilt cannot keep a departed node eligible.
- The incumbent is the eligible node advertising IS_<SERVICE>_SERVER='true'.
  When several do (partitions merging), the best metric, then the lowest MAC,
  is the incumbent; registry order never matters.
- Score is the metric, plus INCUMBENT_BIAS for the incumbent. Highest score
  wins; a tie goes to the lowest MAC.
"""

import math
import os
import re
import shlex
import sys

INCUMBENT_BIAS = 10
MAX_OBSERVED_AGE = 300
SERVER_FIELD = {'mediamtx': 'IS_MEDIAMTX_SERVER', 'mumble': 'IS_MUMBLE_SERVER'}
LINE = re.compile(r"NODE_([0-9a-fA-F]{12})_([A-Z0-9_]+)=(.*)")
MAC = re.compile(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}')


def parse(text):
    nodes = {}
    for line in text.splitlines():
        match = LINE.fullmatch(line.strip())
        if not match:
            continue
        key, field, raw = match.groups()
        try:
            value = shlex.split(raw)
        except ValueError:
            continue
        if len(value) == 1:
            nodes.setdefault(key.lower(), {})[field] = value[0]
    return nodes


def number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def eligible(node, uptime_now):
    mac = node.get('MAC_ADDRESS', '').lower()
    metric = number(node.get('MEAN_THROUGHPUT_MBPS'))
    observed = number(node.get('OBSERVED_AT_UPTIME'))
    if not MAC.fullmatch(mac):
        return None, 'invalid MAC'
    if metric is None or metric < 0:
        return None, 'invalid metric'
    if node.get('NODE_STATE') in ('SHUTTING_DOWN', 'STALE'):
        return None, node['NODE_STATE'].lower().replace('_', ' ')
    if observed is None or observed < 0 or observed > uptime_now:
        return None, 'invalid observation time'
    if uptime_now - observed > MAX_OBSERVED_AGE:
        return None, f'not heard for {uptime_now - observed:.0f} s'
    return (mac, metric), None


def elect(service, text, uptime_now, report=lambda message: None):
    if not (isinstance(uptime_now, (int, float)) and math.isfinite(uptime_now) and uptime_now >= 0):
        raise ValueError(f'invalid local uptime {uptime_now!r}')
    field = SERVER_FIELD[service]
    candidates = {}
    servers = set()
    for key, node in sorted(parse(text).items()):
        entry, reason = eligible(node, uptime_now)
        if entry is None:
            report(f'Skipping {node.get("MAC_ADDRESS") or key}: {reason}')
            continue
        mac, metric = entry
        candidates[mac] = metric
        if node.get(field) == 'true':
            servers.add(mac)
    if not candidates:
        return None, None, None
    incumbent = min(servers, key=lambda mac: (-candidates[mac], mac)) if servers else None
    score = {mac: metric + (INCUMBENT_BIAS if mac == incumbent else 0)
             for mac, metric in candidates.items()}
    winner = min(score, key=lambda mac: (-score[mac], mac))
    return winner, score[winner], incumbent


def main(argv):
    if len(argv) != 2 or argv[0] not in SERVER_FIELD:
        print('usage: mesh-service-election.py {mediamtx|mumble} REGISTRY', file=sys.stderr)
        return 2
    try:
        with open(argv[1]) as registry:
            text = registry.read()
        with open(os.environ.get('MESH_UPTIME_FILE', '/proc/uptime')) as uptime:
            uptime_now = float(uptime.read().split()[0])
    except (OSError, ValueError, IndexError) as error:
        print(f'Cannot read election inputs: {error}', file=sys.stderr)
        return 1
    try:
        winner, best, incumbent = elect(argv[0], text, uptime_now,
                                        lambda message: print(message, file=sys.stderr))
    except ValueError as error:
        print(f'Cannot read election inputs: {error}', file=sys.stderr)
        return 1
    print(f'{winner or "-"} {"-" if best is None else f"{best:g}"} {incumbent or "-"}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
