"""Release package identity, APT pin and regulatory file installation checks."""

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location(
    'release_setup', Path(__file__).with_name('manet-release-setup.py')
)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class ReleaseSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'proc/device-tree').mkdir(parents=True)
        (self.root / 'proc/device-tree/model').write_text(
            'Raspberry Pi Compute Module 4'
        )
        self.base = self.root / 'usr/local/share/manet/release'
        self.base.mkdir(parents=True)
        files = {
            'hostapd.deb': b'Debian package fixture',
            'regulatory.db': b'RGDB fixture',
        }
        for name, value in files.items():
            (self.base / name).write_bytes(value)
        (self.base / 'manifest.json').write_text(
            json.dumps(
                dict(
                    schema=1,
                    board='cm4',
                    hostapd_version=setup.PACKAGE_VERSION,
                    files={
                        n: hashlib.sha256(v).hexdigest()
                        for n, v in files.items()
                    },
                )
            )
        )
        self.pin = self.root / 'etc/apt/preferences.d/01-manet-hostapd'
        self.pin.parent.mkdir(parents=True)
        self.pin.write_text(setup.PIN)
        self.run = Mock(side_effect=self.command)

    def command(self, args, **kwargs):
        if args[0] == 'dpkg-deb':
            output = 'Package: hostapd\nVersion: 2:2.10-24+manet1\nArchitecture: arm64\n'
        elif args[0] == 'dpkg-query':
            output = 'install ok installed 2:2.10-24+manet1 arm64'
        elif args[0] == 'apt-cache':
            output = '  Candidate: 2:2.10-24+manet1\n'
        elif args[-1] == '-v':
            return SimpleNamespace(
                returncode=1,
                stdout='',
                stderr=(
                    'hostapd v2.10-manet-dfs-follow-v1\n'
                    'User space daemon for IEEE 802.11 AP management,\n'
                    'IEEE 802.1X/WPA/WPA2/EAP/RADIUS Authenticator\n'
                ),
            )
        else:
            output = ''
        return SimpleNamespace(returncode=0, stdout=output, stderr='')

    def test_verified_package_and_pin_precede_regdb_installation(self):
        setup.install_release_payload(self.root, self.run)
        self.assertEqual(
            (self.root / 'usr/lib/firmware/regulatory.db').read_bytes(),
            b'RGDB fixture',
        )
        calls = [c.args[0][0] for c in self.run.call_args_list]
        self.assertEqual(calls[:3], ['dpkg-deb', 'dpkg', 'dpkg-query'])
        self.assertEqual(calls[-1], 'apt-cache')

    def test_corruption_and_wrong_pin_fail_before_dpkg(self):
        self.pin.write_text(setup.PIN.replace('1001', '100'))
        with self.assertRaisesRegex(ValueError, 'APT pin'):
            setup.install_release_payload(self.root, self.run)
        self.run.assert_not_called()
        self.pin.write_text(setup.PIN)
        (self.base / 'hostapd.deb').write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            setup.install_release_payload(self.root, self.run)
        self.run.assert_not_called()

    def test_wrong_installed_binary_cannot_commit_regdb(self):
        original = self.command

        def missing_marker(args, **kwargs):
            if args[-1] == '-v':
                return SimpleNamespace(
                    returncode=1, stdout='', stderr='hostapd v2.10\n'
                )
            return original(args, **kwargs)

        self.run.side_effect = missing_marker
        with self.assertRaisesRegex(ValueError, 'lacks DFS follower'):
            setup.install_release_payload(self.root, self.run)
        self.assertFalse(
            (self.root / 'usr/lib/firmware/regulatory.db').exists()
        )

    def test_other_boards_preserve_their_signed_regdb(self):
        for board in ('Radxa ROCK 3A', 'Raspberry Pi 5 Model B'):
            (self.root / 'proc/device-tree/model').write_text(board)
            setup.install_release_payload(self.root, self.run)
            self.assertFalse(
                (self.root / 'usr/lib/firmware/regulatory.db').exists()
            )


if __name__ == '__main__':
    unittest.main()
