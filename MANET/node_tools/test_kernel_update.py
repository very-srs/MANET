#!/usr/bin/env python3
"""Kernel trial transactions in a fake root, with firmware/reset boundaries mocked."""
import hashlib
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import manet_kernel_update as kernel
import test_node_update as fixtures

update = fixtures.update


class KernelUpdateTests(unittest.TestCase):
    command = fixtures.UpdateTests.command
    make_archive = fixtures.UpdateTests.make_archive
    hash_archive = fixtures.UpdateTests.hash_archive
    history = fixtures.UpdateTests.history

    def setUp(self):
        fixtures.UpdateTests.setUp(self)
        self.boot = self.root / 'boot/firmware'
        self.boot.mkdir(parents=True)
        self.config = '[all]\narm_64bit=1\ndtoverlay=mm610x-spi\n'
        self.args = 'root=PARTUUID=old-root rootwait rw\n'
        (self.boot / 'config.txt').write_text(self.config)
        (self.boot / 'cmdline.txt').write_text(self.args)
        (self.boot / 'kernel8.img').write_bytes(b'old kernel')
        (self.root / 'proc/sys/kernel/osrelease').write_text('6.18-old')
        (self.root / 'proc/cmdline').write_text(self.args)
        (self.root / 'proc/uptime').write_text('60 50\n')
        flags = self.root / 'proc/device-tree/chosen/bootloader'
        flags.mkdir(parents=True)
        self.flag = flags / 'tryboot'
        self.flag.write_bytes(b'\0\0\0\0')
        self.profile = dict(backend='cm4-tryboot-v1', boot_uuid='TEST-FAT',
                            watchdog_validated=True, validation_id='fake-reset-test',
                            config_sha256=hashlib.sha256(self.config.encode()).hexdigest(),
                            cmdline_sha256=hashlib.sha256(self.args.encode()).hexdigest(),
                            health_sha256=hashlib.sha256(b'health').hexdigest())
        # A local root-owned qualification file is a board commissioning input.
        # Fake-root tests exercise all subsequent gates without root privileges.
        self.profile_patch = patch.object(kernel.KernelTrial, 'profile', return_value=self.profile)
        self.profile_patch.start()
        self.addCleanup(self.profile_patch.stop)
        self.real_run = update.run_command
        self.run_patch = patch.object(update, 'run_command', side_effect=self.run_fake)
        self.run_patch.start()
        self.addCleanup(self.run_patch.stop)
        self.calls = []
        self.files = {
            'boot/kernel8.img': b'new kernel',
            'boot/bcm2711-rpi-cm4.dtb': b'dtb',
            'boot/overlays/mm610x-spi.dtbo': b'overlay',
            'boot/overlays/README': b'private overlay directory\n',
            'modules/6.18-new/modules.order': b'kernel/test.ko\n',
            'modules/6.18-new/modules.builtin': b'',
            'modules/6.18-new/kernel/test.ko': b'new module',
            'firmware/morse/test.bin': b'new firmware',
        }
        old_modules = self.root / 'usr/lib/modules/6.18-old'
        old_modules.mkdir(parents=True)
        (old_modules / 'old.ko').write_bytes(b'old module')
        self.kernel_archive()

    def run_fake(self, args, **kwargs):
        args = [str(arg) for arg in args]
        self.calls.append(args)
        if args[0] == 'findmnt':
            return b'vfat TEST-FAT\n'
        if args[0] == 'vcgencmd':
            return b'BOOT_WATCHDOG_TIMEOUT=180\n'
        if args[:3] == ['systemctl', 'show', '--property=RuntimeWatchdogUSec']:
            return b'15s\n'
        if args[0] == 'depmod':
            (self.root / 'usr/lib/modules' / args[-1] / 'modules.dep').write_text('kernel/test.ko:\n')
            return b''
        if args[0] == 'reboot' or args == ['systemctl', 'reboot']:
            return b''
        if args[0] == str(self.root / kernel.HEALTH):
            return b''
        return self.real_run(args, **kwargs)

    def kernel_archive(self, release='6.18-new'):
        for name in list(self.members):
            if name.startswith(kernel.PREFIX):
                del self.members[name]
        manifest = dict(schema=1, board='cm4', release=release, files={
            name: dict(size=len(body), sha256=hashlib.sha256(body).hexdigest())
            for name, body in self.files.items()})
        for name, body in self.files.items():
            self.members[kernel.PREFIX + name] = body, 0o644
        self.members[kernel.MANIFEST] = json.dumps(manifest).encode(), 0o644
        self.make_archive()

    def trial_boot(self, good=True):
        info = self.updater.kernel().read()
        (self.root / 'proc/sys/kernel/random/boot_id').write_text('trial-boot')
        if good:
            (self.root / 'proc/sys/kernel/osrelease').write_text(info['release'])
            (self.root / 'proc/cmdline').write_text(
                self.args.strip() + ' manet.kernel_trial=' + info['slot'])
            self.flag.write_bytes(b'\0\0\0\1')

    def stage_only(self):
        self.updater.state.mkdir(parents=True, exist_ok=True)
        self.updater.recovery.mkdir(exist_ok=True)
        self.updater.pending.touch()
        staged = self.work / 'staged'
        staged.mkdir(exist_ok=True)
        for name, (body, _) in self.members.items():
            path = staged / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        return staged

    def assert_old(self):
        self.assertEqual((self.boot / 'config.txt').read_text(), self.config)
        self.assertEqual((self.boot / 'kernel8.img').read_bytes(), b'old kernel')
        self.assertEqual((self.root / 'usr/lib/modules/6.18-old/old.ko').read_bytes(), b'old module')
        self.assertEqual(self.updater.marker.read_text(), self.old_version)

    def test_successful_trial_precedes_runtime_and_versions(self):
        self.updater.update()
        self.assert_old()
        self.assertTrue(self.updater.pending.exists())
        self.assertNotIn('systemctl stop node-manager', self.history())
        self.assertEqual(self.calls[-1], ['reboot', '0 tryboot'])
        self.assertIn(['depmod', '-b', str(self.root), '6.18-new'], self.calls)
        self.assertFalse((self.root / 'manet-kernel').exists())
        trial_config = (self.boot / 'tryboot.txt').read_text()
        self.assertLess(trial_config.index('os_prefix='), trial_config.index('dtoverlay='))
        self.assertTrue(trial_config.endswith('kernel_watchdog_timeout=180\n'))
        self.assertEqual(self.updater.kernel().read()['watchdogs']['eeprom_boot_watchdog'], 'enabled')
        slot = self.updater.kernel().read()['slot']
        trial_args = (self.boot / 'manet' / slot / 'cmdline.txt').read_text().split()
        self.assertIn('watchdog.open_timeout=120', trial_args)
        self.trial_boot()
        self.updater.update(recover_only=True)
        self.assertEqual((self.boot / 'config.txt').read_text(), trial_config)
        self.assertFalse((self.boot / 'tryboot.txt').exists())
        self.assertFalse(self.updater.pending.exists())
        self.assertEqual(self.updater.marker.read_text(), self.new_version)
        self.assertTrue((self.root / 'var/lib/manet-kernel/confirmed.json').exists())

    def test_early_recovery_never_mounts_boot_or_replaces_runtime(self):
        self.updater.update()
        self.calls.clear()
        self.trial_boot()
        self.updater.update(files_only=True)
        self.assert_old()
        self.assertEqual(self.old_manager.read_text(), 'old manager\n')
        self.assertFalse(self.calls)

    def test_failed_trial_boot_does_not_retry(self):
        self.updater.update()
        self.trial_boot(good=False)
        self.calls.clear()
        for _ in range(2):
            with self.assertRaises(kernel.KernelError):
                self.updater.update(recover_only=True)
        self.assert_old()
        self.assertEqual(self.updater.kernel().read()['phase'], 'failed')
        self.assertFalse(any(call[0] == 'reboot' for call in self.calls))
        self.updater.update(cancel_kernel=True)
        self.assertFalse(self.updater.pending.exists())
        self.assertFalse((self.boot / 'tryboot.txt').exists())
        self.assert_old()

    def test_failed_health_reboots_to_old_selector(self):
        self.updater.update()
        self.trial_boot()
        original = self.run_fake
        def failed(args, **kwargs):
            if str(args[0]).endswith('manet-kernel-health-check'):
                raise update.UpdateError('No mesh management path')
            return original(args, **kwargs)
        with patch.object(update, 'run_command', side_effect=failed):
            with self.assertRaisesRegex(kernel.KernelError, 'confirmation failed'):
                self.updater.update(recover_only=True)
        self.assert_old()
        self.assertIn(['systemctl', 'reboot'], self.calls)
        self.assertEqual(self.updater.kernel().read()['phase'], 'failed')

    def test_late_confirmation_is_rejected(self):
        self.updater.update()
        self.trial_boot()
        (self.root / 'proc/uptime').write_text('250 0')
        with self.assertRaisesRegex(kernel.KernelError, 'deadline'):
            self.updater.update(recover_only=True)
        self.assert_old()
        self.assertIn(['systemctl', 'reboot'], self.calls)

    def test_guard_resets_without_waiting_for_update_lock(self):
        self.updater.update()
        self.trial_boot()
        (self.root / 'proc/uptime').write_text('301 0')
        with self.assertRaises(kernel.KernelError):
            self.updater.kernel().guard()
        self.assertIn(['systemctl', 'reboot'], self.calls)
        self.assert_old()

    def test_same_boot_does_not_repeat_reboot_request(self):
        self.updater.update()
        self.calls.clear()
        with self.assertRaisesRegex(kernel.KernelError, 'already requested'):
            self.updater.update(recover_only=True)
        self.assertFalse(any(call[0] == 'reboot' for call in self.calls))
        self.assert_old()

    def test_missing_overlay_and_same_release_fail_before_arming(self):
        del self.files['boot/overlays/mm610x-spi.dtbo']
        self.kernel_archive()
        with self.assertRaisesRegex(kernel.KernelError, 'lacks configured overlay'):
            self.updater.update()
        self.assertFalse(self.updater.pending.exists())
        self.assertNotIn('dependency', self.history())
        self.files['boot/overlays/mm610x-spi.dtbo'] = b'overlay'
        self.kernel_archive()
        (self.root / 'proc/sys/kernel/osrelease').write_text('6.18-new')
        with self.assertRaisesRegex(kernel.KernelError, 'unique uname'):
            self.updater.update()
        self.assertFalse(self.updater.pending.exists())

    def test_payload_hash_and_inventory_mismatch_rejected(self):
        self.members[kernel.PREFIX + 'boot/kernel8.img'] = b'corrupt', 0o644
        self.make_archive()
        with self.assertRaisesRegex(kernel.KernelError, 'hash mismatch'):
            self.updater.update()
        self.assertNotIn('dependency', self.history())
        self.assertFalse(self.updater.pending.exists())

    def test_missing_boot_mount_or_watchdog_fails_before_arming(self):
        original = self.run_fake
        for command, output in (('findmnt', b'ext4 TEST-FAT'), ('systemctl', b'0'),
                                ('systemctl', b'infinity'), ('systemctl', b'0us'),
                                ('systemctl', b''), ('systemctl', b'unknown')):
            def wrong(args, **kwargs):
                return output if args[0] == command else original(args, **kwargs)
            with self.subTest(command=command), patch.object(update, 'run_command', side_effect=wrong):
                with self.assertRaises(kernel.KernelError):
                    self.updater.update()
                self.assertFalse(self.updater.pending.exists())
                self.assertNotIn('dependency', self.history())

    def test_old_eeprom_allows_trial_and_records_missing_boot_watchdog(self):
        staged = self.stage_only()
        for output in (b'BOOT_WATCHDOG_TIMEOUT=0\n', b'BOOT_ORDER=0xf41\n'):
            with self.subTest(output=output):
                def old_eeprom(args, **kwargs):
                    return output if args[0] == 'vcgencmd' else self.run_fake(args, **kwargs)
                trial = kernel.KernelTrial(self.updater, old_eeprom, update.atomic_file, update.sync_directory)
                trial.preflight(staged)
                self.assertEqual(trial.watchdogs()['eeprom_boot_watchdog'], 'disabled-or-absent')
        with patch.object(update, 'run_command', side_effect=old_eeprom):
            self.updater.kernel().prepare(staged)
        self.assertEqual(self.updater.kernel().read()['watchdogs']['eeprom_boot_watchdog'], 'disabled-or-absent')
        self.assertEqual(self.calls[-1], ['reboot', '0 tryboot'])

    def test_eeprom_query_failure_is_diagnostic(self):
        def unavailable(args, **kwargs):
            if args[0] == 'vcgencmd':
                raise update.UpdateError('no mailbox')
            return self.run_fake(args, **kwargs)
        trial = kernel.KernelTrial(self.updater, unavailable, update.atomic_file, update.sync_directory)
        trial.prepare(self.stage_only())
        self.assertEqual(trial.read()['watchdogs']['eeprom_boot_watchdog'], 'unknown')
        self.assertIn('no mailbox', trial.read()['watchdogs']['eeprom_query_error'])

    def test_resumed_staging_rechecks_runtime_watchdog(self):
        staged = self.stage_only()
        def failed_depmod(args, **kwargs):
            if args[0] == 'depmod':
                raise update.UpdateError('interrupted')
            return self.run_fake(args, **kwargs)
        trial = kernel.KernelTrial(self.updater, failed_depmod, update.atomic_file, update.sync_directory)
        with self.assertRaises(update.UpdateError):
            trial.prepare(staged)
        def disabled(args, **kwargs):
            if args[:3] == ['systemctl', 'show', '--property=RuntimeWatchdogUSec']:
                return b'0'
            return self.run_fake(args, **kwargs)
        trial.run = disabled
        with self.assertRaisesRegex(kernel.KernelError, 'runtime watchdog'):
            trial.prepare(staged)
        self.assertFalse((self.boot / 'tryboot.txt').exists())
        self.assert_old()

    def test_rock3a_activation_is_rejected(self):
        staged = self.stage_only()
        (self.root / 'proc/device-tree/model').write_text('Radxa ROCK 3A')
        with self.assertRaisesRegex(kernel.KernelError, 'Rock 3A'):
            self.updater.kernel().prepare(staged)
        self.assert_old()
        self.assertFalse(self.calls)

    def test_every_candidate_write_can_resume_without_changing_old_boot(self):
        staged = self.stage_only()
        # Journal, all payload files, command line, trial selector, attempted
        # journal. Crash after each durable write, including the request boundary.
        steps = len(self.files) + 4
        for fail_at in range(1, steps + 1):
            with self.subTest(fail_at=fail_at):
                shutil.rmtree(self.root / 'usr/lib/modules/6.18-new', ignore_errors=True)
                shutil.rmtree(self.root / 'var/lib/manet-kernel', ignore_errors=True)
                shutil.rmtree(self.boot / 'manet', ignore_errors=True)
                (self.boot / 'tryboot.txt').unlink(missing_ok=True)
                self.updater.kernel().state.unlink(missing_ok=True)
                count = 0
                def crash(target, **kwargs):
                    nonlocal count
                    update.atomic_file(target, **kwargs)
                    count += 1
                    if count == fail_at:
                        raise OSError('simulated power interruption')
                trial = kernel.KernelTrial(self.updater, self.run_fake, crash, update.sync_directory)
                with self.assertRaises(OSError):
                    trial.prepare(staged)
                self.assert_old()
                info = self.updater.kernel().read()
                if info['phase'] == 'attempted':
                    self.trial_boot(good=False)
                    with self.assertRaises(kernel.KernelError):
                        self.updater.kernel().prepare(staged)
                    (self.root / 'proc/sys/kernel/random/boot_id').write_text('boot-test')
                else:
                    self.assertFalse(self.updater.kernel().prepare(staged))
                self.assert_old()

    def test_depmod_failure_never_arms_tryboot(self):
        staged = self.stage_only()
        def failed(args, **kwargs):
            if args[0] == 'depmod':
                raise update.UpdateError('depmod failure')
            return self.run_fake(args, **kwargs)
        trial = kernel.KernelTrial(self.updater, failed, update.atomic_file, update.sync_directory)
        with self.assertRaises(update.UpdateError):
            trial.prepare(staged)
        self.assertFalse((self.boot / 'tryboot.txt').exists())
        self.assert_old()
        self.assertFalse(self.updater.kernel().prepare(staged))

    def test_commit_intent_resumes_after_every_commit_write(self):
        staged = self.stage_only()
        self.updater.kernel().prepare(staged)
        self.trial_boot()
        attempted = self.updater.kernel().state.read_bytes()
        for fail_at in range(1, 5):
            with self.subTest(fail_at=fail_at):
                self.updater.kernel().state.write_bytes(attempted)
                (self.boot / 'config.txt').write_text(self.config)
                count = 0
                def crash(target, **kwargs):
                    nonlocal count
                    update.atomic_file(target, **kwargs)
                    count += 1
                    if count == fail_at:
                        raise OSError('simulated commit interruption')
                trial = kernel.KernelTrial(self.updater, self.run_fake, crash, update.sync_directory)
                with self.assertRaises(OSError):
                    trial.prepare(staged)
                self.assertTrue(self.updater.kernel().prepare(staged))
                self.assertEqual(self.updater.kernel().read()['phase'], 'committed')

    def test_wrong_kernel_identity_requests_normal_reboot(self):
        self.updater.update()
        self.trial_boot()
        (self.root / 'proc/sys/kernel/osrelease').write_text('unexpected-image')
        with self.assertRaisesRegex(kernel.KernelError, 'did not confirm'):
            self.updater.update(recover_only=True)
        self.assertIn(['systemctl', 'reboot'], self.calls)
        self.assert_old()

    def test_existing_candidate_module_tree_is_never_overwritten(self):
        path = self.root / 'usr/lib/modules/6.18-new/other.ko'
        path.parent.mkdir()
        path.write_bytes(b'unowned module')
        with self.assertRaisesRegex(kernel.KernelError, 'already exists'):
            self.updater.update()
        self.assertEqual(path.read_bytes(), b'unowned module')
        self.assertFalse(self.updater.pending.exists())

    def test_kernel_less_updates_still_work_on_all_boards(self):
        for name in list(self.members):
            if name.startswith(kernel.PREFIX):
                del self.members[name]
        self.make_archive()
        for board, model in (('cm4', 'Raspberry Pi Compute Module 4'),
                             ('r3a', 'Radxa ROCK 3A'), ('rpi5', 'Raspberry Pi 5')):
            with self.subTest(board=board):
                for name in update.MARKERS:
                    (self.root / name).write_text(self.old_version)
                (self.root / 'proc/device-tree/model').write_text(model)
                filename = board + '-tools.tar.gz'
                if board != 'cm4':
                    shutil.copyfile(self.package, self.server / filename)
                    (self.server / (filename + '.sha256')).write_text(
                        hashlib.sha256(self.package.read_bytes()).hexdigest() + '  ' + filename + '\n')
                self.calls.clear()
                self.updater.update()
                self.assertEqual(self.updater.marker.read_text(), self.new_version)
                self.assertFalse(self.updater.pending.exists())
                self.assertFalse(any(call[0] in ('reboot', 'depmod', 'findmnt') for call in self.calls))

    def test_candidate_symlink_parent_and_low_boot_space_stop_before_switch(self):
        staged = self.stage_only()
        (self.boot / 'manet').symlink_to(self.server, target_is_directory=True)
        with self.assertRaises(update.UpdateError):
            self.updater.kernel().prepare(staged)
        (self.boot / 'manet').unlink()
        with patch.object(self.updater, 'space', side_effect=update.UpdateError('Insufficient disk space')):
            with self.assertRaises(update.UpdateError):
                self.updater.kernel().prepare(staged)
        self.assertFalse((self.boot / 'tryboot.txt').exists())
        self.assert_old()

    def test_unsupported_boot_settings_are_not_silently_removed(self):
        trial = self.updater.kernel()
        for directive in ('include usercfg.txt', 'auto_initramfs=1',
                          'kernel=/other.img', 'os_prefix=other/', 'initramfs initrd followkernel'):
            with self.subTest(directive=directive), self.assertRaises(kernel.KernelError):
                trial.config(self.config + directive + '\n', 'slot', self.files)

    def test_local_archive_is_offline_and_can_reinstall_same_version(self):
        (self.root / 'run/initial_time_synced').unlink()
        for name in update.MARKERS:
            (self.root / name).write_text(self.new_version)
        self.updater.update(local_archive=self.package, local_version='0.550')
        self.assertNotIn('curl', self.history())
        self.assertEqual(self.updater.kernel().read()['phase'], 'attempted')
        self.assertIn(['reboot', '0 tryboot'], self.calls)

    def test_local_archive_requires_exact_sidecar_and_version(self):
        with self.assertRaisesRegex(update.UpdateError, 'Archive version'):
            self.updater.update(local_archive=self.package, local_version='0.551')
        self.assertFalse(self.updater.pending.exists())
        self.checksum.write_text('bad checksum')
        with self.assertRaisesRegex(update.UpdateError, 'checksum'):
            self.updater.update(local_archive=self.package, local_version='0.550')
        self.assertFalse(self.updater.pending.exists())

    def test_second_kernel_update_preserves_the_first_confirmed_slot(self):
        self.updater.update()
        self.trial_boot()
        self.updater.update(recover_only=True)
        first_config = (self.boot / 'config.txt').read_text()
        first_module = (self.root / 'usr/lib/modules/6.18-new/kernel/test.ko').read_bytes()
        self.files = {name.replace('6.18-new', '6.18-next'): body
                      for name, body in self.files.items()}
        self.files['boot/kernel8.img'] = b'next kernel'
        self.kernel_archive('6.18-next')
        self.updater.update(local_archive=self.package, local_version='0.550')
        self.assertEqual((self.boot / 'config.txt').read_text(), first_config)
        self.assertEqual((self.root / 'usr/lib/modules/6.18-new/kernel/test.ko').read_bytes(), first_module)
        self.trial_boot()
        (self.root / 'proc/sys/kernel/random/boot_id').write_text('next-trial-boot')
        self.updater.update(recover_only=True)
        self.assertNotEqual((self.boot / 'config.txt').read_text(), first_config)
        self.assertFalse(self.updater.pending.exists())

    def test_unqualified_profile_is_rejected(self):
        self.profile_patch.stop()
        with self.assertRaisesRegex(kernel.KernelError, 'locally validated'):
            self.updater.update()
        self.assertFalse(self.updater.pending.exists())


if __name__ == '__main__':
    unittest.main()
