"""Hosts refreshes preserve local entries and avoid unchanged disk writes."""

import fcntl
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import manet_config_io
import manet_hosts as hosts


TOOLS = Path(__file__).resolve().parent


class HostsTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.registry = self.root / 'registry'
        self.hosts = self.root / 'hosts'
        self.lock = self.root / 'hosts.lock'
        self.local = b'127.0.0.1 localhost\n# local comment: \xff\n'
        self.hosts.write_bytes(self.local)
        self.hosts.chmod(0o640)
        self.write_registry()

    def write_registry(self, name='mesh-one', address='10.30.0.6'):
        self.registry.write_text(
            '# Mesh Node Registry\n'
            f'NODE_020000000001_HOSTNAME={shlex.quote(name)}\n'
            f'NODE_020000000001_IPV4_ADDRESS={shlex.quote(address)}\n')

    def update(self):
        return hosts.update(self.registry, self.hosts, self.lock)

    def test_idle_ticks_and_unrelated_telemetry_never_open_a_tempfile(self):
        self.assertTrue(self.update())
        original = self.hosts.stat()
        with self.registry.open('a') as stream:
            stream.write("NODE_020000000001_UPTIME_SECONDS='900'\n")
        with patch.object(manet_config_io.tempfile, 'mkstemp',
                          side_effect=AssertionError('unnecessary disk write')):
            for _ in range(5):
                self.assertFalse(self.update())
        current = self.hosts.stat()
        self.assertEqual((current.st_ino, current.st_mtime_ns),
                         (original.st_ino, original.st_mtime_ns))

    def test_mapping_change_preserves_local_bytes_mode_and_owner(self):
        self.update()
        with self.hosts.open('ab') as stream:
            stream.write(b'192.0.2.5 operator-service\n')
        original = self.hosts.stat()
        self.write_registry(address='10.30.0.13')
        self.assertTrue(self.update())
        data = self.hosts.read_bytes()
        self.assertTrue(data.startswith(self.local))
        self.assertTrue(data.endswith(b'192.0.2.5 operator-service\n'))
        self.assertIn(b'10.30.0.13    mesh-one mesh-one.local\n', data)
        self.assertNotIn(b'10.30.0.6', data)
        info = self.hosts.stat()
        self.assertEqual(info.st_mode & 0o777, 0o640)
        self.assertEqual((info.st_uid, info.st_gid),
                         (original.st_uid, original.st_gid))

    def test_reordering_and_duplicate_rows_do_not_rewrite(self):
        self.registry.write_text(self.registry.read_text() +
            "NODE_020000000002_HOSTNAME='mesh-two'\n"
            "NODE_020000000002_IPV4_ADDRESS='10.30.0.13'\n")
        self.update()
        lines = self.registry.read_text().splitlines()
        self.registry.write_text('\n'.join(reversed(lines)) + '\n' +
            "NODE_020000000003_HOSTNAME='mesh-one'\n"
            "NODE_020000000003_IPV4_ADDRESS='10.30.0.6'\n")
        self.assertFalse(self.update())

    def test_missing_or_empty_registry_keeps_previous_hosts(self):
        self.update()
        original = self.hosts.read_bytes()
        self.registry.unlink()
        self.assertFalse(self.update())
        self.registry.write_text('')
        self.assertFalse(self.update())
        self.assertEqual(self.hosts.read_bytes(), original)
        # A complete empty registry is distinct from a failed/missing snapshot.
        self.registry.write_text('# Mesh Node Registry\n')
        self.assertTrue(self.update())
        self.assertNotIn(b'mesh-one', self.hosts.read_bytes())

    def test_malformed_markers_or_registry_leave_hosts_intact(self):
        for body in (hosts.BEGIN + '\noperator-entry\n',
                     hosts.END + '\n' + hosts.BEGIN + '\n',
                     (hosts.BEGIN + '\n' + hosts.END + '\n') * 2):
            self.hosts.write_text(body)
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.update()
            self.assertEqual(self.hosts.read_text(), body)
        self.hosts.write_bytes(self.local)
        self.registry.write_text("NODE_020000000001_HOSTNAME='unfinished\n")
        with self.assertRaises(ValueError):
            self.update()
        self.assertEqual(self.hosts.read_bytes(), self.local)

    def test_untrusted_names_and_addresses_cannot_execute_or_inject_lines(self):
        sentinel = self.root / 'executed'
        self.write_registry(name=f'$(touch {sentinel})')
        self.update()
        self.assertFalse(sentinel.exists())
        self.assertNotIn(b'touch', self.hosts.read_bytes())
        for name, address in (('mesh bad', '10.30.0.6'),
                              ('mesh-ok', '10.30.0.6 extra'),
                              ('mesh-ok', '999.1.2.3')):
            self.write_registry(name, address)
            self.assertFalse(self.update())
        self.write_registry(name='mesh-one.local')
        self.update()
        self.assertNotIn(b'.local.local', self.hosts.read_bytes())

    def test_failed_publication_keeps_original_and_cleans_scratch(self):
        for operation in ('replace', 'fsync'):
            with self.subTest(operation=operation):
                with patch.object(manet_config_io.os, operation,
                                  side_effect=OSError('injected failure')):
                    with self.assertRaises(OSError):
                        self.update()
                self.assertEqual(self.hosts.read_bytes(), self.local)
                self.assertEqual(list(self.root.glob('.hosts-*')), [])

    def test_symlink_is_not_replaced(self):
        target = self.root / 'operator-hosts'
        self.hosts.rename(target)
        self.hosts.symlink_to(target)
        with self.assertRaises(ValueError):
            self.update()
        self.assertTrue(self.hosts.is_symlink())
        self.assertEqual(target.read_bytes(), self.local)

    def test_shell_entry_point_serializes_refreshes(self):
        env = dict(os.environ, MESH_REGISTRY_FILE=str(self.registry),
                   MANET_HOSTS_FILE=str(self.hosts),
                   MANET_HOSTS_LOCK=str(self.lock), MANET_TOOLS_DIR=str(TOOLS))
        args = ['bash', str(TOOLS / 'mesh-hosts-update.sh')]
        with self.lock.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = subprocess.run(args, env=env, capture_output=True,
                                    text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.hosts.read_bytes(), self.local)
        result = subprocess.run(args, env=env, capture_output=True,
                                text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Updated 1', result.stderr)


if __name__ == '__main__':
    unittest.main()
