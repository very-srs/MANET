"""Exercise no-op decisions without running any commands on live interfaces."""
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import manet_ip_runtime as runtime
import manet_registry_builder as registry
from manet_config_io import atomic_write
import test_mesh_registry as registry_tests

TOOLS, MAC = registry_tests.TOOLS, registry_tests.MAC


class RegistryCacheTests(unittest.TestCase):
    # Reuse the real encoder/Alfred fixture without duplicating its tests.
    setUp = registry_tests.ChunkClaimsTests.setUp
    encode = registry_tests.ChunkClaimsTests.encode
    set_uptime = registry_tests.ChunkClaimsTests.set_uptime
    add_node = registry_tests.ChunkClaimsTests.add_node
    build_registry = registry_tests.ChunkClaimsTests.build_registry
    registry_value = registry_tests.ChunkClaimsTests.registry_value
    def test_unchanged_decode_is_reused_but_expiry_and_claims_are_live(self):
        self.add_node()
        with patch.dict(os.environ, self.env):
            registry.build()
            inode = self.claims.stat().st_ino
            self.set_uptime(1300)
            with patch.object(registry, 'decode', side_effect=AssertionError('unnecessary decode')):
                registry.build()
                self.assertEqual(self.registry_value(MAC, 'OBSERVED_AGE_SECONDS'), '300')
                self.assertEqual(self.claims.stat().st_ino, inode)
                self.set_uptime(1301)
                registry.build()
                self.assertEqual(self.claims.read_text(), '')
                self.assertEqual(self.registry_value(MAC, 'NODE_STATE'), 'STALE')

    def test_only_changed_record_is_decoded(self):
        self.add_node()
        with patch.dict(os.environ, self.env):
            registry.build()
            for kind in (67, 68):
                (self.records / str(kind)).write_text('')
            self.add_node(age=30)
            with patch.object(registry, 'decode', wraps=registry.decode) as decode:
                registry.build()
                self.assertEqual([c.args[0] for c in decode.call_args_list], ['telemetry'])

    def test_corrupt_cache_rebuilds_without_losing_claims(self):
        self.add_node()
        self.build_registry()
        cache = self.root / 'observed/observed.tsv.decoded.json'
        cache.write_text('{')
        self.assertEqual(self.build_registry(), [f'0,{MAC},169738246,7'])

    def test_quoted_identity_survives_missing_identity(self):
        payload = self.encode('identity', '--hostname', "mesh-o'neil", '--mac-addresses', MAC, '--ipv4-address', '10.30.0.6',
                              '--ipv4-chunk-size', '7')
        self.add_node(identity=False)
        (self.records / '67').write_text(f'{{ "{MAC}", "{payload}" }},\n')
        self.build_registry()
        (self.records / '67').write_text('')
        self.build_registry()
        self.assertEqual(registry.previous_fields(self.registry)[MAC.replace(':', '')]['HOSTNAME'], "mesh-o'neil")

    def test_allocator_and_runtime_read_the_builders_shared_claims(self):
        self.add_node()
        expected = self.build_registry()
        with patch.dict(os.environ, self.env):
            inputs = runtime.file_inputs(self.root)
        self.assertEqual(inputs[runtime.CLAIMS_FILE].splitlines(), expected)
        source = (TOOLS / 'mesh-ip-manager.sh').read_text()
        assignment = '\n'.join(line for line in source.splitlines()
                               if line.startswith('CLAIMED_CHUNKS_FILE='))
        script = assignment + '\ncat "$CLAIMED_CHUNKS_FILE"\n'
        result = subprocess.run(['bash', '-c', script], env=self.env,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), expected)

    def test_default_runtime_ignores_old_tmp_claims(self):
        self.add_node()
        expected = self.build_registry()
        old = self.root / 'tmp/claimed_chunks.txt'
        old.parent.mkdir()
        old.write_text('conflicting old snapshot\n')
        with patch.dict(os.environ):
            os.environ.pop('MESH_CLAIMED_CHUNKS_FILE', None)
            inputs = runtime.file_inputs(self.root)
        self.assertEqual(inputs[runtime.CLAIMS_FILE].splitlines(), expected)


class RuntimeCacheTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.cache = Path(scratch.name) / 'runtime.json'
        self.files = {name: '' for name in runtime.INPUTS}
        self.files.update({'etc/mesh.conf': 'ipv4_network=10.30.0.0/24\n',
                           'var/run/my_ipv4_chunk': '0\n', 'var/run/my_ipv4_chunk_size': '7\n',
                           'etc/dnsmasq.d/mesh-eud.conf': 'validated by shell',
                           'pending': [], 'macs': {'br0': MAC}})
        self.live = [('10.30.0.6', 24), ('10.30.0.7', 24)]
        self.startup = types.SimpleNamespace(main=Mock(return_value=0))
        self.isolation = types.SimpleNamespace(ensure=Mock(), eud_ready=Mock(return_value=True))
        self.calls = []
        self.shell_status = 0
        self.service_status = 0
        self.ui_status = 0
        self.patches = [
            patch.object(runtime, 'module', side_effect=lambda name: self.startup if name == 'mesh-ip-startup' else self.isolation),
            patch.object(runtime, 'Path', side_effect=lambda name: self.cache if str(name) == '/run/manet-ip-runtime.json' else Path(name)),
            patch.object(runtime, 'file_inputs', side_effect=lambda: copy.deepcopy(self.files)),
            patch.object(runtime, 'addresses', side_effect=lambda: self.live),
            patch.object(runtime, 'read_values', side_effect=lambda name:
                         {'ipv4_network': '10.30.0.0/24'} if name.endswith('mesh.conf') else
                         {'PERSISTENT_IPV4': '10.30.0.6', 'PERSISTENT_CHUNK': '0', 'PERSISTENT_NETWORK': '10.30.0.0/24'}),
            patch.object(runtime.subprocess, 'run', side_effect=self.command),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def command(self, args, **kw):
        self.calls.append((args, kw))
        status = {'bash': self.shell_status, 'systemctl': self.service_status, 'nft': self.ui_status}[args[0]]
        return subprocess.CompletedProcess(args, status, 'active\n' if status == 0 else '', '')

    def shell_calls(self):
        return [kw for args, kw in self.calls if args[0] == 'bash']

    def prime(self):
        self.assertEqual(runtime.reconcile(), 0)
        self.assertTrue(self.cache.exists())
        self.calls.clear()

    def test_idle_skips_shell_but_keeps_live_checks_each_pass(self):
        self.prime()
        self.assertEqual(runtime.reconcile(), 0)
        self.assertEqual(self.shell_calls(), [])
        self.assertEqual(self.startup.main.call_count, 2)
        self.assertEqual(self.isolation.ensure.call_count, 2)
        self.assertEqual([args[0] for args, _ in self.calls], ['systemctl', 'nft'])

    def test_every_file_input_invalidates_the_cached_pass(self):
        for name in runtime.INPUTS:
            with self.subTest(name=name):
                self.prime()
                previous = self.files[name]
                self.files[name] = None
                runtime.reconcile()
                self.assertEqual(len(self.shell_calls()), 1)
                self.files[name] = previous
                self.cache.unlink(missing_ok=True)

    def test_address_loss_prefix_change_and_eud_loss_reconcile(self):
        for live, ready in [([('10.30.0.6', 24)], True),
                            ([('10.30.0.6', 24), ('10.30.0.7', 32)], True),
                            ([('10.30.0.6', 24), ('10.30.0.7', 24), ('10.30.0.40', 24)], True), (self.live, False)]:
            with self.subTest(live=live, ready=ready):
                self.live = [('10.30.0.6', 24), ('10.30.0.7', 24)]
                self.isolation.eud_ready.return_value = True
                self.prime()
                self.live = live
                self.isolation.eud_ready.return_value = ready
                runtime.reconcile()
                self.assertEqual(len(self.shell_calls()), 1)
                self.assertFalse(self.cache.exists())

    def test_failed_discovery_and_policy_invoke_withdrawal_and_never_cache(self):
        self.prime()
        self.startup.main.return_value = 1
        runtime.reconcile()
        self.assertEqual(self.shell_calls()[0]['env']['MANET_IP_STARTUP_READY'], '0')
        self.assertFalse(self.cache.exists())
        self.startup.main.return_value = 0
        self.isolation.ensure.side_effect = RuntimeError('injected nft error')
        runtime.reconcile()
        self.assertEqual(self.shell_calls()[-1]['env']['MANET_IP_ISOLATED'], '0')
        self.assertFalse(self.cache.exists())

    def test_service_stop_ui_flush_and_pending_reload_prevent_cache_hit(self):
        for kind in ('service', 'ui', 'pending'):
            with self.subTest(kind=kind):
                self.service_status = self.ui_status = 0
                self.files['pending'] = []
                self.prime()
                if kind == 'service': self.service_status = 3
                if kind == 'ui': self.ui_status = 1
                if kind == 'pending': self.files['pending'] = ['run/manet-avahi-host-reload-needed']
                runtime.reconcile()
                self.assertEqual(len(self.shell_calls()), 1)
                self.assertFalse(self.cache.exists())

    def test_failures_and_overlapping_or_incomplete_claims_retry(self):
        self.shell_status = 1
        self.assertEqual(runtime.reconcile(), 1)
        self.assertFalse(self.cache.exists())
        self.shell_status = 0
        for claim in ('0,02:00:00:00:00:02,169738246,7\n',
                      '0,02:00:00:00:00:02,,0\n'):
            self.files[runtime.CLAIMS_FILE] = claim
            runtime.reconcile()
            self.assertFalse(self.cache.exists())

    def test_repaired_output_needs_a_subsequent_live_verification_before_cache(self):
        original = self.command
        def repairing(args, **kw):
            if args[0] == 'bash':
                self.files['etc/avahi/hosts'] = '10.30.0.7 manet.local\n'
            return original(args, **kw)
        with patch.object(runtime.subprocess, 'run', side_effect=repairing):
            runtime.reconcile()
        self.assertFalse(self.cache.exists())
        runtime.reconcile()
        self.assertTrue(self.cache.exists())
        self.calls.clear()
        runtime.reconcile()
        self.assertEqual(self.shell_calls(), [])


class ShellGateTests(unittest.TestCase):
    def test_radio_choices_cache_one_document_and_retry_parser_failures(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            state = root / 'state'
            state.write_text('{"desired":{"wlan0":"down"}}')
            body = f'. "{TOOLS}/manet-common.sh"\n' + '''
jq() { echo called >> "$TEST_ROOT/calls"; command jq "$@"; }
radio_iface_enabled wlan0; echo "$?"
radio_iface_enabled wlan0; echo "$?"
echo '{"desired":{"wlan0":"up"}}' > "$MANET_RADIO_STATE_FILE"
radio_iface_enabled wlan0; echo "$?"
echo '{' > "$MANET_RADIO_STATE_FILE"
radio_iface_enabled wlan0; echo "$?"
radio_iface_enabled wlan0; echo "$?"
echo '{"desired":{"wlan0":"down"}}' > "$MANET_RADIO_STATE_FILE"
radio_iface_enabled wlan0; echo "$?"
rm "$MANET_RADIO_STATE_FILE"
radio_iface_enabled wlan0; echo "$?"
'''
            result = subprocess.run(['bash', '-c', body], capture_output=True, text=True,
                                    env=dict(os.environ, TEST_ROOT=scratch, MANET_RADIO_STATE_FILE=str(state)))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ['1', '1', '0', '0', '0', '1', '0'])
            self.assertEqual(len((root / 'calls').read_text().splitlines()), 5)

    def test_eud_readiness_rechecks_sysfs_before_service_start(self):
        source = (TOOLS / 'mesh-ip-manager.sh').read_text()
        function = re.search(r'^eud_ready\(\) \{.*?^\}', source, re.M | re.S)[0]
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / 'br0/brif/bat0').mkdir(parents=True)
            (root / 'bat0').mkdir()
            (root / 'br0/brif/bat0/state').write_text('3\n')
            (root / 'bat0/carrier').write_text('1\n')
            def check():
                return subprocess.run(['bash', '-c', function + '\neud_ready'],
                                      env=dict(os.environ, MANET_IP_CHECKED='1', MANET_IP_EUD_READY='1',
                                               MANET_SYS_NET=scratch), capture_output=True).returncode
            self.assertEqual(check(), 1)  # A live bat0 never counts as an EUD.
            (root / 'br0/brif/end0').mkdir()
            (root / 'end0').mkdir()
            (root / 'br0/brif/end0/state').write_text('3\n')
            (root / 'end0/carrier').write_text('1\n')
            self.assertEqual(check(), 0)
            (root / 'end0/carrier').write_text('0\n')
            self.assertEqual(check(), 1)

    def test_static_plan_content_cache_reloads_changes_and_retries_bad_input(self):
        source = (TOOLS / 'node-manager-static.sh').read_text()
        function = re.search(r'^load_static_plan\(\) \{.*?^\}', source, re.M | re.S)[0]
        with tempfile.TemporaryDirectory() as scratch:
            body = '''
log() { :; }
python3() { echo called >> "$TEST_ROOT/calls"; command python3 "$@"; }
''' + function + '''
load_static_plan; load_static_plan
echo '{"2.4":2437,"5":5745}' > "$MANET_STATIC_CHANNELS"
load_static_plan; load_static_plan
echo "$STATIC_FREQ_2_4 $STATIC_FREQ_5_0"
echo '{' > "$MANET_STATIC_CHANNELS"
load_static_plan && exit 9
load_static_plan && exit 10
rm "$MANET_STATIC_CHANNELS"
load_static_plan
echo "$STATIC_FREQ_2_4 $STATIC_FREQ_5_0"
'''
            result = subprocess.run(['bash', '-c', body], capture_output=True, text=True,
                                    env=dict(os.environ, TEST_ROOT=scratch,
                                             MANET_STATIC_CHANNELS=str(Path(scratch) / 'plan'),
                                             STATIC_CHANNELS=str(TOOLS / 'manet_static_channels.py')))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, '2437 5745\n2412 5180\n')
            self.assertEqual(len((Path(scratch) / 'calls').read_text().splitlines()), 5)

    def test_empty_alfred_skips_parser_nonempty_always_reaches_it_and_failure_is_not_empty(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            helper = root / 'receiver'
            helper.write_text('#!/bin/bash\ncat >> "$TEST_ROOT/received"\n')
            helper.chmod(0o755)
            body = f'. "{TOOLS}/manet-common.sh"\n' + '''
timeout() { [ "$FAIL" != yes ] || return 1; printf '%s' "$RAW"; }
RAW=' '; sync_alfred_command "$TEST_ROOT/receiver" 70
RAW=staged; sync_alfred_command "$TEST_ROOT/receiver" 70
sync_alfred_command "$TEST_ROOT/receiver" 70
FAIL=yes; sync_alfred_command "$TEST_ROOT/receiver" 70 && exit 9
exit 0
'''
            result = subprocess.run(['bash', '-c', body], env=dict(os.environ, TEST_ROOT=scratch), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'received').read_text(), 'staged\nstaged\n')


