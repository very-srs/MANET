#!/usr/bin/env python3
"""Elect a tourguide using reachable originators mapped to registry identities."""

import argparse
import json
from pathlib import Path
import re
import shlex
import sys


MAC = re.compile(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}')
# Read only identity, tourguide, service and interface fields from the registry.
FIELD = re.compile(r'NODE_([0-9a-f]{12})_(MAC_ADDRESSES|LAST_TOURGUIDE_TIMESTAMP|IS_MEDIAMTX_SERVER|IS_MUMBLE_SERVER|INTERFACES_JSON)=(.*)')


def elect(own, peers, registry, band, excluded=()):
    """Choose a reachable node on the scheduled band with service avoidance."""
    own = own.lower()
    peers = {mac.lower() for mac in peers}
    excluded = {mac.lower() for mac in excluded}
    if not all(MAC.fullmatch(mac) for mac in peers | {own} | excluded):
        raise ValueError('invalid node MAC')
    if not peers:
        return None if own in excluded else own

    nodes = {}
    aliases = {}
    for line in registry.splitlines():
        match = FIELD.fullmatch(line)
        if not match:
            continue
        key, field, raw = match.groups()
        # Restore the canonical MAC from the registry's 12-hex publisher key.
        mac = ':'.join(key[i:i + 2] for i in range(0, 12, 2))
        # Decode quoted data without sourcing network-derived assignments.
        value = shlex.split(raw)
        if len(value) != 1:
            raise ValueError('invalid registry value')
        nodes.setdefault(mac, {})[field] = value[0]
    for mac, fields in nodes.items():
        for alias in [mac, *fields.get('MAC_ADDRESSES', '').lower().split(',')]:
            if not alias:
                continue
            if not MAC.fullmatch(alias) or aliases.get(alias, mac) != mac:
                raise ValueError('invalid or ambiguous registry MAC')
            aliases[alias] = mac

    # Interface MACs are not Alfred record keys. Wait for identity discovery
    # so the elected node can recognize its own address.
    if own not in nodes or not peers.issubset(aliases):
        raise ValueError('reachable peers are missing registry identities')
    candidates = {own} | {aliases[peer] for peer in peers}
    def can_hop(mac):
        """Require an UP mesh interface with numeric MHz in this band."""
        interfaces = json.loads(nodes[mac].get('INTERFACES_JSON', '[]'))
        if not isinstance(interfaces, list):
            raise ValueError('invalid interface report')
        low, high = (2400, 2500) if band == '2.4' else (5000, 5900)
        return any(isinstance(iface, dict) and iface.get('role') == 'mesh'
                   and iface.get('state') == 'UP'
                   and isinstance(iface.get('freq_mhz'), (int, float))
                   and low <= iface['freq_mhz'] < high for iface in interfaces)

    candidates = {mac for mac in candidates if can_hop(mac)}
    if not candidates:
        raise ValueError('no reachable candidate advertises the scheduled band')
    candidates -= excluded
    if not candidates:
        return None  # Every eligible radio is relying on working HaLow.
    # Prefer a node that does not host MediaMTX or Mumble during the hop.
    eligible = [mac for mac in candidates
                if not any(nodes[mac].get(flag) == 'true'
                           for flag in ('IS_MEDIAMTX_SERVER', 'IS_MUMBLE_SERVER'))]
    # Healing must remain possible when every candidate hosts a service.
    eligible = eligible or list(candidates)

    def rank(mac):
        """Rank by oldest tourguide timestamp, then lowest canonical MAC."""
        timestamp = nodes[mac].get('LAST_TOURGUIDE_TIMESTAMP', '0')
        if not re.fullmatch(r'[0-9]+', timestamp):
            raise ValueError('invalid tourguide timestamp')
        return int(timestamp), mac

    return min(eligible, key=rank)


def main():
    """Read reachable peers and registry state, then print any elected node."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self', dest='own', required=True)
    parser.add_argument('--peers', required=True)
    parser.add_argument('--registry', required=True, type=Path)
    parser.add_argument('--band', choices=('2.4', '5'), required=True)
    parser.add_argument('--exclude', default='', help='Canonical nodes advertising working HaLow')
    args = parser.parse_args()
    try:
        # A solo node can heal even before Alfred has built its registry.
        registry = args.registry.read_text() if args.peers.split() else ''
        winner = elect(args.own, args.peers.split(), registry, args.band, args.exclude.split())
    except (OSError, ValueError) as exc:
        print(f'Cannot elect tourguide: {exc}', file=sys.stderr)
        return 1
    if winner is not None:
        print(winner)
    return 0


if __name__ == '__main__':
    sys.exit(main())
