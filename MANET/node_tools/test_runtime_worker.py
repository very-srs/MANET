"""Resident helper requests and invalidation, entirely against local fixtures."""
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

import manet_election_runtime as election
import manet_interfaces as interfaces
import manet_runtime as runtime
from manet_ids import bytes_to_syncthing_id
from manet_ip_runtime import module

TOOLS = Path(__file__).resolve().parent


class RequestTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.directory = self.root / 'runtime'
        self.stop = threading.Event()

    def start(self, handler):
        thread = threading.Thread(target=runtime.serve, args=(self.directory, handler, self.stop))
        thread.start()
        def finish():
            self.stop.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.addCleanup(finish)
        deadline = time.monotonic() + 2
        while not (self.directory / 'ready').exists():
            self.assertTrue(thread.is_alive())
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.005)

    def call(self, action='echo', argument='-', wait='2'):
        return subprocess.run(['bash', '-c', f'. "{TOOLS}/manet-runtime-client.sh"; manet_runtime_call "$@"',
                               'request', action, argument], capture_output=True, text=True, timeout=4,
                              env=dict(os.environ, MANET_RUNTIME_DIR=str(self.directory), MANET_RUNTIME_WAIT=wait))

    def test_unavailable_returns_fallback_status_without_a_request(self):
        self.assertEqual(self.call().returncode, 125)
        self.assertFalse(self.directory.exists())

    def test_every_production_operation_crosses_the_wire_including_ipv4(self):
        # The old [a-z-]+ framing silently discarded ipv4 before dispatch.
        replies = {'ip': '', 'ipv4': '10.30.2.146', 'syncthing': 'certificate-id',
                   'interfaces': '[]', 'mcs': "WLAN2_TX_MCS=''", 'ap-mesh': '{}',
                   'election': 'skip'}
        handler = Mock()
        handler.dispatch.side_effect = lambda action, argument: (0, replies[action])
        self.start(handler)
        for action, output in replies.items():
            with self.subTest(action=action):
                argument = {'mcs': 'wlan2', 'election': 'mediamtx'}.get(action, '-')
                reply = self.call(action, argument, wait='.2')
                self.assertEqual(reply.returncode, 0, reply.stderr)
                self.assertEqual(reply.stdout, output)
        self.assertEqual([call.args[0] for call in handler.dispatch.call_args_list], list(replies))

    def test_required_payload_cannot_be_empty_success(self):
        handler = Mock()
        handler.dispatch.return_value = (0, '')
        self.start(handler)
        for action in ('ipv4', 'mcs', 'interfaces', 'ap-mesh', 'election'):
            with self.subTest(action=action):
                self.assertEqual(self.call(action).returncode, 1)
        for action in ('ip', 'syncthing'):
            self.assertEqual(self.call(action).returncode, 0)

    def test_malformed_request_with_reply_token_is_rejected_promptly(self):
        handler = Mock()
        self.start(handler)
        self.assertEqual(self.call('invalid/action', wait='.2').returncode, 2)
        handler.dispatch.assert_not_called()

    def test_repeated_requests_use_the_same_resident_handler(self):
        calls = []
        def dispatch(action, arg):
            calls.append((action, arg, os.getpid()))
            return 0, f'value {arg}\nsecond line'
        self.start(types.SimpleNamespace(dispatch=dispatch))
        replies = [self.call(argument=str(i)) for i in range(12)]
        self.assertEqual(len(calls), 12)
        for i, reply in enumerate(replies):
            self.assertEqual(reply.returncode, 0, reply.stderr)
            self.assertEqual(reply.stdout, f'value {i}\nsecond line')
        self.assertEqual({pid for _, _, pid in calls}, {os.getpid()})
        self.assertEqual(len(list(self.directory.glob('result.*'))), 1)

    def test_failure_and_timeout_do_not_authorize_replaying_an_action(self):
        calls = []
        def dispatch(action, arg):
            calls.append(action)
            if action == 'slow':
                time.sleep(.1)
            return 7, ''
        self.start(types.SimpleNamespace(dispatch=dispatch))
        self.assertEqual(self.call('fail').returncode, 7)
        self.assertEqual(self.call('slow', wait='.02').returncode, 124)
        # Busy callers fall back BEFORE submitting; no new queue delay.
        self.assertEqual(self.call('busy').returncode, 125)
        deadline = time.monotonic() + 2
        while not (self.directory / 'ready').exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.005)
        # The next request drains the old reply without mistaking it for its own.
        self.assertEqual(self.call('again').returncode, 7)
        self.assertEqual(calls, ['fail', 'slow', 'again'])

    def test_handler_exception_is_reported_and_next_request_still_works(self):
        handler = Mock()
        handler.dispatch.side_effect = [ValueError('injected'), (125, ''), (0, 'recovered')]
        self.start(handler)
        self.assertEqual(self.call().returncode, 1)
        self.assertEqual(self.call().returncode, 1)  # A submitted 125 cannot cause replay.
        self.assertEqual(self.call().stdout, 'recovered')

    def test_nonprivate_runtime_directory_is_rejected(self):
        self.directory.mkdir(mode=0o755)
        with self.assertRaises(PermissionError):
            runtime.serve(self.directory, Mock(), self.stop)