class AgreementCacheTests(unittest.TestCase):
    def test_verified_records_expire_and_credentials_or_backward_time_invalidate(self):
        agreement = runtime.module('mesh-channel-agreement')
        with tempfile.TemporaryDirectory() as scratch:
            runner = agreement.Runtime.__new__(agreement.Runtime)
            runner.conf = Path(scratch) / 'conf'
            runner.conf.write_text('admin_password=one\n')
            runner.transport = object()
            raw = '{ "' + MAC + '", "envelope" },'
            records = {MAC: {'_fresh_until': 145, 'status': {'current': {'5': 5180}}}}
            with patch.object(agreement, 'command', return_value=raw) as command, \
                    patch.object(agreement, 'require_clock') as clock, \
                    patch.object(agreement, 'authenticated_records', return_value=records) as authenticate:
                runner.receive(74, 100)
                received = runner.receive(74, 101)
                received[MAC]['status']['current']['5'] = 5745
                self.assertEqual(runner.receive(74, 102)[MAC]['status']['current']['5'], 5180)
                self.assertEqual(authenticate.call_count, 1)
                self.assertEqual(runner.receive(74, 146), {})
                self.assertEqual(authenticate.call_count, 1)
                # Match real config updates and invalidate the inode even on
                # filesystems where consecutive writes share timestamps.
                atomic_write(runner.conf, 'admin_password=two-new\n')
                runner.receive(74, 103)
                runner.receive(74, 99)
                self.assertEqual(authenticate.call_count, 3)
                command.side_effect = OSError('Alfred failed')
                with self.assertRaises(OSError):
                    runner.receive(74, 104)
                self.assertEqual(clock.call_count, 6)

    def test_rejected_records_are_retried(self):
        agreement = runtime.module('mesh-channel-agreement')
        with tempfile.TemporaryDirectory() as scratch:
            runner = agreement.Runtime.__new__(agreement.Runtime)
            runner.conf = Path(scratch) / 'conf'
            runner.conf.write_text('admin_password=one\n')
            runner.transport = object()
            with patch.object(agreement, 'command', return_value='{ "' + MAC + '", "envelope" },'), \
                    patch.object(agreement, 'require_clock'), \
                    patch.object(agreement, 'authenticated_records', return_value={}) as authenticate:
                runner.receive(74, 100)
                runner.receive(74, 101)
                self.assertEqual(authenticate.call_count, 2)


if __name__ == '__main__':
    unittest.main()
