"""Simulated removable, mounted and replaced disks; no writes to real disks."""

import importlib.util
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('flash_target', Path(__file__).resolve().parents[1] / 'provisioning/flash-target.py')
target = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(target)


class FlashTargetTests(unittest.TestCase):
    def test_confirmation_requires_typed_yes_and_refuses_replaced_target(self):
        source = Path(__file__).resolve().parents[1] / 'provisioning/linux-flasher.sh'
        function = re.search(r'^confirm_flash\(\) \{\n.*?^\}', source.read_text(), re.M | re.S)[0]
        script = ('set -e\ndeclare -A FLASH_TARGET_IDS=([/dev/fake]=selected)\n'
                  'lsblk() { echo 32G; }\n'
                  'python3() { if [ "$2" = fingerprint ]; then echo "$TEST_ID"; else echo "fake disk"; fi; }\n'
                  + function + '\nconfirm_flash /dev/fake\necho WRITE_AUTHORIZED\n')
        for answer, identity, allowed in [('\n', 'selected', False), ('y\n', 'selected', False),
                                           ('yes\n', 'selected', True), ('yes\n', 'replaced', False)]:
            with self.subTest(answer=answer, identity=identity):
                result = subprocess.run(['bash', '-c', script], input=answer, capture_output=True,
                                        text=True, env={'TEST_ID': identity}, timeout=5)
                self.assertEqual('WRITE_AUTHORIZED' in result.stdout, allowed)

    def disk(self, mounts=(), kind='part'):
        return {'name': '/dev/sdz', 'type': 'disk', 'size': 32 * 2**30, 'ro': False,
                'rm': False, 'model': 'USB reader', 'serial': '123', 'maj:min': '8:240',
                'children': [{'type': kind, 'mountpoints': list(mounts)}]}

    def test_system_swap_and_mapped_storage_are_never_targets(self):
        for mounts, kind in [(['/'], 'part'), (['/boot'], 'part'), (['/home'], 'part'),
                             (['[SWAP]'], 'part'), ([], 'lvm'), (['/mnt/backup'], 'part')]:
            with self.subTest(mounts=mounts, kind=kind), self.assertRaises(ValueError):
                target.inspect_disk(self.disk(mounts, kind))

    def test_automounted_cards_remain_visible_and_nonremovable_is_explicit(self):
        info = target.inspect_disk(self.disk(['/media/user/card']))
        self.assertEqual(info['mounts'], ['/media/user/card'])
        self.assertIn('NON-REMOVABLE', info['description'])
        self.assertIn('32.0 GiB', info['description'])

    def prepare(self, states, claims=(), expected='confirmed', umount_rc=0):
        """Run prepare against a sequence of lsblk views; returns the umount calls."""
        states = [{'fingerprint': f, 'mounts': list(m)} for f, m in states]
        claims = iter(claims)
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, umount_rc, '', 'target is busy')
        clock = iter(range(0, 1000))
        with patch.object(target, 'inspect_target', side_effect=states), \
                patch.object(target, 'hold_device') as hold, \
                patch.object(target, 'claimed', side_effect=lambda d: next(claims, False)), \
                patch.object(target, 'holders', return_value=['nautilus (pid 42)']), \
                patch.object(target.time, 'monotonic', side_effect=lambda: next(clock)), \
                patch.object(target.time, 'sleep'), \
                patch.object(target.subprocess, 'run', side_effect=run):
            target.prepare('/dev/sdz', expected)
        hold.assert_called_once_with('/dev/sdz')
        return calls

    def test_prepare_unmounts_only_confirmed_destination_and_waits_until_it_stays_free(self):
        mounted = ('confirmed', ['/media/user/bootfs', '/media/user/rootfs'])
        free = ('confirmed', [])
        calls = self.prepare([mounted, mounted, mounted, free, free])
        self.assertEqual(calls, [['umount', '--', '/media/user/bootfs'],
                                 ['umount', '--', '/media/user/rootfs']] * 2)
        # A mount still in progress claims the disk before lsblk shows it.
        self.assertEqual(self.prepare([free] * 5, claims=[True, True, False]), [])

    def test_prepare_refuses_changed_or_replaced_targets(self):
        for states in ([('different', [])], [('confirmed', []), ('different', [])]):
            with self.subTest(states=states), self.assertRaisesRegex(ValueError, 'changed'):
                self.prepare(states + [('confirmed', [])] * 3)
        with patch.object(target, 'inspect_target', return_value={'fingerprint': 'x', 'mounts': []}), \
                patch.object(target.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'identity changed'):
                target.prepare('/dev/sdz', 'different-card')
            run.assert_not_called()

    def test_prepare_gives_up_naming_what_holds_the_mount(self):
        mounted = ('confirmed', ['/media/user/rootfs'])
        with self.assertRaisesRegex(ValueError, r'rootfs is in use by nautilus \(pid 42\)'):
            self.prepare([mounted] * 100, umount_rc=32)

    def test_automount_hold_rules_and_release(self):
        with tempfile.TemporaryDirectory() as scratch, \
                patch.object(target, 'NOAUTO_RULES', Path(scratch) / 'rules.d/99.rules'), \
                patch.object(target.subprocess, 'run') as run:
            target.hold(target.CM4_RULE)
            target.hold_device('/dev/sdz')
            target.hold(target.CM4_RULE)
            rules = target.NOAUTO_RULES.read_text().splitlines()
            self.assertEqual(rules, [target.CM4_RULE, 'SUBSYSTEM=="block", '
                                     'KERNEL=="sdz|sdz[0-9]*|sdzp[0-9]*", ENV{UDISKS_AUTO}="0"'])
            self.assertIn(['udevadm', 'control', '--reload'], [c.args[0] for c in run.call_args_list])
            target.release()
            self.assertFalse(target.NOAUTO_RULES.exists())
            target.release()

    def test_flasher_holds_before_rpiboot_and_releases_on_exit(self):
        source = (Path(__file__).resolve().parents[1] / 'provisioning/linux-flasher.sh').read_text()
        self.assertLess(source.index('flash-target.py" hold-cm4'), source.index('sudo rpiboot\n'))
        self.assertIn('trap release_automount_hold EXIT', source)

    def test_disk_sequence_change_invalidates_confirmation(self):
        with patch.object(Path, 'read_text', return_value='10'):
            first = target.inspect_disk(self.disk())
        with patch.object(Path, 'read_text', return_value='11'):
            second = target.inspect_disk(self.disk())
        self.assertNotEqual(first['fingerprint'], second['fingerprint'])
