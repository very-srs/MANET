#!/usr/bin/env python3
"""Exercise real encryption, receivers and publishers without live interfaces."""

import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from cryptography.exceptions import InvalidTag
from manet_admin import AdminError, AdminTransport, CONFIG_ACK_TYPE, new_version
import manet_manage


TOOLS = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


config_sync = load('admin_test_config_sync', 'mesh-config-sync.py')
radio_sync = load('admin_test_radio_sync', 'mesh-radio-state.py')
status = load('admin_test_status', 'mesh-status.py')
writer = load('admin_test_writer', 'mesh-config-write.py')


def frame(envelope):
    return '{ "02:00:00:00:00:01", ' + json.dumps(json.dumps(envelope)) + ' },\n'


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.conf = self.root / 'mesh.conf'
        self.conf.write_text('admin_password=shared-test-password\nmtx=y\n')
        self.sender = AdminTransport(self.conf, self.root / 'sender')
        self.receiver = AdminTransport(self.conf, self.root / 'receiver')
        self.now = 1_800_000_000_000_000_000
        clock = patch('manet_admin.time.time_ns', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.payload = {
            'kind': 'mesh_config', 'version': new_version(), 'issued_at': self.now // 10**9,
            'activate_at': 0, 'config': {'mtx': 'n'},
        }
        self.raw = ''
        self.calls = []

    def fake_run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, self.raw if args[:2] == ['alfred', '-r'] else '', '')

    def receiver_patches(self, module, channel):
        for name, value in {
            'ADMIN': self.receiver, 'PENDING_FILE': str(self.root / f'pending-{channel}'),
            'ACK_VERSION_FILE': str(self.root / f'ack-{channel}'),
            'APPLIED_VERSION_FILE': str(self.root / f'applied-{channel}'),
        }.items():
            p = patch.object(module, name, value)
            p.start()
            self.addCleanup(p.stop)
        for p in (patch.object(module, 'log'),
                  patch.object(module, 'run', side_effect=self.fake_run)):
            p.start()
            self.addCleanup(p.stop)

    def test_roundtrip_hides_password_and_binds_channel(self):
        self.payload['config']['admin_password'] = 'new-secret-placeholder'
        envelope = self.sender.seal(70, self.payload)
        self.assertNotIn('new-secret-placeholder', json.dumps(envelope))
        self.assertNotIn('admin_password', json.dumps(envelope))
        self.assertEqual(self.receiver.open(70, envelope).payload, self.payload)
        with self.assertRaises(InvalidTag):
            self.receiver.open(71, envelope)
        second = self.sender.seal(70, self.payload)
        self.assertNotEqual(envelope['nonce'], second['nonce'])
        self.assertNotEqual(envelope['ciphertext'], second['ciphertext'])

    def test_wrong_password_and_tampering_are_rejected(self):
        envelope = self.sender.seal(70, self.payload)
        other = self.root / 'other.conf'
        other.write_text('admin_password=wrong-password\n')
        with self.assertRaises(InvalidTag):
            AdminTransport(other, self.root / 'other').open(70, envelope)
        for field in ('salt', 'nonce', 'ciphertext'):
            with self.subTest(field=field):
                changed = dict(envelope)
                raw = bytearray(base64.b64decode(changed[field]))
                raw[0] ^= 1
                changed[field] = base64.b64encode(raw).decode()
                with self.assertRaises(InvalidTag):
                    self.receiver.open(70, changed)

    def test_mesh_or_ap_password_never_substitutes_for_admin_password(self):
        self.conf.write_text('mesh_key=shared-test-password\nlan_ap_key=shared-test-password\n')
        with self.assertRaises(AdminError):
            self.sender.seal(70, self.payload)
        self.assertEqual(status.get_provisioned_manage_password(
            {'radio_password': 'radio', 'lan_ap_key': 'ap'}), '')

    def test_expired_and_future_records_are_rejected(self):
        envelope = self.sender.seal(70, self.payload)
        self.now += 901 * 10**9
        with self.assertRaises(AdminError):
            self.receiver.open(70, envelope)
        self.now -= 962 * 10**9
        with self.assertRaises(AdminError):
            self.receiver.open(70, envelope)

    def test_plaintext_and_forged_future_timestamp_cannot_hide_valid_message(self):
        valid = self.sender.seal(70, self.payload)
        forged = dict(self.payload, issued_at=10**30, activate_at=1)
        messages = self.receiver.messages(70, frame(forged) + frame(valid))
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].payload, self.payload)

    def test_pending_repeats_allowed_but_completed_replay_survives_restart(self):
        message = self.receiver.open(70, self.sender.seal(70, self.payload))
        self.assertTrue(self.receiver.accept(70, message))
        self.assertTrue(self.receiver.accept(70, message))
        self.receiver.complete(70, message)
        restarted = AdminTransport(self.conf, self.root / 'receiver')
        self.assertFalse(restarted.accept(70, message))
        state = self.root / 'receiver/received-70.json'
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(state.parent.stat().st_mode), 0o700)

    def test_cancel_prevents_replay_of_earlier_activation(self):
        activate = dict(self.payload, activate_at=1)
        older = self.receiver.open(70, self.sender.seal(70, activate))
        cancel = {'kind': 'mesh_config_cancel', 'version': self.payload['version']}
        newer = self.receiver.open(70, self.sender.seal(70, cancel))
        self.assertTrue(self.receiver.accept(70, newer))
        self.receiver.complete(70, newer)
        self.assertFalse(self.receiver.accept(70, older))

    def test_corrupt_replay_history_fails_closed(self):
        message = self.receiver.open(70, self.sender.seal(70, self.payload))
        self.receiver.accept(70, message)
        (self.root / 'receiver/received-70.json').write_text('not-json')
        with self.assertRaises(ValueError):
            self.receiver.accept(70, message)

    def test_password_rotation_uses_old_password_to_deliver_new_password(self):
        self.payload['config']['admin_password'] = 'replacement-test-password'
        envelope = self.sender.seal(70, self.payload)
        message = self.receiver.open(70, envelope)
        self.receiver.accept(70, message)
        self.conf.write_text('admin_password=replacement-test-password\n')
        self.receiver.complete(70, message)
        with self.assertRaises(InvalidTag):
            self.receiver.open(70, envelope)
        next_message = self.sender.seal(70, dict(self.payload, version=new_version()))
        self.assertEqual(self.receiver.open(70, next_message).payload['config'],
                         self.payload['config'])

    def test_config_receiver_rejects_unsigned_apply_and_cancel(self):
        self.receiver_patches(config_sync, 70)
        pending = Path(config_sync.PENDING_FILE)
        pending.write_text(json.dumps(self.payload))
        for payload in (dict(self.payload, activate_at=1),
                        {'kind': 'mesh_config_cancel', 'issued_at': 10**30}):
            with self.subTest(kind=payload['kind']):
                self.raw = frame(payload)
                self.assertEqual(config_sync.sync_once(), 0)
                self.assertTrue(pending.exists())
                self.assertFalse(any(args == [config_sync.APPLY_SCRIPT] for args, _ in self.calls))
                self.assertFalse(Path(config_sync.ACK_VERSION_FILE).exists())

    def test_authenticated_config_stages_acks_activates_once_and_resists_rollback_replay(self):
        self.receiver_patches(config_sync, 70)
        self.raw = frame(self.sender.seal(70, self.payload))
        self.assertEqual(config_sync.sync_once(), 0)
        self.assertTrue(Path(config_sync.PENDING_FILE).exists())
        self.assertFalse(any(args == [config_sync.APPLY_SCRIPT] for args, _ in self.calls))
        ack_wire = next(kwargs['input'] for args, kwargs in self.calls
                        if args == ['alfred', '-s', str(CONFIG_ACK_TYPE)])
        self.assertEqual(self.sender.open(CONFIG_ACK_TYPE, json.loads(ack_wire)).payload['version'],
                         self.payload['version'])
        self.raw = frame(self.sender.seal(70, dict(self.payload, activate_at=1)))
        self.assertEqual(config_sync.sync_once(), 0)
        # Simulate reboot/rollback losing every volatile file.
        for name in ('PENDING_FILE', 'ACK_VERSION_FILE', 'APPLIED_VERSION_FILE'):
            Path(getattr(config_sync, name)).unlink(missing_ok=True)
        self.assertEqual(config_sync.sync_once(), 0)
        self.assertEqual(sum(args == [config_sync.APPLY_SCRIPT] for args, _ in self.calls), 1)
        self.assertFalse(Path(config_sync.PENDING_FILE).exists())

    def test_authenticated_cancel_clears_pending(self):
        self.receiver_patches(config_sync, 70)
        self.raw = frame(self.sender.seal(70, self.payload))
        config_sync.sync_once()
        cancel = {'kind': 'mesh_config_cancel', 'version': self.payload['version']}
        self.raw = frame(self.sender.seal(70, cancel))
        self.assertEqual(config_sync.sync_once(), 0)
        self.assertFalse(Path(config_sync.PENDING_FILE).exists())

    def test_radio_receiver_rejects_unsigned_and_applies_authenticated_once(self):
        self.receiver_patches(radio_sync, 71)
        payload = {'kind': 'radio_state', 'version': new_version(), 'activate_at': 1,
                   'targets': 'all', 'voice_codec': {'codec': 'opus'}}
        with patch.object(radio_sync, 'apply_package') as apply, \
                patch.object(radio_sync, 'send_alfred', return_value=True):
            self.raw = frame(payload)
            self.assertEqual(radio_sync.sync_once(), 0)
            apply.assert_not_called()
            self.raw = frame(self.sender.seal(71, payload))
            self.assertEqual(radio_sync.sync_once(), 0)
            apply.assert_called_once_with(payload)
            Path(radio_sync.APPLIED_VERSION_FILE).unlink()
            self.assertEqual(radio_sync.sync_once(), 0)
            apply.assert_called_once()

    def test_plaintext_ack_cannot_satisfy_radio_or_config_gate(self):
        for type_id, kind in ((72, 'radio_ack'), (CONFIG_ACK_TYPE, 'config_ack')):
            with self.subTest(kind=kind):
                ack = {'kind': kind, 'hostname': 'mesh-test',
                       'version': self.payload['version'], 'ok': True}
                self.raw = frame(ack)
                with patch.object(manet_manage, 'ADMIN', self.receiver), \
                        patch.object(status, 'ADMIN', self.receiver), \
                        patch('subprocess.run', side_effect=self.fake_run):
                    self.assertEqual(manet_manage.read_alfred_objects(type_id, kind), [])
                    self.assertEqual(status.authenticated_config_acks(), {})
                    self.raw = frame(self.sender.seal(type_id, ack))
                    self.assertEqual(manet_manage.read_alfred_objects(type_id, kind), [ack])
                    if type_id == CONFIG_ACK_TYPE:
                        self.assertEqual(status.authenticated_config_acks(),
                                         {'mesh-test': self.payload['version']})

    def test_interrupted_radio_apply_is_not_replayed(self):
        self.receiver_patches(radio_sync, 71)
        payload = {'kind': 'radio_state', 'version': new_version(), 'activate_at': 1,
                   'targets': 'all', 'voice_codec': {'codec': 'opus'}}
        self.raw = frame(self.sender.seal(71, payload))
        with patch.object(radio_sync, 'apply_package', side_effect=SystemExit) as apply, \
                patch.object(radio_sync, 'send_alfred', return_value=True):
            with self.assertRaises(SystemExit):
                radio_sync.sync_once()
            for name in ('PENDING_FILE', 'ACK_VERSION_FILE', 'APPLIED_VERSION_FILE'):
                Path(getattr(radio_sync, name)).unlink(missing_ok=True)
            self.assertEqual(radio_sync.sync_once(), 0)
            apply.assert_called_once()

    def test_interrupted_config_apply_is_not_replayed(self):
        self.receiver_patches(config_sync, 70)
        self.raw = frame(self.sender.seal(70, dict(self.payload, activate_at=1)))
        def interrupted(args, **kwargs):
            if args == [config_sync.APPLY_SCRIPT]:
                raise SystemExit
            return self.fake_run(args, **kwargs)
        with patch.object(config_sync, 'run', side_effect=interrupted) as run:
            with self.assertRaises(SystemExit):
                config_sync.sync_once()
            for name in ('PENDING_FILE', 'ACK_VERSION_FILE', 'APPLIED_VERSION_FILE'):
                Path(getattr(config_sync, name)).unlink(missing_ok=True)
            self.assertEqual(config_sync.sync_once(), 0)
            self.assertEqual(sum(call.args[0] == [config_sync.APPLY_SCRIPT]
                                 for call in run.call_args_list), 1)

    def test_publishers_encrypt_and_report_failures(self):
        with patch.object(status, 'ADMIN', self.sender), \
                patch.object(manet_manage, 'ADMIN', self.sender), \
                patch('subprocess.run', side_effect=self.fake_run):
            self.assertTrue(status.broadcast_config_package(self.payload))
            wire = json.loads(self.calls[-1][1]['input'])
            self.assertEqual(self.receiver.open(70, wire).payload, self.payload)
            cancel = {'kind': 'radio_cancel', 'version': new_version()}
            self.assertTrue(manet_manage.send_alfred_object(71, cancel)[0])
            wire = json.loads(self.calls[-1][1]['input'])
            self.assertEqual(self.receiver.open(71, wire).payload, cancel)
            self.conf.write_text('admin_password=\n')
            self.assertFalse(status.broadcast_config_package(self.payload))
            self.assertFalse(manet_manage.send_alfred_object(71, cancel)[0])

    def test_identical_edits_have_different_ack_transaction_ids(self):
        self.assertNotEqual(status.make_config_version(self.payload),
                            status.make_config_version(self.payload))
        self.assertNotEqual(manet_manage.make_radio_version(self.payload),
                            manet_manage.make_radio_version(self.payload))