class CertificateTests(unittest.TestCase):
    def test_missing_retries_and_unchanged_cert_reuses_decode_then_replacement_invalidates(self):
        with tempfile.TemporaryDirectory() as temporary:
            cert = Path(temporary) / 'cert.pem'
            helpers = runtime.Helpers()
            with patch.dict(os.environ, MANET_SYNCTHING_CERT=str(cert)):
                self.assertEqual(helpers.syncthing_id(), '')
                first, second = b'certificate fixture one', b'certificate fixture two'
                cert.write_text(ssl.DER_cert_to_PEM_cert(first))
                self.assertEqual(helpers.syncthing_id(), bytes_to_syncthing_id(hashlib.sha256(first).digest()))
                with patch.object(ssl, 'PEM_cert_to_DER_cert', side_effect=AssertionError('decoded unchanged cert')):
                    self.assertEqual(helpers.syncthing_id(), helpers.cert_cache[1])
                replacement = cert.with_suffix('.new')
                replacement.write_text(ssl.DER_cert_to_PEM_cert(second))
                os.replace(replacement, cert)
                self.assertEqual(helpers.syncthing_id(), bytes_to_syncthing_id(hashlib.sha256(second).digest()))
                cert.unlink()
                self.assertEqual(helpers.syncthing_id(), '')


