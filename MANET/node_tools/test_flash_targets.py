"""Simulated removable, mounted and replaced disks; no writes to real disks."""

import importlib.util
from pathlib import Path
import re
import subprocess
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

    def test_prepare_unmounts_only_confirmed_destination_and_rechecks_it(self):
        before = {'fingerprint': 'confirmed', 'mounts': ['/media/user/card']}
        after = {'fingerprint': 'confirmed', 'mounts': []}
        with patch.object(target, 'inspect_target', side_effect=[before, after]), \
                patch.object(target.subprocess, 'run') as run:
            target.prepare('/dev/sdz', 'confirmed')
        run.assert_called_once_with(['umount', '--', '/media/user/card'], check=True, timeout=30)
        with patch.object(target, 'inspect_target', return_value=after), \
                patch.object(target.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'identity changed'):
                target.prepare('/dev/sdz', 'different-card')
            run.assert_not_called()

    def test_disk_sequence_change_invalidates_confirmation(self):
        with patch.object(Path, 'read_text', return_value='10'):
            first = target.inspect_disk(self.disk())
        with patch.object(Path, 'read_text', return_value='11'):
            second = target.inspect_disk(self.disk())
        self.assertNotEqual(first['fingerprint'], second['fingerprint'])
