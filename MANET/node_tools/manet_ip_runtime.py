#!/usr/bin/env python3
"""One-shot IP preflight and reconciliation, with a content-based no-op path.

This replaces the interpreter tree below mesh-ip-manager, not its schedule.
Kernel policy, IPv4 addresses, EUD readiness and dnsmasq state are checked on
every pass. A cache hit only skips the shell allocator after an identical,
successful reconciliation; errors, missing outputs and pending work retry.
"""

import fcntl
from functools import lru_cache
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from manet_node_ipv4 import primary_ipv4, read_values
from manet_registry_builder import CLAIMS_FILE, atomic_text, read_json

TOOLS = Path(__file__).resolve().parent
# Include outputs as well as inputs: a deleted/edited config must be repaired.
INPUTS = ('etc/mesh.conf', 'etc/mesh_ipv4_state', CLAIMS_FILE,
          'var/run/my_ipv4_chunk', 'var/run/my_ipv4_chunk_size',
          'etc/dnsmasq.d/mesh-eud.conf', 'etc/avahi/hosts',
          'etc/avahi/avahi-daemon.conf', 'var/lib/no_mesh_if',
          'var/run/manet-ui-firewall.state')
PENDING = ('run/manet-avahi-restart-needed', 'run/manet-avahi-host-reload-needed')


@lru_cache(maxsize=16)
def module(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), TOOLS / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def file_inputs(root=Path('/')):
    values = {}
    override = os.environ.get('MESH_CLAIMED_CHUNKS_FILE')
    for name in INPUTS:
        path = Path(override) if name == CLAIMS_FILE and override else root / name
        try:
            values[name] = path.read_text()
        except FileNotFoundError:
            values[name] = None
    values['pending'] = [name for name in PENDING if (root / name).exists()]
    values['macs'] = {p.parent.name: p.read_text().strip()
                      for p in sorted((root / 'sys/class/net').glob('*/address'))}
    values['tools'] = [(name, (TOOLS / name).stat().st_mtime_ns, (TOOLS / name).stat().st_size)
                       for name in ('manet_ip_runtime.py', 'manet_node_ipv4.py', 'mesh-ip-manager.sh',
                                    'manet-ui-firewall.sh', 'manet-ipcalc.sh')]
    return values


def addresses():
    raw = subprocess.run(['ip', '-j', '-4', 'addr', 'show', 'dev', 'br0'],
                         check=True, capture_output=True, text=True, timeout=3).stdout
    return sorted((a['local'], a['prefixlen']) for link in json.loads(raw)
                  for a in link.get('addr_info', []) if a.get('family') == 'inet')


def healthy_allocation(files, live, ready, active):
    """Only cache a fully installed, unambiguous allocation and serving state."""
    if files['pending'] or ready != active:
        return False
    try:
        conf = dict(line.split('=', 1) for line in (files['etc/mesh.conf'] or '').splitlines()
                    if '=' in line and not line.lstrip().startswith('#'))
        network = ipaddress.IPv4Network(conf['ipv4_network'].strip('"\''), strict=False)
        chunk = int(files['var/run/my_ipv4_chunk'])
        size = int(files['var/run/my_ipv4_chunk_size'])
        if chunk < 0 or not 2 <= size <= 255:
            return False
        first = int(network.network_address) + 6 + chunk * size
        if first + size - 1 >= int(network.broadcast_address):
            return False
        expected = {(str(ipaddress.IPv4Address(first + n)), network.prefixlen) for n in (0, 1)}
        allocation_addresses = {(address, prefix) for address, prefix in live
                                if ipaddress.IPv4Address(address) in network
                                and int(ipaddress.IPv4Address(address)) > int(network.network_address) + 5}
        if expected != allocation_addresses:
            return False
        if not files['etc/dnsmasq.d/mesh-eud.conf']:
            return False
        # A contested or incomplete claim must run the allocator each pass.
        for line in (files[CLAIMS_FILE] or '').splitlines():
            _, mac, start, width = line.split(',')
            if mac in files['macs'].values():
                continue
            start, width = int(start), int(width)
            if not 1 <= width <= 255 or (start <= first + size - 1 and first <= start + width - 1):
                return False
        return True
    except (ValueError, TypeError, KeyError):
        return False


def reconcile():
    env = dict(os.environ, MANET_IP_CHECKED='1')
    cache_path = Path('/run/manet-ip-runtime.json')
    startup = module('mesh-ip-startup')
    isolated, ready, live, primary, address_ok = False, False, [], '', False
    startup_ready = startup.main() == 0
    env['MANET_IP_STARTUP_READY'] = str(int(startup_ready))
    if startup_ready:
        isolation = module('manet-dhcp-isolation')
        try:
            isolation.ensure()
            isolated = True
            ready = isolation.eud_ready()
            live = addresses()
            primary = primary_ipv4(read_values('/etc/mesh_ipv4_state'), read_values('/etc/mesh.conf'),
                                   [a for a, _ in live])
            address_ok = True
        except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
            print(f'IP-MGR: preflight deferred: {error}', file=sys.stderr)
    env.update(MANET_IP_ISOLATED=str(int(isolated)), MANET_IP_EUD_READY=str(int(ready)),
               MANET_IP_ADDRESS_OK=str(int(address_ok)), MANET_IP_PRIMARY=primary)
    before, eligible = None, False
    if startup_ready and isolated and address_ok:
        try:
            files = file_inputs()
            service = subprocess.run(['systemctl', 'is-active', 'dnsmasq.service'],
                                     capture_output=True, text=True, timeout=5)
            ui = subprocess.run(['nft', 'list', 'table', 'inet', 'manet_ui'],
                                capture_output=True, text=True, timeout=5)
            eligible = (bool(primary) and service.returncode in (0, 3) and ui.returncode == 0
                        and healthy_allocation(files, live, ready, service.returncode == 0))
            before = json.dumps([files, live, ready, service.stdout], sort_keys=True)
            if eligible and read_json(cache_path).get('inputs') == before:
                return 0
        except (OSError, ValueError, subprocess.SubprocessError):
            pass  # A failed observation cannot validate a cached reconciliation.
    cache_path.unlink(missing_ok=True)
    result = subprocess.run(['bash', str(TOOLS / 'mesh-ip-manager.sh')], env=env).returncode
    # Cache only a fixed point. If the shell changed any output, the next pass
    # verifies its live effect before caching. Failures never become no-ops.
    if result == 0 and eligible and files == file_inputs():
        atomic_text(cache_path, json.dumps({'inputs': before}), 0o600)
    return result


def main():
    with Path('/run/manet-ip-runtime.lock').open('a') as lock:
        # Bound concurrent dispatcher waits just as the registry builder does.
        deadline = time.monotonic() + 10
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('IP reconciliation busy')
                time.sleep(.05)
        return reconcile()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'IP-MGR: {error}', file=sys.stderr)
        sys.exit(1)
