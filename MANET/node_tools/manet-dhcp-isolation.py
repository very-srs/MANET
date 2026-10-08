#!/usr/bin/env python3
"""Keep DHCP and EUD discovery local; enable wired mDNS only after verification."""

import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

TABLE = 'manet_dhcp'
RULES = Path(os.environ.get('MANET_DHCP_RULES', '/usr/local/share/manet/dhcp-isolation.nft'))
NFT = os.environ.get('NFT', 'nft')
LOCK = Path(os.environ.get('MANET_DHCP_LOCK', '/run/manet-dhcp-isolation.lock'))
DISCOVERY_PORTS = {'udp': [137, 138, 1900, 3702, 5353, 5355], 'tcp': [137, 5355]}


def run(*args):
    return subprocess.run([NFT, *args], check=True, capture_output=True, text=True, timeout=10).stdout


def match(left, right):
    return {'match': {'op': '==', 'left': left, 'right': right}}


def _canonical_expr(expr):
    """Normalize protocol dependencies omitted by nft's JSON listings.

    A transport payload implies l4proto; an ip/ip6 payload implies the matching
    EtherType. Keep all actual address, port, interface and verdict checks.
    """
    later, kept = set(), []
    for entry in reversed(expr):
        m = entry.get('match')
        left = m and m.get('left')
        if m and m.get('op') == '==' and isinstance(left, dict):
            if left == {'meta': {'key': 'l4proto'}} and m.get('right') in later:
                continue
            if (left == {'payload': {'protocol': 'ether', 'field': 'type'}}
                    and m.get('right') in ('ip', 'ip6') and m['right'] in later):
                continue
        if m and isinstance(left, dict):
            protocol = left.get('payload', {}).get('protocol')
            if protocol:
                later.add(protocol)
        kept.append(entry)
    return list(reversed(kept))


def valid_rules(data):
    """Check active table, hooks and normalized matches; ignore counters/handles."""
    chains, rules, tables = {}, {}, 0
    for item in data.get('nftables', []):
        table = item.get('table')
        if table and table.get('family') == 'bridge' and table.get('name') == TABLE:
            if table.get('flags'):
                return False
            tables += 1
        chain = item.get('chain')
        rule = item.get('rule')
        if chain and chain.get('family') == 'bridge' and chain.get('table') == TABLE:
            if chain['name'] in chains:
                return False
            chains[chain['name']] = (chain.get('type'), chain.get('hook'), chain.get('prio'), chain.get('policy'))
        if rule and rule.get('family') == 'bridge' and rule.get('table') == TABLE:
            expressions = [e for e in rule.get('expr', []) if 'counter' not in e]
            rules.setdefault(rule['chain'], []).append(_canonical_expr(expressions))
        for kind, obj in item.items():
            if (kind not in ('table', 'chain', 'rule', 'metainfo') and isinstance(obj, dict)
                    and obj.get('family') == 'bridge' and obj.get('table') == TABLE):
                return False
    expected = {}
    for chain, direction, field, ports in (
            ('forward', 'iifname', 'dport', {'set': [67, 68]}),
            ('forward', 'oifname', 'dport', {'set': [67, 68]}),
            ('input', 'iifname', 'dport', 67),
            ('output', 'oifname', 'sport', 67)):
        expected.setdefault(chain, []).append([
            match({'meta': {'key': direction}}, 'bat0'),
            match({'payload': {'protocol': 'ether', 'field': 'type'}}, 'ip'),
            match({'payload': {'protocol': 'udp', 'field': field}}, ports),
            {'drop': None},
        ])
    for family in ('ip', 'ip6'):
        for protocol, ports in DISCOVERY_PORTS.items():
            for field in ('dport', 'sport'):
                for chain, direction in (('forward', 'iifname'), ('forward', 'oifname'),
                                         ('input', 'iifname'), ('output', 'oifname')):
                    expected[chain].append([
                        match({'meta': {'key': direction}}, 'bat0'),
                        match({'payload': {'protocol': 'ether', 'field': 'type'}}, family),
                        match({'payload': {'protocol': protocol, 'field': field}}, {'set': ports}),
                        {'drop': None},
                    ])
    expected = {chain: [_canonical_expr(expr) for expr in entries] for chain, entries in expected.items()}
    # In the bridge family, nft's symbolic "filter" priority is -200.
    return (tables == 1 and chains == {name: ('filter', name, -200, 'accept') for name in expected}
            and rules == expected)


