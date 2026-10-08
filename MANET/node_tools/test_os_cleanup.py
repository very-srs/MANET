"""Keep package cleanup from removing the radio runtime or active swap."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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
                   'alsa-ucm-conf', 'polkitd', 'gpsd-tools', 'hostapd', 'libnss-resolve',
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

    def test_profile_two_removes_unused_audio_and_user_bus_but_keeps_linux_runtime(self):
        removed = {'pulseaudio', 'pulseaudio-utils', 'rtkit'}
        retained = {'dbus', 'dbus-user-session', 'dbus-daemon', 'libpam-systemd', 'polkitd', 'openssh-client',
                    'gnupg', 'gpg-agent', 'dirmngr', 'keyboxd', 'iperf3', 'wpasupplicant',
                    'alsa-utils', 'gstreamer1.0-alsa', 'e2fsprogs', 'cron', 'man-db'}
        targets, protected = cleanup.choose_packages({n: self.package() for n in removed | retained})
        self.assertEqual(set(targets), removed)
        self.assertEqual(set(protected), retained)
        for name in retained:
            with self.assertRaises(RuntimeError):
                cleanup.validate_plan(f'Remv {name} [1]\n', protected)

    def test_fresh_provisioning_does_not_install_removed_audio_packages(self):
        for template in (TOOLS.parent / 'provisioning').glob('*.sh.template'):
            body = template.read_text()
            for name in ('pulseaudio', 'pulseaudio-utils', 'rtkit', 'dbus-user-session'):
                self.assertNotRegex(body, rf'\b{name}\b')
            self.assertIn('alsa-utils alsa-ucm-conf alsa-topology-conf', body)
            self.assertIn('gstreamer1.0-alsa', body)
            self.assertIn('iperf3/start_daemon boolean true', body)

    def test_unknown_cron_jobs_and_lvm_preserve_their_services(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            policy = cleanup.runtime_policy(root)
            self.assertIn('cron.service', policy['mask_system'])
            self.assertIn('e2scrub_reap.service', policy['mask_system'])
            job = root / 'etc/cron.daily/operator-backup'
            job.parent.mkdir(parents=True)
            job.write_text('#!/bin/sh\nbackup\n')
            job.chmod(0o755)
            lvm = root / 'sys/class/block/dm-0/dm/uuid'
            lvm.parent.mkdir(parents=True)
            lvm.write_text('LVM-example')
            policy = cleanup.runtime_policy(root)
            self.assertIn('cron.service', policy['keep'])
            self.assertIn('e2scrub_reap.service', policy['keep'])
            self.assertNotIn('cron.service', policy['mask_system'])
            self.assertNotIn('e2scrub_all.timer', policy['mask_system'])

    def test_only_guarded_distribution_cron_work_is_redundant(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            job = root / 'etc/cron.daily/man-db'
            job.parent.mkdir(parents=True)
            job.write_text('#!/bin/sh\nif [ -d /run/systemd/system ]; then\n  exit 0\nfi\nmandb\n')
            job.chmod(0o755)
            table = root / 'etc/crontab'
            table.write_text('SHELL=/bin/sh\n17 *\t* * *\troot cd / && run-parts --report /etc/cron.hourly\n'
                             '25 6 * * * root test -x /usr/sbin/anacron || ( cd / && run-parts --report /etc/cron.daily )\n'
                             '47 6 * * 7 root test -x /usr/sbin/anacron || { cd / && run-parts --report /etc/cron.weekly; }\n')
            scrub = root / 'etc/cron.d/e2scrub_all'
            scrub.parent.mkdir()
            scrub.write_text('30 3 * * 0 root test -e /run/systemd/system || SERVICE_MODE=1 /usr/lib/e2scrub_all_cron\n')
            self.assertEqual(cleanup.cron_consumers(root), [])
            table.write_text(table.read_text() + '* * * * * root /usr/local/bin/job\n')
            self.assertEqual(cleanup.cron_consumers(root), [str(table)])
            table.unlink()
            job.write_text('#!/bin/sh\nmandb\n')
            self.assertEqual(cleanup.cron_consumers(root), [str(job)])
            job.write_text('#!/bin/sh\nbackup\nif [ -d /run/systemd/system ]; then\nexit 0\nfi\n')
            self.assertEqual(cleanup.cron_consumers(root), [str(job)])

    def test_user_crontabs_are_preserved(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            table = root / 'var/spool/cron/crontabs/radio'
            table.parent.mkdir(parents=True)
            table.write_text('@daily backup\n')
            self.assertEqual(cleanup.cron_consumers(root), [str(table)])

    def test_runtime_actions_target_exact_units_and_stop_live_user_audio(self):
        calls = []
        def command(args):
            calls.append(args)
            if 'list-unit-files' in args:
                return '\n'.join(n + ' enabled enabled' for n in cleanup.USER_UNITS)
            return 'loaded' if 'LoadState' in args else ''
        with tempfile.TemporaryDirectory() as scratch, patch.object(cleanup, 'command', side_effect=command), \
                patch.object(cleanup, 'user_systemctl', return_value=['runuser', '-u', 'radio', '--', 'systemctl', '--user']):
            policy = cleanup.runtime_policy(Path(scratch))
            record = Path(scratch) / 'actions.jsonl'
            for _ in range(2):  # interrupted/repeated run uses the same safe operations
                cleanup.apply_runtime(policy, ['radio'], record)
            actions = [json.loads(line) for line in record.read_text().splitlines()]
            self.assertIn(['systemctl', '--global', 'mask', *cleanup.USER_UNITS], actions)
            self.assertIn(['runuser', '-u', 'radio', '--', 'systemctl', '--user', 'stop', 'pulseaudio.socket'], actions)
            for action in actions:
                self.assertFalse(any('@wlan' in a or 'wpa_supplicant@' in a or 's1g' in a for a in action))
                self.assertNotIn('polkit.service', action)
                self.assertNotIn('iperf3.service', action)
                if 'dbus.service' in action:
                    self.assertTrue('--user' in action or '--global' in action)

    def test_profile_one_upgrade_dry_run_repeat_and_failure_marker(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            for name, body in {'proc/device-tree/model': 'Raspberry Pi Compute Module 4\0',
                               'etc/os-release': 'VERSION_CODENAME=trixie\n',
                               'proc/swaps': 'Filename Type Size Used Priority\n'}.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
            state = root / 'state'
            state.mkdir()
            marker = state / 'complete'
            marker.write_text('1\n')
            def rooted(name):
                name = str(name)
                return root / name.lstrip('/') if name.startswith(('/proc/', '/etc/', '/var/')) else Path(name)
            def command(args, **kwargs):
                if args[0] == 'apt-get' and '--simulate' in args:
                    return 'Purg pulseaudio [1]\n'
                if 'LoadState' in args:
                    return 'not-found'
                return ''
            with patch.object(cleanup, 'Path', side_effect=rooted), \
                    patch.object(cleanup, 'STATE', state), \
                    patch.object(cleanup.platform, 'release', return_value='6.18-manet'), \
                    patch.object(cleanup, 'package_inventory', return_value={'pulseaudio': self.package(), 'dbus': self.package()}), \
                    patch.object(cleanup, 'command', side_effect=command) as commands, \
                    patch.object(cleanup, 'runtime_policy', return_value={'mask_system': {}, 'mask_user_global': {}, 'keep': {}}), \
                    patch.object(cleanup, 'live_user_managers', return_value=[]), \
                    patch.object(cleanup, 'apply_runtime') as runtime, \
                    patch.object(cleanup.subprocess, 'run') as run, patch('builtins.print'):
                cleanup.run()
                self.assertEqual(marker.read_text(), '1\n')
                runtime.assert_not_called()
                run.assert_not_called()
                runtime.side_effect = RuntimeError('mask failed')
                with self.assertRaisesRegex(RuntimeError, 'mask failed'):
                    cleanup.run(apply=True)
                self.assertEqual(marker.read_text(), '1\n')
                runtime.side_effect = None
                cleanup.run(apply=True)
                self.assertEqual(marker.read_text(), '2\n')
                self.assertTrue(list(state.glob('*-runtime.json')))
                before = commands.call_count
                cleanup.run(apply=True)
                self.assertEqual(commands.call_count, before)
                cleanup.run(apply=True, force=True)
                self.assertGreater(commands.call_count, before)

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