class LiteralConfigTests(unittest.TestCase):
    def test_shell_writer_preserves_metacharacters_without_execution(self):
        with tempfile.TemporaryDirectory() as scratch:
            conf = Path(scratch) / 'mesh.conf'
            source = (TOOLS / 'mesh-config-apply.sh').read_text()
            setter = re.search(r'^conf_set\(\) \{\n.*?^\}', source, re.M | re.S)[0]
            for value in ('abc&12345', 'x|;e printf REVIEW_MARKER #',
                          r'abc\1$hello' + chr(96) + 'id' + chr(96)):
                with self.subTest(value=value):
                    self.assertTrue(config_sync.valid_value('admin_password', value)[0])
                    conf.write_text('admin_password=old\nmtx=y\n')
                    conf.chmod(0o640)
                    env = dict(os.environ, MESH_CONF=str(conf),
                               CONFIG_WRITER=str(TOOLS / 'mesh-config-write.py'))
                    result = subprocess.run(['bash', '-c', setter + '\nconf_set admin_password "$1"',
                                             'test', value], env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(conf.read_text(), f'admin_password={value}\nmtx=y\n')
                    self.assertEqual(result.stdout, '')
                    self.assertEqual(stat.S_IMODE(conf.stat().st_mode), 0o640)

    def test_supplicant_values_are_literal_and_other_lines_survive(self):
        with tempfile.TemporaryDirectory() as scratch:
            conf = Path(scratch) / 'wpa.conf'
            conf.write_text('network={\n  ssid="old"\n  sae_password=old\n}\n')
            writer.write_key(conf, 'ssid', r'MANET|&\test', quoted=True)
            writer.write_key(conf, 'sae_password', 'pass|&word')
            self.assertEqual(conf.read_text(),
                             'network={\n  ssid="MANET|&\\\\test"\n  sae_password=pass|&word\n}\n')
            before = conf.read_bytes()
            with self.assertRaises(ValueError):
                writer.write_key(conf, 'sae_password', 'bad\ninjected=1')
            self.assertEqual(conf.read_bytes(), before)


class WebBoundaryTests(unittest.TestCase):
    def handler(self, path, authenticated=False):
        handler = object.__new__(status.MeshHandler)
        handler.path = path
        handler.client_address = ('127.0.0.1', 12345)
        handler.headers = {}
        handler.rfile = io.BytesIO(b'{}')
        handler._is_perf_host = Mock(return_value=False)
        handler._perf_cookie_valid = Mock(return_value=authenticated)
        handler.send_json = Mock()
        handler.send_html = Mock()
        handler.send_401_json = Mock()
        return handler

    def test_status_remains_public_and_admin_routes_require_login(self):
        with patch.object(status, 'load_kv_file', return_value={'admin_password': 'test'}), \
                patch.object(status, 'is_allowed_ip', return_value=True), \
                patch.object(status, 'render_status_page', return_value='public status'), \
                patch.object(status, 'assemble_status_data', return_value={'nodes': []}), \
                patch.object(status, 'assemble_admin_status') as admin_status, \
                patch.object(status, 'broadcast_config_package') as broadcast:
            handler = self.handler('/')
            handler.do_GET()
            handler.send_html.assert_called_once_with('public status')
            handler = self.handler('/api/data')
            handler.do_GET()
            handler.send_json.assert_called_once_with({'nodes': []})
            handler = self.handler('/api/admin/status')
            handler.do_GET()
            handler.send_401_json.assert_called_once()
            admin_status.assert_not_called()
            for path in ('/api/admin/stage', '/api/admin/activate', '/api/admin/cancel'):
                handler = self.handler(path)
                handler.do_POST()
                handler.send_401_json.assert_called_once()
            broadcast.assert_not_called()

    def test_forged_public_telemetry_ack_cannot_authorize_activation(self):
        version = new_version()
        with patch.object(status, 'load_kv_file', return_value={'admin_password': 'test'}), \
                patch.object(status, 'is_allowed_ip', return_value=True), \
                patch.object(status, 'get_pending_config', return_value={'version': version}), \
                patch.object(status, 'parse_registry', return_value={
                    'peer': {'HOSTNAME': 'mesh-peer', 'CONFIG_ACK_VERSION': version}}), \
                patch.object(status, 'authenticated_config_acks', return_value={}), \
                patch.object(status, 'broadcast_config_package') as broadcast:
            handler = self.handler('/api/admin/activate', authenticated=True)
            handler.do_POST()
            self.assertFalse(handler.send_json.call_args.args[0]['ok'])
            self.assertIn('not ACKed', handler.send_json.call_args.args[0]['error'])
            broadcast.assert_not_called()


if __name__ == '__main__':
    unittest.main()
