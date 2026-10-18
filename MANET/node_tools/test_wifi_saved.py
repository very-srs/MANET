"""Saved uplink credentials stay local and require a completed association."""

import hashlib
import base64
from contextlib import redirect_stdout
from datetime import datetime, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from manet_wifi_saved import SavedNetworks, SAVED_MISSING
import manet_wifi_uplink as wifi
from test_wifi_uplink import PROFILE, SCAN, UplinkHarness
import manet_manage as manage


class StoreTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        env = patch.dict(os.environ, {
            'MANET_WIFI_UPLINK_CONFIG': str(self.root / 'builtin.json'),
            'MANET_WIFI_UPLINK_SAVED_CONFIG': str(self.root / 'saved.json')})
        env.start()
        self.addCleanup(env.stop)
        self.store = SavedNetworks()

    def legacy(self, profile=PROFILE):
        self.store.builtin.write_text(json.dumps(profile))

    def test_migrate_builtin_once_with_exact_names_and_security(self):
        self.legacy(dict(PROFILE, ssid=' House 📶 '))
        self.assertEqual(self.store.public(), [{'ssid': ' House 📶 ', 'security': 'wpa2'}])
        self.assertEqual(self.store.resolve({'use_saved': True, 'ssid': ' House 📶 ',
                                            'security': 'wpa2'})['password'], PROFILE['password'])
        for ssid, security in [('House 📶', 'wpa2'), (' House 📶 ', 'sae')]:
            with self.assertRaisesRegex(ValueError, SAVED_MISSING):
                self.store.resolve(dict(ssid=ssid, security=security, use_saved=True))
        self.store.forget(' House 📶 ', 'wpa2')
        self.assertEqual(SavedNetworks().public(), [])
        self.assertEqual(json.loads(self.store.builtin.read_text())['password'], PROFILE['password'])

    def test_permissions_atomic_failure_and_no_temporary_secrets(self):
        self.legacy()
        self.store.public()
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.lock.stat().st_mode), 0o600)
        before = self.store.path.read_bytes()
        with patch('manet_config_io.os.replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                self.store.forget(PROFILE['ssid'], 'wpa2')
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertFalse(list(self.root.glob('.saved.json-*')))

    def test_new_pending_profile_does_not_migrate_or_overwrite(self):
        self.legacy(dict(PROFILE, save_id='a' * 32))
        self.assertEqual(self.store.public(), [])
        self.store.observe(dict(PROFILE, save_id='a' * 32),
                           {'wpa_state': '4WAY_HANDSHAKE', 'id_str': 'a' * 32})
        self.assertEqual(self.store.public(), [])

    def test_completed_attempt_saved_once_and_forget_survives_reconnect(self):
        profile = dict(PROFILE, save_id='b' * 32)
        self.legacy(profile)
        observed = {'wpa_state': 'COMPLETED', 'id_str': profile['save_id'],
                    'bssid': profile['bssid']}
        self.store.observe(profile, dict(observed, id_str='a' * 32))
        self.assertEqual(self.store.public(), [])
        self.store.observe(profile, observed)
        self.assertEqual(self.store.public(), [{'ssid': profile['ssid'], 'security': 'wpa2'}])
        before = self.store.path.stat().st_mtime_ns
        self.store.observe(profile, observed)
        self.assertEqual(self.store.path.stat().st_mtime_ns, before)
        self.store.forget(profile['ssid'], 'wpa2')
        self.store.observe(profile, observed)
        self.assertEqual(self.store.public(), [])

    def test_old_observation_cannot_save_replaced_profile(self):
        old = dict(PROFILE, save_id='a' * 32)
        self.legacy(dict(old, password='new-secret', save_id='b' * 32))
        self.store.observe(old, {'wpa_state': 'COMPLETED',
                           'id_str': old['save_id'], 'bssid': old['bssid']})
        self.assertEqual(self.store.public(), [])

    def test_forget_also_cancels_unreported_success(self):
        self.legacy()
        self.store.public()
        pending = dict(PROFILE, save_id='c' * 32)
        self.legacy(pending)
        self.store.forget('house', 'wpa2')
        self.store.observe(pending, {'wpa_state': 'COMPLETED',
            'id_str': pending['save_id'], 'bssid': pending['bssid']})
        self.assertEqual(self.store.public(), [])
        fresh = dict(pending, save_id='e' * 32)
        self.legacy(fresh)
        self.store.observe(fresh, {'wpa_state': 'COMPLETED',
            'id_str': fresh['save_id'], 'bssid': fresh['bssid']})
        self.assertEqual(self.store.public(), [{'ssid': 'house', 'security': 'wpa2'}])

    def test_capacity_evicts_least_recent_successful_use(self):
        for index in range(33):
            profile = dict(PROFILE, ssid=f'house-{index}', save_id=f'{index:032x}')
            self.legacy(profile)
            self.store.observe(profile, {'wpa_state': 'COMPLETED',
                               'id_str': profile['save_id'], 'bssid': profile['bssid']})
        entries = self.store.public()
        self.assertEqual(len(entries), 32)
        self.assertNotIn('house-0', [row['ssid'] for row in entries])

    def test_parallel_observation_and_forget_do_not_resurrect_credentials(self):
        from concurrent.futures import ThreadPoolExecutor
        profile = dict(PROFILE, save_id='a' * 32)
        self.legacy(profile)
        status = {'wpa_state': 'COMPLETED', 'id_str': profile['save_id'], 'bssid': profile['bssid']}
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(self.store.observe, profile, status)
            b = pool.submit(self.store.forget, profile['ssid'], profile['security'])
            a.result()
            b.result()
        self.store.observe(profile, status)
        self.assertEqual(self.store.public(), [])

    def test_open_networks_and_unverified_attempts_never_get_saved_badges(self):
        self.legacy(dict(PROFILE, security='open', password=''))
        rows = self.store.annotate([{'ssid': 'house', 'security': 'open'},
                                    {'ssid': 'house', 'security': 'wpa2'}])
        self.assertEqual([row['saved'] for row in rows], [False, False])
        for use_saved in ('true', 1, None):
            with self.assertRaises(ValueError):
                self.store.resolve(dict(PROFILE, password='', use_saved=use_saved))

    def test_corrupt_store_errors_do_not_echo_secrets(self):
        self.store.path.write_text('{"password":"' + PROFILE['password'])
        with self.assertRaisesRegex(ValueError, '^Could not read saved Wi-Fi networks$'):
            self.store.public()

    def test_same_name_keeps_wpa2_and_sae_credentials_separate(self):
        self.legacy(dict(PROFILE, security='sae', password='literal-sae-secret'))
        self.store.public()
        with self.store.transaction() as data:
            self.store.put(data, dict(PROFILE, password='second-secret'))
            self.store.write(data)
        self.assertEqual(len(self.store.public()), 2)
        for security, password in [('sae', 'literal-sae-secret'), ('wpa2', 'second-secret')]:
            resolved = self.store.resolve(dict(PROFILE, security=security, use_saved=True, password=''))
            self.assertEqual(resolved['password'], password)
            if security == 'sae':
                self.assertIn('sae_password="literal-sae-secret"', wifi.client_config(resolved, [5180], 'US'))

    def test_saved_store_never_enters_alfred_config_apply_or_atak_payloads(self):
        import encoder
        import mesh_config
        from manet_cot import hidden_contact, external_position, radio_beacon, Fix
        from google.protobuf.json_format import MessageToDict
        import NodeInfo_pb2
        self.legacy()
        self.store.public()
        secret = PROFILE['password']
        psk = hashlib.pbkdf2_hmac('sha1', secret.encode(), b'house', 4096, 32).hex()
        outputs = []
        tools = Path(__file__).parent
        spec = importlib.util.spec_from_file_location('saved_config_sync', tools / 'mesh-config-sync.py')
        sync = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sync)
        for key in ('uplink_wifi', 'wifi_uplink_saved', 'saved_networks'):
            value = {key: self.store.path.read_text()}
            self.assertFalse(mesh_config.validate_config(value, local=True)[0])
            self.assertFalse(sync.validate_package({'version': 'abcdef', 'config': value})[0])
            conf = self.root / 'mesh.conf'
            conf.write_text('mesh_ssid=MANET\n')
            self.assertEqual(mesh_config.apply_local_to_conf(value, conf), [])
            outputs.append(conf.read_text())
        with patch.object(manage, 'send_alfred_object') as send:
            result = manage.coordinate_radio_change({'uplink_wifi': {'saved_networks': self.store.path.read_text()}})
            self.assertFalse(result['ok'])
            send.assert_not_called()
            outputs.append(json.dumps(result))
        spec = importlib.util.spec_from_file_location('saved_radio_state', tools / 'mesh-radio-state.py')
        radio = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(radio)
        self.assertFalse(radio.validate_pkg({'kind': 'radio_state', 'version': 'abcdef',
                                            'uplink_wifi': PROFILE})[0])
        for kind, args, message in [
                ('identity', ['--hostname', 'radio', '--mac-addresses', '02:00:00:00:00:01'], NodeInfo_pb2.NodeIdentity()),
                ('telemetry', ['--timestamp', '100'], NodeInfo_pb2.NodeTelemetry())]:
            output = io.StringIO()
            with patch.object(sys, 'argv', ['encoder.py', kind, *args]), redirect_stdout(output):
                encoder.main()
            message.ParseFromString(base64.b64decode(output.getvalue()))
            outputs += [output.getvalue(), json.dumps(MessageToDict(message))]
        now = datetime(2026, 10, 18, tzinfo=timezone.utc)
        fix = Fix(39, -105, 'manual')
        outputs += [hidden_contact('radio', 'radio', ('192.0.2.1', 4242), now).decode(),
                    external_position('radio', fix, now).decode(),
                    radio_beacon('radio', 'radio', fix, now).decode()]
        serialized = '\n'.join(outputs)
        for private in (secret, psk, 'wifi-uplink-saved', 'saved_networks'):
            self.assertNotIn(private, serialized)


