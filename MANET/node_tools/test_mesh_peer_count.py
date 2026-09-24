#!/usr/bin/env python3
"""Exercise BATMAN counts and channel decisions without touching live radios."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from test_acs import runtime


TOOLS = Path(__file__).resolve().parent
COUNTER = TOOLS / 'mesh-peer-count.py'
SPEC = importlib.util.spec_from_file_location('mesh_peer_count', COUNTER)
counter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(counter)
PEER = '0c:bf:74:00:2b:f1'
SECOND_PEER = '02:00:00:00:00:02'


class PeerHarness(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='manet-peers-test-')
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        (self.root / 'initial_time_synced').touch()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.table = self.root / 'originators.json'
        self.peers([])
        self.registry = self.root / 'registry'
        self.registry.write_text('')
        self.env = dict(
            os.environ,
            PATH=str(self.bin) + os.pathsep + str(Path(sys.executable).parent)
                 + os.pathsep + os.environ.get('PATH', ''),
            TEST_TABLE=str(self.table), TEST_BATCTL_RC='0',
            BATCTL_PATH=str(self.bin / 'batctl'),
            REGISTRY_STATE_FILE=str(self.registry),
            TEST_COUNTER=str(COUNTER), TEST_ROOT=str(self.root),
            MANET_TOOLS_DIR=str(TOOLS),
            MANET_ACS_LOCK_FILE=str(self.root / 'acs.lock'),
            MANET_ACS_RUN_DIR=str(self.root),
            MANET_IFACE_STATE_DIR=str(self.root / 'roles'),
        )
        self.command('batctl', '''
if sys.argv[1:] != ['meshif', 'bat0', 'originators_json']:
    sys.exit(99)
sys.stdout.write(Path(os.environ['TEST_TABLE']).read_text())
sys.exit(int(os.environ['TEST_BATCTL_RC']))
''')
        self.command('agreement-helper', '''
if sys.argv[1] == 'members':
    print('00:00:00:00:00:00')
elif sys.argv[1] == 'helper-encode':
    print('authenticated-helper')
''')
        self.command('systemd-cat', 'sys.stderr.write(sys.stdin.read())\n')
        self.command('date', "print('1000' if sys.argv[1:] == ['+%s'] else 'test-clock')\n")
        self.command('iw', "print('wiphy 0' if sys.argv[1] == 'dev' else '* 2412 MHz [1] (20 dBm)')\n")

    def command(self, name, body):
        path = self.bin / name
        path.write_text(f'#!{sys.executable}\nimport os, sys\nfrom pathlib import Path\n' + body)
        path.chmod(0o755)

    def peers(self, rows):
        self.table.write_text(json.dumps(rows))

    def active_nodes(self, count):
        self.registry.write_text(''.join(
            f"NODE_02000000{i:04x}_LAST_SEEN_TIMESTAMP='990'\n"
            f"NODE_02000000{i:04x}_NODE_STATE='ACTIVE'\n"
            for i in range(count)))

    def run_command(self, args):
        return subprocess.run(args, env=self.env, capture_output=True,
                              text=True, timeout=15)

    def shell(self, body):
        return self.run_command(['bash', '-c', body])

    def functions(self, script):
        marker = '# === MAIN SETUP ===' if script == 'node-manager-acs.sh' else '# === MAIN EXECUTION ==='
        source = (TOOLS / script).read_text().split(marker, 1)[0]
        # Load definitions only. Hardware/service commands in tests are stubs;
        # the production counter and its callers are executed unchanged.
        return source + '''
BATCTL_PATH="$TEST_ROOT/bin/batctl"
PEER_COUNTER="$TEST_COUNTER"
REGISTRY_STATE_FILE="$TEST_ROOT/registry"
AGREEMENT_TOOL="$TEST_ROOT/bin/agreement-helper"
log() { echo "$1" >&2; }
'''


class PeerCountTests(PeerHarness):
    def count(self):
        return self.run_command([sys.executable, str(COUNTER), '--batctl', self.env['BATCTL_PATH']])

    def test_empty_single_and_multiple_originators(self):
        for rows, expected in [([], 0), ([{'orig_address': PEER, 'best': True}], 1),
                               ([{'orig_address': PEER, 'best': True},
                                 {'orig_address': SECOND_PEER, 'best': True}], 2)]:
            with self.subTest(rows=rows):
                self.peers(rows)
                result = self.count()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, f'{expected}\n')

    def test_multiple_routes_interfaces_and_mac_case_count_once(self):
        self.peers([
            {'orig_address': PEER, 'best': True, 'hard_ifname': 'wlan2'},
            {'orig_address': PEER.upper(), 'best': False, 'hard_ifname': 'wlan0'},
            {'orig_address': SECOND_PEER, 'best': True, 'hard_ifname': 'halow0'},
            {'orig_address': SECOND_PEER, 'best': False, 'hard_ifname': 'end0'},
        ])
        self.assertEqual(self.count().stdout, '2\n')

    def test_failed_or_malformed_query_never_prints_a_count(self):
        cases = [('[]', '1'), ('', '0'), ('not-json', '0'), ('{}', '0'),
                 ('[{}]', '0'), ('[null]', '0'), ('[{"orig_address": null}]', '0'),
                 ('[{"orig_address": "bad-mac"}]', '0'),
                 (json.dumps([{'orig_address': PEER}, {}]), '0')]
        for output, status in cases:
            with self.subTest(output=output, status=status):
                self.table.write_text(output)
                self.env['TEST_BATCTL_RC'] = status
                result = self.count()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')
                self.assertIn('Cannot read BATMAN peers', result.stderr)

    def test_missing_batctl_never_prints_a_count(self):
        self.env['BATCTL_PATH'] = str(self.bin / 'missing')
        result = self.count()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')

    def test_query_has_a_five_second_timeout(self):
        with patch.object(counter.subprocess, 'run', side_effect=subprocess.TimeoutExpired('batctl', 5)) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                counter.peer_count('/test/batctl')
            self.assertEqual(run.call_args.kwargs['timeout'], 5)


class QuorumTests(PeerHarness):
    def quorum(self):
        return self.run_command(['bash', str(TOOLS / 'quorum-checker.sh')])

    def test_empty_registry_is_numeric_and_stays_put(self):
        result = self.quorum()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Originators=0, Active=0', result.stderr)
        self.assertNotIn('integer expression', result.stderr)

    def test_isolation_one_peer_and_small_island(self):
        self.active_nodes(9)
        for count, status in [(0, 1), (1, 1), (2, 0)]:
            with self.subTest(count=count):
                # Alternate routes must not turn a one-peer island into a
                # two-peer island that quorum would allow to stay put.
                rows = [{'orig_address': mac, 'best': best}
                        for mac in [PEER, SECOND_PEER][:count] for best in [True, False]]
                self.peers(rows)
                result = self.quorum()
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertIn(f'Originators={count}', result.stderr)

    def test_query_failure_is_distinct_from_isolation(self):
        self.active_nodes(9)
        for output, status in [('[]', '1'), ('bad-json', '0'), ('[{}]', '0')]:
            with self.subTest(output=output, status=status):
                self.table.write_text(output)
                self.env['TEST_BATCTL_RC'] = status
                result = self.quorum()
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn('SOLO ISOLATION', result.stderr)

    def test_manager_moves_only_on_confirmed_quorum_loss(self):
        self.active_nodes(9)
        source = (TOOLS / 'node-manager-acs.sh').read_text()
        stage = source.split('        # === STAGE 6: QUORUM CHECK ===\n', 1)[1]
        stage = stage.split('        # === STAGE 7.5: PARTITION MERGE', 1)[0]
        body = self.functions('node-manager-acs.sh') + '''
QUORUM_CHECKER="$TEST_ROOT/bin/quorum"
return_to_lobby() { echo RETURN_TO_LOBBY; }
for pass in 1; do
''' + stage + '\ndone\n'
        # Execute the real quorum script through the manager's actual caller.
        (self.bin / 'quorum').symlink_to(TOOLS / 'quorum-checker.sh')
        # Its sibling counter is resolved beside the invoked path.
        (self.bin / 'mesh-peer-count.py').symlink_to(COUNTER)
        for output, status, moves in [('[]', '0', True), ('[]', '1', False),
                                      ('not-json', '0', False),
                                      (json.dumps([{'orig_address': PEER},
                                                   {'orig_address': SECOND_PEER}]), '0', False)]:
            with self.subTest(output=output, status=status):
                self.table.write_text(output)
                self.env['TEST_BATCTL_RC'] = status
                result = self.shell(body)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual('RETURN_TO_LOBBY' in result.stdout, moves, result.stderr)


class LobbyTests(PeerHarness):
    def bootstrap(self, now=600, start=-1):
        return self.shell(self.functions('node-manager-acs.sh') + f'''
NOW={now}
BOOTSTRAPPING=false
BOOTSTRAP_START_WINDOW={start}
update_lobby_bootstrap
echo "$BOOTSTRAPPING $BOOTSTRAP_START_WINDOW $LOBBY_SOLO_LOGGED"
''')

    def test_solo_stays_at_lobby_and_one_peer_allows_bootstrap(self):
        self.assertEqual(self.bootstrap().stdout, 'false -1 true\n')
        self.peers([{'orig_address': PEER, 'best': True}])
        self.assertEqual(self.bootstrap().stdout, 'true 3 false\n')
        # A continuing bootstrap keeps its original start window.
        self.assertEqual(self.bootstrap(now=800, start=3).stdout, 'true 3 false\n')

    def test_lost_peer_resets_bootstrap(self):
        self.assertEqual(self.bootstrap(start=2).stdout, 'false -1 true\n')

    def test_failed_query_defers_without_claiming_solo_then_restarts(self):
        for output, status in [('[]', '1'), ('bad-json', '0')]:
            with self.subTest(output=output, status=status):
                self.table.write_text(output)
                self.env['TEST_BATCTL_RC'] = status
                result = self.bootstrap(start=2)
                self.assertEqual(result.stdout, 'false -1 false\n')
                self.assertIn('Deferring lobby bootstrap', result.stderr)
                self.assertNotIn('Solo in discovery', result.stderr)
        self.env['TEST_BATCTL_RC'] = '0'
        self.peers([{'orig_address': PEER}])
        self.assertEqual(self.bootstrap(now=1000).stdout, 'true 5 false\n')


class PartitionTests(PeerHarness):
    def test_partition_comparison_uses_unique_peers_plus_self(self):
        self.peers([{'orig_address': PEER, 'best': True},
                    {'orig_address': PEER.upper(), 'best': False}])
        result = self.shell(self.functions('tourguide-manager.sh') + '\nget_partition_size\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        size = int(result.stdout)
        self.assertEqual(size, 2)
        local = {'boot': 'a' * 32, 'acs': True, 'ready': True,
                 'current': {'2.4': 2437}, 'allowed': {'2.4': [2437, 2462]}}
        for foreign_size, mac, moves in [(3, '02:00:00:00:00:04', True),
                                         (1, '02:00:00:00:00:01', False),
                                         (2, '02:00:00:00:00:01', True),
                                         (2, '02:00:00:00:00:04', False)]:
            with self.subTest(size=foreign_size, mac=mac):
                selected = runtime.recovery_destination(
                    {mac: {'channels': {'2.4': 2462}, 'size': foreign_size}}, local,
                    1000, '02:00:00:00:00:03', (), size)
                self.assertEqual(selected is not None, moves)

    def test_size_counts_self_once_and_deduplicates_routes(self):
        for peers in [[], [PEER], [PEER, SECOND_PEER]]:
            with self.subTest(peers=peers):
                self.peers([{'orig_address': mac, 'best': best}
                            for mac in peers for best in [True, False]])
                result = self.shell(self.functions('tourguide-manager.sh') + '\nget_partition_size\n')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, f'{len(peers) + 1}\n')

    def test_failed_query_has_no_partition_size(self):
        self.env['TEST_BATCTL_RC'] = '1'
        result = self.shell(self.functions('tourguide-manager.sh') + '\nget_partition_size\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')

    def test_invalid_pre_hop_size_cannot_create_a_partition_merge(self):
        for size in ['', '0', 'bad']:
            with self.subTest(size=size):
                self.env['TEST_SIZE'] = size
                result = self.shell(self.functions('tourguide-manager.sh') + '''
CONTROL_IFACE=lo
ELECTION_OUTPUT_FILE="$TEST_ROOT/election"
analyze_partition_data '02:00:00:00:00:01 foreign-beacon' 2437 5200 "$TEST_SIZE"
''')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Deferring partition comparison', result.stderr)
                self.assertFalse((self.root / 'election').exists())

    def test_failed_size_query_stops_before_beacon_encoding_or_hop(self):
        source = (TOOLS / 'tourguide-manager.sh').read_text()
        main = source.split('# === MAIN EXECUTION ===\n', 1)[1]
        body = self.functions('tourguide-manager.sh') + '''
CONTROL_IFACE=lo
load_mesh_roles() { WPA_IFACE_2_4=wlan0; WPA_IFACE_5_0=wlan1; }
acs_configs_ready() { return 0; }
acs_discovery_state() { echo false; }
get_current_freq() { echo 2437; }
date() { echo 750; }  # First 2.4 GHz slot, distinct from the data channel.
elect_tourguide() { echo "$1"; }
ENCODER_PATH="$TEST_ROOT/bin/encoder"
hop_to_lobby_frequency() { echo HOP; exit 0; }
hop_to_data_frequency() { :; }
''' + main
        self.command('encoder', '''
Path(os.environ['TEST_ROOT'], 'encoded').write_text(' '.join(sys.argv[1:]))
print('helper-beacon')
''')
        for output, status in [('[]', '1'), ('[{}]', '0')]:
            with self.subTest(output=output, status=status):
                self.table.write_text(output)
                self.env['TEST_BATCTL_RC'] = status
                result = self.shell(body)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Skipping tourguide hop', result.stderr)
                self.assertNotIn('HOP', result.stdout)
                self.assertFalse((self.root / 'encoded').exists())
        self.env['TEST_BATCTL_RC'] = '0'
        self.peers([{'orig_address': PEER}, {'orig_address': PEER.upper()}])
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('HOP', result.stdout)
        self.assertIn('--partition-size 2', (self.root / 'encoded').read_text())

    def test_hop_uses_original_size_without_requerying_lobby_topology(self):
        # After hopping the table may contain lobby visitors or lose data
        # peers. The advertised pre-hop size remains the comparison baseline.
        self.command('batctl', '''
if sys.argv[1:] != ['meshif', 'bat0', 'originators_json']:
    sys.exit(99)
state = Path(os.environ['TEST_ROOT'], 'queried')
if state.exists():
    sys.exit(1)
state.touch()
print('[]')
''')
        self.command('encoder', "print('helper-beacon')\n")
        source = (TOOLS / 'tourguide-manager.sh').read_text()
        main = source.split('# === MAIN EXECUTION ===\n', 1)[1].replace(
            '/var/run/tourguide_state', str(self.root / 'tourguide_state'))
        body = self.functions('tourguide-manager.sh') + '''
CONTROL_IFACE=lo
load_mesh_roles() { WPA_IFACE_2_4=wlan0; WPA_IFACE_5_0=wlan1; }
acs_configs_ready() { return 0; }
acs_discovery_state() { echo false; }
get_current_freq() { echo 2437; }
date() { echo 750; }
elect_tourguide() { echo "$1"; }
ENCODER_PATH="$TEST_ROOT/bin/encoder"
ELECTION_OUTPUT_FILE="$TEST_ROOT/election"
hop_to_lobby_frequency() { echo HOP_TO_LOBBY; date() { echo 1080; }; }
hop_to_data_frequency() { echo RETURN_TO_DATA; }
sleep() { :; }
alfred() {
    if [ "$1" = -r ]; then
        echo '{ "02:00:00:00:00:01", "foreign-beacon" },'
    else
        cat >/dev/null
    fi
}
''' + main
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'HOP_TO_LOBBY\nRETURN_TO_DATA\n')
        self.assertNotIn('Cannot read BATMAN peers', result.stderr)
        self.assertFalse((self.root / 'election').exists())


if __name__ == '__main__':
    unittest.main()
