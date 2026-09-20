#!/usr/bin/env python3
"""Refresh allocation data and defer IPv4 until Alfred has had time to sync.

This is polled by mesh-ip-manager, never slept inside: node-manager must keep
publishing identity and telemetry over IPv6 while every node is unallocated.
"""

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time


# Alfred's default, also explicit in radio-setup.sh's alfred.service. Allow one
# period when all visible peers have records, at most two when records are
# missing. This bounds discovery delay, not propagation under packet loss:
# late claims still need the allocator's MAC conflict resolution.
ALFRED_SYNC_SECONDS = 10
SETTLE_SECONDS = ALFRED_SYNC_SECONDS
DISCOVERY_LIMIT_SECONDS = 2 * ALFRED_SYNC_SECONDS
MAC_RE = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}")


def command(*args, timeout=5):
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          timeout=timeout).stdout


def ipv6_ready(addresses):
    for interface in addresses:
        for address in interface.get('addr_info', []):
            flags = address.get('flags', [])
            if (address.get('family') == 'inet6'
                    and address.get('scope') == 'link'
                    and not any(address.get(flag) or flag in flags
                                for flag in ('tentative', 'dadfailed'))):
                return True
    return False


def alfred_ready(status):
    return ('- mode: primary' in status
            and re.search(r'^- interface: br0\n\s+- status: active$',
                          status, re.MULTILINE) is not None)


def originators(rows):
    # JSON avoids the shifting '*' column in batctl's human-readable output.
    if not isinstance(rows, list):
        raise ValueError('invalid BATMAN originator response')
    peers = set()
    for row in rows:
        mac = row['orig_address'].lower()
        if not MAC_RE.fullmatch(mac):
            raise ValueError('invalid BATMAN originator MAC')
        peers.add(mac)
    return peers


def registry_macs(registry):
    """Only count nodes with both telemetry and a decoded/cached identity.

    The registry is built by joining those records. A telemetry-only entry has
    its Alfred MAC but no hostname; it cannot tell us whether a chunk is taken.
    Never source these network-derived assignments into a shell.
    """
    nodes = {}
    for line in registry.splitlines():
        match = re.fullmatch(r'NODE_([0-9a-f]{12})_(HOSTNAME|MAC_ADDRESSES)=(.*)', line)
        if match:
            key, field, value = match.groups()
            parts = shlex.split(value)
            if len(parts) != 1:
                raise ValueError('invalid registry assignment')
            nodes.setdefault(key, {})[field] = parts[0]
    covered = set()
    for fields in nodes.values():
        if fields.get('HOSTNAME'):
            covered.update(mac.lower() for mac in fields.get('MAC_ADDRESSES', '').split(',')
                           if MAC_RE.fullmatch(mac.lower()))
    return covered


def advance(state, now, peers, covered):
    """One bounded observation window; membership changes never restart it."""
    peers = sorted(peers)
    start = state.get('since', now)
    if not isinstance(start, (int, float)) or start > now:
        start = now
    next_state = {'since': start, 'peers': peers}
    missing = set(peers) - covered
    elapsed = now - start
    remaining = SETTLE_SECONDS - elapsed
    if remaining > 0:
        return next_state, False, f'waiting for Alfred propagation ({remaining:.0f}s remaining)'
    if missing:
        remaining = DISCOVERY_LIMIT_SECONDS - elapsed
        peer_list = ', '.join(sorted(missing))
        if remaining > 0:
            return next_state, False, (f'waiting for Alfred identity/telemetry from {peer_list} '
                                       f'({remaining:.0f}s until discovery deadline)')
        next_state['complete'] = True
        return next_state, True, ('IPv4 discovery deadline reached; proceeding with available claims; '
                                  f'missing Alfred records from {peer_list}')
    next_state['complete'] = True
    return next_state, True, f'IPv4 discovery complete ({len(peers)} BATMAN originators)'


def read_state(path):
    try:
        state = json.loads(path.read_text())
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state) + '\n')
    temporary.replace(path)


def check(state, registry_path, builder, own_mac_path):
    # Also refresh after bootstrap: ACS used to reuse a snapshot for 180 s,
    # concealing newly published claims and delaying collision resolution.
    command(builder, timeout=60)
    if state.get('complete'):
        return state, True, ''
    if not ipv6_ready(json.loads(command('ip', '-j', '-6', 'addr', 'show', 'dev', 'br0'))):
        return state, False, 'waiting for usable br0 link-local IPv6'
    if not alfred_ready(command('alfred', '-S')):
        return state, False, 'waiting for an active Alfred primary on br0'
    peers = originators(json.loads(command('batctl', 'meshif', 'bat0', 'originators_json')))
    covered = registry_macs(registry_path.read_text())
    own_mac = own_mac_path.read_text().strip().lower()
    if not MAC_RE.fullmatch(own_mac) or own_mac not in covered:
        return state, False, 'waiting for our initial Alfred identity and telemetry publication'
    peers.discard(own_mac)
    return advance(state, time.monotonic(), peers, covered)


def main():
    # All discovery state is volatile. Saved IPv4 preferences in /etc do not
    # bypass this check after reboot; manager restarts within a boot can reuse it.
    state_path = Path(os.environ.get('MESH_IP_STARTUP_STATE', '/var/run/mesh-ip-startup.json'))
    registry_path = Path(os.environ.get('MESH_REGISTRY_FILE', '/var/run/mesh_node_registry'))
    builder = os.environ.get('MESH_REGISTRY_BUILDER', '/usr/local/bin/mesh-registry-builder.sh')
    own_mac_path = Path('/sys/class/net/br0/address')
    state = read_state(state_path)
    try:
        state, ready, message = check(state, registry_path, builder, own_mac_path)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        # Failed local operations still block this pass, even past the deadline.
        # Keep the original start time so recovery cannot restart the wait.
        ready, message = False, f'waiting for mesh discovery: {error}'
    save_state(state_path, state)
    if message:
        print('IP-MGR: ' + message, file=sys.stderr)
    return 0 if ready else 1


if __name__ == '__main__':
    sys.exit(main())
