"""DHCP/discovery boundary and nft readback verification.

The fixture retains captured CM4 nft 1.1.3 DHCP entries. New discovery entries
model that listing format (implied protocol matches omitted); they are not
claimed as a new device capture. Expanded dependencies are tested too.
"""
import copy
import importlib.util
import json
import ipaddress
import socket
import struct
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('dhcp_isolation', TOOLS / 'manet-dhcp-isolation.py')
isolation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(isolation)


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.rules = json.loads((TOOLS / 'fixtures/dhcp-isolation-nft.json').read_text())

    def test_cm4_listing_format_and_changing_counters_are_accepted(self):
        self.assertTrue(isolation.valid_rules(self.rules))
        for item in self.rules['nftables']:
            for expr in item.get('rule', {}).get('expr', []):
                if 'counter' in expr:
                    expr['counter'] = {'packets': 123, 'bytes': 45678}
        self.assertTrue(isolation.valid_rules(self.rules))

    def test_explicit_and_omitted_protocol_dependencies_both_validate(self):
        for item in self.rules['nftables']:
            expr = item.get('rule', {}).get('expr')
            if expr is None:
                continue
            expanded = []
            for entry in expr:
                payload = entry.get('match', {}).get('left', {}).get('payload', {})
                protocol = payload.get('protocol')
                if protocol in ('ip', 'ip6'):
                    expanded.append(isolation.match({'payload': {'protocol': 'ether', 'field': 'type'}}, protocol))
                elif protocol in ('udp', 'tcp'):
                    expanded.append(isolation.match({'meta': {'key': 'l4proto'}}, protocol))
                expanded.append(entry)
            expr[:] = expanded
        self.assertTrue(isolation.valid_rules(self.rules))

    def test_every_effective_match_and_verdict_is_required(self):
        for index, item in enumerate(self.rules['nftables']):
            for number, expr in enumerate(item.get('rule', {}).get('expr', [])):
                if 'counter' in expr:
                    continue
                with self.subTest(rule=index, expression=number):
                    changed = copy.deepcopy(self.rules)
                    del changed['nftables'][index]['rule']['expr'][number]
                    self.assertFalse(isolation.valid_rules(changed))

    def test_wrong_discovery_ports_family_transport_and_inactive_table_rejected(self):
        for change in ('ports', 'family', 'transport', 'dormant', 'table', 'set'):
            with self.subTest(change=change):
                data = copy.deepcopy(self.rules)
                rule = next(x['rule'] for x in data['nftables'] if 'rule' in x
                            and any(e.get('match', {}).get('right') == 'ip6' for e in x['rule']['expr']))
                if change == 'ports':
                    rule['expr'][2]['match']['right']['set'].remove(5355)
                elif change == 'family':
                    rule['expr'][1]['match']['right'] = 'ip'
                elif change == 'transport':
                    rule['expr'][2]['match']['left']['payload']['protocol'] = 'sctp'
                elif change == 'dormant':
                    next(x['table'] for x in data['nftables'] if 'table' in x)['flags'] = ['dormant']
                elif change == 'table':
                    data['nftables'] = [x for x in data['nftables'] if 'table' not in x]
                else:
                    data['nftables'].append({'set': {'family': 'bridge', 'table': 'manet_dhcp', 'name': 'unexpected'}})
                self.assertFalse(isolation.valid_rules(data))

    def test_dhcp_only_and_mdns_only_policies_require_upgrade(self):
        self.rules['nftables'] = [x for x in self.rules['nftables'] if 'rule' not in x or any(
            e.get('match', {}).get('right') in (67, {'set': [67, 68]}) for e in x['rule']['expr'])]
        self.assertFalse(isolation.valid_rules(self.rules))
        self.rules['nftables'].append({'rule': {'family': 'bridge', 'table': 'manet_dhcp',
            'chain': 'forward', 'expr': [isolation.match({'meta': {'key': 'iifname'}}, 'bat0'),
                isolation.match({'payload': {'protocol': 'ip', 'field': 'daddr'}}, '224.0.0.251'),
                isolation.match({'payload': {'protocol': 'udp', 'field': 'dport'}}, 5353), {'drop': None}]}})
        self.assertFalse(isolation.valid_rules(self.rules))

    def test_dhcp_requires_a_forwarding_eud_port_with_carrier(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            ports = root / 'br0/brif'
            ports.mkdir(parents=True)
            self.assertFalse(isolation.eud_ready(root))
            for iface, carrier, state, expected in [('bat0', '1', '3', False),
                                                    ('end0', '0', '3', False),
                                                    ('end0', '1', '2', False),
                                                    ('end0', '1', '3', True),
                                                    ('wlan1', '1', '3', True)]:
                with self.subTest(iface=iface, carrier=carrier, state=state):
                    for port in ports.iterdir():
                        (port/'state').unlink()
                        port.rmdir()
                    (ports/iface).mkdir()
                    (root/iface).mkdir(exist_ok=True)
                    (ports/iface/'state').write_text(state+'\n')
                    (root/iface/'carrier').write_text(carrier+'\n')
                    self.assertEqual(isolation.eud_ready(root), expected)

    def test_every_hook_and_rule_is_required(self):
        for n, item in enumerate(self.rules['nftables']):
            if 'chain' in item or 'rule' in item:
                changed = copy.deepcopy(self.rules)
                del changed['nftables'][n]
                self.assertFalse(isolation.valid_rules(changed))

    def test_wrong_ports_interface_verdict_or_hook_are_rejected(self):
        for replacement in ('wrong-interface', 'wrong-port', 'accept', 'wrong-hook', 'extra-accept'):
            with self.subTest(replacement=replacement):
                changed = copy.deepcopy(self.rules)
                rule = next(x['rule'] for x in changed['nftables'] if 'rule' in x)
                chain = next(x['chain'] for x in changed['nftables'] if 'chain' in x)
                if replacement == 'wrong-interface':
                    rule['expr'][0]['match']['right'] = 'bat'
                elif replacement == 'wrong-port':
                    rule['expr'][2]['match']['right'] = 68
                elif replacement == 'accept':
                    rule['expr'][-1] = {'accept': None}
                elif replacement == 'wrong-hook':
                    chain['hook'] = 'prerouting'
                else:
                    extra = copy.deepcopy(rule)
                    extra['expr'] = [{'accept': None}]
                    changed['nftables'].insert(0, {'rule': extra})
                self.assertFalse(isolation.valid_rules(changed))

    def test_unavailable_or_malformed_policy_is_not_reported_as_protected(self):
        for result in ('not-json', 'null', '[]', '{"nftables":[null]}'):
            with patch.object(isolation, 'run', return_value=result):
                self.assertFalse(isolation.check())
        with patch.object(isolation, 'run', side_effect=subprocess.CalledProcessError(1, 'nft')):
            self.assertFalse(isolation.check())

    def invoke(self, mode, root):
        with patch.object(isolation, 'LOCK', root / 'lock'), \
                patch.object(sys, 'argv', ['manet-dhcp-isolation.py', mode]), \
                patch.object(isolation, 'configure_avahi'):
            isolation.main()

    def test_ensure_preserves_healthy_counters_and_repairs_missing_policy(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            with patch.object(isolation, 'check', return_value=True), \
                    patch.object(isolation, 'run') as run:
                self.invoke('ensure', root)
                run.assert_not_called()
            with patch.object(isolation, 'check', side_effect=[False, True]), \
                    patch.object(isolation, 'run') as run:
                self.invoke('ensure', root)
                run.assert_called_once_with('-f', str(isolation.RULES))

    def test_failed_load_or_verification_fails_and_releases_lock_for_retry(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            for error in (subprocess.CalledProcessError(1, 'nft'), None):
                with patch.object(isolation, 'check', return_value=False), \
                        patch.object(isolation, 'run', side_effect=error):
                    with self.assertRaises((subprocess.CalledProcessError, RuntimeError)):
                        self.invoke('apply', root)
                with patch.object(isolation, 'check', return_value=True), \
                        patch.object(isolation, 'run') as run:
                    self.invoke('apply', root)
                    run.assert_called_once_with('-f', str(isolation.RULES))


class PacketPolicyTests(unittest.TestCase):
    def setUp(self):
        # Interpret the shipped rule file, so a fixture-only change cannot pass.
        self.rules = []
        chain = None
        for line in (TOOLS.parent / 'share/manet/dhcp-isolation.nft').read_text().splitlines():
            tokens = shlex.split(line, comments=True)
            if tokens[:1] == ['chain']:
                chain = tokens[1]
            if tokens[:1] not in (['iifname'], ['oifname']):
                continue
            expr = []
            while tokens:
                key = tokens.pop(0)
                if key == 'counter':
                    continue
                if key == 'drop':
                    expr.append({'drop': None})
                    self.assertEqual(tokens, [])
                    break
                if key in ('iifname', 'oifname'):
                    left = {'meta': {'key': key}}
                else:
                    self.assertIn(key, ('ether', 'ip', 'ip6', 'udp', 'tcp'))
                    left = {'payload': {'protocol': key, 'field': tokens.pop(0)}}
                right = tokens.pop(0)
                if right == '{':
                    ports = []
                    while tokens[0] != '}':
                        ports.append(int(tokens.pop(0).rstrip(',')))
                    tokens.pop(0)
                    right = {'set': ports}
                elif right.isdecimal():
                    right = int(right)
                expr.append(isolation.match(left, right))
            self.rules.append({'rule': {'family': 'bridge', 'table': 'manet_dhcp', 'chain': chain, 'expr': expr}})

    def drops(self, chain, ingress='', egress='', family='ip', destination='224.0.0.251',
              protocol='udp', sport=40000, dport=5353):
        for item in self.rules:
            rule = item['rule']
            if rule['chain'] != chain:
                continue
            matched = True
            for expr in rule['expr'][:-1]:
                match = expr['match']
                left, right = match['left'], match['right']
                if 'meta' in left:
                    value = {'iifname': ingress, 'oifname': egress}[left['meta']['key']]
                else:
                    payload = left['payload']
                    if payload['protocol'] == 'ether':
                        value = family
                    elif payload['protocol'] in ('ip', 'ip6'):
                        value = destination if payload['protocol'] == family else None
                    else:
                        value = {'sport': sport, 'dport': dport}[payload['field']] if protocol == payload['protocol'] else None
                matched &= value in right['set'] if isinstance(right, dict) else value == right
            if matched:
                self.assertEqual(rule['expr'][-1], {'drop': None})
                return True
        return False

    def test_shipped_rules_and_readback_validator_agree(self):
        data = json.loads((TOOLS / 'fixtures/dhcp-isolation-nft.json').read_text())
        data['nftables'] = [x for x in data['nftables'] if 'rule' not in x] + self.rules
        self.assertTrue(isolation.valid_rules(data))
        self.assertEqual(len(self.rules), 36)

    def test_discovery_and_unicast_replies_drop_only_at_mesh_boundary(self):
        for family, destinations in [('ip', ('224.0.0.251', '224.0.0.252', '239.255.255.250',
                                               '255.255.255.255', '10.30.2.255', '192.0.2.1')),
                                     ('ip6', ('ff02::fb', 'ff02::1:3', 'ff02::c', 'ff05::c', 'fe80::1'))]:
            for protocol, ports in [('udp', (137, 138, 1900, 3702, 5353, 5355)), ('tcp', (137, 5355))]:
                for port in ports:
                    for source, dest in ((40000, port), (port, 40000), (port, port)):
                        for address in destinations:
                            for ingress in ('bat0', 'end0', 'wlan3'):
                                args = dict(family=family, destination=address, protocol=protocol, sport=source, dport=dest)
                                with self.subTest(family=family, protocol=protocol, source=source, dest=dest, ingress=ingress):
                                    self.assertEqual(self.drops('input', ingress=ingress, **args), ingress == 'bat0')
                                    self.assertEqual(self.drops('output', egress=ingress, **args), ingress == 'bat0')
                                    for egress in ('bat0', 'end0', 'wlan3'):
                                        self.assertEqual(self.drops('forward', ingress, egress, **args),
                                                         'bat0' in (ingress, egress))

    def test_mesh_services_multicast_control_and_unicast_dns_pass(self):
        traffic = [('ip', '239.2.3.1', 'udp', 6969), ('ip', '224.10.10.1', 'udp', 17012),
                   ('ip', '239.5.5.55', 'udp', 7171), ('ip6', 'ff02::1', 'udp', 16962),
                   ('ip', '255.255.255.255', 'udp', 21027), ('ip', '10.30.2.255', 'udp', 21027),
                   ('ip6', 'ff12::8384', 'udp', 21027)]
        traffic += [('ip', '239.192.41.1', 'udp', port) for port in range(38801, 38865)]
        traffic += [('ip', '224.1.2.3', 'udp', port) for port in (8002, 8003, 8006, 8007)]
        for protocol, ports in [('udp', (53, 123, 4242, 4349, 10011, 64738, 22000, 8000, 8001, 8004, 8005, 8189, 8890)),
                                ('tcp', (22, 53, 80, 443, 4242, 64738, 22000, 8384, 8554, 8322, 1935, 1936, 8888, 8889, 8087, 8088, 8089))]:
            for family, address in [('ip', '192.0.2.1'), ('ip6', 'fe80::1')]:
                traffic += [(family, address, protocol, port) for port in ports]
        for address in ('224.0.0.1', '224.0.0.2', '224.0.0.22', '239.192.41.1'):
            traffic.append(('ip', address, 'igmp', 0))
        for address in ('ff02::1', 'ff02::2', 'ff02::16', 'ff02::1:ff00:1'):
            traffic.append(('ip6', address, 'icmpv6', 0))
        traffic += [('arp', 'ff:ff:ff:ff:ff:ff', 'arp', 0), ('0x4305', 'ff:ff:ff:ff:ff:ff', 'batman', 0)]
        for family, group, protocol, port in traffic:
            for chain in ('input', 'output', 'forward'):
                for source, dest in ((40000, port), (port, 40000)):
                    with self.subTest(family=family, group=group, protocol=protocol, port=port, chain=chain):
                        self.assertFalse(self.drops(chain, 'bat0', 'bat0', family, group, protocol, source, dest))

    def test_dhcp_stays_isolated_and_local_dhcp_works(self):
        for ingress, egress in (('bat0', 'end0'), ('end0', 'bat0')):
            for port in (67, 68):
                self.assertTrue(self.drops('forward', ingress, egress, destination='255.255.255.255', dport=port))
        self.assertTrue(self.drops('input', 'bat0', destination='192.0.2.1', dport=67))
        self.assertTrue(self.drops('output', egress='bat0', destination='255.255.255.255', sport=67, dport=68))
        for local in ('end0', 'wlan3'):
            self.assertFalse(self.drops('input', local, destination='192.0.2.1', dport=67))
            self.assertFalse(self.drops('output', egress=local, destination='255.255.255.255', sport=67, dport=68))


class AvahiTests(unittest.TestCase):
    def test_guard_interfaces_idempotence_failure_and_restart_retry(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            config = root / 'etc/avahi/avahi-daemon.conf'
            config.parent.mkdir(parents=True)
            config.write_text('[server]\nhost-name=manet\nallow-interfaces=wlan3\n[reflector]\nenable-reflector=no\n')
            (root / 'var/lib').mkdir(parents=True)
            (root / 'var/lib/no_mesh_if').write_text('wlan3\n')
            bridge = root / 'sys/class/net/br0'
            bridge.mkdir(parents=True)
            with patch.object(isolation.subprocess, 'run') as run:
                isolation.configure_avahi(True, root)
                self.assertIn('allow-interfaces=wlan3,br0\n', config.read_text())
                self.assertIn('host-name=manet\n', config.read_text())
                self.assertIn('[publish]\npublish-addresses=no\n', config.read_text())
                self.assertIn('enable-reflector=no\n', config.read_text())
                run.assert_called_once()
                isolation.configure_avahi(True, root)
                run.assert_called_once()
                isolation.configure_avahi(False, root)
                self.assertIn('allow-interfaces=wlan3\n', config.read_text())
                self.assertNotIn(',br0', config.read_text())
                bridge.rmdir()
                isolation.configure_avahi(True, root)
                self.assertNotIn(',br0', config.read_text())
                (root / 'var/lib/no_mesh_if').unlink()
                isolation.configure_avahi(True, root)
                self.assertIn('allow-interfaces=lo\n', config.read_text())
                bridge.mkdir()
                run.side_effect = subprocess.TimeoutExpired('systemctl', 10)
                with self.assertRaises(subprocess.TimeoutExpired):
                    isolation.configure_avahi(True, root)
                self.assertTrue((root / 'run/manet-avahi-restart-needed').exists())
                run.side_effect = None
                run.reset_mock()
                isolation.configure_avahi(True, root)
                run.assert_called_once()
                self.assertFalse((root / 'run/manet-avahi-restart-needed').exists())

    def test_apply_ensure_and_check_order_avahi_after_live_verification(self):
        with tempfile.TemporaryDirectory() as scratch:
            with patch.object(isolation, 'LOCK', Path(scratch) / 'lock'), \
                    patch.object(isolation, 'check') as check, \
                    patch.object(isolation, 'run') as run, \
                    patch.object(isolation, 'configure_avahi') as avahi:
                events = []
                check.side_effect = lambda: events.append('verified') or True
                run.side_effect = lambda *args: events.append('loaded')
                avahi.side_effect = lambda protected: events.append(('avahi', protected))
                for mode, expected in [('apply', ['loaded', 'verified', ('avahi', True)]),
                                       ('ensure', ['verified', ('avahi', True)]), ('check', ['verified'])]:
                    events.clear()
                    with patch.object(sys, 'argv', ['isolation', mode]):
                        isolation.main()
                    self.assertEqual(events, expected)
                events.clear()
                check.side_effect = lambda: events.append('failed-check') or False
                with patch.object(sys, 'argv', ['isolation', 'ensure']):
                    with self.assertRaises(RuntimeError):
                        isolation.main()
                self.assertEqual(events, ['failed-check', 'loaded', 'failed-check', ('avahi', False)])

    def test_first_boot_config_can_precede_avahi_package(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            config = root / 'etc/avahi/avahi-daemon.conf'
            config.parent.mkdir(parents=True)
            config.write_text('[server]\nhost-name=manet\n')
            for load_state in ('loaded', 'not-found'):
                with patch.object(isolation.subprocess, 'run', side_effect=[
                        subprocess.CalledProcessError(5, 'systemctl'),
                        subprocess.CompletedProcess([], 0, stdout=load_state + '\n')]):
                    if load_state == 'loaded':
                        with self.assertRaises(subprocess.CalledProcessError):
                            isolation.configure_avahi(True, root)
                    else:
                        isolation.configure_avahi(True, root)
            self.assertIn('allow-interfaces=lo\n', config.read_text())
            self.assertFalse((root / 'run/manet-avahi-restart-needed').exists())

    def test_setup_and_existing_updater_use_guarded_helper(self):
        setup = (TOOLS / 'radio-setup.sh').read_text().split('# === mDNS: manet.local ===')[1].split('# === UPS')[0]
        self.assertIn('/usr/local/bin/manet-dhcp-isolation.py ensure', setup)
        self.assertNotIn('allow-interfaces=', setup)
        updater = (TOOLS / 'node-update.py').read_text()
        self.assertIn("str(self.destination('usr/local/bin/manet-dhcp-isolation.py')), 'ensure'", updater)

    def test_guard_disables_automatic_addresses_and_preserves_static_internal_entry(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            config = root / 'etc/avahi/avahi-daemon.conf'
            config.parent.mkdir(parents=True)
            config.write_text('[server]\nhost-name=perf\nhost-name-from-machine-id=yes\n'
                              '[publish]\npublish-addresses=yes\npublish-workstation=no\n')
            hosts = config.with_name('hosts')
            hosts.write_text('# operator printer\n192.0.2.5 printer.local\n'
                             '10.30.2.147 manet.local\n')
            with patch.object(isolation.subprocess, 'run') as run:
                isolation.configure_avahi(True, root)
                self.assertIn('host-name=manet\n', config.read_text())
                self.assertIn('host-name-from-machine-id=no\n', config.read_text())
                self.assertIn('publish-addresses=no\n', config.read_text())
                self.assertIn('publish-workstation=no\n', config.read_text())
                self.assertNotIn('host-name=perf', config.read_text())
                self.assertEqual(hosts.read_text(), '# operator printer\n192.0.2.5 printer.local\n10.30.2.147 manet.local\n')
                run.assert_called_once()
                isolation.configure_avahi(True, root)
                run.assert_called_once()


class StaticAvahiTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        for name in ('avahi', 'dnsmasq', 'run'):
            (self.root / name).mkdir()
        self.hosts = self.root / 'avahi/hosts'
        self.dns = self.root / 'dnsmasq/mesh-eud.conf'
        self.events = self.root / 'events'
        self.pending = self.root / 'run/manet-avahi-host-reload-needed'
        source = (TOOLS / 'mesh-ip-manager.sh').read_text()
        self.source = source
        names = ('ip_to_int', 'int_to_ip', 'valid_eud_gateway', 'update_avahi_host',
                 'configure_dnsmasq', 'release_control_ips')
        self.functions = '\n'.join(re.search(r'^' + name + r'\(\) \{.*?^\}', source, re.M | re.S)[0]
                                   for name in names)
        self.functions = self.functions.replace('/etc/avahi/hosts', '"${TEST_ROOT}/avahi/hosts"')
        self.functions = self.functions.replace('/etc/dnsmasq.d/mesh-eud.conf', '"${TEST_ROOT}/dnsmasq/mesh-eud.conf"')
        self.functions = self.functions.replace('/etc/dnsmasq.d/.mesh-eud.conf', '"${TEST_ROOT}/dnsmasq/.mesh-eud.conf"')
        self.functions = self.functions.replace('/run/manet-avahi-host-reload-needed',
                                                '"${TEST_ROOT}/run/manet-avahi-host-reload-needed"')
        # Lease deletion is unrelated here; keep every filesystem operation in scratch.
        self.functions = self.functions.replace('/var/lib/misc/dnsmasq.leases /run/dnsmasq.leases /tmp/dnsmasq.leases',
                                                '"${TEST_ROOT}/leases"')

    def shell(self, body, expected=0):
        stubs = r'''log() { :; }
ip_in_cidr() { return 1; }
ensure_dnsmasq_running() { :; }
python3() { return 1; }
ip() { echo "ip $*" >> "$TEST_ROOT/events"; }
systemctl() {
    echo "$*" >> "$TEST_ROOT/events"
    case "$1" in
        is-active) [ ! -f "$TEST_ROOT/inactive" ] ;;
        reload) [ ! -f "$TEST_ROOT/fail-reload" ] ;;
        is-enabled) echo enabled ;;
    esac
}
MTX_VIP=10.30.2.2
MUMBLE_VIP=10.30.2.3
IPV4_NETWORK=10.30.2.0/24
'''
        result = subprocess.run(['bash', '-c', stubs + self.functions + '\n' + body],
                                capture_output=True, text=True, timeout=5,
                                env=dict(os.environ, TEST_ROOT=str(self.root)))
        self.assertEqual(result.returncode, expected, result.stderr)
        return result

    def reloads(self):
        return self.events.read_text().splitlines().count('reload avahi-daemon.service') if self.events.exists() else 0

    def test_cm4_dns_and_atomic_hosts_use_only_internal_address_then_follow_chunk(self):
        others = '# operator printer\n192.0.2.5 printer.local\n'
        # CM4 br0 has .146 (mesh), .147 (internal), .2 (VIP); .3 can appear too.
        self.hosts.write_text(others + '10.30.2.2 MANET.local.\n10.30.2.3 manet.local\n')
        with self.hosts.open() as previous:
            self.shell('configure_dnsmasq 10.30.2.146 10.30.2.147 10.30.2.148 10.30.2.150')
            self.assertIn('10.30.2.2 MANET.local.', previous.read())
        self.assertEqual(self.hosts.read_text(), others + '10.30.2.147 manet.local\n')
        self.assertEqual([line for line in self.dns.read_text().splitlines() if line.startswith('address=')],
                         ['address=/manet.local/10.30.2.147', 'address=/mumble.local/10.30.2.3',
                          'address=/mtx.local/10.30.2.2'])
        self.assertIn('dhcp-range=10.30.2.148,10.30.2.150,4m', self.dns.read_text())
        self.assertEqual(self.reloads(), 1)
        before = self.hosts.stat()
        self.shell('configure_dnsmasq 10.30.2.146 10.30.2.147 10.30.2.148 10.30.2.150')
        self.assertEqual(self.hosts.stat().st_ino, before.st_ino)
        self.assertEqual(self.hosts.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(self.reloads(), 1)
        self.shell('configure_dnsmasq 10.30.2.156 10.30.2.157 10.30.2.158 10.30.2.160')
        self.assertEqual(self.hosts.read_text(), others + '10.30.2.157 manet.local\n')
        self.assertIn('address=/manet.local/10.30.2.157', self.dns.read_text())
        self.assertEqual(self.reloads(), 2)
        self.assertFalse(list(self.hosts.parent.glob('hosts.*')))

    def test_unknown_invalid_mesh_or_vip_gateway_withdraws_instead_of_publishing(self):
        for address in ('', 'bad', '::1', '10.30.2.146', '10.30.2.2', '10.30.2.3',
                        '127.0.0.1', '10.30.2.256', '0.0.0.0'):
            with self.subTest(address=address):
                self.hosts.write_text('# keep\n10.30.2.147 manet.local\n')
                self.shell('configure_dnsmasq 10.30.2.146 ' + shlex.quote(address) +
                           ' 10.30.2.148 10.30.2.150', expected=1)
                self.assertEqual(self.hosts.read_text(), '# keep\n')
                self.assertFalse(self.dns.exists())
        before = self.reloads()
        self.shell('update_avahi_host ""')
        self.assertEqual(self.reloads(), before)

    def test_unchanged_dns_still_repairs_missing_static_entry_without_restart(self):
        branch = re.search(r'                if \[ "\$NEEDS_DNSMASQ_UPDATE" = true \]; then.*?                fi',
                           self.source, re.S)[0]
        self.shell('NEEDS_DNSMASQ_UPDATE=false\nBR0_PRIMARY=10.30.2.146\nBR0_SECONDARY=10.30.2.147\n'
                   'DHCP_START=10.30.2.148\nDHCP_END=10.30.2.150\n' + branch)
        self.assertEqual(self.hosts.read_text(), '10.30.2.147 manet.local\n')
        self.assertEqual(self.reloads(), 1)
        self.assertFalse(self.dns.exists())
        self.assertNotIn('restart', self.events.read_text())

    def test_retired_generated_dns_aliases_trigger_reconfiguration_once(self):
        self.shell('configure_dnsmasq 10.30.2.146 10.30.2.147 10.30.2.148 10.30.2.150')
        self.dns.write_text(self.dns.read_text() + 'address=/old-dashboard.local/10.30.2.147\n')
        start = self.source.index('                if [ ! -f "$DNSMASQ_CONF" ]; then')
        end = self.source.index('\n\n                # The web UI', start)
        branch = self.source[start:end]
        body = ('DNSMASQ_CONF="$TEST_ROOT/dnsmasq/mesh-eud.conf"\nNEEDS_DNSMASQ_UPDATE=false\n'
                'BR0_PRIMARY=10.30.2.146\nBR0_SECONDARY=10.30.2.147\n'
                'DHCP_START=10.30.2.148\nDHCP_END=10.30.2.150\n' + branch)
        self.shell(body)
        self.assertNotIn('old-dashboard', self.dns.read_text())
        before = self.dns.stat()
        self.shell(body)
        self.assertEqual(self.dns.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(self.reloads(), 1)

    def test_release_withdraws_and_inactive_avahi_reads_hosts_when_started(self):
        (self.root / 'inactive').touch()
        self.shell('update_avahi_host 10.30.2.147 10.30.2.148')
        self.assertEqual(self.hosts.read_text(), '10.30.2.147 manet.local\n')
        self.assertEqual(self.reloads(), 0)
        (self.root / 'inactive').unlink()
        self.shell('release_control_ips')
        self.assertEqual(self.hosts.read_text(), '')
        self.assertEqual(self.reloads(), 1)

    def test_failed_reload_is_retried_without_rewriting_hosts(self):
        (self.root / 'fail-reload').touch()
        self.shell('update_avahi_host 10.30.2.147 10.30.2.148', expected=1)
        self.assertTrue(self.pending.exists())
        before = self.hosts.stat()
        (self.root / 'fail-reload').unlink()
        self.shell('update_avahi_host 10.30.2.147 10.30.2.148')
        self.assertEqual(self.reloads(), 2)
        self.assertEqual(self.hosts.stat().st_ino, before.st_ino)
        self.assertFalse(self.pending.exists())


spec = importlib.util.spec_from_file_location('mesh_census', TOOLS / 'manet-mesh-census.py')
census = importlib.util.module_from_spec(spec)
spec.loader.exec_module(census)


class CensusTests(unittest.TestCase):
    def frame(self, family=4, port=6969, source='02:00:00:00:00:02', fragment=0, extension=False):
        udp = struct.pack('!HHHH', 40000, port, 8, 0)
        if family == 4:
            header = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 28, 1, fragment, 64, 17, 0,
                                 socket.inet_aton('192.0.2.2'), socket.inet_aton('239.2.3.1'))
            ether = bytes.fromhex('01005e020301') + bytes.fromhex(source.replace(':', '')) + b'\x08\x00'
        else:
            ext = bytes([17, 0]) + b'\x00' * 6 if extension else b''
            if fragment:
                ext = struct.pack('!BBHI', 17, 0, fragment, 1)
            header = struct.pack('!IHBB16s16s', 6 << 28, 8 + len(ext), 44 if fragment else 0 if extension else 17, 64,
                                 ipaddress.ip_address('fe80::2').packed, ipaddress.ip_address('ff02::fb').packed) + ext
            ether = bytes.fromhex('3333000000fb') + bytes.fromhex(source.replace(':', '')) + b'\x86\xdd'
        return ether + header + udp

    def test_ipv4_ipv6_vlan_extensions_and_fragments(self):
        for family in (4, 6):
            frame = self.frame(family, extension=True)
            packet = census.decode(frame)
            self.assertEqual((packet['protocol'], packet['sport'], packet['dport']), (f'IPv{family}/UDP', 40000, 6969))
            self.assertEqual(packet['destination_kind'], 'multicast')
            tagged = frame[:12] + bytes.fromhex('8100000788a80008') + frame[12:]
            packet = census.decode(tagged)
            self.assertEqual(packet['vlan'], '7,8')
            self.assertEqual(packet['dport'], 6969)
            packet = census.decode(self.frame(family, fragment=8))
            self.assertTrue(packet['protocol'].endswith('/fragment'))
            self.assertIsNone(packet['dport'])
        for length in range(len(self.frame(6))):
            packet = census.decode(self.frame(6)[:length])
            self.assertIsNone(packet['dport'])
        padded = self.frame()[:16] + b'\x00\x14' + self.frame()[18:]
        self.assertIsNone(census.decode(padded)['dport'])  # padding after IPv4 total length
        arp = bytes.fromhex('ffffffffffff0200000000020806') + b'\x00' * 28
        self.assertEqual(census.decode(arp)['protocol'], 'ARP')
        self.assertEqual(census.decode(arp)['destination_kind'], 'broadcast')

    def test_sources_are_attributed_conservatively(self):
        packet = census.decode(self.frame())
        mac = packet['source_mac']
        self.assertEqual(census.source_port(packet, 'out', (set(), set(), {mac: {'end0'}})), 'end0(fdb)')
        self.assertEqual(census.source_port(packet, 'out', (set(), set(), {mac: {'wlan3'}})), 'wlan3(fdb)')
        self.assertEqual(census.source_port(packet, 'in', (set(), set(), {})), 'bat0')
        for ports in (set(), {'bat0'}, {'bat0', 'end0'}, {'end0', 'wlan3'}):
            self.assertEqual(census.source_port(packet, 'out', (set(), set(), {mac: ports})), 'unknown')
        self.assertEqual(census.source_port(packet, 'out', ({mac}, {'192.0.2.2'}, {})), 'local(mac/ip)')
        self.assertEqual(census.source_port(packet, 'out', ({mac}, {'192.0.2.1'}, {})), 'routed-or-unknown')
        packet['vlan'] = '7'
        self.assertEqual(census.source_port(packet, 'out', (set(), set(), {mac: {'end0'}})), 'unknown')

    def test_summary_is_bounded_and_accounts_for_both_directions(self):
        stats = census.Census(max_rows=2)
        frame, snapshot = self.frame(), (set(), set(), {})
        for direction in ('in', 'out', 'out'):
            stats.add(frame, len(frame), direction, snapshot)
        stats.add(self.frame(port=123), len(frame), 'out', snapshot)
        rows = stats.report()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['packets'], 2)
        self.assertEqual(rows[0]['direction'], 'out')
        self.assertEqual(rows[0]['destination'], '239.2.3.1')
        self.assertEqual(stats.totals['packets'], 4)
        self.assertEqual(stats.totals['overflow_packets'], 1)
        self.assertEqual(stats.totals['bytes'], 4 * len(frame))

    def test_capture_uses_only_bat0_no_transmits_and_reports_loss(self):
        from unittest.mock import MagicMock
        sock = MagicMock()
        sock.__enter__.return_value = sock
        now = [0.0]
        frame = self.frame()
        def receive(buffers, *_):
            buffers[0][:len(frame)] = frame
            now[0] += 1
            return len(frame), [], 0, ('bat0', 0, 4, 1, b'')
        sock.recvmsg_into.side_effect = receive
        sock.getsockopt.return_value = struct.pack('II', 4, 2)
        with patch.object(census.socket, 'socket', return_value=sock), \
                patch.object(census.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(census, 'source_snapshot', return_value=(set(), set(), {})):
            result = census.capture(2 / 60, 100)
        sock.bind.assert_called_once_with(('bat0', 0))
        sock.send.assert_not_called()
        sock.sendto.assert_not_called()
        self.assertEqual(result['totals']['packets'], 2)
        self.assertEqual(result['kernel_drops'], 2)
        self.assertEqual(result['seconds'], 2)


class ManagerRecoveryTests(unittest.TestCase):
    def test_failed_isolation_stops_dhcp_and_successful_retry_reuses_pool_and_leases(self):
        source = (TOOLS / 'mesh-ip-manager.sh').read_text()
        definitions = '\n'.join(re.search(r'^' + name + r'\(\) \{.*?^\}', source, re.M | re.S)[0]
                                for name in ('ensure_dhcp_isolation', 'eud_ready', 'ensure_dnsmasq_running'))
        # Execute the real steady-state branch after its pool comparison,
        # ensuring recovery is actually wired into allocation reconciliation.
        branch = re.search(r'                if \[ "\$NEEDS_DNSMASQ_UPDATE" = true \]; then.*?                fi',
                           source, re.S)[0]
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / 'pool').write_text('existing pool')
            (root / 'leases').write_text('existing leases')
            stubs = '''
log() { :; }
python3() {
    if [ "$2" = eud-ready ]; then [ "$TEST_PORT" = present ];
    else [ "$TEST_ISOLATION" = good ]; fi
}
systemctl() {
    echo "$*" >> "$TEST_ROOT/events"
    case "$1" in
        is-active) [ -f "$TEST_ROOT/running" ] ;;
        stop) rm -f "$TEST_ROOT/running" ;;
        start) touch "$TEST_ROOT/running" ;;
    esac
}
configure_dnsmasq() { echo unexpected-reconfigure; exit 99; }
update_avahi_host() { :; }
'''
            script = stubs + definitions + '\nensure_dhcp_isolation || exit 1\nNEEDS_DNSMASQ_UPDATE=false\n' + branch
            (root / 'running').touch()
            for state, port, expected in [('bad', 'present', 1), ('good', 'absent', 0),
                                           ('good', 'present', 0), ('good', 'present', 0),
                                           ('good', 'absent', 0)]:
                result = subprocess.run(['bash', '-c', script], capture_output=True, text=True,
                                        env=dict(os.environ, TEST_ROOT=scratch, TEST_ISOLATION=state,
                                                 TEST_PORT=port))
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertEqual((root / 'running').exists(), state == 'good' and port == 'present')
                self.assertEqual((root / 'pool').read_text(), 'existing pool')
                self.assertEqual((root / 'leases').read_text(), 'existing leases')
            events = (root / 'events').read_text()
            self.assertEqual(events.count('stop dnsmasq.service'), 2)
            self.assertEqual(events.count('start dnsmasq.service'), 1)


if __name__ == '__main__':
    unittest.main()
