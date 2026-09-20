#!/usr/bin/env python3
"""Run rollback and its receiving gate with temporary files and fake radios."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


TOOLS = Path(__file__).resolve().parent
SCRIPT = TOOLS / 'mesh-config-rollback.sh'
SPEC = importlib.util.spec_from_file_location('rollback_config_sync', TOOLS / 'mesh-config-sync.py')
config_sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(config_sync)
PEER = '0c:bf:74:00:2b:f1'
VERSION = 'aabbcc001122'


class RollbackHarness(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='manet-rollback-test-')
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.conf = self.root / 'mesh.conf'
        self.old_config = 'mesh_key=previous-working-key\nmtx=y\n'
        self.conf.write_text(self.old_config)
        self.wpa = self.root / 'wpa'
        self.wpa.mkdir()
        self.supplicant = self.wpa / 'wpa_supplicant-wlan0.conf'
        self.old_supplicant = 'network={\nsae_password=previous-working-key\n}\n'
        self.supplicant.write_text(self.old_supplicant)
        self.state = self.root / 'rollback'
        self.runtime = self.root / 'run'
        self.runtime.mkdir()
        self.interfaces = self.root / 'interfaces'
        self.interfaces.mkdir()
        (self.interfaces / 'mesh_if').write_text('wlan0\n')
        (self.interfaces / 'halow_if').write_text('wlan1\n')
        self.output = self.root / 'originators.json'
        self.peers([{'orig_address': PEER, 'best': True}])
        self.service_log = self.root / 'services'
        self.env = dict(
            os.environ,
            PATH=str(self.bin) + os.pathsep + str(Path(sys.executable).parent)
                 + os.pathsep + os.environ.get('PATH', ''),
            BATCTL=str(self.bin / 'batctl'),
            MANET_MESH_CONF=str(self.conf), MANET_WPA_DIR=str(self.wpa),
            MANET_ROLLBACK_DIR=str(self.state), MANET_RUN_DIR=str(self.runtime),
            MANET_IFACE_STATE_DIR=str(self.interfaces), MANET_ROLLBACK_GRACE='300',
            TEST_NOW='1000', TEST_ORIGINATORS=str(self.output),
            TEST_SERVICE_LOG=str(self.service_log), TEST_BATCTL_RC='0',
            TEST_SERVICE_RC='0', TEST_COPY_FAIL='',
        )
        self.command('batctl', '''
if sys.argv[1:] != ['meshif', 'bat0', 'originators_json']:
    sys.exit(99)
sys.stdout.write(Path(os.environ['TEST_ORIGINATORS']).read_text())
sys.exit(int(os.environ['TEST_BATCTL_RC']))
''')
        self.command('date', '''
print(os.environ['TEST_NOW'] if sys.argv[1:] == ['+%s'] else 'test-clock')
''')
        self.command('systemctl', '''
with open(os.environ['TEST_SERVICE_LOG'], 'a') as log:
    log.write(' '.join(sys.argv[1:]) + '\\n')
sys.exit(int(os.environ['TEST_SERVICE_RC']))
''')
        self.command('cp', f'''
if os.environ.get('TEST_COPY_FAIL') == sys.argv[-2]:
    sys.exit(1)
os.execv({shutil.which('cp')!r}, ['cp', *sys.argv[1:]])
''')

    def command(self, name, body):
        path = self.bin / name
        path.write_text(f'#!{sys.executable}\nimport os, sys\nfrom pathlib import Path\n' + body)
        path.chmod(0o755)

    def peers(self, rows):
        self.output.write_text(json.dumps(rows))

    def call(self, *args):
        return subprocess.run(['bash', str(SCRIPT), *args], env=self.env,
                              capture_output=True, text=True, timeout=15)

    def arm(self):
        result = self.call('arm', VERSION)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return (self.state / 'state').read_text()

    def changed(self):
        self.conf.write_text('mesh_key=changed-nonworking-key\nmtx=y\n')
        self.supplicant.write_text('network={\nsae_password=changed-nonworking-key\n}\n')

    def deadline(self):
        self.env['TEST_NOW'] = '1300'


class RollbackScriptTests(RollbackHarness):
    def test_counts_selected_routes_and_deduplicates_originators(self):
        self.peers([
            {'orig_address': PEER, 'best': True},
            {'orig_address': PEER.upper(), 'best': False},
            {'orig_address': '02:00:00:00:00:02', 'best': True},
        ])
        self.assertIn('PEERS_BEFORE=2\n', self.arm())
        self.assertEqual((self.state.stat().st_mode & 0o777), 0o700)
        self.assertEqual(((self.state / 'state').stat().st_mode & 0o777), 0o600)

    def test_lost_peer_restores_snapshot_after_deadline_across_processes(self):
        self.assertIn('PEERS_BEFORE=1\n', self.arm())
        self.changed()
        self.peers([])
        volatile = ('my_ipv4_chunk', 'mesh_ipv4_state', 'mesh_applied_config_version',
                    'mesh_pending_config.json', 'mesh_config_ack_version')
        for name in volatile:
            (self.runtime / name).write_text('new state')
        self.env['TEST_NOW'] = '1299'
        self.assertEqual(self.call('check').returncode, 0)
        self.assertNotEqual(self.conf.read_text(), self.old_config)
        self.assertTrue(self.state.exists())
        self.assertFalse(self.service_log.exists())
        self.deadline()
        result = self.call('check')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.conf.read_text(), self.old_config)
        self.assertEqual(self.supplicant.read_text(), self.old_supplicant)
        self.assertFalse(self.state.exists())
        self.assertTrue(all(not (self.runtime / name).exists() for name in volatile))
        self.assertIn('restart wpa_supplicant@wlan0.service', self.service_log.read_text())
        self.assertIn('restart wpa_supplicant@wlan1.service', self.service_log.read_text())
        self.assertIn('restart batman-enslave.service', self.service_log.read_text())

    def test_returned_peer_commits_new_configuration(self):
        self.arm()
        self.changed()
        self.deadline()
        self.assertEqual(self.call('check').returncode, 0)
        self.assertNotEqual(self.conf.read_text(), self.old_config)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.service_log.exists())

    def test_successful_empty_baseline_preserves_solo_node_behavior(self):
        self.peers([])
        self.assertIn('PEERS_BEFORE=0\n', self.arm())
        self.changed()
        self.deadline()
        result = self.call('check')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('no peers before the change', result.stdout)
        self.assertNotEqual(self.conf.read_text(), self.old_config)
        self.assertFalse(self.state.exists())

    def test_failed_or_malformed_baseline_cannot_arm(self):
        for output, code in [('[]', '1'), ('', '0'), ('not-json', '0'),
                             ('{}', '0'), ('[{}]', '0'), ('[null]', '0'),
                             ('[{"orig_address": "bad-mac"}]', '0')]:
            with self.subTest(output=output, code=code):
                self.output.write_text(output)
                self.env['TEST_BATCTL_RC'] = code
                self.assertNotEqual(self.call('arm', VERSION).returncode, 0)
                self.assertFalse(self.state.exists())
                self.assertEqual(self.conf.read_text(), self.old_config)

    def test_failed_or_malformed_recovery_query_restores_at_deadline(self):
        for output, code in [('[]', '1'), ('not-json', '0')]:
            with self.subTest(output=output, code=code):
                self.env.update(TEST_NOW='1000', TEST_BATCTL_RC='0')
                self.peers([{'orig_address': PEER, 'best': True}])
                self.arm()
                self.changed()
                self.output.write_text(output)
                self.env['TEST_BATCTL_RC'] = code
                self.deadline()
                result = self.call('check')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('Cannot verify peer recovery', result.stdout)
                self.assertEqual(self.conf.read_text(), self.old_config)
                self.assertFalse(self.state.exists())

    def test_snapshot_copy_failure_cannot_arm_or_leave_partial_state(self):
        for path in (self.conf, self.supplicant):
            with self.subTest(path=path):
                self.env['TEST_COPY_FAIL'] = str(path)
                self.assertNotEqual(self.call('arm', VERSION).returncode, 0)
                self.assertFalse(self.state.exists())
                self.assertEqual(list(self.root.glob('rollback.new.*')), [])

    def test_new_change_cannot_overwrite_active_trial_or_reset_deadline(self):
        saved = self.arm()
        self.changed()
        self.env['TEST_NOW'] = '1200'
        for version in (VERSION, 'ddeeff001122'):
            self.assertNotEqual(self.call('arm', version).returncode, 0)
            self.assertEqual((self.state / 'state').read_text(), saved)
            self.assertEqual((self.state / 'mesh.conf').read_text(), self.old_config)

    def test_partial_restore_retains_backup_and_finishes_even_if_peer_returns(self):
        self.arm()
        self.changed()
        self.peers([])
        self.deadline()
        self.env['TEST_COPY_FAIL'] = str(self.state / 'wpa' / self.supplicant.name)
        self.assertNotEqual(self.call('check').returncode, 0)
        self.assertTrue((self.state / 'restoring').exists())
        self.assertEqual(self.conf.read_text(), self.old_config)
        self.assertNotEqual(self.supplicant.read_text(), self.old_supplicant)
        self.env['TEST_COPY_FAIL'] = ''
        self.peers([{'orig_address': PEER, 'best': True}])
        self.assertEqual(self.call('check').returncode, 0)
        self.assertEqual(self.supplicant.read_text(), self.old_supplicant)
        self.assertFalse(self.state.exists())

    def test_failed_service_restart_keeps_snapshot_for_next_check(self):
        self.arm()
        self.changed()
        self.peers([])
        self.deadline()
        self.env['TEST_SERVICE_RC'] = '1'
        self.assertNotEqual(self.call('check').returncode, 0)
        self.assertTrue((self.state / 'restoring').exists())
        self.env['TEST_SERVICE_RC'] = '0'
        self.assertEqual(self.call('check').returncode, 0)
        self.assertFalse(self.state.exists())

    def test_incomplete_state_is_not_mistaken_for_solo_node(self):
        self.arm()
        (self.state / 'state').write_text("VERSION='aabbcc'\nDEADLINE=1300\n")
        self.deadline()
        self.assertNotEqual(self.call('check').returncode, 0)
        self.assertTrue(self.state.exists())


class ReceiverRollbackTests(RollbackHarness):
    def setUp(self):
        super().setUp()
        self.admin = Mock()
        self.payload = {'kind': 'mesh_config', 'version': VERSION, 'activate_at': 1,
                        'config': {'mesh_key': 'replacement-key'}}
        self.calls = []
        danger_check = config_sync.package_is_dangerous
        patches = {
            'ADMIN': self.admin,
            'PENDING_FILE': str(self.runtime / 'mesh_pending_config.json'),
            'ACK_VERSION_FILE': str(self.runtime / 'mesh_config_ack_version'),
            'APPLIED_VERSION_FILE': str(self.runtime / 'mesh_applied_config_version'),
            'ROLLBACK_SCRIPT': str(SCRIPT),
            'latest_config_package': lambda: SimpleNamespace(payload=self.payload),
            'publish_ack': Mock(return_value=True), 'log': Mock(),
            'package_is_dangerous': lambda pkg: danger_check(pkg, str(self.conf)),
            'run': self.receiver_run,
        }
        for name, value in patches.items():
            p = patch.object(config_sync, name, value)
            p.start()
            self.addCleanup(p.stop)

    def receiver_run(self, args, **kwargs):
        self.calls.append(args)
        if args == [config_sync.APPLY_SCRIPT]:
            self.admin.complete.assert_called_once()
            self.changed()  # The real apply touches live interfaces; simulate it.
            return subprocess.CompletedProcess(args, 0, '', '')
        return subprocess.run(args, env=self.env, capture_output=True, text=True, **kwargs)

    def assert_not_applied(self):
        self.assertNotIn([config_sync.APPLY_SCRIPT], self.calls)
        self.assertEqual(self.conf.read_text(), self.old_config)
        self.admin.complete.assert_not_called()

    def test_dangerous_change_arms_before_applying(self):
        self.assertEqual(config_sync.sync_once(), 0)
        self.assertEqual(self.calls, [[str(SCRIPT), 'arm', VERSION], [config_sync.APPLY_SCRIPT]])
        self.assertEqual((self.state / 'mesh.conf').read_text(), self.old_config)

    def test_failed_baseline_blocks_apply_and_can_retry_after_query_recovers(self):
        self.env['TEST_BATCTL_RC'] = '1'
        self.assertEqual(config_sync.sync_once(), 1)
        self.assert_not_applied()
        self.env['TEST_BATCTL_RC'] = '0'
        self.assertEqual(config_sync.sync_once(), 0)
        self.assertTrue(self.state.exists())
        self.assertEqual(self.calls.count([config_sync.APPLY_SCRIPT]), 1)

    def test_failed_snapshot_blocks_apply(self):
        self.env['TEST_COPY_FAIL'] = str(self.supplicant)
        self.assertEqual(config_sync.sync_once(), 1)
        self.assert_not_applied()

    def test_missing_or_nonexecutable_rollback_blocks_apply(self):
        missing = self.root / 'missing-rollback'
        nonexecutable = self.root / 'nonexecutable-rollback'
        nonexecutable.write_text('#!/bin/sh\nexit 0\n')
        for path in (missing, nonexecutable):
            with self.subTest(path=path), patch.object(config_sync, 'ROLLBACK_SCRIPT', str(path)):
                self.assertEqual(config_sync.sync_once(), 1)
                self.assert_not_applied()

    def test_rollback_timeout_blocks_apply(self):
        with patch.object(config_sync, 'run', side_effect=subprocess.TimeoutExpired('rollback', 30)):
            self.assertEqual(config_sync.sync_once(), 1)
        self.assert_not_applied()

    def test_explicit_override_allows_apply_without_rollback(self):
        self.payload['no_rollback'] = True
        self.env['TEST_BATCTL_RC'] = '1'
        self.assertEqual(config_sync.sync_once(), 0)
        self.assertEqual(self.calls, [[config_sync.APPLY_SCRIPT]])
        self.assertFalse(self.state.exists())

    def test_override_must_be_a_boolean(self):
        for value in ('false', 'true', 1, 0, None):
            with self.subTest(value=value):
                self.payload['no_rollback'] = value
                self.assertFalse(config_sync.validate_package(self.payload)[0])
                self.assertEqual(config_sync.sync_once(), 0)
                self.assert_not_applied()

    def test_safe_change_does_not_require_batman_or_rollback(self):
        self.payload['config'] = {'mtx': 'n'}
        self.env['TEST_BATCTL_RC'] = '1'
        self.assertEqual(config_sync.sync_once(), 0)
        self.assertEqual(self.calls, [[config_sync.APPLY_SCRIPT]])
        self.assertFalse(self.state.exists())


if __name__ == '__main__':
    unittest.main()
