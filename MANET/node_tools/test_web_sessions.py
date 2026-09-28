#!/usr/bin/env python3
"""Session lifetime and HTTP login/logout checks without a listening socket."""

from concurrent.futures import ThreadPoolExecutor
import http.client
from http.cookies import SimpleCookie
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from manet_web_sessions import SessionStore


TOOLS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('web_session_status', TOOLS / 'mesh-status.py')
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.password = 'shared-test-password'
        self.now = 1000.0
        self.store = self.new_store()

    def new_store(self, limit=64):
        return SessionStore(lambda: self.password, lifetime=120, limit=limit,
                            clock=lambda: self.now)

    def test_logins_have_independent_random_tokens(self):
        first = self.store.login(self.password)
        second = self.store.login(self.password)
        self.assertNotEqual(first, second)
        self.assertRegex(first, r'^[A-Za-z0-9_-]{43}$')
        self.assertTrue(self.store.valid(first))
        self.assertTrue(self.store.valid(second))
        self.assertFalse(self.store.valid('a' * 43))
        self.assertFalse(self.store.valid('a' * 64))

    def test_bad_credentials_and_malformed_tokens_are_rejected(self):
        for supplied in ('', 'wrong', None, {}, '\ud800'):
            with self.subTest(password=repr(supplied)):
                self.assertIsNone(self.store.login(supplied))
        for token in ('', None, {}, 'é' * 43, 'a' * 10000):
            with self.subTest(token=repr(token)[:30]):
                self.assertFalse(self.store.valid(token))
                self.store.logout(token)
        self.password = 'pássword-安全'
        self.assertTrue(self.store.valid(self.store.login(self.password)))

    def test_logout_revokes_only_that_session(self):
        first = self.store.login(self.password)
        second = self.store.login(self.password)
        self.store.logout(first)
        self.store.logout(first)
        self.assertFalse(self.store.valid(first))
        self.assertTrue(self.store.valid(second))

    def test_successful_relogin_replaces_old_token(self):
        first = self.store.login(self.password)
        other = self.store.login(self.password)
        self.assertIsNone(self.store.login('wrong', first))
        self.assertTrue(self.store.valid(first))
        replacement = self.store.login(self.password, first)
        self.assertNotEqual(first, replacement)
        self.assertFalse(self.store.valid(first))
        self.assertTrue(self.store.valid(replacement))
        self.assertTrue(self.store.valid(other))

    def test_expiry_is_absolute_and_independent_of_wall_clock(self):
        token = self.store.login(self.password)
        for offset in (20, 40, 80, 119):
            self.now = 1000 + offset
            with patch('time.time', return_value=0 if offset < 50 else 4_000_000_000):
                self.assertTrue(self.store.valid(token))
        self.now = 1120
        self.assertFalse(self.store.valid(token))

    def test_password_changes_or_removal_invalidate_sessions(self):
        for changed in ('new-password', ''):
            with self.subTest(changed=changed):
                self.password = 'original'
                token = self.store.login(self.password)
                self.password = changed
                self.assertFalse(self.store.valid(token))
                self.assertIsNone(self.store.login('original'))
                self.password = 'original'
                self.assertFalse(self.store.valid(token))

    def test_other_nodes_and_restarted_processes_reject_old_tokens(self):
        token = self.store.login(self.password)
        other = self.new_store()
        self.assertFalse(other.valid(token))
        self.assertFalse(self.store.valid(other.login(self.password)))

    def test_limit_evicts_oldest_only_after_successful_login(self):
        self.store = self.new_store(limit=2)
        first = self.store.login(self.password)
        second = self.store.login(self.password)
        self.assertIsNone(self.store.login('wrong'))
        self.assertTrue(self.store.valid(first))
        third = self.store.login(self.password)
        self.assertFalse(self.store.valid(first))
        self.assertTrue(self.store.valid(second))
        self.assertTrue(self.store.valid(third))

    def test_expired_entries_make_room_for_new_sessions(self):
        self.store = self.new_store(limit=2)
        old = self.store.login(self.password)
        self.now += 60
        recent = self.store.login(self.password)
        self.now += 60
        new = self.store.login(self.password)
        self.assertFalse(self.store.valid(old))
        self.assertTrue(self.store.valid(recent))
        self.assertTrue(self.store.valid(new))

    def test_concurrent_logins_and_logouts(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            tokens = list(pool.map(self.store.login, [self.password] * 32))
            self.assertEqual(len(set(tokens)), 32)
            self.assertTrue(all(pool.map(self.store.valid, tokens)))
            list(pool.map(self.store.logout, tokens[::2]))
            self.assertEqual(list(pool.map(self.store.valid, tokens)),
                             [bool(i % 2) for i in range(32)])


class MemoryConnection:
    def __init__(self, raw):
        self.raw = raw
        self.output = bytearray()

    def settimeout(self, seconds):
        pass

    def makefile(self, *args):
        return io.BytesIO(self.raw)

    def sendall(self, data):
        self.output.extend(data)


class WebSessionTests(unittest.TestCase):
    def setUp(self):
        status.STATUS_CACHE.invalidate()
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.conf = Path(scratch.name) / 'mesh.conf'
        self.password = 'audit-test-password'
        self.write_conf(self.password)
        self.now = 1000.0
        self.store = SessionStore(status.get_provisioned_manage_password, lifetime=120,
                                  clock=lambda: self.now)
        for target, name, value in (
                (status, 'MESH_CONF_FILE', str(self.conf)),
                (status, 'WEB_SESSIONS', self.store)):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def write_conf(self, password):
        self.conf.write_text(f'admin_password={password}\nipv4_network=10.30.2.0/24\n'
                             'mesh_key=radio-password\nlan_ap_key=ap-password\n')

    def request(self, method, path, body=b'', cookie='', client='127.0.0.1'):
        headers = [f'{method} {path} HTTP/1.1', 'Host: manet.local',
                   f'Content-Length: {len(body)}']
        if cookie:
            headers.append(f'Cookie: {status.PERF_AUTH_COOKIE}={cookie}')
        raw = ('\r\n'.join(headers) + '\r\n\r\n').encode() + body
        connection = MemoryConnection(raw)
        status.MeshHandler(connection, (client, 12345), SimpleNamespace())
        response = http.client.HTTPResponse(MemoryConnection(bytes(connection.output)))
        response.begin()
        response.payload = response.read()
        return response

    def login(self, path='/manage/login', cookie='', next_path='/manage/'):
        body = urlencode({'password': self.password, 'next': next_path}).encode()
        response = self.request('POST', path, body, cookie)
        self.assertEqual(response.status, 303)
        cookies = SimpleCookie(response.getheader('Set-Cookie'))
        return response, cookies[status.PERF_AUTH_COOKIE].value

    def test_form_and_legacy_logins_set_private_cookie_and_safe_redirect(self):
        for path in ('/manage/login', '/auth/perf-login'):
            with self.subTest(path=path):
                response, token = self.login(path, next_path='/manage/?theme=dark#voice')
                self.assertEqual(response.getheader('Location'), '/manage/?theme=dark#voice')
                self.assertEqual(response.getheader('Cache-Control'), 'no-store')
                cookie = SimpleCookie(response.getheader('Set-Cookie'))[status.PERF_AUTH_COOKIE]
                self.assertEqual(cookie['max-age'], '120')
                self.assertEqual(cookie['path'], '/')
                self.assertTrue(cookie['httponly'])
                self.assertEqual(cookie['samesite'], 'Lax')
                self.assertTrue(self.store.valid(token))

    def test_json_login_returns_session_and_sets_cookie(self):
        response = self.request('POST', '/api/perf-auth',
                                json.dumps({'password': self.password}).encode())
        self.assertEqual(response.status, 200)
        data = json.loads(response.payload)
        cookie = SimpleCookie(response.getheader('Set-Cookie'))[status.PERF_AUTH_COOKIE]
        self.assertEqual(cookie.value, data['token'])
        self.assertEqual(data['expires_in'], 120)
        self.assertEqual(response.getheader('Cache-Control'), 'no-store')
        self.assertTrue(self.store.valid(data['token']))

    def test_json_login_rejects_bad_body_and_non_admin_passwords(self):
        bodies = [b'not json', b'[]', b'null', b'{}']
        bodies += [json.dumps({'password': value}).encode()
                   for value in (None, [], '\ud800', 'wrong', 'radio-password', 'ap-password')]
        for body in bodies:
            with self.subTest(body=body):
                response = self.request('POST', '/api/perf-auth', body)
                self.assertEqual(response.status, 401)
                self.assertFalse(json.loads(response.payload)['ok'])
                self.assertIsNone(response.getheader('Set-Cookie'))

    def test_logout_rejects_replayed_cookie_but_other_login_survives(self):
        for path in ('/manage/logout', '/auth/perf-logout'):
            with self.subTest(path=path):
                _, first = self.login()
                _, other = self.login()
                response = self.request('GET', path, cookie=first)
                self.assertEqual(response.status, 303)
                self.assertIn('Max-Age=0', response.getheader('Set-Cookie'))
                self.assertEqual(response.getheader('Cache-Control'), 'no-store')
                self.assertEqual(self.request('GET', '/manage/api/measure/status', cookie=first).status, 401)
                self.assertEqual(self.request('GET', '/manage/api/measure/status', cookie=other).status, 200)

    def test_relogin_invalidates_prior_cookie(self):
        _, first = self.login()
        _, replacement = self.login(cookie=first)
        self.assertNotEqual(first, replacement)
        self.assertEqual(self.request('GET', '/manage/api/measure/status', cookie=first).status, 401)
        self.assertEqual(self.request('GET', '/manage/api/measure/status', cookie=replacement).status, 200)

    def test_expired_session_cannot_reach_get_post_or_delete_handlers(self):
        _, token = self.login()
        self.now += 120
        with patch('manet_manage.set_voice_channel') as voice, \
                patch('manet_manage.delete_session') as delete, \
                patch.object(status, 'assemble_admin_status') as config, \
                patch.object(status, 'broadcast_config_package') as publish:
            routes = [('GET', '/manage/api/measure/status'),
                      ('POST', '/manage/api/voice/channel'),
                      ('DELETE', '/manage/api/sessions/audit'),
                      ('GET', '/api/admin/status'),
                      ('POST', '/api/admin/stage'),
                      ('POST', '/api/admin/activate'),
                      ('POST', '/api/admin/cancel')]
            for method, path in routes:
                with self.subTest(method=method, path=path):
                    response = self.request(method, path, b'{}', token)
                    self.assertEqual(response.status, 401)
                    self.assertTrue(json.loads(response.payload)['auth_required'])
            voice.assert_not_called()
            delete.assert_not_called()
            config.assert_not_called()
            publish.assert_not_called()
        self.assertEqual(self.request('GET', '/manage/', cookie=token).status, 401)

    def test_valid_cookie_reaches_management_handlers(self):
        _, token = self.login()
        with patch('manet_manage.set_voice_channel', return_value={'ok': True}) as voice, \
                patch('manet_manage.delete_session', return_value=(True, '')) as delete, \
                patch.object(status, 'assemble_admin_status', return_value={'config': {}}) as config, \
                patch.object(status, 'get_my_hostname', return_value='test-node'):
            response = self.request('POST', '/manage/api/voice/channel', b'{"channel":2}', token)
            self.assertEqual(response.status, 200)
            voice.assert_called_once_with(2)
            response = self.request('DELETE', '/manage/api/sessions/audit', cookie=token)
            self.assertEqual(response.status, 200)
            delete.assert_called_once_with('audit')
            response = self.request('GET', '/api/admin/status', cookie=token)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader('Cache-Control'), 'no-store')
            config.assert_called_once()

    def test_password_change_and_missing_config_revoke_sessions(self):
        _, token = self.login()
        self.write_conf('changed-password')
        self.assertEqual(self.request('GET', '/manage/api/measure/status', cookie=token).status, 401)
        self.write_conf(self.password)
        self.assertEqual(self.request('GET', '/manage/api/measure/status', cookie=token).status, 401)
        _, new = self.login()
        self.conf.unlink()
        self.assertEqual(self.request('GET', '/manage/api/measure/status', cookie=new).status, 401)
        response = self.request('POST', '/api/perf-auth',
                                json.dumps({'password': self.password}).encode())
        self.assertEqual(response.status, 401)

    def test_unrelated_config_edits_preserve_sessions(self):
        _, token = self.login()
        with self.conf.open('a') as stream:
            stream.write('voice_channel=2\n')
        self.assertEqual(self.request('GET', '/manage/api/measure/status', cookie=token).status, 200)

    def test_status_remains_public_when_management_session_expires(self):
        _, token = self.login()
        self.now += 120
        with patch.object(status, 'assemble_status_data', return_value={'nodes': []}):
            response = self.request('GET', '/api/data', cookie=token)
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.payload), {'nodes': []})

    def test_login_still_requires_an_allowed_source_address(self):
        response = self.request('POST', '/api/perf-auth',
                                json.dumps({'password': self.password}).encode(),
                                client='203.0.113.10')
        self.assertEqual(response.status, 403)
        self.assertIsNone(response.getheader('Set-Cookie'))

    def test_redirect_cannot_leave_manage_or_inject_headers(self):
        for target in ('https://example.com', '//example.com', '/api/debug',
                       '/manage/\r\nX-Injected: yes', '/manage/\\example.com'):
            with self.subTest(target=target):
                response, _ = self.login(next_path=target)
                self.assertEqual(response.getheader('Location'), '/manage/')
                self.assertIsNone(response.getheader('X-Injected'))

    def test_management_json_errors_keep_http_status(self):
        _, token = self.login()
        with patch('manet_manage.voice_status', side_effect=RuntimeError('unavailable')):
            response = self.request('GET', '/manage/api/voice', cookie=token)
        self.assertEqual(response.status, 500)
        self.assertEqual(json.loads(response.payload)['error'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
