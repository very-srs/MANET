"""Keep package cleanup from removing the radio runtime or active swap."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('os_cleanup', TOOLS / 'manet-os-cleanup.py')
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


class CleanupTests(unittest.TestCase):
    def package(self, essential=False):
        return {'size_kib': 100, 'essential': essential, 'version': '1'}

    def test_headers_can_go_but_kernel_firmware_and_runtime_are_protected(self):
        runtime = ['linux-image-6.12.47+rpt-rpi-v8', 'linux-base-rpi-v8',
                   'firmware-mediatek', 'firmware-realtek', 'raspi-firmware',
                   'python3-cryptography', 'libnl-route-3-200:arm64', 'rfkill',
                   'alsa-ucm-conf', 'rtkit', 'gpsd-tools', 'hostapd', 'libnss-resolve',
                   'netcat-openbsd', 'busybox', 'zstd']
        packages = {n: self.package() for n in runtime + ['gpsd-clients',
                    'linux-headers-6.12.47+rpt-common-rpi', 'build-essential']}
        packages['essential-example'] = self.package(True)
        targets, protected = cleanup.choose_packages(packages)
        self.assertEqual(set(targets), {'gpsd-clients', 'build-essential',
                                       'linux-headers-6.12.47+rpt-common-rpi'})
        self.assertEqual(set(protected), set(runtime) | {'essential-example'})

    def test_cli_gps_tools_must_exist_before_gui_client_removal(self):
        with self.assertRaisesRegex(RuntimeError, 'gpsd-tools'):
            cleanup.choose_packages({'gpsd-clients': self.package()})

    def test_apt_cannot_remove_a_protected_dependency_or_upgrade_anything(self):
        for output in ('Purg hostapd [1]\n', 'Remv libnl-route-3-200 [3]\n',
                       'Inst systemd [1] (2 Debian)\n', 'Conf systemd (2 Debian)\n'):
            with self.subTest(output=output), self.assertRaises(RuntimeError):
                cleanup.validate_plan(output, ['hostapd', 'libnl-route-3-200:arm64'])
        self.assertEqual(cleanup.validate_plan('Purg gcc [14]\nPurg python3-scipy [1]\n',
                                              ['python3', 'hostapd']), ['gcc', 'python3-scipy'])

    def test_empty_policy_plan_is_safe_to_repeat(self):
        self.assertEqual(cleanup.choose_packages({'hostapd': self.package()}), ([], ['hostapd']))
        self.assertEqual(cleanup.validate_plan('0 upgraded, 0 to remove\n', ['hostapd']), [])

    def test_swap_file_is_never_deleted_while_active_attached_or_a_symlink(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / 'swap'
            path.touch()
            header = 'Filename Type Size Used Priority\n'
            self.assertTrue(cleanup.swap_is_unused(path, header, ''))
            self.assertFalse(cleanup.swap_is_unused(path, header + '/var/swap file 10 0 -2\n', ''))
            self.assertFalse(cleanup.swap_is_unused(path, header + '/dev/zram0 partition 10 0 100\n', ''))
            self.assertFalse(cleanup.swap_is_unused(path, header, '/dev/loop0: (/var/swap)'))
            link = Path(scratch) / 'link'
            link.symlink_to(path)
            self.assertFalse(cleanup.swap_is_unused(link, header, ''))
            self.assertFalse(cleanup.swap_is_unused(Path(scratch), header, ''))


if __name__ == '__main__':
    unittest.main()
