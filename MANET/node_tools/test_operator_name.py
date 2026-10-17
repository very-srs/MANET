"""Exercise public label writes through HTTP without opening a socket."""

import http.client
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from manet_config_io import load_kv_file
from test_web_sessions import MemoryConnection, status


class OperatorNameTests(unittest.TestCase):
    """Keep public edits local and independent of management credentials."""

    def setUp(self):
        """Provide a radio configuration and its local client address pool."""
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.conf = Path(scratch.name) / 'mesh.conf'
        self.original = ('# Local radio\nipv4_network=10.30.2.0/24\n'
                         'admin_password=secret\nmesh_key=mesh-password\n'
                         'atak=n\ncustom_setting=preserve-me\n')
        self.conf.write_text(self.original)
        self.conf.chmod(0o600)
        dhcp = self.conf.parent / 'mesh-eud.conf'
        dhcp.write_text('dhcp-range=10.30.2.128,10.30.2.131,4m\n')
        for name, value in (('MESH_CONF_FILE', str(self.conf)),
                            ('ATAK_DHCP_CONF', dhcp)):
            target = patch.object(status, name, value)
            target.start()
            self.addCleanup(target.stop)
        status.STATUS_CACHE.invalidate()
        self.addCleanup(status.STATUS_CACHE.invalidate)

    def request(self, payload, *, client='10.30.2.129', headers=None,
                path='/api/operator-name', method='POST'):
        """Send an unauthenticated request through the real HTTP handler."""
        body = payload if isinstance(payload, bytes) else json.dumps(
            payload).encode()
        fields = {'Host': 'manet.local', 'Content-Length': str(len(body)),
                  'Content-Type': 'application/json'}
        fields.update(headers or {})
        lines = [f'{method} {path} HTTP/1.1']
        lines.extend(f'{key}: {value}' for key, value in fields.items())
        raw = ('\r\n'.join(lines) + '\r\n\r\n').encode() + body
        connection = MemoryConnection(raw)
        status.MeshHandler(connection, (client, 12345), SimpleNamespace())
        response = http.client.HTTPResponse(
            MemoryConnection(bytes(connection.output)))
        response.begin()
        response.payload = response.read()
        return response

    def test_save_and_clear_preserve_config_without_admin_or_atak(self):
        """A connected person can assign a label with no clock or login."""
        for password in ('secret', ''):
            self.conf.write_text(self.original.replace('secret', password))
            before = load_kv_file(self.conf)
            for name in ('Team Lead <A>', '', 'é' * 16):
                with self.subTest(password=bool(password), name=name), \
                        patch.object(status, 'require_clock') as clock, \
                        patch.object(status, 'apply_local_settings') as ap, \
                        patch.object(status.STATUS_CACHE, 'invalidate') as cache:
                    response = self.request({'operator_name': name}, headers={
                        'Origin': 'http://manet.local',
                        'Referer': 'http://manet.local/'})
                    self.assertEqual(response.status, 200, response.payload)
                    self.assertEqual(json.loads(response.payload),
                                     {'ok': True, 'operator_name': name})
                    after = load_kv_file(self.conf)
                    self.assertEqual(after.pop('operator_name'), name)
                    self.assertEqual(after, before)
                    self.assertTrue(self.conf.read_text().startswith(
                        '# Local radio\n'))
                    self.assertEqual(self.conf.stat().st_mode & 0o777, 0o600)
                    clock.assert_not_called()
                    ap.assert_not_called()
                    cache.assert_called_once()

    def test_rejects_other_nodes_and_nonlocal_clients(self):
        """Mesh subnet membership alone cannot authorize a label write."""
        for client in ('10.30.2.140', '192.168.1.2', '127.0.0.1', '::1'):
            with self.subTest(client=client):
                self.assertEqual(self.request(
                    {'operator_name': 'wrong radio'}, client=client).status,
                    403)
                self.assertEqual(self.conf.read_text(), self.original)
        status.ATAK_DHCP_CONF.unlink()
        self.assertEqual(self.request({'operator_name': 'no pool'}).status,
                         403)

    def test_rejects_foreign_origins_and_non_json_requests(self):
        """Cross-origin browser submissions cannot change the label."""
        for headers, code in (
                ({'Origin': 'http://other.test'}, 403),
                ({'Origin': 'null'}, 403),
                ({'Referer': 'http://other.test/'}, 403),
                ({'Origin': 'http://user@manet.local'}, 403),
                ({'Origin': 'http://[invalid'}, 403),
                ({'Content-Type': 'text/plain'}, 415),
                ({'Content-Type': 'application/x-www-form-urlencoded'}, 415)):
            with self.subTest(headers=headers):
                response = self.request({'operator_name': 'bad'},
                                        headers=headers)
                self.assertEqual(response.status, code)
                self.assertEqual(self.conf.read_text(), self.original)

    def test_rejects_other_settings_and_invalid_or_oversized_names(self):
        """The public route accepts exactly one validated display label."""
        for payload in (
                {}, [], None, b'{bad', b'\xff',
                {'operator_name': None}, {'operator_name': 123},
                {'admin_password': 'replacement'},
                {'operator_name': 'A', 'node': 'peer'},
                {'operator_name': 'A', 'mesh_ssid': 'replacement'},
                {'operator_name': 'a' * 33}, {'operator_name': 'é' * 17},
                {'operator_name': '\ud800'}, {'operator_name': 'A\nB'},
                {'operator_name': '"A"'}):
            with self.subTest(payload=repr(payload)):
                response = self.request(payload)
                self.assertEqual(response.status, 400, response.payload)
                self.assertEqual(self.conf.read_text(), self.original)
        self.assertEqual(self.request(b' ' * 2049).status, 413)
        self.assertEqual(self.conf.read_text(), self.original)

    def test_save_failure_and_busy_writer_leave_file_unchanged(self):
        """Failed saves and concurrent changes cannot report success."""
        with patch.object(status, 'apply_local_to_conf',
                          side_effect=OSError('disk failure')):
            response = self.request({'operator_name': 'A'})
            self.assertEqual(response.status, 503)
            self.assertIn('could not be saved', response.payload.decode())
        with status.MANAGEMENT_WRITE:
            self.assertEqual(self.request({'operator_name': 'A'}).status, 503)
        self.assertEqual(self.conf.read_text(), self.original)

    def test_get_cannot_write_and_admin_settings_still_require_login(self):
        """The public exception does not open configuration or GET writes."""
        self.assertEqual(self.request({'operator_name': 'A'},
                                      method='GET').status, 404)
        self.assertEqual(self.request({'config': {'mesh_ssid': 'B'}},
                                      path='/api/admin/stage').status, 401)
        self.assertEqual(self.conf.read_text(), self.original)


if __name__ == '__main__':
    unittest.main()
