"""MediaMTX fixed-point check; freshness and live service state are never cached."""
from functools import lru_cache
import ipaddress
import json
from pathlib import Path
import subprocess

from manet_ip_runtime import module, TOOLS
from manet_node_ipv4 import read_values


@lru_cache(maxsize=1)
def ipv6_vip(config, helper_generation):
    # Reuse the deployed derivation, including its prefix normalization. Its
    # only input is radvd-mesh.conf; a config/helper change invalidates this.
    value = subprocess.run(['bash', str(TOOLS / 'mtx-ip.sh')], check=True,
                           capture_output=True, text=True, timeout=5).stdout.strip()
    return str(ipaddress.IPv6Interface(value).ip)


def settled(winner, own, vip4, vip6, addresses, state):
    has4 = vip4 in addresses
    has6 = vip6 in addresses
    if winner == own:
        return has4 and has6 and state == 'active'
    # A failed service still takes the shell path to reset-failed.
    return not has4 and not has6 and state == 'inactive'


def check(service):
    if service != 'mediamtx':
        raise ValueError('unsupported service')
    registry = Path('/var/run/mesh_node_registry').read_text()
    now = float(Path('/proc/uptime').read_text().split()[0])
    winner, score, incumbent = module('mesh-service-election').elect(service, registry, now)
    ranking = f'{winner or "-"} {"-" if score is None else f"{score:g}"} {incumbent or "-"}'
    try:
        own = Path('/sys/class/net/br0/address').read_text().strip()
        if not own:
            return ranking
        conf = read_values('/etc/mesh.conf')
        network = ipaddress.IPv4Network(conf['ipv4_network'], strict=False)
        vip4 = str(network.network_address + 2)
        helper = (TOOLS / 'mtx-ip.sh').stat()
        vip6 = ipv6_vip(Path('/etc/radvd-mesh.conf').read_bytes(),
                        (helper.st_ino, helper.st_mtime_ns, helper.st_ctime_ns, helper.st_size))
        raw = subprocess.run(['ip', '-j', 'addr', 'show', 'dev', 'br0'], check=True,
                             capture_output=True, text=True, timeout=3).stdout
        addresses = {str(ipaddress.ip_address(a['local'])) for row in json.loads(raw)
                     for a in row.get('addr_info', [])}
        state = subprocess.run(['systemctl', 'is-active', 'mediamtx.service'],
                               capture_output=True, text=True, timeout=5).stdout.strip()
        if settled(winner, own, vip4, vip6, addresses, state):
            return 'skip'
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        pass  # Incomplete observation takes the normal reconciliation path.
    return ranking