class BuiltinSavedTests(UplinkHarness):
    def association(self, profile=None):
        profile = profile or json.loads(self.uplink.config.read_text())
        return ('wpa_state=COMPLETED\nid_str=' + profile['save_id'] +
                '\nbssid=' + profile['bssid'] + '\n')

    def test_only_completed_association_saves_and_wrong_password_keeps_good_copy(self):
        self.choose()
        self.assertEqual(SavedNetworks().public(), [])
        self.model.associated = 5180
        original = self.uplink.invoke
        def invoke(args, **kwargs):
            return self.association() if args[0] == 'wpa_cli' and args[-1] == 'status' else original(args, **kwargs)
        with patch.object(self.uplink, 'invoke', side_effect=invoke):
            self.uplink.follow_ap(self.uplink.profile())
        self.assertEqual(SavedNetworks().public(), [{'ssid': 'house', 'security': 'wpa2'}])
        self.choose(dict(PROFILE, password='wrong-password'))
        self.model.associated = None
        self.uplink.follow_ap(self.uplink.profile())
        self.assertEqual(SavedNetworks().resolve(dict(PROFILE, password='', use_saved=True))['password'], PROFILE['password'])

    def test_saved_selection_lookup_and_refusal_before_role_changes(self):
        self.uplink.store(self.uplink.config, PROFILE)
        self.choose(dict(PROFILE, password='', use_saved=True))
        self.assertEqual(self.uplink.profile()['password'], PROFILE['password'])
        SavedNetworks().forget('house', 'wpa2')
        calls = list(self.radio.calls)
        with self.assertRaisesRegex(ValueError, SAVED_MISSING):
            self.choose(dict(PROFILE, password='', use_saved=True))
        self.assertEqual(self.radio.calls, calls)

    def test_scan_and_status_never_contain_credentials(self):
        self.uplink.store(self.uplink.config, PROFILE)
        built = self.uplink.discover('wlan1')
        self.assertTrue(built['networks'][0]['saved'])
        self.assertFalse(built['networks'][1]['saved'])
        secret = PROFILE['password']
        psk = hashlib.pbkdf2_hmac('sha1', secret.encode(), b'house', 4096, 32).hex()
        output = json.dumps([built, self.uplink.read_status(), SavedNetworks().public()])
        output += self.uplink.scan_path.read_text() + '\n'.join(self.radio.calls)
        self.assertNotIn(secret, output)
        self.assertNotIn(psk, output)

    def test_activation_failure_preserves_saved_password_and_previous_profile(self):
        self.uplink.store(self.uplink.config, PROFILE)
        SavedNetworks().public()
        original = self.uplink.activate
        def activate(profile):
            if profile['password'] == 'wrong-password':
                raise subprocess.CalledProcessError(1, ['helper', 'wrong-password'])
            return original(profile)
        with patch.object(self.uplink, 'activate', side_effect=activate):
            with self.assertRaisesRegex(RuntimeError, 'previous profile restored') as error:
                self.choose(dict(PROFILE, password='wrong-password'))
        self.assertNotIn('wrong-password', str(error.exception))
        self.assertEqual(self.uplink.profile()['password'], PROFILE['password'])
        self.assertEqual(SavedNetworks().resolve(dict(PROFILE, password='', use_saved=True))['password'], PROFILE['password'])

    def test_admin_status_scan_connect_and_forget_responses_are_secret_free(self):
        from test_web_sessions import WebSessionTests
        http = WebSessionTests()
        http.setUp()
        self.addCleanup(http.doCleanups)
        self.uplink.store(self.uplink.config, PROFILE)
        SavedNetworks().public()
        def helper(action, request=None):
            if action == 'status':
                return self.uplink.read_status()
            if action == 'scan':
                return self.uplink.discover(request['iface'])
            if action == 'set':
                return self.uplink.set_profile(request)
            if action == 'forget':
                return {'saved_networks': SavedNetworks().forget(request['ssid'], request['security'])}
            raise AssertionError(action)
        _, cookie = http.login()
        responses = []
        with patch.object(manage, 'builtin_wifi_request', side_effect=helper):
            for method, suffix, request in [
                    ('GET', '', None), ('POST', '/scan', {'iface': 'wlan1'}),
                    ('POST', '', dict(PROFILE, password='', use_saved=True)),
                    ('POST', '/forget', {'ssid': 'house', 'security': 'wpa2'})]:
                response = http.request(method, '/manage/api/uplink/wifi' + suffix,
                    json.dumps(request).encode() if request else b'', cookie)
                self.assertEqual(response.status, 200)
                self.assertIsNot(json.loads(response.payload).get('ok'), False)
                responses.append(response.payload.decode())
        psk = hashlib.pbkdf2_hmac('sha1', PROFILE['password'].encode(), b'house', 4096, 32).hex()
        for secret in (PROFILE['password'], psk):
            self.assertNotIn(secret, '\n'.join(responses))




