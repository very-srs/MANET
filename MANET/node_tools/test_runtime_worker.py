"""Resident helper requests and invalidation, entirely against local fixtures."""
import fcntl
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
                   'election': 'skip', 'hosting': 'true',
                   'service-vip': 'fd00::2/128'}
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
        for action in ('ipv4', 'mcs', 'interfaces', 'ap-mesh', 'election',
                       'hosting', 'service-vip'):
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

    def test_operator_reason_reaches_caller_but_other_exception_text_does_not(self):
        handler = Mock()
        handler.dispatch.side_effect = [
            RuntimeError('Radio transition busy;\nretry shortly'),
            subprocess.CalledProcessError(1, ['wpa_cli', 'set_network', '0', 'psk', 'secret']),
            RuntimeError(''),
        ]
        self.start(handler)
        busy, command, empty = self.call(), self.call(), self.call()
        self.assertEqual((busy.returncode, busy.stdout), (1, ''))
        self.assertEqual(busy.stderr, 'Radio transition busy; retry shortly\n')
        self.assertEqual(command.stderr, 'CalledProcessError\n')
        self.assertNotIn('secret', command.stderr)
        self.assertEqual(empty.stderr, 'RuntimeError\n')

    def test_service_observer_waits_only_to_its_lock_deadline(self):
        """Busy reads have a bounded wait and submit no request on expiry."""
        handler = Mock()
        handler.dispatch.return_value = (0, 'skip')
        self.start(handler)
        with (self.directory / 'client.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with patch.dict(os.environ, MANET_RUNTIME_LOCK_WAIT='.05'):
                before = time.monotonic()
                self.assertEqual(self.call('election', 'mumble').returncode,
                                 125)
                elapsed = time.monotonic() - before
                self.assertGreaterEqual(elapsed, .05)
                self.assertLess(elapsed, 1)
        handler.dispatch.assert_not_called()

    def test_nonprivate_runtime_directory_is_rejected(self):
        self.directory.mkdir(mode=0o755)
        with self.assertRaises(PermissionError):
            runtime.serve(self.directory, Mock(), self.stop)


class ServiceShellTests(unittest.TestCase):
    """Keep service wrappers on the runtime's no-replay contract."""

    def test_fallback_only_when_no_request_was_submitted(self):
        """Only 125 permits the one-shot telemetry or VIP helper to run."""
        script = r'''
. "$MANET_TOOLS_DIR/manet-service-common.sh"
manet_runtime_call() { return "$RESULT"; }
python3() { printf 'python3 %s\n' "$*" >&2; printf 'true\n'; }
vip_helper() { printf 'fd00::2/128\n'; }
service_election_preflight mediamtx; printf 'preflight=%s\n' "$?"
service_election_hosting mumble; printf 'hosting=%s\n' "$?"
service_election_ipv6 mumble vip_helper >/dev/null
printf 'vip=%s\n' "$?"
'''
        for rc in (125, 1, 124):
            with self.subTest(status=rc):
                result = subprocess.run(
                    ['bash', '-c', script], capture_output=True, text=True,
                    env=dict(os.environ, MANET_TOOLS_DIR=str(TOOLS),
                             RESULT=str(rc)), timeout=2)
                if rc == 125:
                    self.assertEqual(result.stdout,
                                     'preflight=125\nhosting=0\nvip=0\n')
                    self.assertEqual(result.stderr.count('python3 '), 2)
                else:
                    self.assertEqual(result.stdout,
                                     f'preflight={rc}\nhosting=1\nvip={rc}\n')
                    self.assertEqual(result.stderr, '')


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
    """Exercise fresh observations and the real shell-to-worker fast path."""

    def setUp(self):
        """Provide two services, proc tables and live command observations."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / 'runtime'
        self.stop = threading.Event()
        self.own = '02:00:00:00:00:01'
        self.other = '02:00:00:00:00:02'
        self.conf = {'ipv4_network': '10.30.0.0/24'}
        self.addresses = ['10.30.0.2', 'fd00::2', '10.30.0.3', 'fd00::3']
        self.state = 'active'
        self.commands = []
        self.files = {
            '/var/run/mesh_node_registry': "\n".join(
                f"NODE_020000000001_{key}='{value}'" for key, value in
                dict(MAC_ADDRESS=self.own, MEAN_THROUGHPUT_MBPS='5',
                     NODE_STATE='ACTIVE', OBSERVED_AT_UPTIME='100').items()),
            '/proc/uptime': '400 0\n',
            '/sys/class/net/br0/address': self.own,
            '/etc/radvd-mesh.conf': 'prefix fd00::/48 {}',
            '/var/run/mediamtx-election.lock.state': 'leader',
            '/var/run/mumble-election.lock.state': 'leader',
            '/proc/net/tcp': self.socket_table('0A', 8554, 8889, 64738),
            '/proc/net/tcp6': self.socket_table('0A'),
            '/proc/net/udp': self.socket_table('07', 64738),
            '/proc/net/udp6': self.socket_table('07'),
        }
        for name, value in self.files.items():
            path = self.fixture_path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
        for name in ('mtx-ip.sh', 'mumble-ip.sh'):
            (self.root / name).write_text('fixture helper')
        election.ipv6_vip.cache_clear()
        self.addCleanup(election.ipv6_vip.cache_clear)
        # Replace this module's command interface, not subprocess.run itself:
        # integration tests must still launch the actual Bash entry points.
        runner = types.SimpleNamespace(
            run=self.command, SubprocessError=subprocess.SubprocessError)
        for patcher in (patch.object(election, 'Path', self.fixture_path),
                        patch.object(election, 'TOOLS', self.root),
                        patch.object(election, 'read_values',
                                     return_value=self.conf),
                        patch.object(election, 'subprocess', runner)):
            patcher.start()
            self.addCleanup(patcher.stop)
        database = self.fixture_path(election.MUMBLE_DATABASE)
        database.parent.mkdir(parents=True)
        database.write_bytes(b'database fixture')
        stamp = self.fixture_path('/var/run/mumble-election.lock.db-export')
        stamp.write_text(election.mumble_database_signature())

    def test_database_and_wal_stat_changes_require_export(self):
        """Keep unchanged owners cheap, but detect DB/WAL writes and removal."""
        stamp = self.fixture_path('/var/run/mumble-election.lock.db-export')
        for suffix in ('', '-wal'):
            path = self.fixture_path(election.MUMBLE_DATABASE + suffix)
            path.write_bytes(b'first')
            self.assertNotEqual(election.check('mumble'), 'skip')
            stamp.write_text(election.mumble_database_signature())
            self.assertEqual(election.check('mumble'), 'skip')
            # Same-size writes still invalidate via nanosecond metadata.
            path.write_bytes(b'other')
            self.assertNotEqual(election.check('mumble'), 'skip')
            stamp.write_text(election.mumble_database_signature())
            path.write_bytes(b'longer payload')
            self.assertNotEqual(election.check('mumble'), 'skip')
            stamp.write_text(election.mumble_database_signature())
        path.unlink()
        self.assertNotEqual(election.check('mumble'), 'skip')
        stamp.write_text(election.mumble_database_signature())
        self.assertEqual(election.check('mumble'), 'skip')
        stamp.unlink()
        self.assertNotEqual(election.check('mumble'), 'skip')
        self.assertEqual(election.check('mediamtx'), 'skip')
        self.fixture_path(election.MUMBLE_DATABASE).unlink()
        self.assertNotEqual(election.check('mumble'), 'skip')

    def test_pending_final_export_prevents_follower_skip(self):
        """A stopped follower must retry failed final snapshot publication."""
        self.fixture_path('/sys/class/net/br0/address').write_text(self.other)
        self.state = 'inactive'
        self.addresses = []
        pending = self.fixture_path(
            '/var/run/mumble-election.lock.release-pending')
        pending.touch()
        self.assertNotEqual(election.check('mumble'), 'skip')
        pending.unlink()
        self.assertEqual(election.check('mumble'), 'skip')

    def fixture_path(self, name):
        """Redirect runtime reads and election locks into temporary files."""
        return self.root / str(name).lstrip('/')

    @staticmethod
    def socket_table(state, *ports, ipv6=False, address=None):
        """Render kernel socket columns without requiring socket privileges."""
        address = address or '0' * (32 if ipv6 else 8)
        return ('  sl  local_address rem_address st\n' + ''.join(
            f' {i}: {address}:{port:04X} {"0" * len(address)}:0000 '
            f'{state} 00000000:00000000 00:00000000 00000000 0 0 1\n'
            for i, port in enumerate(ports)))

    def command(self, args, **kwargs):
        """Record every resident subprocess and return current live inputs."""
        self.commands.append(args)
        if args[0] == 'bash':
            suffix = 2 if args[1].endswith('mtx-ip.sh') else 3
            output = f'fd00:0000::000{suffix}/128'
        elif args[0] == 'systemctl':
            output = self.state
        elif args[0] == 'ip':
            output = json.dumps([dict(addr_info=[dict(local=a)
                                                for a in self.addresses])])
        else:
            self.fail(f'unexpected resident subprocess: {args}')
        rc = 3 if args[0] == 'systemctl' and output != 'active' else 0
        return subprocess.CompletedProcess(args, rc, output)

    def test_fresh_inputs_and_per_service_vip_cache_invalidation(self):
        """Keep both normalized VIPs cached while observing live state anew."""
        for _ in range(2):
            for service in election.SERVICES:
                self.assertEqual(election.check(service), 'skip')
                self.assertEqual(runtime.Helpers().dispatch(
                    'service-vip', service)[1],
                    f'fd00::{election.SERVICES[service][1]}/128')
        self.assertEqual(sum(c[0] == 'bash' for c in self.commands), 2)
        self.fixture_path('/proc/uptime').write_text('401 0\n')
        for service in election.SERVICES:
            self.assertEqual(election.check(service), '- - -')
        self.fixture_path('/proc/uptime').write_text('400 0\n')
        for service in election.SERVICES:
            with self.subTest(service=service):
                self.state = 'failed'
                self.assertNotEqual(election.check(service), 'skip')
                self.state = 'active'
                vip = election.service_ipv6(service)
                self.addresses.remove(vip)
                self.assertNotEqual(election.check(service), 'skip')
                self.addresses.append(vip)
        self.conf['ipv4_network'] = '10.40.0.0/24'
        self.assertNotEqual(election.check('mediamtx'), 'skip')
        self.fixture_path('/etc/radvd-mesh.conf').write_text('prefix fd01::/48')
        for service in election.SERVICES:
            election.check(service)
        self.assertEqual(sum(c[0] == 'bash' for c in self.commands), 4)
        (self.root / 'mumble-ip.sh').write_text('replaced helper generation')
        election.check('mumble')
        self.assertEqual(sum(c[0] == 'bash' for c in self.commands), 5)

    def test_required_ports_are_live_in_both_address_families(self):
        """Missing ports, non-wildcard sockets and wrong states cannot serve."""
        for service, ports in (('mediamtx', (8554, 8889)),
                               ('mumble', (64738,))):
            for missing in ports:
                self.fixture_path('/proc/net/tcp').write_text(
                    self.socket_table('0A', *(p for p in ports
                                              if p != missing)))
                self.assertNotEqual(election.check(service), 'skip')
                self.assertFalse(election.hosting(service))
            self.fixture_path('/proc/net/tcp').write_text(
                self.socket_table('0A', *ports, address='0100007F'))
            self.assertNotEqual(election.check(service), 'skip')
            self.fixture_path('/proc/net/tcp').write_text(
                self.socket_table('01', *ports))
            self.assertNotEqual(election.check(service), 'skip')
            self.fixture_path('/proc/net/tcp6').write_text(
                self.socket_table('0A', *ports, ipv6=True))
            self.assertEqual(election.check(service), 'skip')
            self.assertTrue(election.hosting(service))
            self.fixture_path('/proc/net/tcp6').write_text(
                self.socket_table('0A'))
        self.fixture_path('/proc/net/tcp').write_text(
            self.socket_table('0A', 64738))
        self.fixture_path('/proc/net/udp').write_text(
            self.socket_table('01', 64738))
        self.assertNotEqual(election.check('mumble'), 'skip')
        self.assertFalse(election.hosting('mumble'))
        self.fixture_path('/proc/net/udp6').write_text(
            self.socket_table('07', 64738, ipv6=True))
        self.assertEqual(election.check('mumble'), 'skip')
        self.assertTrue(election.hosting('mumble'))

    def test_pending_transitions_and_concurrent_changes_never_skip(self):
        """Announcements, publications and waits require the shell path."""
        for service in election.SERVICES:
            state = self.fixture_path(
                f'/var/run/{service}-election.lock.state')
            pending = self.fixture_path(
                f'/var/run/{service}-election.lock.publish-pending')
            with self.subTest(service=service):
                for transition in ('', 'announce-failed', 'start-failed',
                                   'waiting:' + self.other):
                    state.write_text(transition)
                    self.assertNotEqual(election.check(service), 'skip')
                state.unlink()
                self.assertNotEqual(election.check(service), 'skip')
                state.write_text('leader')
                pending.write_text('true')
                self.assertNotEqual(election.check(service), 'skip')
                pending.unlink()
                with self.fixture_path(
                        f'/var/run/{service}-election.lock').open('a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.assertNotEqual(election.check(service), 'skip')
                self.assertEqual(election.check(service), 'skip')

    def test_non_owner_must_be_inactive_without_vips_or_pending_work(self):
        """Keep partial withdrawals, failed units and waiting hosts active."""
        self.fixture_path('/sys/class/net/br0/address').write_text(self.other)
        for service in election.SERVICES:
            state = self.fixture_path(
                f'/var/run/{service}-election.lock.state')
            pending = self.fixture_path(
                f'/var/run/{service}-election.lock.publish-pending')
            state.write_text('follower')
            self.state = 'inactive'
            self.addresses = [election.service_ipv4(service)]
            self.assertNotEqual(election.check(service), 'skip')
            self.addresses = [election.service_ipv6(service)]
            self.assertNotEqual(election.check(service), 'skip')
            self.addresses = []
            for status in ('active', 'failed', 'activating', 'deactivating'):
                self.state = status
                self.assertNotEqual(election.check(service), 'skip')
            self.state = 'inactive'
            state.write_text('waiting:' + self.own)
            self.assertNotEqual(election.check(service), 'skip')
            state.write_text('follower')
            pending.write_text('false')
            self.assertNotEqual(election.check(service), 'skip')
            pending.unlink()
            self.assertEqual(election.check(service), 'skip')
            self.assertFalse(election.hosting(service))

    def test_observation_failures_reconcile_and_do_not_advertise(self):
        """Unreadable and malformed proc snapshots never imply readiness."""
        self.fixture_path('/proc/net/tcp').write_text('header\n0: broken x 0A')
        self.assertNotEqual(election.check('mediamtx'), 'skip')
        self.assertFalse(election.hosting('mediamtx'))
        self.fixture_path('/proc/net/tcp').unlink()
        self.assertNotEqual(election.check('mediamtx'), 'skip')
        self.assertFalse(election.hosting('mediamtx'))

    def test_settled_shell_passes_start_no_python_or_socket_tools(self):
        """Count actual shell commands through the FIFO, for both roles."""
        RequestTests.start(self, runtime.Helpers())
        binaries = self.root / 'bin'
        binaries.mkdir()
        spawns = self.root / 'spawns'
        spawns.touch()
        # Intercept shell-side interpreters and transition-only commands.
        # flock is needed by the real FIFO client and is counted as well.
        for name in ('python3', 'ss', 'sqlite3', 'flock'):
            body = f'printf "%s\\n" {name} >> "$SPAWNS"\n'
            body += ('exec /usr/bin/flock "$@"\n' if name == 'flock'
                     else 'exit 99\n')
            path = binaries / name
            path.write_text('#!/bin/bash\n' + body)
            path.chmod(0o755)
        env = dict(os.environ, MANET_RUNTIME_DIR=str(self.directory),
                   MANET_TOOLS_DIR=str(TOOLS), SPAWNS=str(spawns),
                   PATH=str(binaries) + os.pathsep + os.environ['PATH'])
        # Warm both config caches, then measure one pass for each role.
        for service in election.SERVICES:
            election.service_ipv6(service)
        for owner in (True, False):
            self.fixture_path('/sys/class/net/br0/address').write_text(
                self.own if owner else self.other)
            self.state = 'active' if owner else 'inactive'
            if not owner:
                self.addresses = []
            for service in election.SERVICES:
                with self.subTest(owner=owner, service=service):
                    self.fixture_path(
                        f'/var/run/{service}-election.lock.state').write_text(
                            'leader' if owner else 'follower')
                    spawns.write_text('')
                    self.commands.clear()
                    result = subprocess.run(
                        ['bash', str(TOOLS / f'{service}-election.sh')],
                        env=env, capture_output=True, text=True, timeout=4)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(spawns.read_text().splitlines(),
                                     ['flock'])
                    self.assertEqual(spawns.read_text().count('python3'), 0)
                    self.assertEqual([c[0] for c in self.commands],
                                     ['ip', 'systemctl'])
        # Managers launch the two elections concurrently. Brief FIFO lock
        # contention must not send one of these settled passes to Python.
        for owner in (True, False):
            self.fixture_path('/sys/class/net/br0/address').write_text(
                self.own if owner else self.other)
            self.state = 'active' if owner else 'inactive'
            self.addresses = (['10.30.0.2', 'fd00::2',
                               '10.30.0.3', 'fd00::3'] if owner else [])
            for service in election.SERVICES:
                self.fixture_path(
                    f'/var/run/{service}-election.lock.state').write_text(
                        'leader' if owner else 'follower')
            spawns.write_text('')
            self.commands.clear()
            def delayed_command(args, **kwargs):
                """Force the two real shell callers to overlap in the worker."""
                time.sleep(.03)
                return self.command(args, **kwargs)
            with patch.object(election.subprocess, 'run', delayed_command):
                result = subprocess.run(
                    ['bash', '-c',
                     'bash "$MANET_TOOLS_DIR/mediamtx-election.sh" & a=$!; '
                     'bash "$MANET_TOOLS_DIR/mumble-election.sh" & b=$!; '
                     'wait "$a"; ra=$?; wait "$b"; rb=$?; '
                     '[ "$ra" = 0 ] && [ "$rb" = 0 ]'],
                    env=env, capture_output=True, text=True, timeout=4)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(spawns.read_text().splitlines(),
                             ['flock', 'flock'])
            self.assertEqual([c[0] for c in self.commands],
                             ['ip', 'systemctl', 'ip', 'systemctl'])
        # Frequent telemetry also uses the worker, not ss or a new Python.
        self.state = 'active'
        self.addresses = ['10.30.0.2', '10.30.0.3']
        spawns.write_text('')
        result = subprocess.run(
            ['bash', '-c', '. "$MANET_TOOLS_DIR/manet-manager-common.sh"; '
             'is_hosting_service && is_hosting_mumble_service'],
            env=env, capture_output=True, text=True, timeout=4)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(spawns.read_text().splitlines(), ['flock', 'flock'])

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
