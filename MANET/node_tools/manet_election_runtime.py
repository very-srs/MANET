"""Observe service election fixed points without caching live readiness."""
import fcntl
from functools import lru_cache
import ipaddress
import json
from pathlib import Path
import subprocess

from manet_ip_runtime import module, TOOLS
from manet_node_ipv4 import read_values


SERVICES = {
    'mediamtx': ('mediamtx.service', 2, 'mtx-ip.sh', {8554, 8889}, set()),
    'mumble': ('mumble-server.service', 3, 'mumble-ip.sh', {64738}, {64738}),
}

MUMBLE_DATABASE = '/var/lib/mumble-server/mumble-server.sqlite'


def mumble_database_signature(database=MUMBLE_DATABASE):
    """Stat the DB and WAL without opening SQLite or starting a process."""
    files = []
    for suffix in ('', '-wal'):
        try:
            info = Path(database + suffix).stat()
        except FileNotFoundError:
            if not suffix:
                raise
            files.append(None)
        else:
            files.append((info.st_mtime_ns, info.st_size, info.st_ctime_ns,
                          info.st_ino))
    return json.dumps(files, separators=(',', ':'))


def mumble_database_dirty(database, stamp):
    """Require an export after any observed change or missing export stamp."""
    try:
        previous = Path(stamp).read_text().strip()
        return mumble_database_signature(database) != previous
    except OSError:
        return True


@lru_cache(maxsize=2)
def ipv6_vip(config, helper_generation, service='mediamtx'):
    """Cache normalized VIPs by service, config bytes and helper metadata."""
    # Both services must fit in the cache when their elections alternate.
    value = subprocess.run(
        ['bash', str(TOOLS / SERVICES[service][2])], check=True,
        capture_output=True, text=True, timeout=5).stdout.strip()
    return str(ipaddress.IPv6Interface(value).ip)


def service_ipv6(service):
    """Return the cached canonical IPv6 VIP, invalidating changed inputs."""
    helper = (TOOLS / SERVICES[service][2]).stat()
    generation = (helper.st_ino, helper.st_mtime_ns, helper.st_ctime_ns,
                  helper.st_size)
    return ipv6_vip(Path('/etc/radvd-mesh.conf').read_bytes(), generation,
                    service)


def service_ipv4(service):
    """Derive the reserved IPv4 VIP from the current mesh subnet."""
    conf = read_values('/etc/mesh.conf')
    network = ipaddress.IPv4Network(conf['ipv4_network'], strict=False)
    return str(network.network_address + SERVICES[service][1])


def local_addresses():
    """Read current bridge addresses with a bounded ip probe."""
    raw = subprocess.run(['ip', '-j', 'addr', 'show', 'dev', 'br0'],
                         check=True, capture_output=True, text=True,
                         timeout=3).stdout
    return {str(ipaddress.ip_address(a['local'])) for row in json.loads(raw)
            for a in row.get('addr_info', [])}


def unit_state(service):
    """Observe systemd state, retaining failed and transitional states."""
    result = subprocess.run(
        ['systemctl', 'is-active', SERVICES[service][0]],
        capture_output=True, text=True, timeout=5)
    state = result.stdout.strip()
    return state if state != 'active' or result.returncode == 0 else ''


def wildcard_ports(table, state):
    """Extract wildcard ports in a Linux proc socket table's ready state."""
    ports = set()
    for line in table.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[3] != state:
            continue
        address, port = fields[1].split(':')
        # Proc renders wildcard IPv4/IPv6 as 8/32 zero hex digits. Bound
        # unconnected UDP (07) and listening TCP (0A) match ss -l semantics.
        if address in ('0' * 8, '0' * 32):
            ports.add(int(port, 16))
    return ports


def listeners_ready(service):
    """Read fresh TCP/UDP listener tables without starting socket tools."""
    _, _, _, required_tcp, required_udp = SERVICES[service]
    present = []
    for protocol, state in (('tcp', '0A'), ('udp', '07')):
        ports = set()
        for suffix in ('', '6'):
            try:
                table = Path(f'/proc/net/{protocol}{suffix}').read_text()
            except FileNotFoundError:
                # A kernel without IPv6 need not expose tcp6/udp6.
                if suffix:
                    continue
                raise
            ports.update(wildcard_ports(table, state))
        present.append(ports)
    return required_tcp <= present[0] and required_udp <= present[1]


def settled(winner, own, vip4, vip6, addresses, state, ready, transition,
            pending):
    """Require complete ownership or withdrawal, with no handover work."""
    if pending or transition.startswith('waiting:'):
        return False
    has4 = vip4 in addresses
    has6 = vip6 in addresses
    if winner == own:
        # Only leader records a completed announcement. Missing state and
        # announce-failed must reconcile even when both VIPs already exist.
        return (has4 and has6 and state == 'active' and ready
                and transition == 'leader')
    return not has4 and not has6 and state == 'inactive'


def check(service):
    """Return skip for a settled election, otherwise its fresh ranking."""
    if service not in SERVICES:
        raise ValueError('unsupported service')
    registry = Path('/var/run/mesh_node_registry').read_text()
    now = float(Path('/proc/uptime').read_text().split()[0])
    winner, score, incumbent = module('mesh-service-election').elect(
        service, registry, now)
    metric = '-' if score is None else f'{score:g}'
    ranking = f'{winner or "-"} {metric} {incumbent or "-"}'
    try:
        # Do not skip a concurrent transition using a partial observation.
        # The shell takes this same lock exclusively before changing state.
        lock = f'/var/run/{service}-election.lock'
        with Path(lock).open('a') as guard:
            fcntl.flock(guard, fcntl.LOCK_SH | fcntl.LOCK_NB)
            own = Path('/sys/class/net/br0/address').read_text().strip()
            if not own:
                return ranking
            try:
                transition = Path(lock + '.state').read_text().strip()
            except FileNotFoundError:
                transition = ''
            pending = (Path(lock + '.publish-pending').exists()
                       or Path(lock + '.release-pending').exists())
            if pending or transition.startswith('waiting:'):
                return ranking
            if (service == 'mumble' and winner == own
                    and mumble_database_dirty(MUMBLE_DATABASE,
                                              lock + '.db-export')):
                return ranking
            addresses = local_addresses()
            state = unit_state(service)
            ready = state == 'active' and listeners_ready(service)
            if settled(winner, own, service_ipv4(service),
                       service_ipv6(service), addresses, state, ready,
                       transition, pending):
                return 'skip'
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        pass  # Incomplete observation takes the normal reconciliation path.
    return ranking


def hosting(service):
    """Check live service readiness and IPv4 ownership for serving telemetry."""
    try:
        return (unit_state(service) == 'active' and listeners_ready(service)
                and service_ipv4(service) in local_addresses())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return False
