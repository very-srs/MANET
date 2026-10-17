"""Existing CM4 auto-initramfs installations retain their fallback boot set."""
import json
import os
import shutil
import subprocess
import types
import unittest
from unittest.mock import Mock, patch

import manet_kernel_update as kernel
import test_kernel_field as field
import test_kernel_update as fixtures

import test_release_bootstrap as bridge

update = fixtures.update


class InitramfsTrialTests(unittest.TestCase):
    command = field.FieldTrialTests.command
    make_archive = field.FieldTrialTests.make_archive
    hash_archive = field.FieldTrialTests.hash_archive
    history = field.FieldTrialTests.history
    kernel_archive = field.FieldTrialTests.kernel_archive
    stage_only = field.FieldTrialTests.stage_only
    trial_boot = field.FieldTrialTests.trial_boot
    assert_old = field.FieldTrialTests.assert_old
    write = field.FieldTrialTests.write
    run_fake = field.FieldTrialTests.run_fake

    def setUp(self):
        field.FieldTrialTests.setUp(self)
        self.config += 'auto_initramfs=1\n'
        (self.boot / 'config.txt').write_text(self.config)
        (self.boot / 'initramfs8').write_bytes(b'old initramfs')

    def test_trial_then_health_commit_changes_both_settings_together(self):
        self.updater.update()
        self.assert_old()
        info = self.updater.kernel().read()
        self.assertEqual(info['initramfs']['previous_auto_initramfs'],
                         [{'section': '[all]', 'value': '1'}])
        trial_config = (self.boot / 'tryboot.txt').read_text()
        self.assertIn('os_prefix=manet/' + info['slot'] + '/', trial_config)
        self.assertEqual([s for s in trial_config.splitlines() if s.startswith('auto_initramfs=')],
                         ['auto_initramfs=0'])
        self.trial_boot()
        self.updater.update(recover_only=True)
        self.assertEqual((self.boot / 'config.txt').read_text(), trial_config)
        self.assertFalse(self.updater.pending.exists())
        self.assertEqual((self.boot / 'initramfs8').read_bytes(), b'old initramfs')

    def test_existing_sections_are_supported_without_changing_the_fallback(self):
        for section in ('[all]', '[cm4]', '[pi4]', '[none]', '[tryboot]', '[HDMI:0]'):
            with self.subTest(section=section):
                base = self.config + section + '\nauto_initramfs=1 # old image\n'
                candidate = self.updater.kernel().config(base, 'slot', self.files)
                self.assertNotIn('auto_initramfs=1', candidate)
                self.assertIn('[all]\nkernel=kernel8.img', candidate)
                self.assertIn('auto_initramfs=0', candidate)
                self.assert_old()

    def test_firmware_fallback_keeps_old_kernel_and_initramfs(self):
        self.updater.update()
        self.trial_boot(good=False)
        self.calls.clear()
        for _ in range(2):
            with self.assertRaisesRegex(kernel.KernelError, 'fallback kernel retained'):
                self.updater.update(recover_only=True)
        self.assert_old()
        self.assertEqual((self.boot / 'initramfs8').read_bytes(), b'old initramfs')
        self.assertNotIn(['reboot', '0 tryboot'], self.calls)

    def test_failed_health_keeps_original_auto_initramfs(self):
        self.updater.update()
        self.trial_boot()
        self.write('sys/class/net/bat0/flags', '0x1002')
        with self.assertRaisesRegex(kernel.KernelError, 'bat0'):
            self.updater.update(recover_only=True)
        self.assert_old()
        self.assertIn(['systemctl', 'reboot'], self.calls)

    def test_commit_interruption_recovers_auto_initramfs_with_slot(self):
        fixtures.KernelUpdateTests.test_commit_intent_resumes_after_every_commit_write(self)
        self.assertNotIn('auto_initramfs=1', (self.boot / 'config.txt').read_text())

    def test_staging_interruption_preserves_original_auto_initramfs(self):
        fixtures.KernelUpdateTests.test_every_candidate_write_can_resume_without_changing_old_boot(self)

    def test_second_trial_records_disabled_initramfs_as_its_fallback(self):
        fixtures.KernelUpdateTests.test_second_kernel_update_preserves_the_first_confirmed_slot(self)
        record = json.loads((self.root / 'var/lib/manet-kernel/confirmed.json').read_text())
        self.assertEqual(record['initramfs']['previous_auto_initramfs'],
                         [{'section': '[all]', 'value': '0'}])

    def test_resumed_staging_rechecks_root_before_requesting_reboot(self):
        staged = self.stage_only()
        trial = self.updater.kernel()
        with patch.object(trial, 'persist_modules', side_effect=OSError('interrupted staging')):
            with self.assertRaises(OSError):
                trial.prepare(staged)
        self.write('proc/cmdline', 'root=/dev/mapper/cryptroot\n')
        with self.assertRaisesRegex(kernel.KernelError, 'encrypted or mapped'):
            trial.prepare(staged)
        self.assertFalse((self.boot / 'tryboot.txt').exists())
        self.assertNotIn(['reboot', '0 tryboot'], self.calls)
        self.assert_old()

    def test_refusal_reaches_updater_status(self):
        self.write('proc/cmdline', 'root=UUID=example\n')
        with patch.object(update, 'Updater', return_value=self.updater), \
                patch.object(update.sys, 'argv', ['node-update.py']), \
                patch.object(update.os, 'geteuid', return_value=0), \
                patch.object(update.signal, 'signal'):
            self.assertEqual(update.main(), 1)
        self.assertIn('root selector the kernel cannot mount directly',
                      self.updater.log.call_args.args[0])
        self.assertTrue(self.updater.log.call_args.kwargs['error'])

    def test_root_requirements_refuse_before_arming_with_plain_reason(self):
        cases = (
            ('root=UUID=abc', 'root selector'),
            ('root=LABEL=rootfs', 'root selector'),
            ('root=/dev/disk/by-uuid/abc', 'root selector'),
            ('root=/dev/mapper/cryptroot', 'encrypted or mapped'),
            ('root=/dev/dm-0', 'encrypted or mapped'),
            ('root=/dev/nfs nfsroot=server:/root', 'network root'),
            ('root=PARTUUID=old-root rd.luks.uuid=abc', 'encrypted'),
            ('root=PARTUUID=old-root rd.lvm.lv=vg/root', 'LVM'),
            ('root=/dev/md0', 'RAID'),
            ('root=PARTUUID=old-root rootfstype=overlay', 'root filesystem'),
        )
        for args, reason in cases:
            for name in ('proc/cmdline', 'boot/firmware/cmdline.txt'):
                with self.subTest(args=args, name=name):
                    self.write('proc/cmdline', self.args)
                    self.write('boot/firmware/cmdline.txt', self.args)
                    self.write(name, args + '\n')
                    with self.assertRaisesRegex(kernel.KernelError, reason):
                        self.updater.update()
                    self.assertFalse(self.updater.pending.exists())
                    self.assertFalse((self.boot / 'tryboot.txt').exists())
                    self.assert_old()

    def test_explicit_initramfs_directives_explain_refusal(self):
        for directive in ('initramfs initrd followkernel', 'initramfs\tinitrd 0x0',
                          'ramfsfile=initramfs8', 'ramfsaddr=-1'):
            with self.subTest(directive=directive):
                (self.boot / 'config.txt').write_text(self.config + directive + '\n')
                with self.assertRaisesRegex(kernel.KernelError, 'custom initramfs'):
                    self.updater.update()
                self.assertFalse(self.updater.pending.exists())

    def test_modular_root_filesystem_or_storage_refuses(self):
        self.write('proc/modules', 'ext4 123 1 - Live 0x0\n')
        with self.assertRaisesRegex(kernel.KernelError, 'root filesystem ext4.*module'):
            self.updater.update()
        self.write('proc/modules', 'morse 123 1 - Live 0x0\n')
        device = self.root / 'sys/devices/platform/mmc/block/mmcblk0/mmcblk0p2'
        device.mkdir(parents=True)
        block = self.root / 'sys/dev/block'
        block.mkdir(parents=True)
        (block / '179:2').symlink_to(device)
        driver = self.root / 'sys/devices/platform/mmc/driver'
        driver.mkdir()
        (driver / 'module').symlink_to(self.root / 'sys/module/mmc_block')
        with self.assertRaisesRegex(kernel.KernelError, 'root storage.*mmc_block.*module'):
            self.updater.update()
        self.assertFalse(self.updater.pending.exists())

    def test_direct_device_and_unrelated_modules_can_trial(self):
        self.args = 'root=/dev/mmcblk0p2 rootfstype=ext4 rootwait rw\n'
        self.write('proc/cmdline', self.args)
        self.write('boot/firmware/cmdline.txt', self.args)
        self.write('proc/modules', 'morse 123 1 - Live 0x0\n')
        self.updater.update()
        self.assertEqual(self.updater.kernel().read()['phase'], 'attempted')

    def test_other_kernel_block_identifiers_need_no_userspace_lookup(self):
        for root in ('PARTLABEL=rootfs', '179:2', 'b302', '/dev/nvme0n1p2',
                     'PARTUUID=1234abcd-01/PARTNROFF=1'):
            with self.subTest(root=root):
                self.write('proc/cmdline', 'root=' + root)
                evidence = self.updater.kernel().initramfs_preflight(self.config, 'root=' + root)
                self.assertEqual(evidence['running_root'], root)

    def test_v0574_refused_bootstrap_recovers_through_next_wrapper(self):
        # Load the released updater and kernel helper, not a simulation of
        # their blanket auto_initramfs refusal.
        legacy = types.ModuleType('initramfs_legacy_update')
        old_kernel = types.ModuleType('initramfs_legacy_kernel')
        old_dir = self.work / 'legacy'
        old_dir.mkdir()
        for module, name in ((old_kernel, 'manet_kernel_update.py'), (legacy, 'node-update.py')):
            source = subprocess.check_output(['git', 'show', 'e44a097:MANET/node_tools/' + name],
                                             cwd=bridge.REPO, text=True)
            module.__file__ = str(old_dir / name)
            (old_dir / name).write_text(source)
            with patch.dict('sys.modules', {'manet_kernel_update': old_kernel}):
                exec(compile(source, module.__file__, 'exec'), module.__dict__)
        shutil.copyfile(bridge.REPO / 'MANET/node_tools/manet_release.py', old_dir / 'manet_release.py')
        for name in update.MARKERS:
            (self.root / name).write_text('0.574\n10/2026\n')
            self.members[name] = b'0.574\n10/2026\n', 0o644
        self.kernel_archive()
        outer = self.work / 'outer-old'
        for name, (body, mode) in self.members.items():
            path = outer / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            path.chmod(mode)
        bridge.packaging.stage(self.package, outer)
        base = self.root / 'usr/local/share/manet/bootstrap'
        shutil.copytree(outer / 'usr/local/share/manet/bootstrap', base)
        with patch.object(legacy, 'run_command', side_effect=self.run_fake):
            with self.assertRaisesRegex(old_kernel.KernelError, 'auto_initramfs=1'):
                bridge.bootstrap.apply_embedded_update(self.root, legacy, Mock())
            self.assertFalse(self.updater.pending.exists())
            for name in update.MARKERS:
                self.members[name] = b'0.575\n10/2026\n', 0o644
            for name in ('node-update.py', 'manet_kernel_update.py', 'manet_release.py'):
                self.members['usr/local/bin/' + name] = (
                    (bridge.REPO / 'MANET/node_tools' / name).read_bytes(), 0o755)
            self.kernel_archive()
            outer_new = self.work / 'outer-new'
            for name, (body, mode) in self.members.items():
                path = outer_new / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
                path.chmod(mode)
            bridge.packaging.stage(self.package, outer_new)
            self.members = {str(p.relative_to(outer_new)): (p.read_bytes(), p.stat().st_mode & 0o777)
                            for p in outer_new.rglob('*') if p.is_file() and not p.is_symlink()}
            self.make_archive()
            legacy.Updater(self.root).update(local_archive=self.package, local_version='0.575')
        self.assertEqual(self.updater.marker.read_text().splitlines()[0], '0.575')
        for name in ('node-update.py', 'manet_kernel_update.py', 'manet_release.py'):
            self.assertEqual((self.root / 'usr/local/bin' / name).read_bytes(),
                             (bridge.REPO / 'MANET/node_tools' / name).read_bytes())
        self.assertFalse(bridge.bootstrap.apply_embedded_update(self.root, update, Mock()))
        with self.assertRaisesRegex(kernel.KernelError, 'already requested'):
            bridge.bootstrap.apply_embedded_update(self.root, update, Mock())
        self.assertEqual(self.calls.count(['reboot', '0 tryboot']), 1)
        self.trial_boot()
        self.assertTrue(bridge.bootstrap.apply_embedded_update(self.root, update, Mock()))
        self.assertFalse((base / 'manifest.json').exists())
        self.assertFalse(bridge.bootstrap.apply_embedded_update(self.root, update, Mock()))
        self.assertEqual(self.calls.count(['reboot', '0 tryboot']), 1)

    def test_direct_update_retires_stale_bootstrap_before_firmware_setup(self):
        base = self.root / 'usr/local/share/manet/bootstrap'
        self.write('usr/local/share/manet/bootstrap/manifest.json', 'stale or damaged')
        self.write('usr/local/share/manet/bootstrap/cm4-tools.tar.gz', 'old payload')
        self.write('etc/systemd/system/manet-release-bootstrap.timer', 'old timer')
        self.updater.update()
        self.trial_boot()
        self.updater.update(recover_only=True)
        self.assertFalse((base / 'manifest.json').exists())
        self.assertFalse((base / 'cm4-tools.tar.gz').exists())
        self.assertFalse((self.root / 'etc/systemd/system/manet-release-bootstrap.timer').exists())
        self.assertIn(['systemctl', 'disable', '--now', 'manet-release-bootstrap.timer'], self.calls)
        self.assertFalse(bridge.bootstrap.apply_embedded_update(self.root, update, Mock()))

    def test_stale_payload_without_timer_cannot_block_runtime_update(self):
        self.write('etc/manet-kernel-update.conf', 'enabled=n\n')
        manifest = self.write('usr/local/share/manet/bootstrap/manifest.json', 'stale')
        archive = self.write('usr/local/share/manet/bootstrap/cm4-tools.tar.gz', 'stale')
        with patch.dict(os.environ, {'TEST_SYSTEMCTL_FAIL':
                                     'disable --now manet-release-bootstrap.timer'}):
            self.updater.update()
        self.assertFalse(manifest.exists())
        self.assertFalse(archive.exists())
        self.assertFalse(self.updater.pending.exists())
        self.assertEqual(self.updater.marker.read_text(), self.new_version)


if __name__ == '__main__':
    unittest.main()
