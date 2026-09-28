"""Run the firewall script against a transaction-recording nft stand-in."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class FirewallTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.pool = self.root / 'dnsmasq.conf'
        self.pool.write_text('dhcp-range=10.30.1.10,10.30.1.19,12h\n')
        (self.root / 'mesh.conf').write_text('ipv4_network=10.30.0.0/16\n')
        self.state = self.root / 'state'
        self.table = self.root / 'table'
        self.calls = self.root / 'calls'
        nft = self.root / 'nft'
        nft.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root = Path(os.environ['TEST_ROOT'])
args = sys.argv[1:]
if args == ['list', 'table', 'inet', 'manet_ui']:
    sys.exit(0 if (root / 'table').exists() else 1)
assert args == ['-f', '-'], args
rules = sys.stdin.read()
with (root / 'calls').open('a') as out:
    out.write(json.dumps(rules) + '\\n')
if (root / 'fail').exists():
    sys.exit(1)
(root / 'table').write_text(rules)
''')
        nft.chmod(0o755)
        self.env = dict(os.environ, TEST_ROOT=str(self.root), NFT=str(nft),
                        MANET_DNSMASQ_CONF=str(self.pool),
                        MANET_MESH_CONF=str(self.root / 'mesh.conf'),
                        MANET_UI_FW_STATE=str(self.state))

    def run_script(self):
        return subprocess.run(['bash', str(Path(__file__).with_name('manet-ui-firewall.sh'))],
                              env=self.env, capture_output=True, text=True, timeout=15)

    def test_complete_ruleset_in_one_transaction_and_idempotent(self):
        self.assertEqual(self.run_script().returncode, 0)
        transactions = self.calls.read_text().splitlines()
        self.assertEqual(len(transactions), 1)
        rules = json.loads(transactions[0])
        self.assertIn('iifname lo accept', rules)
        self.assertIn('tcp dport 80 ip saddr 10.30.1.10-10.30.1.19 accept', rules)
        self.assertIn('tcp dport 80 drop', rules)
        self.assertIn('tcp dport 5201 ip saddr 10.30.0.0/16 accept', rules)
        self.assertIn('tcp dport 5201 drop', rules)
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.calls.read_text().splitlines(), transactions)

    def test_failed_replacement_preserves_table_and_state_then_retries(self):
        self.assertEqual(self.run_script().returncode, 0)
        before = self.table.read_bytes(), self.state.read_bytes()
        self.pool.write_text('dhcp-range=10.30.2.10,10.30.2.19,12h\n')
        (self.root / 'fail').touch()
        self.assertNotEqual(self.run_script().returncode, 0)
        self.assertEqual((self.table.read_bytes(), self.state.read_bytes()), before)
        (self.root / 'fail').unlink()
        self.assertEqual(self.run_script().returncode, 0)
        self.assertIn('10.30.2.10-10.30.2.19', self.table.read_text())

    def test_failed_first_install_has_no_success_marker(self):
        (self.root / 'fail').touch()
        self.assertNotEqual(self.run_script().returncode, 0)
        self.assertFalse(self.state.exists())

    def test_missing_table_is_rebuilt_despite_matching_marker(self):
        self.assertEqual(self.run_script().returncode, 0)
        self.table.unlink()
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)

    def test_missing_pool_leaves_existing_rules_alone(self):
        self.assertEqual(self.run_script().returncode, 0)
        before = self.table.read_bytes(), self.state.read_bytes()
        self.pool.unlink()
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual((self.table.read_bytes(), self.state.read_bytes()), before)


if __name__ == '__main__':
    unittest.main()
