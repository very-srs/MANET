#!/usr/bin/env python3
"""Exercise real downloads (fake curl), archives and installation in a fake root."""
import fcntl
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

SPEC = importlib.util.spec_from_file_location('node_update', Path(__file__).with_name('node-update.py'))
update = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(update)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='manet-update-test-')
        self.addCleanup(scratch.cleanup)
        self.work = Path(scratch.name)
        self.root = self.work / 'root'
        for directory in ('etc', 'usr/local/bin', 'proc/device-tree', 'run'):
            (self.root / directory).mkdir(parents=True)
        (self.root / 'proc/device-tree/model').write_bytes(b'Raspberry Pi Compute Module 4\0')
        self.old_version = '0.549\n09/2026\n'
        self.new_version = '0.550\n09/2026\n'
        for name in update.MARKERS:
            (self.root / name).write_text(self.old_version)
        (self.root / 'etc/mesh.conf').write_text('acs=y\n')
        self.old_manager = self.root / 'usr/local/bin/node-manager.sh'
        self.old_manager.write_text('old manager\n')
        self.updater = update.Updater(self.root)
        self.updater.log = Mock()
        self.server = self.work / 'server'
        self.server.mkdir()
        self.package = self.server / 'cm4-tools.tar.gz'
        self.checksum = self.server / 'cm4-tools.tar.gz.sha256'
        self.release = self.server / 'release.txt'
        self.release.write_text(self.new_version)
        self.events = self.work / 'events'
        commands = self.work / 'bin'
        commands.mkdir()
        self.commands = commands
        self.command('curl', '''
args = sys.argv[1:]
with open(os.environ['TEST_EVENTS'], 'a') as stream:
    stream.write('curl ' + args[-1] + '\\n')
if os.environ.get('TEST_DOWNLOAD_FAIL') == args[-1]:
    sys.exit(22)
name = 'release.txt' if 'raw.githubusercontent.com' in args[-1] else args[-1].rsplit('/', 1)[1]
source = Path(os.environ['TEST_SERVER']) / name
if not source.exists():
    sys.exit(22)
shutil.copyfile(source, args[args.index('--output') + 1])
''')
        self.command('systemctl', '''
args = ' '.join(sys.argv[1:])
with open(os.environ['TEST_EVENTS'], 'a') as stream:
    stream.write('systemctl ' + args + '\\n')
    marker = Path(os.environ['TEST_ROOT']) / 'etc/manet_version.txt'
    stream.write('marker ' + (marker.read_text().splitlines()[0] if marker.exists() else 'missing') + '\\n')
if os.environ.get('TEST_SYSTEMCTL_FAIL') == args:
    sys.exit(1)
''')
        self.environment = patch.dict(os.environ, {
            'PATH': str(commands) + os.pathsep + os.environ['PATH'],
            'TEST_EVENTS': str(self.events), 'TEST_SERVER': str(self.server),
            'TEST_ROOT': str(self.root), 'TEST_SYSTEMCTL_FAIL': '',
            'TEST_DOWNLOAD_FAIL': '', 'TEST_DEPENDENCY_FAIL': '0',
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.members = {}
        for name in update.REQUIRED:
            if name in update.MARKERS:
                body = self.new_version.encode()
            elif name.endswith('manet-admin-setup.sh'):
                body = b'#!/bin/sh\necho dependency >> "$TEST_EVENTS"\nexit "$TEST_DEPENDENCY_FAIL"\n'
            else:
                body = ('#!/bin/sh\n# ' + name + '\n').encode()
            self.members[name] = (body, 0o755 if name.endswith(('.sh', '.py')) else 0o644)
        self.make_archive()

    def command(self, name, body):
        path = self.commands / name
        path.write_text(f'#!{sys.executable}\nimport os, sys, shutil\nfrom pathlib import Path\n' + body)
        path.chmod(0o755)

    def make_archive(self, extras=()):
        with tarfile.open(self.package, 'w:gz') as archive:
            for name, (body, mode) in sorted(self.members.items()):
                member = tarfile.TarInfo('./' + name)
                member.size = len(body)
                member.mode = mode
                archive.addfile(member, io.BytesIO(body))
            for member, body in extras:
                archive.addfile(member, io.BytesIO(body) if body is not None else None)
        self.hash_archive()

    def hash_archive(self):
        self.checksum.write_text(hashlib.sha256(self.package.read_bytes()).hexdigest() + '  cm4-tools.tar.gz\n')

    def history(self):
        return self.events.read_text() if self.events.exists() else ''

    def assert_rejected_before_install(self):
        with self.assertRaises((update.UpdateError, OSError, ValueError, EOFError, tarfile.TarError)):
            self.updater.update()
        self.assertEqual(self.updater.marker.read_text(), self.old_version)
        self.assertEqual(self.old_manager.read_text(), 'old manager\n')
        self.assertNotIn('dependency', self.history())
        self.assertNotIn('systemctl', self.history())
        self.assertFalse(self.updater.pending.exists())
        self.assertFalse(list(self.updater.state.glob('download-*')))

    def test_success_installs_and_commits_version_after_services(self):
        link = tarfile.TarInfo('etc/systemd/system/multi-user.target.wants/manet-admin-setup.service')
        link.type = tarfile.SYMTYPE
        link.linkname = '../manet-admin-setup.service'
        directory = tarfile.TarInfo('usr/')
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o700
        self.make_archive([(link, None), (directory, None)])
        original_mode = (self.root / 'usr').stat().st_mode
        self.updater.update()
        for name in update.MARKERS:
            self.assertEqual((self.root / name).read_text(), self.new_version)
        self.assertIn('node-manager-acs.sh', self.old_manager.read_text())
        self.assertEqual((self.root / 'usr').stat().st_mode, original_mode)
        self.assertEqual((self.root / link.name).readlink(), Path('../manet-admin-setup.service'))
        self.assertIn('systemctl is-active --quiet node-manager.service', self.history())
        self.assertNotIn('marker 0.550', self.history())
        self.assertFalse(self.updater.pending.exists())
        self.assertFalse(list(self.updater.state.glob('download-*')))
        self.updater.log.assert_called_with('Node tools updated to version 0.550')

    def test_static_selection(self):
        (self.root / 'etc/mesh.conf').write_text('acs=n\n')
        self.updater.update()
        self.assertIn('node-manager-static.sh', self.old_manager.read_text())

    def test_missing_malformed_wrong_filename_and_mismatched_checksum(self):
        valid = self.checksum.read_text()
        for content in (None, '', 'garbage', valid.replace('cm4-', 'r3a-'), '0' * 64 + '  cm4-tools.tar.gz\n', valid + valid):
            with self.subTest(content=content):
                if content is None:
                    self.checksum.unlink(missing_ok=True)
                else:
                    self.checksum.write_text(content)
                self.assert_rejected_before_install()

    def test_download_failure(self):
        for url in (update.VERSION_URL, update.PACKAGE_URLS['cm4']):
            with self.subTest(url=url), patch.dict(os.environ, TEST_DOWNLOAD_FAIL=url):
                self.assert_rejected_before_install()

    def test_wrong_release_even_with_valid_hash(self):
        self.release.write_text('0.551\n09/2026\n')
        self.assert_rejected_before_install()

    def test_disagreeing_version_markers(self):
        self.members['usr/local/bin/version.txt'] = (b'0.550\n10/2026\n', 0o644)
        self.make_archive()
        self.assert_rejected_before_install()

    def test_missing_required_file(self):
        del self.members['usr/local/bin/node-manager-acs.sh']
        self.make_archive()
        self.assert_rejected_before_install()

    def test_truncated_gzip_footer_with_matching_hash(self):
        self.package.write_bytes(self.package.read_bytes()[:-5])
        self.hash_archive()
        self.assert_rejected_before_install()

    def test_invalid_tar_with_matching_hash(self):
        self.package.write_bytes(gzip.compress(b'not a tar file'))
        self.hash_archive()
        self.assert_rejected_before_install()

    def test_unsafe_paths_and_excluded_payloads(self):
        for name in ('/', './', '/etc/unwanted', '../unwanted', 'usr/../unwanted',
                     'etc/mesh.conf', 'etc/systemd/network/test.network',
                     'usr/lib/modules/test.ko', 'usr/local/bin/node-manager.sh'):
            with self.subTest(name=name):
                member = tarfile.TarInfo(name)
                self.make_archive([(member, b'')])
                self.assert_rejected_before_install()

    def test_links_devices_and_duplicate_paths(self):
        for kind, name, target in (
            (tarfile.SYMTYPE, 'etc/escape', '/tmp'),
            (tarfile.SYMTYPE, 'etc/escape', '../../tmp'),
            (tarfile.SYMTYPE, 'usr/local/bin', '../../etc/manet_version.txt'),
            (tarfile.LNKTYPE, 'etc/hardlink', 'etc/manet_version.txt'),
            (tarfile.CHRTYPE, 'etc/device', ''),
            (tarfile.REGTYPE, 'etc/manet_version.txt', ''),
        ):
            with self.subTest(kind=kind, name=name):
                member = tarfile.TarInfo(name)
                member.type, member.linkname = kind, target
                self.make_archive([(member, None)])
                self.assert_rejected_before_install()

    def test_existing_parent_symlink_cannot_redirect_install(self):
        (self.root / 'etc/systemd').symlink_to(self.server, target_is_directory=True)
        self.assert_rejected_before_install()
        self.assertFalse((self.server / 'system').exists())

    def test_low_disk_space_before_download_and_before_staging(self):
        real_usage = shutil.disk_usage(self.root)
        with patch.object(update.shutil, 'disk_usage', return_value=real_usage._replace(free=0)):
            self.assert_rejected_before_install()
        real_space = self.updater.space
        def space(requests):
            if len(requests) > 1:
                raise update.UpdateError('Not enough space for staging + installation')
            real_space(requests)
        with patch.object(self.updater, 'space', side_effect=space):
            self.assert_rejected_before_install()

    def test_dependency_failure_does_not_install_payload(self):
        with patch.dict(os.environ, TEST_DEPENDENCY_FAIL='1'):
            with self.assertRaises(update.UpdateError):
                self.updater.update()
        self.assertEqual(self.updater.marker.read_text(), self.old_version)
        self.assertEqual(self.old_manager.read_text(), 'old manager\n')
        self.assertNotIn('systemctl', self.history())

    def test_service_failures_preserve_marker_and_retry_in_routine_mode(self):
        for failure in ('daemon-reload', 'restart mesh-status.service',
                        'restart node-manager.service', 'is-active --quiet node-manager.service'):
            with self.subTest(failure=failure):
                for name in update.MARKERS:
                    (self.root / name).write_text(self.old_version)
                self.updater.routine = False
                with patch.dict(os.environ, TEST_SYSTEMCTL_FAIL=failure):
                    with self.assertRaises(update.UpdateError):
                        self.updater.update()
                self.assertEqual(self.updater.marker.read_text(), self.old_version)
                self.assertTrue(self.updater.pending.exists())
                self.updater.routine = True
                self.updater.update()
                self.assertEqual(self.updater.marker.read_text(), self.new_version)
                self.assertFalse(self.updater.pending.exists())

    def test_copy_failure_retains_old_file_and_pending_retry(self):
        original_replace = os.replace
        target = self.root / 'usr/local/bin/node-manager-acs.sh'
        target.write_text('old acs\n')
        def fail(source, destination):
            if destination == target:
                raise OSError('simulated disk failure')
            original_replace(source, destination)
        with patch.object(update.os, 'replace', side_effect=fail):
            with self.assertRaises(OSError):
                self.updater.update()
        self.assertEqual(target.read_text(), 'old acs\n')
        self.assertEqual(self.updater.marker.read_text(), self.old_version)
        self.assertTrue(self.updater.pending.exists())
        self.assertNotIn('systemctl', self.history())
        self.assertFalse(list(target.parent.glob('.manet-update-*')))

    def test_final_version_write_failure_retains_pending_retry(self):
        original_replace = os.replace
        def fail(source, destination):
            if destination == self.updater.marker:
                raise OSError('version commit failed')
            original_replace(source, destination)
        with patch.object(update.os, 'replace', side_effect=fail):
            with self.assertRaises(OSError):
                self.updater.update()
        self.assertEqual(self.updater.marker.read_text(), self.old_version)
        self.assertTrue(self.updater.pending.exists())
        self.updater.routine = True
        self.updater.update()
        self.assertEqual(self.updater.marker.read_text(), self.new_version)
        self.assertFalse(self.updater.pending.exists())

    def test_missing_or_corrupt_local_version_is_repairable(self):
        self.updater.routine = True
        for content in (None, 'invalid\n', '0.550\nwrong date\n'):
            with self.subTest(content=content):
                if content is None:
                    self.updater.marker.unlink()
                else:
                    self.updater.marker.write_text(content)
                self.updater.update()
                self.assertEqual(self.updater.marker.read_text(), self.new_version)

    def test_new_directories_are_not_group_writable_under_permissive_umask(self):
        original = os.umask(0o002)
        try:
            self.updater.update()
        finally:
            os.umask(original)
        self.assertEqual((self.root / 'etc/systemd/system').stat().st_mode & 0o777, 0o755)

    def test_incomplete_update_retries_even_when_version_matches(self):
        for name in update.MARKERS:
            (self.root / name).write_text(self.new_version)
        self.updater.state.mkdir(parents=True)
        self.updater.pending.write_text('interrupted after version commit\n')
        self.updater.routine = True
        self.updater.update()
        self.assertIn('systemctl restart node-manager.service', self.history())
        self.assertFalse(self.updater.pending.exists())

    def test_staging_write_failure_does_not_touch_live_files(self):
        with patch.object(update.shutil, 'copyfileobj', side_effect=OSError('staging write failed')):
            self.assert_rejected_before_install()

    def test_lock_prevents_concurrent_update(self):
        with (self.root / 'run/manet-update.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.updater.update()
        self.assertEqual(self.history(), '')
        self.updater.log.assert_called_with('Another tools update is already running')

    def test_routine_daily_throttle_and_current_version(self):
        self.updater.routine = True
        self.updater.update()
        self.assertEqual(self.history(), '')
        for name in update.MARKERS:
            (self.root / name).write_text(self.new_version)
        old = time.time() - 90000
        os.utime(self.updater.marker, (old, old))
        self.updater.update()
        self.assertIn('curl ' + update.VERSION_URL, self.history())
        self.assertNotIn('tools.tar.gz', self.history())
        self.assertGreater(self.updater.marker.stat().st_mtime, old)

    def test_all_board_urls(self):
        for model, board in (('ROCK3 Model A', 'r3a'), ('Raspberry Pi 5 Model B', 'rpi5')):
            with self.subTest(board=board):
                (self.root / 'proc/device-tree/model').write_text(model)
                self.assertEqual(self.updater.board(), board)
                filename = update.PACKAGE_URLS[board].rsplit('/', 1)[1]
                shutil.copyfile(self.package, self.server / filename)
                (self.server / (filename + '.sha256')).write_text(self.checksum.read_text().replace('cm4-tools.tar.gz', filename))
                self.updater.marker.write_text(self.old_version)
                self.updater.update()
                self.assertIn(update.PACKAGE_URLS[board] + '.sha256', self.history())

    def test_command_failure_and_timeout_are_reported(self):
        with self.assertRaisesRegex(update.UpdateError, 'failed \\(7\\)'):
            update.run_command([sys.executable, '-c', 'import sys; print("bad"); sys.exit(7)'])
        with self.assertRaises(subprocess.TimeoutExpired):
            update.run_command([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=0.05)

    def test_cli_reports_failure_to_journal_and_returns_nonzero(self):
        with patch.object(update, 'Updater', return_value=self.updater), \
                patch.object(update.os, 'geteuid', return_value=0), \
                patch.object(update.signal, 'signal'), \
                patch.object(sys, 'argv', ['node-update.py', '--routine']):
            self.checksum.unlink()
            self.assertEqual(update.main(), 1)
        self.assertTrue(self.updater.log.call_args.kwargs['error'])
        self.assertIn('Tools update failed', self.updater.log.call_args.args[0])

    def test_routine_errors_are_quiet_but_logged(self):
        quiet = update.Updater(self.root, routine=True)
        with patch.object(update.syslog, 'syslog') as logger, \
                patch.object(sys, 'stdout', new_callable=io.StringIO) as stdout, \
                patch.object(sys, 'stderr', new_callable=io.StringIO) as stderr:
            quiet.log('example error', error=True)
            self.assertEqual(stdout.getvalue() + stderr.getvalue(), '')
            logger.assert_called_once_with(update.syslog.LOG_ERR, 'example error')


if __name__ == '__main__':
    unittest.main()
