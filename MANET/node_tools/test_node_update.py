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

from manet_release import STABLE_MANIFEST, DOWNLOADS, PACKAGES
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
        for directory in ('etc', 'usr/local/bin', 'proc/device-tree', 'proc/sys/kernel/random', 'run'):
            (self.root / directory).mkdir(parents=True)
        (self.root / 'run/initial_time_synced').touch()
        (self.root / 'proc/sys/kernel/random/boot_id').write_text('boot-test')
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
        self.release = self.server / 'manet-release.json'
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
name = args[-1].rsplit('/', 1)[1]
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
stopped = Path(os.environ['TEST_ROOT']) / 'run/test-stopped-services'
units = set(stopped.read_text().splitlines()) if stopped.exists() else set()
if sys.argv[1] == 'stop':
    units.add(sys.argv[-1])
    stopped.write_text('\\n'.join(sorted(units)))
if sys.argv[1] == 'show':
    active = sys.argv[-1] in os.environ.get('TEST_ACTIVE_SERVICES', '').split(',')
    print('active' if active and sys.argv[-1] not in units else 'inactive')
if sys.argv[1] == 'is-active' and sys.argv[-1] == 'mesh-config-apply.service':
    sys.exit(3)
''')
        self.environment = patch.dict(os.environ, {
            'PATH': str(commands) + os.pathsep + os.environ['PATH'],
            'TEST_EVENTS': str(self.events), 'TEST_SERVER': str(self.server),
            'TEST_ROOT': str(self.root), 'TEST_SYSTEMCTL_FAIL': '',
            'TEST_DOWNLOAD_FAIL': '', 'TEST_DEPENDENCY_FAIL': '0',
            'MANET_TIME_RUN_DIR': str(self.root / 'run'),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        from test_config_sandbox import isolate_config
        isolate_config(self, self.root)
        self.members = {}
        for name in update.REQUIRED:
            if name in update.MARKERS:
                body = self.new_version.encode()
            elif name in update.UPDATER:
                # Ship the running updater, so installs do not hand off to a stub.
                body = Path(__file__).with_name(Path(name).name).read_bytes()
            elif name.endswith('manet-admin-setup.sh'):
                body = b'#!/bin/sh\necho dependency >> "$TEST_EVENTS"\nexit "$TEST_DEPENDENCY_FAIL"\n'
            elif name.endswith('manet-kernel-protect.py'):
                body = (b'#!/bin/sh\necho kernel-protection >> "$TEST_EVENTS"\n'
                        b'exit "${TEST_KERNEL_PROTECTION_FAIL:-0}"\n')
            elif name.endswith('manet-dhcp-isolation.py'):
                body = b'#!/bin/sh\necho isolation >> "$TEST_EVENTS"\nexit "${TEST_ISOLATION_FAIL:-0}"\n'
            elif name.endswith('manet-uplink-dns.py'):
                body = (b'#!/bin/sh\necho qualified-dns >> "$TEST_EVENTS"\n'
                        b'exit "${TEST_UPLINK_DNS_FAIL:-0}"\n')
            elif name.endswith('mesh-config-sync.py'):
                body = (b'#!/bin/sh\necho config-migration >> "$TEST_EVENTS"\n'
                        b'exit "${TEST_CONFIG_MIGRATION_FAIL:-0}"\n')
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
        asset = {'sha256': hashlib.sha256(self.package.read_bytes()).hexdigest(), 'size': self.package.stat().st_size}
        self.manifest = {'schema': 1, 'version': '0.550', 'tag': 'v0.550', 'commit': 'a' * 40,
                         'assets': {name: asset for name in PACKAGES}}
        self.release.write_text(json.dumps(self.manifest))

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
        self.assertEqual(self.old_manager.readlink(), Path('node-manager-acs.sh'))
        self.assertEqual((self.root / 'usr').stat().st_mode, original_mode)
        self.assertEqual((self.root / link.name).readlink(), Path('../manet-admin-setup.service'))
        self.assertIn('systemctl is-active --quiet node-manager.service', self.history())
        self.assertNotIn('marker 0.550', self.history())
        self.assertFalse(self.updater.pending.exists())
        self.assertFalse(list(self.updater.state.glob('download-*')))
        self.updater.log.assert_called_with('Node tools updated to version 0.550')

    def test_uplink_retirement_precedes_activation_and_failed_cleanup_retries(self):
        helper = 'usr/local/bin/manet-retire-uplink.py'
        self.members[helper] = (b'#!/bin/sh\necho retire-uplink >> "$TEST_EVENTS"\n'
                                b'test ! -f "$TEST_ROOT/fail-retirement"\n', 0o755)
        self.make_archive()
        failure = self.root / 'fail-retirement'
        failure.touch()
        with self.assertRaises(update.UpdateError):
            self.updater.update()
        self.assertTrue(self.updater.pending.exists())
        self.assertEqual(self.updater.marker.read_text(), self.old_version)
        self.assertNotIn('systemctl restart node-manager.service', self.history())
        failure.unlink()
        self.updater.update(recover_only=True)
        history = self.history()
        self.assertEqual(history.count('retire-uplink'), 2)
        self.assertLess(history.index('retire-uplink'),
                        history.index('systemctl restart node-manager.service'))
        self.assertFalse(self.updater.pending.exists())

    def test_dns_migration_precedes_runtime_activation_and_version_commit(self):
        """Activate the installed DNS policy before restarting the manager."""
        self.updater.update()
        history = self.history()
        enable = ('systemctl enable --now manet-uplink-dns.path '
                  'manet-uplink-dns.service')
        self.assertLess(history.index('qualified-dns'), history.index(enable))
        self.assertLess(history.index(enable),
                        history.index('systemctl restart node-manager.service'))
        self.assertNotIn('marker 0.550', history)

    def test_failed_dns_migration_keeps_update_pending_and_recovers(self):
        """A DNS activation failure must not publish the new version."""
        with patch.dict(os.environ, {'TEST_UPLINK_DNS_FAIL': '1'}):
            with self.assertRaises(update.UpdateError):
                self.updater.update()
        self.assertTrue(self.updater.pending.exists())
        self.assertEqual(self.updater.marker.read_text(), self.old_version)
        self.assertNotIn('systemctl restart node-manager.service', self.history())
        self.updater.update(recover_only=True)
        self.assertFalse(self.updater.pending.exists())
        self.assertEqual(self.updater.marker.read_text(), self.new_version)
        self.assertEqual(self.history().count('qualified-dns'), 2)

    def test_config_migration_failure_stays_pending_until_offline_retry(self):
        """Migration is an activation gate, before any manager restart."""
        with patch.dict(os.environ, TEST_CONFIG_MIGRATION_FAIL='1'):
            with self.assertRaises(update.UpdateError):
                self.updater.update()
        self.assertTrue(self.updater.pending.exists())
        for name in update.MARKERS:
            self.assertEqual((self.root / name).read_text(), self.old_version)
        self.assertNotIn('systemctl restart node-manager.service', self.history())
        self.events.write_text('')
        self.updater.update(recover_only=True)
        self.assertFalse(self.updater.pending.exists())
        self.assertNotIn('curl ', self.history())
        self.assertNotIn('dependency', self.history())
        self.assertIn('config-migration', self.history())
        for service in update.RESTARTED_SERVICES:
            self.assertIn('systemctl try-restart ' + service, self.history())

    def test_config_units_stop_before_first_payload_replacement(self):
        """A running worker and manager cannot race installed config files."""
        original = self.updater.install_payload

        def install(directory, members):
            """Observe quiescence at the first possible payload write."""
            for unit in ('node-manager.service', 'mesh-config-apply.service'):
                self.assertIn('systemctl stop ' + unit, self.history())
            self.assertTrue(
                (self.updater.recovery / 'config-staging.json').exists())
            self.assertNotIn('config-migration', self.history())
            return original(directory, members)

        with patch.dict(os.environ, TEST_ACTIVE_SERVICES=
                        'node-manager.service,mesh-config-apply.service'), \
                patch.object(self.updater, 'install_payload', install):
            self.updater.update()
        history = self.history()
        self.assertLess(history.index('config-migration'),
                        history.index('systemctl restart node-manager.service'))

    def test_config_stop_failure_does_not_replace_payload(self):
        """Failure to quiesce must leave the old payload and versions intact."""
        with patch.dict(os.environ,
                        TEST_SYSTEMCTL_FAIL='stop node-manager.service'):
            with self.assertRaises(update.UpdateError):
                self.updater.update()
        self.assertEqual(self.old_manager.read_text(), 'old manager\n')
        self.assertTrue(self.updater.pending.exists())
        self.assertNotIn('config-migration', self.history())
        for name in update.MARKERS:
            self.assertEqual((self.root / name).read_text(), self.old_version)

    def real_config_migration(self):
        """Ship real import code and prepare a consumed legacy activation."""
        tools = Path(__file__).resolve().parent
        for name in ('mesh-config-sync.py', 'manet_config_journal.py',
                     'manet_admin.py', 'manet_admin_limits.py', 'manet_clock.py',
                     'manet_config_io.py', 'mesh_config.py',
                     'manet_supplicant.py'):
            self.members['usr/local/bin/' + name] = (
                (tools / name).read_bytes(), 0o755)
        package = {'version': 'abc123', 'activate_at': 1,
                   'config': {'mtx': 'n'}}
        pending = self.root / 'run/mesh_pending_config.json'
        pending.write_text(json.dumps(package))
        pending.chmod(0o600)
        (self.root / 'run/mesh_config_ack_version').write_text('abc123')
        admin = self.root / 'var/lib/manet-admin'
        admin.mkdir(parents=True)
        (admin / 'received-70.json').write_text(json.dumps(
            {'done': True, 'order': [100, 'a' * 32]}))
        self.make_archive()
        return package

    def test_interrupted_migration_recovers_after_run_is_lost(self):
        """Death before the first journal write retains consumed work."""
        package = self.real_config_migration()
        with patch.object(self.updater, 'migrate_config',
                          side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.updater.update()
        saved = self.updater.recovery / 'config-staging.json'
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.updater.pending.exists())
        for path in (self.root / 'run').iterdir():
            path.unlink()
        (self.root / 'proc/sys/kernel/random/boot_id').write_text('new-boot')
        self.events.write_text('')
        self.updater.update(recover_only=True, files_only=True)
        self.assertEqual(self.history(), '')
        # Normal boot can start the manager before update finishing runs.
        # Its first sync must see the original package and consumed ACK.
        pending = self.root / 'run/mesh_pending_config.json'
        self.assertEqual(json.loads(pending.read_text()), package)
        self.assertEqual(pending.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            (self.root / 'run/mesh_config_ack_version').read_text(), 'abc123')
        self.updater.update(recover_only=True)
        journal = self.root / 'var/lib/manet-config/journal.json'
        job = json.loads(journal.read_text())['job']
        self.assertEqual(job['package'], package)
        self.assertEqual(job['phase'], 'prepared')
        self.assertTrue(job['consumed'])
        self.assertTrue(job['acknowledged'])
        self.assertFalse(self.updater.pending.exists())
        self.assertNotIn('curl ', self.history())
        self.assertNotIn('dependency', self.history())

    def test_migration_child_death_after_journal_save_retries_idempotently(self):
        """A partial import retires the old trial on offline recovery."""
        self.real_config_migration()
        legacy = self.root / 'var/lib/manet-config-rollback'
        legacy.mkdir()
        (legacy / 'mesh.conf').write_text('mtx=y\n')
        (legacy / 'state').write_text(
            'VERSION=abc123\nPEERS_BEFORE=1\nDEADLINE=1234\nBOOT_ID=old\n')
        name = 'usr/local/bin/manet_config_journal.py'
        source, mode = self.members[name]
        source = source.replace(
            b'        self.save()\n        self.retire_legacy()',
            b'        self.save()\n'
            b'        if os.environ.get("TEST_MIGRATION_DIE"):\n'
            b'            os._exit(73)\n        self.retire_legacy()')
        self.members[name] = (source, mode)
        self.make_archive()
        with patch.dict(os.environ, TEST_MIGRATION_DIE='1'):
            with self.assertRaisesRegex(update.UpdateError, '73'):
                self.updater.update()
        self.assertTrue(self.updater.pending.exists())
        self.assertTrue((legacy / 'state').exists())
        journal = self.root / 'var/lib/manet-config/journal.json'
        before = journal.read_bytes()
        for name in update.MARKERS:
            self.assertEqual((self.root / name).read_text(), self.old_version)
        for path in (self.root / 'run').iterdir():
            path.unlink()
        self.updater.update(recover_only=True)
        self.assertEqual(journal.read_bytes(), before)
        self.assertFalse((legacy / 'state').exists())
        self.assertTrue((legacy / 'journal-owned').exists())
        self.assertFalse(self.updater.pending.exists())

    def test_runtime_daemons_restart_and_only_active_ones_are_checked(self):
        with patch.dict(os.environ, {'TEST_ACTIVE_SERVICES': 'manet-atak.service,gps-reader.service'}):
            self.updater.update()
        history = self.history()
        for service in update.RESTARTED_SERVICES:
            self.assertIn('systemctl try-restart ' + service, history)
            self.assertNotIn('systemctl restart ' + service, history)
            check = 'systemctl is-active --quiet ' + service
            if service in ('manet-atak.service', 'gps-reader.service'):
                self.assertIn(check, history)
            else:
                self.assertNotIn(check, history)
        self.assertNotIn('marker 0.550', history)

    def test_runtime_restart_or_health_failure_withholds_version(self):
        for action in ('try-restart', 'is-active --quiet'):
            with self.subTest(action=action), patch.dict(os.environ, {
                    'TEST_ACTIVE_SERVICES': 'manet-atak.service',
                    'TEST_SYSTEMCTL_FAIL': action + ' manet-atak.service'}):
                with self.assertRaises(update.UpdateError):
                    self.updater.update()
                self.assertTrue(self.updater.pending.exists())
                self.assertEqual(self.updater.marker.read_text(), self.old_version)
        self.updater.update()
        self.assertFalse(self.updater.pending.exists())

    def test_failed_runtime_still_requires_health_check_on_same_boot_retry(self):
        with patch.dict(os.environ, {'TEST_ACTIVE_SERVICES': 'manet-atak.service',
                                    'TEST_SYSTEMCTL_FAIL': 'try-restart manet-atak.service'}):
            with self.assertRaises(update.UpdateError):
                self.updater.update()
        with patch.dict(os.environ, {'TEST_ACTIVE_SERVICES': '',
                                    'TEST_SYSTEMCTL_FAIL': 'is-active --quiet manet-atak.service'}):
            with self.assertRaises(update.UpdateError):
                self.updater.update()
        self.assertEqual(self.updater.marker.read_text(), self.old_version)
        # A real reboot starts services according to their configured state.
        (self.root / 'proc/sys/kernel/random/boot_id').write_text('next-boot')
        with patch.dict(os.environ, {'TEST_ACTIVE_SERVICES': '',
                                    'TEST_SYSTEMCTL_FAIL': 'is-active --quiet manet-atak.service'}):
            self.updater.update()
        self.assertFalse(self.updater.pending.exists())

    def test_runtime_list_covers_shipped_resident_units_and_excludes_oneshots(self):
        units = Path(__file__).parents[1] / 'systemd'
        residents = set()
        for unit in units.glob('*.service'):
            source = unit.read_text()
            if ('@' not in unit.name and 'Type=simple' in source and '/usr/local/bin/' in source
                    and 'PartOf=node-manager.service' not in source):
                residents.add(unit.name)
        self.assertEqual(set(update.RUNTIME_SERVICES), residents)

    def dfs_handoff_fixture(self):
        from test_acs_dfs import dfs_plan
        safety = Path(__file__).with_name('manet_dfs_safety.py')
        self.members['usr/local/bin/manet_dfs_safety.py'] = (safety.read_bytes(), 0o755)
        self.command('iw', "print('Interface mesh5\\n type mesh point\\n channel 52 (5260 MHz)')")
        self.command('ip', "with open(os.environ['TEST_EVENTS'], 'a') as f: f.write('ip ' + ' '.join(sys.argv[1:]) + '\\n')")
        config = self.root / 'etc/wpa_supplicant/wpa_supplicant-mesh5.conf'
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('frequency=5260\n')
        (self.root / 'run/manet-dfs-contract.json').write_text(json.dumps(dict(
            boot='boottest', iface='mesh5', operating=dfs_plan())))
        self.make_archive()
        return config

    def test_failed_installer_after_dfs_handoff_leaves_radio_safe(self):
        config = self.dfs_handoff_fixture()
        with patch.dict(os.environ, TEST_CONFIG_MIGRATION_FAIL='1'):
            with self.assertRaises(update.UpdateError):
                self.updater.update()
        history = self.history()
        self.assertIn('ip link set dev mesh5 down', history)
        self.assertLess(history.index('ip link set dev mesh5 down'),
                        history.index('systemctl stop node-manager.service'))
        self.assertIn('frequency=5200', config.read_text())
        self.assertNotIn('systemctl start wpa_supplicant@mesh5.service', history)
        self.assertTrue(self.updater.pending.exists())
        self.assertEqual(self.updater.marker.read_text(), self.old_version)

    def test_guardian_start_failure_prevents_update_commit_and_resume(self):
        self.dfs_handoff_fixture()
        with patch.dict(os.environ, TEST_SYSTEMCTL_FAIL='enable --now manet-dfs-watch.service'):
            with self.assertRaises(update.UpdateError):
                self.updater.update()
        self.assertNotIn('systemctl start wpa_supplicant@mesh5.service', self.history())
        self.assertNotIn('try-restart wpa_supplicant@', self.history())
        self.assertTrue(self.updater.pending.exists())
        self.assertEqual(self.updater.marker.read_text(), self.old_version)

    def test_firmware_setup_runs_after_install_and_before_version(self):
        name = 'usr/local/bin/manet-mt7916-setup.sh'
        self.members[name] = (
            b'#!/bin/sh\n'
            b'test -f "$TEST_ROOT/usr/local/share/manet/mt7916-firmware.json" || exit 2\n'
            b'echo firmware-setup >> "$TEST_EVENTS"\n'
            b'echo "manet-mt7916-firmware: test stock fallback"\n', 0o755)
        self.make_archive()
        self.updater.update()
        history = self.history()
        self.assertLess(history.index('systemctl daemon-reload'), history.index('firmware-setup'))
        self.assertLess(history.index('firmware-setup'), history.index('restart node-manager.service'))
        self.updater.log.assert_any_call('manet-mt7916-firmware: test stock fallback')
        self.assertNotIn('modprobe', history)

    def test_firmware_setup_failure_leaves_update_pending(self):
        self.members['usr/local/bin/manet-mt7916-setup.sh'] = (b'#!/bin/sh\nexit 1\n', 0o755)
        self.make_archive()
        with self.assertRaises(update.UpdateError):
            self.updater.update()
        self.assertTrue(self.updater.pending.exists())
        self.assertEqual(self.updater.marker.read_text(), self.old_version)
        self.assertNotIn('restart node-manager.service', self.history())

    def test_mesh_update_installs_binary_and_prepares_before_restart(self):
        tools = Path(__file__).parent
        helper = (tools / 'manet-mesh-supplicant.py').read_text().replace(
            "WPA_DIR = Path('/etc/wpa_supplicant')",
            "WPA_DIR = Path(os.environ['TEST_ROOT']) / 'etc/wpa_supplicant'")
        self.members['usr/local/bin/manet-mesh-supplicant.py'] = (
            helper.encode(), 0o755)
        self.members['usr/local/bin/manet_config_io.py'] = (
            (tools / 'manet_config_io.py').read_bytes(), 0o755)
        binary = 'usr/sbin/wpa_supplicant_manet'
        self.members[binary] = (b'new mesh binary', 0o755)
        configs = self.root / 'etc/wpa_supplicant'
        configs.mkdir()
        live = configs / 'wpa_supplicant-wlan0.conf'
        lobby = configs / 'wpa_supplicant-wlan0-lobby.conf'
        # An already-updated inactivity setting must not skip width migration.
        for path in (live, lobby):
            path.write_text('mesh_max_inactivity=10\nnetwork={\n'
                            '    mode=5\n    frequency=5805\n}\n')
        self.make_archive()
        original = update.run_command

        def command(args, **kwargs):
            if args[:2] == ['systemctl', 'try-restart']:
                self.assertEqual((self.root / binary).read_bytes(),
                                 b'new mesh binary')
                self.assertTrue(live.read_text().startswith(
                    'mesh_max_inactivity=10\n'))
                for path in (live, lobby):
                    self.assertIn('    max_oper_chwidth=0\n', path.read_text())
                    self.assertIn('    disable_ht40=1\n', path.read_text())
                self.assertEqual(self.updater.marker.read_text(),
                                 self.old_version)
            return original(args, **kwargs)

        with patch.object(update, 'run_command', side_effect=command):
            self.updater.update()
        self.assertEqual((self.root / binary).stat().st_mode & 0o777, 0o755)
        self.assertIn('systemctl try-restart wpa_supplicant@*.service',
                      self.history())
        self.assertNotIn('try-restart wpa_supplicant-s1g', self.history())

    def halow_fixture(self, flags):
        (self.root / 'var/lib').mkdir(parents=True, exist_ok=True)
        (self.root / 'var/lib/halow_if').write_text('wlan2\n')
        net = self.root / 'sys/class/net/wlan2'
        net.mkdir(parents=True)
        (net / 'flags').write_text(flags + '\n')

    def test_halow_downed_by_old_dfs_fence_is_restarted(self):
        self.halow_fixture('0x1002')
        self.updater.update()
        self.assertIn('systemctl try-restart wpa_supplicant-s1g-wlan2.service', self.history())

    def test_running_halow_is_not_restarted_by_update(self):
        self.halow_fixture('0x1003')
        self.updater.update()
        self.assertNotIn('try-restart wpa_supplicant-s1g', self.history())

    def publish(self, release, migration=0):
        """Serve one board archive as the current stable release."""
        text = f'{release}\n09/2026\n'.encode()
        for name in update.MARKERS:
            self.members[name] = (text, 0o644)
        self.members['usr/local/bin/mesh-config-sync.py'] = (
            f'#!/bin/sh\necho config-migration >> "$TEST_EVENTS"\nexit {migration}\n'.encode(), 0o755)
        self.make_archive()
        self.manifest.update(version=release, tag='v' + release)
        self.release.write_text(json.dumps(self.manifest))

    def stuck_release(self):
        """Install 0.550 cleanly, then leave a broken 0.551 pending."""
        self.publish('0.550')
        self.updater.update()
        self.publish('0.551', migration=1)
        with self.assertRaises(update.UpdateError):
            self.updater.update()
        self.assertTrue(self.updater.pending.exists())

    def version_now(self):
        return self.updater.marker.read_text().splitlines()[0]

    def test_repeated_failures_roll_back_to_the_last_clean_release(self):
        self.stuck_release()
        installed = json.loads((self.updater.installed / 'release.json').read_text())
        self.assertEqual(installed['version'], '0.550')
        for _ in range(update.RECOVERY_ATTEMPTS - 1):
            with self.assertRaises(update.UpdateError):
                self.updater.update(recover_only=True)
        self.updater.update(recover_only=True)
        self.assertEqual(self.version_now(), '0.550')
        self.assertFalse(self.updater.pending.exists())
        self.assertFalse(self.updater.recovery.exists())
        self.assertEqual(json.loads(self.updater.failed.read_text())['version'], '0.551')
        # The broken release is not downloaded again.
        self.events.write_text('')
        self.updater.update()
        self.assertNotIn('cm4-tools.tar.gz', self.history())
        self.assertEqual(self.version_now(), '0.550')

    def test_no_stored_release_restarts_management_and_stays_pending(self):
        self.stuck_release()
        shutil.rmtree(self.updater.installed)
        for _ in range(update.RECOVERY_ATTEMPTS - 1):
            with self.assertRaises(update.UpdateError):
                self.updater.update(recover_only=True)
        self.events.write_text('')
        with self.assertRaisesRegex(update.UpdateError, 'no previous release'):
            self.updater.update(recover_only=True)
        self.assertIn('systemctl start node-manager.service', self.history())
        self.assertTrue(self.updater.pending.exists())

    def test_daily_check_replaces_a_stuck_release_with_a_newer_one(self):
        self.stuck_release()
        self.publish('0.552')
        daily = update.Updater(self.root, routine=True)
        daily.log = Mock()
        daily.update()
        self.assertEqual(self.version_now(), '0.552')
        self.assertFalse(daily.pending.exists())

    def test_manual_run_finishes_the_pinned_release_offline(self):
        self.stuck_release()
        self.publish('0.552')
        self.events.write_text('')
        with self.assertRaises(update.UpdateError):
            self.updater.update()
        self.assertNotIn('curl', self.history())

    def test_explicit_local_archive_replaces_a_stuck_release(self):
        self.stuck_release()
        self.publish('0.551')
        local = self.work / 'local'
        local.mkdir()
        shutil.copyfile(self.package, local / 'cm4-tools.tar.gz')
        shutil.copyfile(self.checksum, local / 'cm4-tools.tar.gz.sha256')
        self.updater.update(local_archive=local / 'cm4-tools.tar.gz', local_version='0.551')
        self.assertEqual(self.version_now(), '0.551')
        self.assertFalse(self.updater.pending.exists())

    def test_mesh_preparation_failure_keeps_update_pending(self):
        self.members['usr/local/bin/manet-mesh-supplicant.py'] = (
            b'#!/bin/sh\nexit 1\n', 0o755)
        self.make_archive()
        with self.assertRaises(update.UpdateError):
            self.updater.update()
        self.assertTrue(self.updater.pending.exists())
        self.assertEqual(self.updater.marker.read_text(), self.old_version)
        self.assertNotIn('try-restart wpa_supplicant', self.history())

    def test_mesh_preparation_respects_channel_change_lock(self):
        original = update.run_command

        def command(args, **kwargs):
            if args[0] == 'flock':
                self.assertEqual(args[1:4], ['--exclusive', '--timeout', '10'])
                args = list(args)
                args[3] = '0'
            return original(args, **kwargs)

        with (self.root / 'run/channel-election.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(update, 'run_command', side_effect=command):
                with self.assertRaises(update.UpdateError):
                    self.updater.update()
        self.assertTrue(self.updater.pending.exists())
        self.assertEqual(self.updater.marker.read_text(), self.old_version)
        self.assertNotIn('try-restart wpa_supplicant', self.history())

    def test_static_selection(self):
        (self.root / 'etc/mesh.conf').write_text('acs=n\n')
        self.updater.update()
        self.assertEqual(self.old_manager.readlink(), Path('node-manager-static.sh'))

    def test_raw_kernel_files_require_transaction(self):
        for name in ('boot/firmware/kernel8.img', 'usr/lib/modules/test/kernel/test.ko',
                     'usr/lib/firmware/morse/test.bin', 'etc/manet-kernel-trial.json'):
            with self.subTest(name=name):
                self.members[name] = (b'candidate', 0o644)
                self.make_archive()
                self.assert_rejected_before_install()
                del self.members[name]

    def test_release_payload_can_install_system_components(self):
        """Install representative files in a fake root, without booting them."""
        payload = {
            'usr/lib/firmware/example/test.bin': b'test firmware',
            'etc/systemd/network/test.network': b'[Match]\nName=test0\n',
            'etc/mesh.conf': b'acs=n\n',
            'opt/manet/example': b'additional software',
            'var/lib/manet-example/schema': b'2\n',
        }
        for name, body in payload.items():
            self.members[name] = (body, 0o644)
        self.make_archive()
        self.updater.update()
        for name, body in payload.items():
            self.assertEqual((self.root / name).read_bytes(), body)
        self.assertEqual(self.old_manager.readlink(),
                         Path('node-manager-static.sh'))
        self.assertEqual(self.updater.marker.read_text(), self.new_version)

    def test_extended_payload_recovers_from_retained_archive(self):
        """Retry additional payload paths offline after activation fails."""
        payload = {
            'usr/lib/firmware/example/test.bin': b'test firmware',
            'etc/systemd/network/test.network': b'[Match]\nName=test0\n',
            'etc/mesh.conf': b'acs=n\n',
        }
        for name, body in payload.items():
            self.members[name] = (body, 0o644)
        self.make_archive()
        with patch.dict(os.environ, {
                'TEST_SYSTEMCTL_FAIL': 'restart mesh-status.service'}):
            with self.assertRaises(update.UpdateError):
                self.updater.update()
        self.assertTrue(self.updater.pending.exists())
        self.assertEqual(self.updater.marker.read_text(), self.old_version)
        for name in payload:
            (self.root / name).unlink()
        self.server.rename(self.work / 'offline')
        (self.root / 'run/initial_time_synced').unlink()
        self.events.write_text('')
        self.updater.update(recover_only=True)
        for name, body in payload.items():
            self.assertEqual((self.root / name).read_bytes(), body)
        self.assertEqual(self.old_manager.readlink(),
                         Path('node-manager-static.sh'))
        self.assertNotIn('curl', self.history())
        self.assertFalse(self.updater.pending.exists())
        self.assertEqual(self.updater.marker.read_text(), self.new_version)

    def test_update_retires_vip_deleting_route_service(self):
        unit = self.root / 'etc/systemd/system/mesh-default-route-fix.service'
        unit.parent.mkdir(parents=True)
        unit.write_text('old unit')
        link = unit.parent / 'multi-user.target.wants/mesh-default-route-fix.service'
        link.parent.mkdir()
        link.symlink_to('../mesh-default-route-fix.service')
        script = self.root / 'usr/local/bin/mesh-default-route-fix.sh'
        script.write_text('old script')
        self.updater.update()
        self.assertFalse(unit.exists())
        self.assertFalse(link.is_symlink())
        self.assertFalse(script.exists())
        self.assertIn('systemctl disable --now mesh-default-route-fix.service', self.history())

    def test_update_retires_battery_reader(self):
        """Stop the service before reload and remove its files and state."""
        unit = self.root / 'etc/systemd/system/battery-reader.service'
        unit.parent.mkdir(parents=True)
        unit.write_text('old unit')
        link = unit.parent / 'multi-user.target.wants' / unit.name
        link.parent.mkdir()
        link.symlink_to('../' + unit.name)
        script = self.root / 'usr/local/bin/battery-reader.py'
        script.write_text('old script')
        status = self.root / 'run/battery_status.json'
        status.write_text('{"percentage": 80}')
        self.updater.update()
        self.assertFalse(unit.exists())
        self.assertFalse(link.is_symlink())
        self.assertFalse(script.exists())
        self.assertFalse(status.exists())
        history = self.history()
        self.assertLess(
            history.index('systemctl disable --now ' + unit.name),
            history.index('systemctl daemon-reload'))
        self.updater.retire_battery_reader()
        self.assertEqual(self.history(), history)

    def test_update_retires_old_cpu_unit_before_enabling_new_policy(self):
        unit = self.root / 'etc/systemd/system/cpu-powersave.service'
        unit.parent.mkdir(parents=True)
        unit.write_text('old unit')
        link = unit.parent / 'multi-user.target.wants' / unit.name
        link.parent.mkdir()
        link.symlink_to('../' + unit.name)
        self.updater.update()
        self.assertFalse(unit.exists())
        self.assertFalse(link.is_symlink())
        self.assertTrue((unit.parent / 'manet-cpu-policy.service').is_file())
        self.assertTrue((self.root / 'usr/local/bin/manet-cpu-policy.sh').is_file())
        history = self.history()
        self.assertLess(history.index('systemctl disable --now ' + unit.name),
                        history.index('systemctl daemon-reload'))
        self.assertLess(history.index('systemctl daemon-reload'),
                        history.index('enable --now manet-cpu-policy.service'))
        self.assertLess(history.index('enable --now manet-cpu-policy.service'),
                        history.index('restart node-manager.service'))
        self.updater.retire_cpu_powersave()
        self.assertEqual(self.history(), history)

    def test_cpu_retirement_removes_a_stale_wants_link(self):
        link = self.root / 'etc/systemd/system/multi-user.target.wants/cpu-powersave.service'
        link.parent.mkdir(parents=True)
        link.symlink_to('../cpu-powersave.service')
        self.updater.retire_cpu_powersave()
        self.assertFalse(link.is_symlink())
        self.assertNotIn('systemctl', self.history())

    def test_missing_malformed_wrong_filename_and_mismatched_checksum(self):
        valid = self.checksum.read_text()
        for content in (None, '', 'garbage', valid.replace('cm4-', 'r3a-'), '0' * 64 + '  cm4-tools.tar.gz\n', valid + valid):
            with self.subTest(content=content):
                if content is None:
                    self.checksum.unlink(missing_ok=True)
                else:
                    self.checksum.write_text(content)
                self.assert_rejected_before_install()

    def test_update_retires_role_blind_boot_units_but_keeps_guarded_ap_action(self):
        directory = self.root / 'etc/systemd/system'
        links = directory / 'multi-user.target.wants'
        links.mkdir(parents=True)
        units = ('ap-interface-setup.service', 'ap-txpower.service', 'ebtables-restore.service',
                 'manet-txpower.service', 'halow-txpower-wlan2.service',
                 'manet-halow-power.service')
        for name in units:
            (directory / name).write_text('old boot unit')
            (links / name).symlink_to('../' + name)
        (self.root / 'etc/ebtables.rules').write_text('obsolete rules')
        self.updater.update()
        for name in units:
            self.assertIn('systemctl disable --now ' + name, self.history())
            self.assertFalse((links / name).is_symlink())
            self.assertEqual((directory / name).exists(), name == 'ap-txpower.service')
        self.assertFalse((self.root / 'etc/ebtables.rules').exists())
        self.assertFalse((self.root / 'usr/local/bin/manet-halow-power.py').exists())
        self.assertLess(self.history().index('isolation'), self.history().index('restart node-manager.service'))

    def test_isolation_failure_stops_existing_dhcp_and_preserves_pending_retry(self):
        with patch.dict(os.environ, TEST_ISOLATION_FAIL='1'):
            with self.assertRaises(update.UpdateError):
                self.updater.update()
        self.assertEqual(self.updater.marker.read_text(), self.old_version)
        self.assertTrue(self.updater.pending.exists())
        self.assertIn('systemctl stop dnsmasq.service', self.history())
        self.assertNotIn('restart node-manager.service', self.history())
        self.updater.routine = True
        self.updater.update()
        self.assertEqual(self.updater.marker.read_text(), self.new_version)
        self.assertFalse(self.updater.pending.exists())

    def test_download_failure(self):
        for url in (STABLE_MANIFEST, DOWNLOADS + '/v0.550/cm4-tools.tar.gz'):
            with self.subTest(url=url), patch.dict(os.environ, TEST_DOWNLOAD_FAIL=url):
                self.assert_rejected_before_install()

    def test_wrong_release_even_with_valid_hash(self):
        self.manifest.update(version='0.551', tag='v0.551')
        self.release.write_text(json.dumps(self.manifest))
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

    def test_unsafe_paths(self):
        for name in ('/', './', '/etc/unwanted', '../unwanted', 'usr/../unwanted',
                     '/boot/kernel.img', 'boot/../etc/unwanted',
                     'var//lib/unwanted', 'boot/kernel\n.img',
                     'boot/kernel\x7f.img'):
            with self.subTest(name=name):
                member = tarfile.TarInfo(name)
                self.make_archive([(member, b'')])
                self.assert_rejected_before_install()

    def test_boot_parent_symlink_cannot_redirect_install(self):
        """Broader payload scope must not permit installed symlink escapes."""
        (self.root / 'boot').symlink_to(self.server, target_is_directory=True)
        self.members['boot/kernel.img'] = (b'test kernel image', 0o644)
        self.make_archive()
        self.assert_rejected_before_install()
        self.assertFalse((self.server / 'kernel.img').exists())

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

    def test_kernel_protection_before_dependencies_and_during_install(self):
        self.updater.update()
        events = self.events.read_text().splitlines()
        protects = [i for i, event in enumerate(events)
                    if event == 'kernel-protection']
        self.assertEqual(len(protects), 2)
        self.assertLess(protects[0], events.index('dependency'))
        self.assertGreater(protects[1], events.index('dependency'))
        self.assertLess(protects[1], events.index('systemctl daemon-reload'))

    def test_kernel_protection_failure_stops_before_apt(self):
        with patch.dict(os.environ, TEST_KERNEL_PROTECTION_FAIL='1'):
            with self.assertRaises(update.UpdateError):
                self.updater.update()
        self.assertNotIn('dependency', self.events.read_text())
        self.assertEqual(self.updater.marker.read_text(), self.old_version)

    def test_kernel_policy_and_helper_are_required(self):
        for name in ('etc/apt/preferences.d/00-manet-kernel',
                     'usr/local/bin/manet-kernel-protect.py'):
            with self.subTest(name=name):
                self.assertIn(name, update.REQUIRED)

    def test_dependency_failure_does_not_install_payload(self):
        with patch.dict(os.environ, TEST_DEPENDENCY_FAIL='1'):
            with self.assertRaises(update.UpdateError):
                self.updater.update()
        self.assertEqual(self.updater.marker.read_text(), self.old_version)
        self.assertEqual(self.old_manager.read_text(), 'old manager\n')
        self.assertNotIn('systemctl', self.history())

    def test_service_failures_preserve_marker_and_retry_in_routine_mode(self):
        for failure in ('daemon-reload', 'enable --now manet-dhcp-isolation.service',
                        'enable --now manet-cpu-policy.service',
                        'enable --now manet-mesh-power.service', 'restart mesh-status.service',
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
        self.assertNotIn('systemctl daemon-reload', self.history())
        self.assertNotIn('systemctl restart', self.history())
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
        with patch.dict(os.environ, TEST_SYSTEMCTL_FAIL='daemon-reload'):
            with self.assertRaises(update.UpdateError):
                self.updater.update()
        for name in update.MARKERS:
            (self.root / name).write_text(self.new_version)
        self.updater.routine = True
        self.updater.update()
        self.assertIn('systemctl restart node-manager.service', self.history())
        self.assertFalse(self.updater.pending.exists())

    def interrupt_install(self):
        with patch.dict(os.environ, TEST_SYSTEMCTL_FAIL='daemon-reload'):
            with self.assertRaises(update.UpdateError):
                self.updater.update()
        self.events.write_text('')

    def test_offline_recovery_uses_pinned_release_without_apt(self):
        self.interrupt_install()
        shutil.rmtree(self.server)
        self.updater.development = True
        with patch.dict(os.environ, TEST_DEPENDENCY_FAIL='1'):
            self.updater.update()
        self.assertNotIn('curl', self.history())
        self.assertNotIn('dependency', self.history())
        self.assertEqual(self.updater.marker.read_text(), self.new_version)
        self.assertFalse(self.updater.recovery.exists())

    def test_early_recovery_restores_files_without_starting_services(self):
        self.interrupt_install()
        target = self.root / 'usr/local/bin/mesh-status.py'
        target.write_text('incomplete release')
        self.updater.update(files_only=True)
        self.assertEqual(target.read_bytes(), self.members[
            'usr/local/bin/mesh-status.py'][0])
        self.assertEqual(self.history(), '')
        self.assertEqual(self.updater.marker.read_text(), self.old_version)
        self.assertTrue(self.updater.pending.exists())
        self.updater.update(recover_only=True)
        self.assertFalse(self.updater.pending.exists())
        self.assertIn('restart node-manager.service', self.history())

    def test_recovery_without_pending_does_not_download_or_activate(self):
        self.updater.update(recover_only=True)
        self.updater.update(files_only=True)
        self.assertEqual(self.history(), '')

    def test_missing_recovery_fails_without_downloading_a_different_release(self):
        self.updater.state.mkdir(parents=True)
        self.updater.pending.write_text('incomplete')
        with self.assertRaisesRegex(update.UpdateError, 'missing'):
            self.updater.update()
        self.assertTrue(self.updater.pending.exists())
        self.assertEqual(self.history(), '')

    def test_corrupt_recovery_is_not_installed_or_discarded(self):
        self.interrupt_install()
        package = self.updater.recovery / 'package.tar.gz'
        package.write_bytes(b'corrupt')
        with self.assertRaisesRegex(update.UpdateError, 'SHA-256'):
            self.updater.update(recover_only=True)
        self.assertEqual(self.history(), '')
        self.assertEqual(self.updater.marker.read_text(), self.old_version)
        self.assertTrue(self.updater.pending.exists())
        self.assertTrue(package.exists())

    def test_boot_recovery_is_armed_before_first_payload_replacement(self):
        original = update.atomic_file
        def check(destination, *args, **kwargs):
            if destination == self.updater.pending:
                for name, target in update.RECOVERY_UNITS.items():
                    link = self.root / 'etc/systemd/system' / target / name
                    self.assertTrue(link.is_symlink())
                    self.assertTrue(link.exists())
                for name in ('package.tar.gz', 'package.sha256',
                             'node-update.py', 'manet_release.py',
                             'release.json'):
                    path = self.updater.recovery / name
                    self.assertTrue(path.is_file())
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(self.old_manager.read_text(), 'old manager\n')
            return original(destination, *args, **kwargs)
        with patch.object(update, 'atomic_file', side_effect=check):
            self.updater.update()

    def test_stale_scratch_cleanup_preserves_unrelated_files_and_link_target(self):
        self.updater.state.mkdir(parents=True)
        stale = self.updater.state / 'download-abcdefgh'
        stale.mkdir()
        (stale / 'partial').write_text('partial')
        link = self.updater.state / 'download-12345678'
        link.symlink_to(self.server, target_is_directory=True)
        keep = self.updater.state / 'operator-notes'
        keep.write_text('keep')
        self.updater.routine = True
        self.updater.update()
        self.assertFalse(stale.exists())
        self.assertFalse(link.is_symlink())
        self.assertTrue(self.package.exists())
        self.assertEqual(keep.read_text(), 'keep')

    def test_recovery_removes_abandoned_payload_siblings_only(self):
        self.interrupt_install()
        directory = self.root / 'usr/local/bin'
        stale = directory / '.manet-update-abcdefgh'
        stale.write_text('interrupted copy')
        link = directory / '.manet-link-abcdefgh'
        link.mkdir()
        (link / 'link').symlink_to(self.server, target_is_directory=True)
        keep = directory / '.operator-cache'
        keep.write_text('keep')
        self.updater.update(recover_only=True)
        self.assertFalse(stale.exists())
        self.assertFalse(link.exists())
        self.assertTrue(self.server.exists())
        self.assertEqual(keep.read_text(), 'keep')

    def test_process_death_recovers_using_saved_updater_and_preserves_user_files(self):
        user_file = self.root / 'usr/local/bin/operator-service.py'
        user_file.write_text('operator service')
        # Exit inside a payload replacement: no finally blocks or temp-dir
        # cleanup run. This exercises a real second process and orphan staging.
        script = '''