def check():
    try:
        return valid_rules(json.loads(run('-j', 'list', 'table', 'bridge', TABLE)))
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def eud_ready(sysnet=None):
    """A forwarding local bridge port must exist before serving EUD DHCP."""
    sysnet = Path(sysnet or os.environ.get('MANET_SYS_NET', '/sys/class/net'))
    try:
        ports = list((sysnet / 'br0/brif').iterdir())
    except OSError:
        return False
    for port in ports:
        if port.name == 'bat0':
            continue
        try:
            # Linux BR_STATE_FORWARDING=3. Carrier also excludes a stale
            # port object left behind by an unplug or AP shutdown.
            if (port.joinpath('state').read_text().strip() == '3'
                    and sysnet.joinpath(port.name, 'carrier').read_text().strip() == '1'):
                return True
        except OSError:
            continue
    return False


def atomic_write(path, contents):
    info = path.stat()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
            temporary = stream.name
            stream.write(contents)
            stream.flush()
            os.fchmod(stream.fileno(), info.st_mode & 0o777)
            os.fchown(stream.fileno(), info.st_uid, info.st_gid)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def configure_avahi(protected, root=Path('/')):
    """Reconcile the installed config, also on tools updates and later br0 creation.

    The caller holds our lock and has verified the complete policy. Never infer
    protection from a file on disk. A missing AP/bridge falls back to loopback,
    since an empty allow-interfaces would allow every interface.
    """
    config = root / 'etc/avahi/avahi-daemon.conf'
    if not config.exists():
        return
    allowed = []
    try:
        ap = (root / 'var/lib/no_mesh_if').read_text().splitlines()[0].strip()
    except (OSError, IndexError):
        ap = ''
    if re.fullmatch(r'[a-zA-Z0-9_.:-]{1,15}', ap) and ap not in ('br0', 'bat0'):
        allowed.append(ap)
    if protected and (root / 'sys/class/net/br0').exists():
        allowed.append('br0')
    original = config.read_text()
    settings = {'server': {'allow-interfaces': ','.join(allowed or ['lo']),
                           'host-name': 'manet', 'host-name-from-machine-id': 'no'},
                'publish': {'publish-addresses': 'no'}}
    lines, section, found = [], '', set()
    for line in original.splitlines():
        if line.strip().startswith('['):
            section = line.strip().strip('[]')
            if section in settings:
                if section in found:
                    raise ValueError(f'duplicate Avahi {section} section')
                found.add(section)
                lines.extend([line, *(f'{key}={value}' for key, value in settings[section].items())])
                continue
        if section in settings and line.partition('=')[0].strip() in settings[section]:
            continue
        lines.append(line)
    if 'server' not in found:
        raise ValueError('Avahi server section missing')
    if 'publish' not in found:
        lines.extend(['', '[publish]', 'publish-addresses=no'])
    updated = '\n'.join(lines) + '\n'
    updates = {config: updated} if updated != original else {}
    # The IP manager owns the static management address in /etc/avahi/hosts.
    pending = root / 'run/manet-avahi-restart-needed'
    if not updates and not pending.exists():
        return
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.touch()
    for path, contents in updates.items():
        atomic_write(path, contents)
    try:
        subprocess.run(['systemctl', 'try-restart', 'avahi-daemon.service'],
                       check=True, capture_output=True, text=True, timeout=10)
    except subprocess.CalledProcessError:
        # Install archives contain the config before radio-setup installs the
        # Avahi package. Preparing that config must not break DHCP first boot.
        state = subprocess.run(['systemctl', 'show', '-p', 'LoadState', '--value', 'avahi-daemon.service'],
                               check=True, capture_output=True, text=True, timeout=10).stdout.strip()
        if state != 'not-found':
            raise
    pending.unlink(missing_ok=True)


def ensure(apply=False):
    """Importable per-pass check, shared with the IP runtime interpreter."""
    # Bound serialization even if a previous installer is stuck in netlink.
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open('a') as lock:
        import time
        deadline = time.monotonic() + 15
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('DHCP isolation update is busy')
                time.sleep(.1)
        try:
            if apply or not check():
                run('-f', str(RULES))
                if not check():
                    raise RuntimeError('Installed DHCP/discovery isolation failed verification')
        except (OSError, RuntimeError, subprocess.SubprocessError):
            configure_avahi(False)
            raise
        configure_avahi(True)


def main():
    mode = sys.argv[1:]
    if mode == ['eud-ready']:
        sys.exit(0 if eud_ready() else 1)
    if mode not in (['check'], ['ensure'], ['apply']):
        raise ValueError('usage: manet-dhcp-isolation.py {check|ensure|apply|eud-ready}')
    if mode == ['check']:
        if not check():
            raise RuntimeError('DHCP isolation rules are missing or incorrect')
        return
    ensure(apply=mode == ['apply'])


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        detail = getattr(error, 'stderr', '') or str(error)
        print('manet-dhcp-isolation.py: ' + detail.strip(), file=sys.stderr)
        sys.exit(1)
