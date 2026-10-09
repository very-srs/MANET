#!/usr/bin/env python3
"""Refresh the managed hosts block only when peer mappings change."""

import argparse
import fcntl
import ipaddress
import os
from pathlib import Path
import re
import shlex
import sys

from manet_config_io import atomic_write


BEGIN = '# === BEGIN MESH HOSTS ==='
END = '# === END MESH HOSTS ==='
FIELD = re.compile(r'NODE_([0-9a-fA-F]{12})_(HOSTNAME|IPV4_ADDRESS)=(.*)')
LABEL = re.compile(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?')


def entries(registry):
    nodes = {}
    for line in registry.splitlines():
        match = FIELD.fullmatch(line)
        if match:
            node, key, value = match.groups()
            # The registry contains quoted assignments, never executable code.
            parts = shlex.split(value)
            if len(parts) != 1:
                raise ValueError('Malformed registry host assignment')
            nodes.setdefault(node.lower(), {})[key] = parts[0]
    result = set()
    for node in nodes.values():
        name = node.get('HOSTNAME', '')
        if (not name or len(name) > 253
                or not all(LABEL.fullmatch(p) for p in name.split('.'))):
            continue
        try:
            address = ipaddress.IPv4Address(node.get('IPV4_ADDRESS', ''))
        except ValueError:
            continue
        result.add((name, str(address)))
    return sorted(result)


def render(hosts, rows):
    block = [BEGIN]
    for name, address in rows:
        aliases = name
        if not name.endswith('.local'):
            aliases += ' ' + name + '.local'
        block.append(f'{address}    {aliases}')
    block.append(END)
    block = '\n'.join(block) + '\n'
    lines = hosts.splitlines(keepends=True)
    begins = [i for i, line in enumerate(lines)
              if line.rstrip('\r\n') == BEGIN]
    ends = [i for i, line in enumerate(lines) if line.rstrip('\r\n') == END]
    if begins or ends:
        if len(begins) != 1 or len(ends) != 1 or begins[0] >= ends[0]:
            raise ValueError('Malformed mesh hosts markers; hosts left intact')
        return ''.join(lines[:begins[0]]) + block + ''.join(lines[ends[0] + 1:])
    if hosts and not hosts.endswith('\n'):
        hosts += '\n'
    if hosts and not hosts.endswith('\n\n'):
        hosts += '\n'
    return hosts + block


def update(registry, hosts, lock):
    # /run owns this lock across invocations; replacing /etc/hosts must not
    # replace the lock inode too. Do not tie it to one service's lifetime.
    with lock.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            snapshot = registry.read_text()
        except FileNotFoundError:
            return False
        if not snapshot.strip():
            return False
        if hosts.is_symlink():
            raise ValueError('Hosts file is a symlink; leaving it intact')
        original = hosts.read_bytes()
        metadata = hosts.stat()
        rows = entries(snapshot)
        text = original.decode('utf-8', errors='surrogateescape')
        rendered = render(text, rows)
        contents = rendered.encode('utf-8', errors='surrogateescape')
        if contents == original:
            return False
        # A same-directory temporary and fsynced rename retain a complete
        # hosts file across interruption, along with its ownership and mode.
        atomic_write(hosts, contents, metadata)
        print(f'mesh-hosts-update.sh: Updated {len(rows)} mesh host entries',
              file=sys.stderr)
        return True


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        update(
            Path(os.environ.get('MESH_REGISTRY_FILE',
                                '/var/run/mesh_node_registry')),
            Path(os.environ.get('MANET_HOSTS_FILE', '/etc/hosts')),
            Path(os.environ.get('MANET_HOSTS_LOCK', '/run/manet-hosts.lock')),
        )
    except BlockingIOError:
        return 0  # Another refresh owns the snapshot and hosts file.
    except (OSError, ValueError) as error:
        print(f'mesh-hosts-update.sh: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
