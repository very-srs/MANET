"""The one-time 0.541 upgrade; optional integration uses the published payload."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / 'releases/upgrade-0.541-to-0.559.py'
SPEC = importlib.util.spec_from_file_location('legacy_upgrade', SOURCE)
upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade)


class LegacyFixture(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='manet-legacy-test-')
        self.addCleanup(scratch.cleanup)
        self.work = Path(scratch.name)
        self.root = self.work / 'root'
        for name, body in {
            'proc/device-tree/model': 'Raspberry Pi Compute Module 4 Rev 1.0\0',
            'etc/os-release': 'ID=debian\nVERSION_ID="13"\n',
            'etc/manet_version.txt': '0.541\n08/2026\n',
            'usr/local/bin/version.txt': '0.541\n08/2026\n',
            'etc/mesh.conf': 'acs=Y\nadmin_password=test-only-password\nmesh_ssid=keep-me\n',
            'usr/local/bin/node-manager.sh': '# old copied manager\n',
            'etc/wpa_supplicant/wpa_supplicant-wlan2-s1g.conf': 'channel=12\nop_class=71\n',
            'etc/systemd/network/10-bat0.network': '[Match]\nName=bat0\n',
            'etc/mesh_ipv4_state': 'saved allocation\n',
            'home/radio/application/data': 'operator data\n',
        }.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        self.cache = self.work / 'cache'
        self.cache.mkdir()


class LegacyUpgradeTests(LegacyFixture):
    def test_cm4_source_and_os_gate(self):
        self.assertEqual(upgrade.preflight(self.root), '0.541')
        for name, body in (
                ('etc/manet_version.txt', '0.562\n'),
                ('etc/os-release', 'VERSION_ID="12"\n'),
                ('proc/device-tree/model', 'Raspberry Pi 5')):
            path = self.root / name
            before = path.read_text()
            path.write_text(body)
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                upgrade.preflight(self.root)
            path.write_text(before)

    def test_never_overrides_newer_offline_recovery(self):
        (self.root / 'var/lib/manet-update/recovery').mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError, 'newer updater recovery'):
            upgrade.preflight(self.root)

    def test_bad_payload_rejected_before_import(self):
        def fake_fetch(name, path, limit):
            path.write_bytes(b'not a release')
        with patch.object(upgrade, 'fetch', side_effect=fake_fetch):
            with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
                upgrade.prepare(self.cache)
        self.assertFalse((self.cache / 'node-update.py').exists())


@unittest.skipUnless(os.environ.get('MANET_0559_ARCHIVE'), 'set MANET_0559_ARCHIVE for published-payload checks')
class PublishedLegacyUpgradeTests(LegacyFixture):
    def setUp(self):
        super().setUp()
        self.package = Path(os.environ['MANET_0559_ARCHIVE'])
        self.assertEqual(upgrade.digest(self.package), upgrade.SHA256)
        manifest = {'schema': 1, 'version': upgrade.VERSION, 'tag': 'v' + upgrade.VERSION,
                    'commit': upgrade.COMMIT,
                    'assets': {upgrade.PACKAGE: {'size': upgrade.SIZE, 'sha256': upgrade.SHA256}}}

        def fake_fetch(name, path, limit):
            if name == upgrade.PACKAGE:
                shutil.copyfile(self.package, path)
            elif name.endswith('.sha256'):
                path.write_text(f'{upgrade.SHA256}  {upgrade.PACKAGE}\n')
            else:
                path.write_text(json.dumps(manifest))

        with patch.object(upgrade, 'fetch', side_effect=fake_fetch):
            self.module, self.cached_package, self.checksum = upgrade.prepare(self.cache)
        updater = self.module.Updater(self.root)
        self.members = updater.validate(self.cached_package, self.checksum,
                                        upgrade.PACKAGE, upgrade.VERSION)
        self.preserved = {name: (self.root / name).read_bytes() for name in (
            'etc/mesh.conf', 'etc/mesh_ipv4_state',
            'etc/wpa_supplicant/wpa_supplicant-wlan2-s1g.conf',
            'etc/systemd/network/10-bat0.network', 'home/radio/application/data')}
        units = self.root / 'etc/systemd/system'
        links = units / 'multi-user.target.wants'
        links.mkdir(parents=True)
        for name in ('ap-interface-setup.service', 'halow-txpower-wlan2.service',
                     'mesh-default-route-fix.service', 'ebtables-restore.service'):
            (units / name).write_text('old generated unit\n')
            (links / name).symlink_to('../' + name)

    def test_real_payload_installs_preserving_configuration(self):
        upgrade.backup(self.module, self.cache, self.root)
        saved = self.cache / 'configuration-before-upgrade.tar.gz'
        original_backup = saved.read_bytes()
        events = []

        def command(args, timeout=120):
            events.append([str(arg) for arg in args])
            self.assertEqual((self.root / 'etc/manet_version.txt').read_text().splitlines()[0], '0.541')
            return b''

        with patch.object(self.module, 'run_command', side_effect=command):
            upgrade.install_cached(self.module, self.cache, self.cached_package, self.checksum, self.root)
        for name, body in self.preserved.items():
            self.assertEqual((self.root / name).read_bytes(), body)
        self.assertEqual((self.root / 'usr/local/bin/node-manager.sh').readlink(), Path('node-manager-acs.sh'))
        self.assertFalse((self.root / 'etc/systemd/system/ap-interface-setup.service').exists())
        self.assertIn(['systemctl', 'is-active', '--quiet', 'one-shot-time-sync.service'], events)
        self.assertTrue(any(event[0].endswith('manet-admin-setup.sh') for event in events))
        self.assertFalse((self.root / 'var/lib/manet-update/in-progress').exists())
        upgrade.backup(self.module, self.cache, self.root)
        self.assertEqual(saved.read_bytes(), original_backup)
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        with tarfile.open(saved) as archive:
            self.assertEqual(archive.extractfile('etc/mesh.conf').read(), self.preserved['etc/mesh.conf'])

    def test_activation_failure_is_retryable_and_does_not_advance_version(self):
        def fail(args, timeout=120):
            if list(args) == ['systemctl', 'restart', 'node-manager.service']:
                raise self.module.UpdateError('test activation failure')
            return b''

        with patch.object(self.module, 'run_command', side_effect=fail):
            with self.assertRaisesRegex(self.module.UpdateError, 'activation failure'):
                upgrade.install_cached(self.module, self.cache, self.cached_package, self.checksum, self.root)
        self.assertEqual((self.root / 'etc/manet_version.txt').read_text().splitlines()[0], '0.541')
        self.assertTrue((self.root / 'var/lib/manet-update/in-progress').exists())
        with patch.object(self.module, 'run_command', return_value=b''):
            upgrade.install_cached(self.module, self.cache, self.cached_package, self.checksum, self.root)
        self.assertEqual((self.root / 'etc/manet_version.txt').read_text().splitlines()[0], '0.559')
        self.assertFalse((self.root / 'var/lib/manet-update/in-progress').exists())


if __name__ == '__main__':
    unittest.main()
