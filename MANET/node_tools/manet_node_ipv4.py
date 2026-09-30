#!/usr/bin/env python3
"""Select this node's assigned IPv4 address, never a service VIP or EUD alias."""

import ipaddress
import json
from pathlib import Path
import subprocess
import sys


def read_values(path):
    values = {}
    try:
        for line in Path(path).read_text().splitlines():
            key, sep, value = line.partition('=')
            if sep and not key.lstrip().startswith('#'):
                values[key.strip()] = value.strip().strip('\"\'')
    except FileNotFoundError:
        pass
    return values


def primary_ipv4(state, config, addresses):
    """A remembered allocation counts only while its primary is on the bridge."""
    try:
        if not state.get('PERSISTENT_CHUNK', '').isdigit():
            return ''
        address = ipaddress.IPv4Address(state['PERSISTENT_IPV4'])
        network = ipaddress.IPv4Network(state['PERSISTENT_NETWORK'], strict=False)
        if network != ipaddress.IPv4Network(config['ipv4_network'], strict=False):
            return ''
        # The first five host addresses are service VIPs, not node allocations.
        if not (int(network.network_address) + 5 < int(address) < int(network.broadcast_address)):
            return ''
        return str(address) if str(address) in addresses else ''
    except (KeyError, ValueError, TypeError):
        return ''


def current_ipv4(iface='br0'):
    state = read_values('/etc/mesh_ipv4_state')
    config = read_values('/etc/mesh.conf')
    result = subprocess.run(['ip', '-j', '-4', 'addr', 'show', 'dev', iface],
                            check=True, capture_output=True, text=True, timeout=3)
    addresses = [item['local'] for link in json.loads(result.stdout)
                 for item in link.get('addr_info', []) if item.get('family') == 'inet']
    return primary_ipv4(state, config, addresses)


if __name__ == '__main__':
    try:
        print(current_ipv4(sys.argv[1] if len(sys.argv) > 1 else 'br0'))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f'Cannot read the node IPv4 address: {error}', file=sys.stderr)
        sys.exit(1)