import importlib.util, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).parent))
spec = importlib.util.spec_from_file_location('updater', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
original = module.os.replace
def replace(source, destination):
    original(source, destination)
    if str(destination).endswith('/usr/local/bin/node-manager-acs.sh'):
        os._exit(73)
module.os.replace = replace
module.Updater(Path(sys.argv[2])).update()
'''
        result = subprocess.run([sys.executable, '-c', script,
                                 str(Path(update.__file__)), str(self.root)],
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 73, result.stderr.decode())
        self.assertTrue(self.updater.pending.exists())
        self.assertTrue(list(self.updater.state.glob('download-*')))
        shutil.rmtree(self.server)
        self.events.write_text('')
        # Installed modules can be from different releases; use the saved pair.
        recovery_script = '''
import importlib.util, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
spec = importlib.util.spec_from_file_location(
    'updater', Path(sys.argv[1]) / 'node-update.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.Updater(Path(sys.argv[2])).update(recover_only=True)
'''
        result = subprocess.run([sys.executable, '-c', recovery_script,
                                 str(self.updater.recovery), str(self.root)],
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertNotIn('curl', self.history())
        self.assertEqual(self.updater.marker.read_text(), self.new_version)
        self.assertEqual(user_file.read_text(), 'operator service')
        self.assertEqual((self.root / 'etc/mesh.conf').read_text(), 'acs=y\n')
        self.assertFalse(self.updater.recovery.exists())
        self.assertFalse(list(self.updater.state.glob('download-*')))

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
        self.assertIn('curl ' + STABLE_MANIFEST, self.history())
        self.assertNotIn('tools.tar.gz', self.history())
        self.assertGreater(self.updater.marker.stat().st_mtime, old)

    def newer_updater(self):
        """Ship an updater that differs from the one running this test."""
        name = 'usr/local/bin/node-update.py'
        self.members[name] = self.members[name][0] + b'# newer release\n', 0o755
        self.make_archive()
        return self.members[name][0]

    def test_newer_updater_checks_again_from_a_private_copy(self):
        body = self.newer_updater()
        lock_path = self.root / 'run/manet-update.lock'

        def replaced(path, args):
            # The lock stays held through the handoff.
            self.assertTrue(os.get_inheritable(int(args[3])))
            with lock_path.open('a') as other, self.assertRaises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            raise SystemExit
        with patch.object(update.os, 'execv', side_effect=replaced) as execv:
            with self.assertRaises(SystemExit):
                self.updater.update()
        copy = self.updater.handoff / 'node-update.py'
        self.assertEqual(copy.read_bytes(), body)
        self.assertEqual(copy.stat().st_mode & 0o777, 0o700)
        for name in ('manet_release.py', 'manet_kernel_update.py'):
            self.assertEqual((self.updater.handoff / name).read_bytes(),
                             Path(__file__).with_name(name).read_bytes())
        archive = self.updater.handoff / 'cm4-tools.tar.gz'
        self.assertEqual(archive.read_bytes(), self.package.read_bytes())
        self.assertTrue((self.updater.handoff / 'cm4-tools.tar.gz.sha256').is_file())
        args = execv.call_args.args[1]
        self.assertEqual(args[:3], [sys.executable, str(copy), '--handed-off'])
        self.assertEqual(args[4:], ['--local-archive', str(archive), '--local-version', '0.550'])
        # Nothing live changes before the new updater arms its own recovery.
        self.assertFalse((self.root / 'usr/local/bin/node-update.py').exists())
        self.assertEqual(self.updater.marker.read_text(), self.old_version)
        self.assertFalse(self.updater.pending.exists())
        self.assertNotIn('dependency', self.history())
        self.assertEqual(self.old_manager.read_text(), 'old manager\n')
        # The copy runs under the inherited lock with the verified download,
        # keeps itself while running and never hands off a second time.
        lock = lock_path.open('a')
        fcntl.flock(lock, fcntl.LOCK_EX)
        with patch.object(update, '__file__', str(copy)), patch.object(update.os, 'execv') as execv:
            self.updater.update(handed_off=os.dup(lock.fileno()), local_archive=archive,
                                local_version='0.550')
        lock.close()
        execv.assert_not_called()
        self.assertTrue(copy.exists())
        self.assertFalse(archive.exists())
        downloads = [line for line in self.history().splitlines()
                     if line.startswith('curl ') and line.endswith('/cm4-tools.tar.gz')]
        self.assertEqual(len(downloads), 1)
        self.assertEqual(self.updater.marker.read_text(), self.new_version)
        self.assertEqual((self.root / 'usr/local/bin/node-update.py').read_bytes(), body)
        self.assertEqual((self.updater.installed / 'node-update.py').read_bytes(), body)
        # Any later run from elsewhere removes the copy.
        self.updater.update()
        self.assertFalse(self.updater.handoff.exists())

    def test_handed_off_descriptor_must_be_the_update_lock(self):
        (self.root / 'run/manet-update.lock').touch()
        with (self.work / 'not-the-lock').open('a') as other:
            for descriptor in (other.fileno(), sys.stdout.fileno()):
                with self.subTest(descriptor=descriptor):
                    with self.assertRaisesRegex(update.UpdateError, 'not the update lock'):
                        self.updater.update(handed_off=descriptor)
                    # A refused descriptor stays open for its owner.
                    os.fstat(descriptor)
        self.assertEqual(self.history(), '')

    def test_handoff_keeps_the_requested_mode(self):
        self.newer_updater()
        updater = update.Updater(self.root, routine=True, development=True, allow_downgrade=True)
        updater.log = Mock()
        with patch.object(update.os, 'execv', side_effect=SystemExit) as execv:
            with self.assertRaises(SystemExit):
                updater.update(local_archive=self.package, local_version='0.550')
        argv = execv.call_args.args[1][1:]
        self.assertEqual(argv[1], '--handed-off')
        self.assertEqual(argv[3:], ['--routine', '--development', '--allow-downgrade',
                                    '--local-archive', str(self.package), '--local-version', '0.550'])
        # An operator's own archive is read in place, never moved.
        self.assertTrue(self.package.exists())
        self.assertFalse((updater.handoff / 'cm4-tools.tar.gz').exists())
        runner = Mock()
        with patch.object(update, 'Updater', return_value=runner) as factory, \
                patch.object(update.os, 'geteuid', return_value=0), \
                patch.object(update.signal, 'signal'), \
                patch.object(sys, 'argv', argv):
            self.assertEqual(update.main(), 0)
        factory.assert_called_with(routine=True, development=True, allow_downgrade=True)
        runner.update.assert_called_with(recover_only=False, files_only=False, cancel_kernel=False,
                                         local_archive=self.package, local_version='0.550',
                                         handed_off=int(argv[2]))

    def test_reinstall_and_downgrade_keep_the_running_updater(self):
        self.newer_updater()
        self.updater.allow_downgrade = True
        for installed in (self.new_version, '0.551\n09/2026\n'):
            with self.subTest(installed=installed.split()[0]):
                for name in update.MARKERS:
                    (self.root / name).write_text(installed)
                with patch.object(update.os, 'execv') as execv:
                    self.updater.update(local_archive=self.package, local_version='0.550')
                execv.assert_not_called()
                self.assertEqual(self.updater.marker.read_text(), self.new_version)

    def test_unsynced_manual_update_reports_clock_before_any_download(self):
        (self.root / 'run/initial_time_synced').unlink()
        for development in (False, True):
            with self.subTest(development=development):
                self.updater.development = development
                with patch.object(update, 'Updater', return_value=self.updater), \
                        patch.object(update.os, 'geteuid', return_value=0), \
                        patch.object(update.signal, 'signal'), \
                        patch.object(sys, 'argv', ['node-update.py']):
                    self.assertEqual(update.main(), 1)
                self.assertIn('Clock not synced', self.updater.log.call_args.args[0])
                self.assertEqual(self.history(), '')

    def test_unsynced_local_recovery_does_not_need_a_clock(self):
        with patch.dict(os.environ, TEST_SYSTEMCTL_FAIL='daemon-reload'):
            with self.assertRaises(update.UpdateError):
                self.updater.update()
        (self.root / 'run/initial_time_synced').unlink()
        self.events.unlink()
        self.updater.update(files_only=True)
        self.assertEqual(self.history(), '')
        self.updater.update(recover_only=True)
        self.assertNotIn('curl ', self.history())
        self.assertEqual(self.updater.marker.read_text(), self.new_version)

    def test_update_requires_clock_trigger_payload_and_activates_it(self):
        for name in ('manet-auto-update.path', 'manet-auto-update-clock.service',
                     'manet-auto-update.service'):
            with self.subTest(unit=name):
                self.assertIn('etc/systemd/system/' + name, update.REQUIRED)
        self.assertIn('usr/local/bin/manet-auto-update.sh', update.REQUIRED)
        self.updater.update()
        self.assertIn('systemctl enable --now manet-auto-update.path', self.history())

    def test_all_board_urls(self):
        for model, board in (('ROCK3 Model A', 'r3a'), ('Radxa ROCK 3A', 'r3a'),
                            ('Raspberry Pi 5 Model B', 'rpi5')):
            with self.subTest(board=board):
                (self.root / 'proc/device-tree/model').write_text(model)
                self.assertEqual(self.updater.board(), board)
                filename = f'{board}-tools.tar.gz'
                shutil.copyfile(self.package, self.server / filename)
                (self.server / (filename + '.sha256')).write_text(self.checksum.read_text().replace('cm4-tools.tar.gz', filename))
                self.updater.marker.write_text(self.old_version)
                self.updater.update()
                self.assertIn(DOWNLOADS + '/v0.550/' + filename + '.sha256', self.history())

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

    def test_stable_does_not_silently_downgrade_a_development_node(self):
        for name in update.MARKERS:
            (self.root / name).write_text('0.551\n09/2026\n')
        self.updater.update()
        self.assertNotIn('systemctl', self.history())
        self.assertIn('newer than selected', self.updater.log.call_args.args[0])
        self.updater.allow_downgrade = True
        self.updater.update()
        self.assertEqual(self.updater.marker.read_text(), self.new_version)

    def test_development_download_stays_on_selected_release(self):
        self.updater.development = True
        (self.server / 'releases?per_page=100&page=1').write_text(json.dumps([{
            'id': 1, 'tag_name': 'v0.550', 'draft': False, 'prerelease': True,
            'published_at': '2026-09-28T00:00:00Z',
            'assets': [{'name': 'manet-release.json'}],
        }]))
        self.updater.update()
        self.assertNotIn(STABLE_MANIFEST, self.history())
        self.assertIn(DOWNLOADS + '/v0.550/manet-release.json', self.history())
        self.assertEqual(self.updater.marker.read_text(), self.new_version)


if __name__ == '__main__':
    unittest.main()
