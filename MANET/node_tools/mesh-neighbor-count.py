#!/usr/bin/env python3
"""Count direct BATMAN neighbor nodes, merging radio addresses via the registry.

Exit 0: exact count on stdout; 1: neighbor query unavailable; 3: connected but
node identities are incomplete/ambiguous, so no exact count is available yet.
"""

import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys


MAC = re.compile(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}')
IDENTITY = re.compile(r'NODE_([0-9a-fA-F]{12})_MAC_ADDRESSES=(.*)')


def neighbor_addresses(batctl, timeout=5):
    result = subprocess.run(
        [batctl, 'meshif', 'bat0', 'neighbors_json'], check=True,
        capture_output=True, text=True, timeout=timeout,
    )
    rows = json.loads(result.stdout)
    if not isinstance(rows, list):
        raise ValueError('neighbors response is not a list')
    neighbors = set()
    for row in rows:
        address = row.get('neigh_address') if isinstance(row, dict) else None
        if not isinstance(address, str) or not MAC.fullmatch(address.lower()):
            raise ValueError('invalid neighbor address')
        neighbors.add(address.lower())
    return neighbors


def count_nodes(neighbors, registry):
    # One radio address is unambiguously one neighbor even during cold discovery.
    if len(neighbors) <= 1:
        return len(neighbors)
    owners = {}
    for line in registry.splitlines():
        match = IDENTITY.fullmatch(line)
        if not match:
            continue
        key = match[1].lower()
        node = ':'.join(key[i:i + 2] for i in range(0, 12, 2))
        values = shlex.split(match[2])
        if len(values) != 1:
            raise ValueError('invalid registry identity')
        for address in [node, *values[0].lower().split(',')]:
            if not address:
                continue
            if not MAC.fullmatch(address):
                raise ValueError('invalid registry MAC')
            owners.setdefault(address, set()).add(node)
    # Never guess that two unmapped radio addresses are two different nodes.
    if any(len(owners.get(address, ())) != 1 for address in neighbors):
        return None
    return len({next(iter(owners[address])) for address in neighbors})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batctl', default='/usr/sbin/batctl')
    parser.add_argument('--registry', type=Path, default=Path('/var/run/mesh_node_registry'))
    args = parser.parse_args()
    try:
        neighbors = neighbor_addresses(args.batctl)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f'Cannot read BATMAN neighbors: {exc}', file=sys.stderr)
        return 1
    try:
        registry = args.registry.read_text() if len(neighbors) > 1 else ''
        count = count_nodes(neighbors, registry)
    except (OSError, ValueError):
        count = None
    if count is None:
        print('Direct neighbors present; node identities not yet resolved', file=sys.stderr)
        return 3
    print(count)
    return 0


if __name__ == '__main__':
    sys.exit(main())
