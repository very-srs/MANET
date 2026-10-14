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

from manet_config_io import atomic_write, load_kv_file


BEGIN = '# === BEGIN MESH HOSTS ==='
END = '# === END MESH HOSTS ==='
# Only hostnames and IPv4 fields with a 12-hex node ID enter this map.
FIELD = re.compile(r'NODE_([0-9a-fA-F]{12})_(HOSTNAME|IPV4_ADDRESS)=(.*)')
# DNS labels allow 1-63 alphanumeric/hyphen characters, no edge hyphens.
LABEL = re.compile(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?')


def entries(registry, network, reserved=()):
    """Return unambiguous single-label peers inside the configured network."""
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
    result = {}
    for node in nodes.values():
        name = node.get('HOSTNAME', '').lower()
        if (not LABEL.fullmatch(name) or name in reserved
                or name + '.local' in reserved):
            continue
        try:
            address = ipaddress.IPv4Address(node.get('IPV4_ADDRESS', ''))
        except ValueError:
            continue
        if (address not in network or address in
                (network.network_address, network.broadcast_address)):
            continue
        result.setdefault(name, set()).add(str(address))
    # Conflicting claims have no winner. Identical claims collapse to one row.
    return sorted((name, next(iter(addresses))) for name, addresses in result.items()
                  if len(addresses) == 1)


def render(hosts, rows):
    """Replace the marked hosts block while retaining surrounding bytes."""
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


def update(registry, hosts, lock, conf=None):
    """Lock, validate and atomically refresh hosts only when mappings change."""
    # Keep the lock inode in /run across invocations and service restarts.
    # Replacing /etc/hosts must not replace the lock inode.
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
        # Preserve undecodable bytes in operator-owned lines outside the block.
        text = original.decode('utf-8', errors='surrogateescape')
        # Validate the block and inspect only the operator-owned aliases.
        unmanaged = render(text, [])
        # The node's own registry row keeps its name on its br0 address. A
        # peer claiming the same label conflicts and both rows are omitted.
        reserved = {'localhost', 'localhost.local'}
        for line in unmanaged.splitlines():
            fields = line.split('#', 1)[0].split()
            reserved.update(name.lower().rstrip('.') for name in fields[1:])
        config = load_kv_file(conf or os.environ.get('MANET_MESH_CONF', '/etc/mesh.conf'))
        network = ipaddress.IPv4Network(config.get('ipv4_network', ''), strict=False)
        rows = entries(snapshot, network, reserved)
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
    """Run a hosts refresh, treating a competing lock holder as success."""
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