class HttpSavedTests(unittest.TestCase):
    legacy = StoreTests.legacy

    def setUp(self):
        StoreTests.setUp(self)
        from test_web_sessions import WebSessionTests
        self.http = WebSessionTests()
        self.http.setUp()
        self.addCleanup(self.http.doCleanups)

    def test_forget_requires_admin_session_and_only_post_mutates(self):
        self.legacy()
        self.store.public()
        def helper(action, request=None):
            self.assertEqual(action, 'forget')
            return {'saved_networks': self.store.forget(request['ssid'], request['security'])}
        url = '/manage/api/uplink/wifi/forget'
        body = json.dumps({'ssid': 'house', 'security': 'wpa2'}).encode()
        with patch.object(manage, 'builtin_wifi_request', side_effect=helper) as call:
            self.assertEqual(self.http.request('POST', url, body).status, 401)
            call.assert_not_called()
            _, cookie = self.http.login()
            self.assertEqual(self.http.request('GET', url, cookie=cookie).status, 404)
            call.assert_not_called()
            response = self.http.request('POST', url, body, cookie)
            self.assertTrue(json.loads(response.payload)['ok'])
            self.assertEqual(self.store.public(), [])
            self.assertNotIn(PROFILE['password'], response.payload.decode())

    def test_api_transport_errors_never_echo_subprocess_secrets(self):
        password = PROFILE['password']
        psk = hashlib.pbkdf2_hmac('sha1', password.encode(), b'house', 4096, 32).hex()
        _, cookie = self.http.login()
        for iface in ('wlan0', 'wlan1'):
            body = json.dumps(dict(PROFILE, iface=iface)).encode()
            with patch.object(manage.subprocess, 'run', side_effect=subprocess.CalledProcessError(
                    1, ['helper', password, psk], output=password, stderr=psk)):
                response = self.http.request('POST', '/manage/api/uplink/wifi', body, cookie)
            self.assertFalse(json.loads(response.payload)['ok'])
            self.assertNotIn(password, response.payload.decode())
            self.assertNotIn(psk, response.payload.decode())


if __name__ == '__main__':
    unittest.main()
