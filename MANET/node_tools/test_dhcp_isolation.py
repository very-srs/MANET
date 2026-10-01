"""DHCP policy verification against JSON captured from nft 1.1.3 on CM4."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
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

    def test_real_kernel_output_and_changing_counters_are_accepted(self):
        self.assertTrue(isolation.valid_rules(self.rules))
        for item in self.rules['nftables']:
            for expr in item.get('rule', {}).get('expr', []):
                if 'counter' in expr:
                    expr['counter'] = {'packets': 123, 'bytes': 45678}
        self.assertTrue(isolation.valid_rules(self.rules))

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
                patch.object(sys, 'argv', ['manet-dhcp-isolation.py', mode]):
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


class ManagerRecoveryTests(unittest.TestCase):
    def test_failed_isolation_stops_dhcp_and_successful_retry_reuses_pool_and_leases(self):
        source = (TOOLS / 'mesh-ip-manager.sh').read_text()
        definitions = '\n'.join(re.search(r'^' + name + r'\(\) \{.*?^\}', source, re.M | re.S)[0]
                                for name in ('ensure_dhcp_isolation', 'ensure_dnsmasq_running'))
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
