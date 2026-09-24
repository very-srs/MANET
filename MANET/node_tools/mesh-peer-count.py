#!/usr/bin/env python3
"""Count distinct BATMAN originators; an unavailable table is never zero peers."""

import argparse
import json
import re
import subprocess
import sys


def peer_addresses(batctl, timeout=5):
    result = subprocess.run(
        [batctl, 'meshif', 'bat0', 'originators_json'],
        check=True, capture_output=True, text=True, timeout=timeout,
    )
    rows = json.loads(result.stdout)
    if not isinstance(rows, list):
        raise ValueError('originators response is not a list')
    peers = set()
    for row in rows:
        mac = row.get('orig_address') if isinstance(row, dict) else None
        if not isinstance(mac, str) or not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', mac):
            raise ValueError('invalid originator address')
        peers.add(mac.lower())
    return sorted(peers)


def peer_count(batctl, timeout=5):
    return len(peer_addresses(batctl, timeout))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batctl', default='/usr/sbin/batctl')
    parser.add_argument('--list', action='store_true', help='print originator MACs instead of their count')
    args = parser.parse_args()
    try:
        peers = peer_addresses(args.batctl)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f'Cannot read BATMAN peers: {exc}', file=sys.stderr)
        return 1
    if args.list:
        for mac in peers:
            print(mac)
    else:
        print(len(peers))
    return 0


if __name__ == '__main__':
    sys.exit(main())
