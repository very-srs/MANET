"""Field trials use local hardware and boot state without a bench profile."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import types
import unittest
from unittest.mock import patch

import manet_kernel_update as kernel
import test_kernel_update as fixtures


class FieldTrialTests(unittest.TestCase):
    command = fixtures.KernelUpdateTests.command
    make_archive = fixtures.KernelUpdateTests.make_archive
    hash_archive = fixtures.KernelUpdateTests.hash_archive
    history = fixtures.KernelUpdateTests.history
    kernel_archive = fixtures.KernelUpdateTests.kernel_archive
    stage_only = fixtures.KernelUpdateTests.stage_only
    trial_boot = fixtures.KernelUpdateTests.trial_boot
    assert_old = fixtures.KernelUpdateTests.assert_old

    def setUp(self):
        fixtures.KernelUpdateTests.setUp(self)
        self.profile_patch.stop()
        self.write('sys/class/net/bat0/flags', '0x1003')
        self.write('sys/class/watchdog/watchdog0/state', 'active')
        self.write('sys/class/watchdog/watchdog0/timeout', '15')
        self.write('sys/class/watchdog/watchdog0/dev', '250:0')
        self.write('proc/1/fd/placeholder', '')
        (self.root / 'proc/1/fd/placeholder').unlink()
        (self.root / 'proc/1/fd/10').symlink_to('/dev/watchdog0')
        self.write('sys/bus/pci/devices/placeholder', '')
        (self.root / 'sys/bus/pci/devices/placeholder').unlink()
        self.write('sys/bus/usb/devices/placeholder', '')
        (self.root / 'sys/bus/usb/devices/placeholder').unlink()
        health_wait = patch.object(kernel, 'FIELD_HEALTH_WAIT', 0, create=True)
        health_wait.start()
        self.addCleanup(health_wait.stop)

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def run_fake(self, args, **kwargs):
        strings = [str(arg) for arg in args]
        if strings[0] == 'findmnt' and strings[-1] == str(self.root):
            self.calls.append(strings)
            if 'SOURCE,FSTYPE,MAJ:MIN' in strings:
                return b'/dev/mmcblk0p2 ext4 179:2\n'
            return b'ext4 rw,relatime\n'
        if strings[:2] == ['systemctl', 'is-active']:
            self.calls.append(strings)
            return b''
        if strings[:2] == ['systemctl', '--failed']:
            self.calls.append(strings)
            return b''
        if strings[0] == 'modinfo':
            self.calls.append(strings)
            return b'1\n' if 'manet_timing' in strings else b'6.18-new SMP preempt mod_unload aarch64\n'
        return fixtures.KernelUpdateTests.run_fake(self, args, **kwargs)

    def radio(self, bus, name, identity, driver, module, phys):
        path = self.root / 'sys/bus' / bus / 'devices' / name
        keys = ('vendor', 'device') if bus == 'pci' else ('idVendor', 'idProduct')
        for key, value in zip(keys, identity):
            self.write(str(path.relative_to(self.root) / key), value)
        bound = path if bus == 'pci' else path / (name + ':1.0')
        bound.mkdir(exist_ok=True)
        target = self.root / 'sys/bus' / bus / 'drivers' / driver
        target.mkdir(parents=True, exist_ok=True)
        (bound / 'driver').symlink_to(target)
        (self.root / 'sys/module' / module).mkdir(parents=True, exist_ok=True)
        for i in range(phys):
            phy = self.root / 'sys/class/ieee80211' / (name + '-phy' + str(i))
            phy.mkdir(parents=True)
            (phy / 'device').symlink_to(bound)
        return path

    def test_solo_default_trial_derives_profile_and_confirms_without_network(self):
        self.updater.update()
        info = self.updater.kernel().read()
        self.assertEqual(info['profile']['boot_uuid'], 'TEST-FAT')
        self.assertEqual(info['profile']['config_sha256'], hashlib.sha256(self.config.encode()).hexdigest())
        self.assertFalse((self.root / kernel.PROFILE).exists())
        self.trial_boot()
        (self.root / 'run/initial_time_synced').unlink()
        self.updater.update(recover_only=True)
        self.assertFalse(self.updater.pending.exists())
        self.assertFalse(any(call[0] in ('ping', 'chronyc', 'batctl') for call in self.calls))

    def test_boot_profile_uses_this_nodes_files_and_fat_uuid(self):
        config = '[all]\narm_64bit=1\ndtparam=audio=on\ngpio=17=op,dh\n'
        args = 'console=serial0,115200 root=PARTUUID=another-root rootwait rw\n'
        (self.boot / 'config.txt').write_text(config)
        (self.boot / 'cmdline.txt').write_text(args)
        def local_mount(args, **kwargs):
            if args[0] == 'findmnt' and str(args[-1]) == str(self.boot):
                return b'vfat OTHER-FAT\n'
            return self.run_fake(args, **kwargs)
        with patch.object(fixtures.update, 'run_command', side_effect=local_mount):
            self.updater.update()
        info = self.updater.kernel().read()
        self.assertEqual(info['profile']['boot_uuid'], 'OTHER-FAT')
        self.assertEqual(info['profile']['config_sha256'], hashlib.sha256(config.encode()).hexdigest())
        self.assertEqual(info['base_cmdline'], args)
        self.assertIn(config, (self.boot / 'tryboot.txt').read_text())
        self.assertIn('root=PARTUUID=another-root', (self.boot / 'manet' / info['slot'] / 'cmdline.txt').read_text())

    def test_radios_detected_by_bus_ids_even_without_interfaces(self):
        self.radio('pci', '0000:01:00.0', ('0x14c3', '0x7916'), 'mt7915e', 'mt7915e', 2)
        self.radio('usb', '1-1', ('325b', '8100'), 'morse_usb', 'morse', 1)
        self.updater.update()
        self.trial_boot()
        self.updater.update(recover_only=True)
        self.assertFalse(self.updater.pending.exists())
        self.assertTrue(any('manet_timing' in c for c in self.calls))

    def test_present_unbound_pci_fails_and_never_retries(self):
        path = self.radio('pci', '0000:01:00.0', ('0x14c3', '0x7916'), 'mt7915e', 'mt7915e', 2)
        (path / 'driver').unlink()
        self.updater.update()
        self.trial_boot()
        self.calls.clear()
        for _ in range(2):
            with self.assertRaises(kernel.KernelError):
                self.updater.update(recover_only=True)
        self.assert_old()
        self.assertEqual(self.updater.kernel().read()['phase'], 'failed')
        self.assertNotIn(['reboot', '0 tryboot'], self.calls)

    def test_missing_phy_or_timing_marker_rejects_confirmation(self):
        self.radio('pci', '0000:01:00.0', ('0x14c3', '0x7916'), 'mt7915e', 'mt7915e', 1)
        self.updater.update()
        self.trial_boot()
        with self.assertRaisesRegex(kernel.KernelError, 'PHY'):
            self.updater.update(recover_only=True)
        self.assert_old()

    def test_disappeared_usb_is_not_mistaken_for_absent_hardware(self):
        path = self.radio('usb', '1-1', ('325b', '8100'), 'morse_usb', 'morse', 1)
        self.updater.update()
        (path / 'idVendor').unlink()
        (path / 'idProduct').unlink()
        self.trial_boot()
        with self.assertRaisesRegex(kernel.KernelError, 'hardware'):
            self.updater.update(recover_only=True)
        self.assert_old()

    def test_pid1_must_hold_active_watchdog(self):
        self.updater.update()
        self.trial_boot()
        (self.root / 'proc/1/fd/10').unlink()
        with self.assertRaisesRegex(kernel.KernelError, 'watchdog'):
            self.updater.update(recover_only=True)
        self.assert_old()

    def test_bench_profile_removal_cannot_switch_pending_trial_to_field_health(self):
        # Old transactions and new bench overrides both require their local
        # pinned profile on recovery, even though new field trials do not.
        with patch.object(kernel.KernelTrial, 'profile', return_value=self.profile):
            self.updater.update()
        self.trial_boot()
        with self.assertRaisesRegex(kernel.KernelError, 'Missing bench profile'):
            self.updater.update(recover_only=True)
        self.assert_old()

    def test_unrelated_failed_service_does_not_reject_an_isolated_radio(self):
        staged = self.stage_only()
        trial = self.updater.kernel()
        trial.prepare(staged)
        self.trial_boot()
        def unrelated(args, **kwargs):
            if args[:2] == ['systemctl', '--failed']:
                return b'chrony.service loaded failed failed no clock\n'
            return self.run_fake(args, **kwargs)
        trial.run = unrelated
        self.assertTrue(trial.prepare(staged))

    def test_changed_module_is_not_confirmed(self):
        self.updater.update()
        self.trial_boot()
        self.write('usr/lib/modules/6.18-new/kernel/test.ko', 'wrong module')
        with self.assertRaisesRegex(kernel.KernelError, 'module'):
            self.updater.update(recover_only=True)
        self.assert_old()

    def test_opt_out_installs_tools_without_boot_or_module_writes(self):
        self.write('etc/manet-kernel-update.conf', 'enabled=n\n')
        self.updater.update()
        self.assertEqual(self.updater.marker.read_text(), self.new_version)
        self.assertEqual((self.boot / 'config.txt').read_text(), self.config)
        self.assertFalse((self.boot / 'tryboot.txt').exists())
        self.assertFalse((self.root / 'usr/lib/modules/6.18-new').exists())
        self.assertFalse(any(c[0] in ('reboot', 'depmod', 'findmnt') for c in self.calls))

    def test_opt_out_cannot_abandon_already_attempted_trial(self):
        self.updater.update()
        self.write('etc/manet-kernel-update.conf', 'enabled=n\n')
        self.trial_boot(good=False)
        with self.assertRaises(kernel.KernelError):
            self.updater.update(recover_only=True)
        self.assert_old()

    def test_opted_out_recovery_keeps_selection_after_setting_changes(self):
        self.write('etc/manet-kernel-update.conf', 'enabled=n\n')
        with patch.object(self.updater, 'install', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                self.updater.update()
        (self.root / 'etc/manet-kernel-update.conf').unlink()
        self.updater.update(recover_only=True)
        self.assertEqual(self.updater.marker.read_text(), self.new_version)
        self.assertFalse((self.boot / 'tryboot.txt').exists())

    def test_derived_profile_is_pinned_across_trial_mount_change(self):
        self.updater.update()
        self.trial_boot()
        original = self.run_fake
        def other_fat(args, **kwargs):
            if args[0] == 'findmnt' and str(args[-1]) == str(self.boot):
                return b'vfat OTHER-FAT\n'
            return original(args, **kwargs)
        with patch.object(fixtures.update, 'run_command', side_effect=other_fat):
            with self.assertRaisesRegex(kernel.KernelError, 'FAT'):
                self.updater.update(recover_only=True)
        self.assert_old()

    def test_invalid_local_policy_is_not_ignored_or_overwritten(self):
        self.write('etc/manet-kernel-update.conf', 'enabled=maybe\n')
        with self.assertRaisesRegex(kernel.KernelError, 'policy'):
            self.updater.update()
        self.assertFalse(self.updater.pending.exists())
        self.members['etc/manet-kernel-update.conf'] = b'enabled=y\n', 0o644
        self.make_archive()
        with self.assertRaisesRegex(kernel.KernelError, 'locally'):
            self.updater.validate(self.package, self.checksum, self.package.name, '0.550')

    def test_boot_config_support_is_unchanged_for_derived_profiles(self):
        for directive in ('include extra.txt', 'auto_initramfs=2', 'os_prefix=custom/'):
            with self.subTest(directive=directive):
                (self.boot / 'config.txt').write_text(self.config + directive + '\n')
                with self.assertRaisesRegex(kernel.KernelError, 'Unsupported'):
                    self.updater.update()
                self.assertFalse(self.updater.pending.exists())

    def test_local_health_failures_do_not_confirm(self):
        staged = self.stage_only()
        trial = self.updater.kernel()
        trial.prepare(staged)
        self.trial_boot()
        attempted = trial.state.read_bytes()
        original = self.run_fake
        cases = {
            'multi-user': (['systemctl', 'is-active', '--quiet', 'multi-user.target'], None),
            'critical': (['systemctl', '--failed', '--no-legend', '--plain', '--no-pager'], b'radio-setup-run-once.service loaded failed failed setup\n'),
            'supplicant': (['systemctl', '--failed', '--no-legend', '--plain', '--no-pager'], b'wpa_supplicant-s1g-wlan2.service loaded failed failed mesh\n'),
            'root': (['findmnt', '--noheadings', '--raw', '--output', 'FSTYPE,OPTIONS', '--mountpoint', str(self.root)], b'ext4 ro\n'),
            'timeout': (['systemctl', 'is-active', '--quiet', 'multi-user.target'], 'timeout'),
        }
        for reason, (match, output) in cases.items():
            with self.subTest(reason=reason):
                trial.state.write_bytes(attempted)
                def unhealthy(args, **kwargs):
                    if list(map(str, args)) == match:
                        if output is None:
                            raise kernel.KernelError('not active')
                        if output == 'timeout':
                            raise subprocess.TimeoutExpired(args, kwargs['timeout'])
                        return output
                    return original(args, **kwargs)
                trial.run = unhealthy
                with self.assertRaisesRegex(kernel.KernelError, 'confirmation failed'):
                    trial.prepare(staged)
                self.assert_old()
        trial.state.write_bytes(attempted)
        trial.run = original
        self.write('sys/class/net/bat0/flags', '0x1002')
        with self.assertRaisesRegex(kernel.KernelError, 'bat0'):
            trial.prepare(staged)

    def test_health_waits_for_local_startup_but_stops_at_its_deadline(self):
        staged = self.stage_only()
        trial = self.updater.kernel()
        trial.prepare(staged)
        self.trial_boot()
        attempted = trial.state.read_bytes()
        for succeeds in (True, False):
            trial.state.write_bytes(attempted)
            now = [0]
            def delayed(args, **kwargs):
                if args[-1] == 'multi-user.target' and (not succeeds or now[0] < 4):
                    raise kernel.KernelError('multi-user still starting')
                return self.run_fake(args, **kwargs)
            trial.run = delayed
            with patch.object(kernel, 'FIELD_HEALTH_WAIT', 60), \
                    patch.object(kernel.time, 'monotonic', side_effect=lambda: now[0]), \
                    patch.object(kernel.time, 'sleep', side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)):
                if succeeds:
                    self.assertTrue(trial.prepare(staged))
                    self.assertEqual(now[0], 4)
                    (self.boot / 'config.txt').write_text(self.config)
                else:
                    with self.assertRaisesRegex(kernel.KernelError, 'multi-user'):
                        trial.prepare(staged)
                    self.assertEqual(now[0], 60)
                    self.assert_old()

    def test_radio_module_identity_is_checked(self):
        self.radio('pci', '0000:01:00.0', ('0x14c3', '0x7916'), 'mt7915e', 'mt7915e', 2)
        staged = self.stage_only()
        trial = self.updater.kernel()
        trial.prepare(staged)
        self.trial_boot()
        attempted = trial.state.read_bytes()
        for field, value, error in (('vermagic', b'6.18-old SMP', 'release'),
                                    ('manet_timing', b'', 'manet_timing')):
            trial.state.write_bytes(attempted)
            def wrong_module(args, **kwargs):
                return value if args[0] == 'modinfo' and field in args else self.run_fake(args, **kwargs)
            trial.run = wrong_module
            with self.assertRaisesRegex(kernel.KernelError, error):
                trial.prepare(staged)

    def test_spi_empty_overlay_does_not_require_a_hat_but_bound_hat_is_pinned(self):
        device = self.root / 'sys/bus/spi/devices/spi0.0'
        device.mkdir(parents=True)
        self.assertEqual(self.updater.kernel().hardware(), [])
        driver = self.root / 'sys/bus/spi/drivers/morse_spi'
        driver.mkdir(parents=True)
        (device / 'driver').symlink_to(driver)
        self.updater.update()
        (device / 'driver').unlink()
        self.trial_boot()
        with self.assertRaisesRegex(kernel.KernelError, 'hardware'):
            self.updater.update(recover_only=True)

    def test_second_derived_trial_uses_saved_base_and_keeps_previous_slot(self):
        fixtures.KernelUpdateTests.test_second_kernel_update_preserves_the_first_confirmed_slot(self)

    def test_v0569_wrapper_defaults_to_field_trial_and_honors_opt_out(self):
        import test_release_bootstrap as bridge
        stage = self.work / 'outer'
        for name, (body, mode) in self.members.items():
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            target.chmod(mode)
        bridge.packaging.stage(self.package, stage)
        self.members = {str(p.relative_to(stage)): (p.read_bytes(), p.stat().st_mode & 0o777)
                        for p in stage.rglob('*') if p.is_file() and not p.is_symlink()}
        self.make_archive()
        source = subprocess.check_output(['git', 'show', 'v0.569:MANET/node_tools/node-update.py'],
                                         cwd=bridge.REPO, text=True)
        legacy = types.ModuleType('field_legacy_update')
        source_path = self.work / 'legacy/node-update.py'
        source_path.parent.mkdir()
        source_path.write_text(source)
        shutil.copyfile(bridge.REPO / 'MANET/node_tools/manet_release.py', source_path.with_name('manet_release.py'))
        legacy.__file__ = str(source_path)
        exec(compile(source, 'v0.569/node-update.py', 'exec'), legacy.__dict__)
        legacy.Updater(self.root).update()
        self.write('etc/manet-kernel-update.conf', 'enabled=n\n')
        self.assertFalse(bridge.bootstrap.apply_embedded_update(self.root, fixtures.update))
        self.assertFalse((self.boot / 'tryboot.txt').exists())
        (self.root / 'etc/manet-kernel-update.conf').unlink()
        self.assertFalse(bridge.bootstrap.apply_embedded_update(self.root, fixtures.update))
        self.trial_boot()
        self.assertTrue(bridge.bootstrap.apply_embedded_update(self.root, fixtures.update, lambda *a, **k: None))
        self.assertFalse(self.updater.pending.exists())


if __name__ == '__main__':
    unittest.main()