class TelemetryTests(unittest.TestCase):
    def test_collector_errors_drop_partial_output_and_previous_mcs_peer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'wlan2').mkdir()
            source = (TOOLS / 'manet-common.sh').read_text()
            collector = re.search(r'^collect_radio_mcs\(\) \{.*?^\}', source, re.M | re.S)[0]
            collector = collector.replace('/sys/class/net', str(root))
            body = f'. "{TOOLS}/manet-common.sh"\n' + collector + '''
manet_runtime_call() { printf 'partial'; return 1; }
HALOW_MCS_SUMMARY=/bin/true
WLAN2_MCS_PEER=old-peer
WLAN2_TX_MCS=old-rate
collect_radio_mcs
printf 'peer=<%s> rate=<%s>\\n' "$WLAN2_MCS_PEER" "$WLAN2_TX_MCS"
collect_interfaces_json
'''
            result = subprocess.run(['bash', '-c', body], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, 'peer=<> rate=<>\n[]\n')

    def test_lightweight_interfaces_preserve_roles_addresses_and_live_morse_fields(self):
        raw = [dict(ifname='wlan0', operstate='UP', addr_info=[dict(family='inet', local='10.0.0.6'),
                                                              dict(family='inet6', local='fe80::1')]),
               dict(ifname='wlan1', operstate='DOWN'), dict(ifname='wlan2', operstate='UP'),
               dict(ifname='br0', operstate='UP')]
        iw = '\n\tInterface wlan0\n\t\ttype mesh point\n\t\tchannel 1 (2412 MHz)\n\t\ttxpower 20.00 dBm\n'
        iw += '\tInterface wlan1\n\t\ttype AP\n\tInterface wlan2\n\t\ttype mesh point\n'
        def command(*args):
            return {('ip', '-j', 'addr'): json.dumps(raw), ('iw', 'dev'): iw,
                    ('batctl', 'if'): 'wlan0: active\nwlan2: inactive\n',
                    ('ethtool', '-i', 'wlan0'): 'driver: mt76',
                    ('ethtool', '-i', 'wlan1'): 'driver: mt76',
                    ('ethtool', '-i', 'wlan2'): 'driver: morse_usb'}[args]
        with tempfile.TemporaryDirectory() as temporary, patch.object(interfaces, 'command', side_effect=command), \
                patch('manet_radio.get_halow_driver_info', return_value=dict(channel=9, freq_mhz=905, halow_bw=4)):
            rows = interfaces.collect(Path(temporary), Path(temporary) / 'no_mesh')
        self.assertEqual([row['role'] for row in rows], ['mesh', 'ap', 'mesh'])
        self.assertEqual(rows[0]['ipv4'], ['10.0.0.6'])
        self.assertEqual(rows[0]['txpower_dbm'], '20.00')
        self.assertEqual(rows[2]['freq_mhz'], '905')
        self.assertEqual(rows[2]['halow_bw'], '4')

    def test_legacy_dump_entry_exits_before_importing_web_server(self):
        code = '''import runpy, sys, types
sys.modules['manet_interfaces'] = types.SimpleNamespace(collect=lambda: [])
sys.argv = ['mesh-status.py', '--dump-interfaces']
try: runpy.run_path(sys.argv[0], run_name='__main__')
except SystemExit as e: assert e.code == 0
assert 'manet_manage' not in sys.modules
assert 'http.server' not in sys.modules
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=TOOLS, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '[]\n')

    def test_mcs_is_sampled_on_every_request_and_shell_output_is_quoted(self):
        helper = module('halow-mcs-summary')
        first = 'Station 02:00:00:00:00:01 (on wlan2)\n\ttx bitrate: 4.0 MBit/s MCS 2\n'
        with patch.object(helper.subprocess, 'run', side_effect=[types.SimpleNamespace(stdout=first),
                                                               types.SimpleNamespace(stdout='')]):
            self.assertEqual(helper.collect('wlan2')['tx_mcs'], 'MCS2')
            self.assertEqual(helper.collect('wlan2')['peer_count'], 0)
        self.assertIn("WLAN2_TX_MCS='MCS2 N1'", helper.shell_output(dict(iface='wlan2', tx_mcs='MCS2 N1',
                      rx_mcs='', peer_mac='', signal_dbm='', peer_count=1)))


class ElectionTests(unittest.TestCase):
    def test_resident_check_rechecks_inputs_without_rederiving_unchanged_vip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            own = '02:00:00:00:00:01'
            files = {'/var/run/mesh_node_registry': "\n".join(
                f"NODE_020000000001_{key}='{value}'" for key, value in
                dict(MAC_ADDRESS=own, MEAN_THROUGHPUT_MBPS='5', NODE_STATE='ACTIVE',
                     OBSERVED_AT_UPTIME='100').items()),
                '/proc/uptime': '400 0\n', '/sys/class/net/br0/address': own,
                '/etc/radvd-mesh.conf': 'prefix fd00::/48 {}'}
            paths = {name: root / str(i) for i, name in enumerate(files)}
            for name, value in files.items():
                paths[name].write_text(value)
            (root / 'mtx-ip.sh').write_text('fixture helper')
            conf = {'ipv4_network': '10.30.0.0/24'}
            live = ['10.30.0.2', 'fd00::2']
            state = ['active']
            commands = []
            def command(args, **kwargs):
                commands.append(args)
                output = {'bash': 'fd00::2/128', 'systemctl': state[0],
                          'ip': json.dumps([dict(addr_info=[dict(local=a) for a in live])])}[args[0]]
                return subprocess.CompletedProcess(args, 0, output)
            election.ipv6_vip.cache_clear()
            self.addCleanup(election.ipv6_vip.cache_clear)
            with patch.object(election, 'Path', side_effect=lambda name: paths[str(name)]), \
                    patch.object(election, 'TOOLS', root), \
                    patch.object(election, 'read_values', return_value=conf), \
                    patch.object(election.subprocess, 'run', side_effect=command):
                self.assertEqual(election.check('mediamtx'), 'skip')
                self.assertEqual(election.check('mediamtx'), 'skip')
                self.assertEqual(sum(args[0] == 'bash' for args in commands), 1)
                paths['/proc/uptime'].write_text('401 0\n')
                self.assertEqual(election.check('mediamtx'), '- - -')
                paths['/proc/uptime'].write_text('400 0\n')
                state[0] = 'failed'
                self.assertNotEqual(election.check('mediamtx'), 'skip')
                state[0] = 'active'
                live.pop()
                self.assertNotEqual(election.check('mediamtx'), 'skip')
                live.append('fd00::2')
                conf['ipv4_network'] = '10.40.0.0/24'
                self.assertNotEqual(election.check('mediamtx'), 'skip')
                paths['/etc/radvd-mesh.conf'].write_text('prefix fd01::/48 {}')
                election.check('mediamtx')
                self.assertEqual(sum(args[0] == 'bash' for args in commands), 2)

    def test_live_vip_loss_service_failure_and_winner_change_require_reconciliation(self):
        own, other = '02:00:00:00:00:01', '02:00:00:00:00:02'
        args = (own, own, '10.30.0.2', 'fd00::2')
        live = {'10.30.0.2', 'fd00::2'}
        self.assertTrue(election.settled(*args, live, 'active'))
        self.assertFalse(election.settled(*args, {'10.30.0.2'}, 'active'))
        self.assertFalse(election.settled(*args, live, 'failed'))
        self.assertFalse(election.settled(other, *args[1:], live, 'active'))
        self.assertTrue(election.settled(other, *args[1:], set(), 'inactive'))
        self.assertFalse(election.settled(other, *args[1:], set(), 'failed'))

    def test_identical_registry_still_expires_on_the_current_boot_clock(self):
        helper = module('mesh-service-election')
        data = "\n".join(f"NODE_020000000001_{key}='{value}'" for key, value in
                         dict(MAC_ADDRESS='02:00:00:00:00:01', MEAN_THROUGHPUT_MBPS='5',
                              NODE_STATE='ACTIVE', OBSERVED_AT_UPTIME='100').items())
        self.assertEqual(helper.elect('mediamtx', data, 400)[0], '02:00:00:00:00:01')
        with patch.object(helper, 'shlex') as shlex:
            self.assertIsNone(helper.elect('mediamtx', data, 401)[0])
            shlex.split.assert_not_called()


if __name__ == '__main__':
    unittest.main()
