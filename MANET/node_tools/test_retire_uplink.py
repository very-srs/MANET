"""Exercise field retirement, retry and protection of current Wi-Fi/HaLow roles."""

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
import unittest.mock


TOOLS = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location('retire_uplink', TOOLS / 'manet-retire-uplink.py')
retire = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(retire)


class RetirementTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.cleanup = retire.Retirement(self.root)
        self.units = {'usb-wifi-client@wlan5.service', 'wpa_supplicant@wlan6-uplink.service'}
        self.calls = []
        self.cleanup.run = self.command

    def command(self, *args):
        self.calls.append(args)
        if args[1] in ('list-units', 'list-unit-files'):
            return '\n'.join(unit + ' enabled' for unit in sorted(self.units))
        if args[:3] == ('systemctl', 'disable', '--now'):
            self.units.remove(args[3])
        return ''

    def test_no_matching_unit_files_is_not_an_error(self):
        # systemctl list-unit-files exits 1 with no output when nothing matches.
        def none_listed(*args):
            self.calls.append(args)
            if args[1] == 'list-unit-files':
                raise subprocess.CalledProcessError(1, args, output='')
            return ''
        self.cleanup.run = none_listed
        rule = self.write('etc/udev/rules.d/81-usb-wifi-halow-recovery.rules')
        self.cleanup.apply()
        self.assertFalse(rule.exists())
        self.assertIn(('systemctl', 'daemon-reload'), self.calls)

    def test_other_listing_failures_still_fail_the_update(self):
        for code, output in ((1, 'usb-wifi-client@wlan5.service enabled\n'), (4, '')):
            with self.subTest(code=code):
                def failing(*args):
                    if args[1] == 'list-unit-files':
                        raise subprocess.CalledProcessError(code, args, output=output)
                    return ''
                self.cleanup.run = failing
                with self.assertRaises(subprocess.CalledProcessError):
                    self.cleanup.apply()

    def test_requirement_is_removed_before_the_unit_it_names(self):
        dropin = self.write('etc/systemd/system/manet-wifi-uplink.service.d/retire-uplink.conf')
        unit = self.write('etc/systemd/system/manet-retire-uplink.service')
        order = []
        real = retire.Path.unlink
        def unlink(path, missing_ok=False):
            if path in (dropin, unit):
                order.append(path.name)
            return real(path, missing_ok=missing_ok)
        with unittest.mock.patch.object(retire.Path, 'unlink', unlink):
            self.cleanup.apply()
        self.assertEqual(order, ['retire-uplink.conf', 'manet-retire-uplink.service'])

    def write(self, name, content='unchanged\n'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def artifacts(self):
        names = [
            'etc/manet/usb-wifi-uplink.conf', 'run/manet-usb-wifi-scan.json',
            'etc/systemd/system/usb-wifi-client@.service',
            'etc/systemd/system/usb-wifi-client@wlan5.service',
            'etc/systemd/system/wpa_supplicant@wlan6-uplink.service',
            'usr/local/bin/usb-wifi-uplink.sh', 'usr/local/bin/usb-wifi-halow-recovery.sh',
            'usr/local/share/man/man8/usb-wifi-uplink.sh.8',
            'usr/local/share/man/man8/usb-wifi-uplink.sh.8.gz',
            'etc/udev/rules.d/81-usb-wifi-halow-recovery.rules',
            'etc/systemd/system/manet-retire-uplink.service',
            'etc/systemd/system/manet-wifi-uplink.service.d/retire-uplink.conf',
        ]
        for iface in ('wlan5', 'wlan6'):
            names += [f'etc/wpa_supplicant/wpa_supplicant-{iface}-uplink.conf',
                      f'etc/systemd/network/20-{iface}-usb-wifi-uplink.network']
        paths = [self.write(name) for name in names]
        link = self.root / 'etc/systemd/system/multi-user.target.wants/usb-wifi-client@wlan5.service'
        link.parent.mkdir()
        link.symlink_to('../usb-wifi-client@.service')
        return paths + [link]

    def test_removes_retired_artifacts_and_keeps_builtin_halow_and_mesh_intact(self):
        artifacts = self.artifacts()
        preserved = [self.write(name) for name in (
            'etc/manet/wifi-uplink.json', 'etc/manet/wifi-uplink-saved.json',
            'etc/mesh.conf', 'var/lib/wifi_uplink_if',
            'etc/systemd/system/manet-wifi-client@.service',
            'etc/systemd/system/wpa_supplicant_s1g@wlan7.service',
            'etc/udev/rules.d/80-morse.rules',
        )]
        self.write('var/lib/halow_if', 'wlan7\n')
        self.write('var/lib/mesh_if', 'wlan8\n')
        self.write('var/lib/no_mesh_if', 'wlan9\n')
        driver = self.root / 'sys/class/net/wlan10/device/driver'
        driver.parent.mkdir(parents=True)
        driver.symlink_to('/sys/bus/usb/drivers/morse_usb')
        for iface in ('wlan0', 'wlan1', 'wlan2', 'wlan3', 'wlan7', 'wlan8', 'wlan9', 'wlan10'):
            preserved.append(self.write(f'etc/wpa_supplicant/wpa_supplicant-{iface}-uplink.conf'))
        before = {p: p.read_bytes() for p in preserved}
        self.cleanup.apply()
        for path in artifacts:
            self.assertFalse(path.exists() or path.is_symlink(), str(path))
        self.assertEqual({p: p.read_bytes() for p in preserved}, before)
        stops = [c[-1] for c in self.calls if c[:3] == ('systemctl', 'disable', '--now')]
        self.assertEqual(stops, ['usb-wifi-client@wlan5.service', 'wpa_supplicant@wlan6-uplink.service'])
        self.assertIn(('udevadm', 'control', '--reload-rules'), self.calls)
        self.assertIn(('networkctl', 'reload'), self.calls)
        self.calls.clear()
        self.cleanup.apply()
        self.assertFalse(any(c[0] in ('networkctl', 'udevadm') or 'disable' in c for c in self.calls))

    def test_absent_artifacts_and_orphaned_unplugged_config(self):
        self.units.clear()
        self.cleanup.apply()
        orphan = self.write('etc/wpa_supplicant/wpa_supplicant-wlan5-uplink.conf')
        self.cleanup.apply()
        self.assertFalse(orphan.exists())
        self.assertFalse(any('disable' in c for c in self.calls))

    def test_stop_failure_preserves_credentials_and_retry_dependency(self):
        paths = self.artifacts()
        def failed(*args):
            if args[:3] == ('systemctl', 'disable', '--now'):
                raise subprocess.CalledProcessError(1, args)
            return self.command(*args)
        self.cleanup.run = failed
        with self.assertRaises(subprocess.CalledProcessError):
            self.cleanup.apply()
        self.assertTrue(all(p.exists() for p in paths))
        self.cleanup.run = self.command
        self.cleanup.apply()
        self.assertFalse(any(p.exists() for p in paths))

    def test_reload_failure_retries_after_deleted_files_and_keeps_dependency(self):
        self.artifacts()
        def failed(*args):
            if args == ('networkctl', 'reload'):
                raise subprocess.CalledProcessError(1, args)
            return self.command(*args)
        self.cleanup.run = failed
        with self.assertRaises(subprocess.CalledProcessError):
            self.cleanup.apply()
        self.assertTrue((self.root / 'var/lib/manet-retire-uplink/reload-pending').exists())
        self.assertTrue((self.root / 'etc/systemd/system/manet-retire-uplink.service').exists())
        self.cleanup.run = self.command
        self.cleanup.apply()
        self.assertFalse((self.root / 'var/lib/manet-retire-uplink/reload-pending').exists())
        self.assertFalse((self.root / 'etc/systemd/system/manet-retire-uplink.service').exists())

    def test_first_install_dependency_runs_before_builtin_client_startup(self):
        systemd = TOOLS.parent / 'systemd'
        unit = (systemd / 'manet-retire-uplink.service').read_text()
        dropin = (systemd / 'manet-wifi-uplink.service.d/retire-uplink.conf').read_text()
        self.assertIn('Type=oneshot', unit)
        self.assertIn('Before=manet-wifi-uplink.service', unit)
        self.assertIn('ExecStart=/usr/local/bin/manet-retire-uplink.py', unit)
        self.assertIn('Requires=manet-retire-uplink.service', dropin)
        self.assertIn('After=manet-retire-uplink.service', dropin)


if __name__ == '__main__':
    unittest.main()
