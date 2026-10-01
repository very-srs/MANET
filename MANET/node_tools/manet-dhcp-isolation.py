#!/usr/bin/env python3
"""Install and verify the node-local DHCP boundary at bat0's bridge port."""

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

TABLE = 'manet_dhcp'
RULES = Path(os.environ.get('MANET_DHCP_RULES', '/usr/local/share/manet/dhcp-isolation.nft'))
NFT = os.environ.get('NFT', 'nft')
LOCK = Path(os.environ.get('MANET_DHCP_LOCK', '/run/manet-dhcp-isolation.lock'))


def run(*args):
    return subprocess.run([NFT, *args], check=True, capture_output=True, text=True, timeout=10).stdout


def match(left, right):
    return {'match': {'op': '==', 'left': left, 'right': right}}


def valid_rules(data):
    """Check hook placement and packet matches, ignoring only runtime counters."""
    chains, rules = {}, {}
    for item in data.get('nftables', []):
        chain = item.get('chain')
        rule = item.get('rule')
        if chain and chain.get('family') == 'bridge' and chain.get('table') == TABLE:
            chains[chain['name']] = (chain.get('type'), chain.get('hook'), chain.get('prio'), chain.get('policy'))
        if rule and rule.get('family') == 'bridge' and rule.get('table') == TABLE:
            expressions = [e for e in rule.get('expr', []) if 'counter' not in e]
            rules.setdefault(rule['chain'], []).append(expressions)
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
    # In the bridge family, nft's symbolic "filter" priority is -200.
    return (chains == {name: ('filter', name, -200, 'accept') for name in expected}
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
        if mode == ['apply'] or not check():
            run('-f', str(RULES))
            if not check():
                raise RuntimeError('Installed DHCP isolation failed verification')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        detail = getattr(error, 'stderr', '') or str(error)
        print('ERROR: DHCP isolation: ' + detail.strip(), file=sys.stderr)
        sys.exit(1)
