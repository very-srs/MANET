"""Offline firmware preparation and failure recovery, without a radio."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import zlib

SPEC = importlib.util.spec_from_file_location('mt7916_firmware', Path(__file__).with_name('manet-mt7916-firmware.py'))
fw = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fw)
REPO = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((REPO / 'MANET/share/manet/mt7916-firmware.json').read_text())
CACHE = Path(os.environ.get('MANET_FIRMWARE_CACHE', REPO / 'kernel-work/cache/linux-firmware' /
                           MANIFEST['linux_firmware']['commit']))


def fixture():
    # Synthetic firmware: one 16-byte region, one descriptor, a common trailer.
    stock = bytearray(92)
    struct.pack_into('<I', stock, 36, 16)
    stock[58] = 1
    expected = bytearray(stock)
    expected[0] = 0xa5
    state = 0
    for word in struct.unpack('<4I', expected[:16]):
        parity = sum((state >> bit) & 1 for bit in range(32) if (0x10921111 >> bit) & 1) % 2
        state = ((state >> 1) | (parity << 31)) ^ word
    struct.pack_into('<I', expected, 12, state)
    struct.pack_into('<I', expected, 88, zlib.crc32(expected[:88]))
    spec = {'size': 92, 'data_end': 16, 'input_sha256': fw.digest(stock),
            'output_sha256': fw.digest(expected), 'replacements': [{'offset': 0, 'hex': 'a5'}]}
    blobs = dict(zip(fw.NAMES, (bytes(stock), b'fake WA', b'fake ROM')))
    manifest = {'wm_patch': spec, 'linux_firmware': {'files': {
        n: {'sha256': fw.digest(b)} for n, b in blobs.items()}}}
    return blobs, manifest, bytes(expected)


class FirmwareTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.tool = fw.Firmware(self.root)
        gate = patch.object(fw, 'timing_driver', return_value=True)
        self.gate = gate.start()
        self.addCleanup(gate.stop)
        self.boot_file = self.root / 'proc/sys/kernel/random/boot_id'
        self.boot_file.parent.mkdir(parents=True)
        self.boot_file.write_text('boot-one\n')
        self.pci = self.root / 'sys/bus/pci/devices'
        self.pci.mkdir(parents=True)
        self.primary = self.make_pci('0000:01:00.0')
        self.blobs, self.manifest, self.expected = fixture()
        unique_hash = patch.object(fw, 'WM_SHA256', fw.digest(self.expected))
        unique_hash.start()
        self.addCleanup(unique_hash.stop)
        self.tool.assets.mkdir(parents=True)
        (self.tool.assets / 'firmware/mt7916').mkdir(parents=True)
        for name, data in self.blobs.items():
            (self.tool.assets / 'firmware/mt7916' / name).write_bytes(data)
        self.write_manifest()
        (self.root / 'etc').mkdir()
        self.config = self.root / 'etc/mesh.conf'
        self.config.write_text('positioning=y\n')
        self.base = self.root / 'lib/firmware/mediatek'
        self.base.mkdir(parents=True)
        for name in fw.NAMES:
            (self.base / name).write_bytes(b'apt file ' + name.encode())
        self.base_before = {n: (self.base / n).read_bytes() for n in fw.NAMES}

    def write_manifest(self):
        (self.tool.assets / 'mt7916-firmware.json').write_text(json.dumps(self.manifest))

    def assert_stock(self):
        self.assertTrue(all(not os.path.lexists(self.tool.updates / n) for n in fw.NAMES))
        self.assertEqual(self.base_before, {n: (self.base / n).read_bytes() for n in fw.NAMES})

    def snapshot(self):
        return {p.name: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes())
                for p in self.tool.updates.iterdir()}

    def test_enable_idempotence_disable(self):
        self.assertTrue(self.tool.run()[0])
        for name in fw.NAMES:
            path = self.tool.updates / name
            self.assertFalse(path.is_symlink())
            self.assertEqual(path.read_bytes(), self.expected if name == fw.NAMES[0] else self.blobs[name])
        before = self.snapshot()
        # No stock reads, reseals, temp creation or filesystem writes on a ready
        # boot. Opening the directory read-only for its lock is allowed.
        shutil.rmtree(self.tool.assets / 'firmware')
        real_open = os.open
        def read_only_open(path, flags, *args, **kwargs):
            self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)
            self.assertFalse(flags & (os.O_CREAT | os.O_TRUNC))
            return real_open(path, flags, *args, **kwargs)
        with patch.object(fw, 'patch_wm', side_effect=AssertionError('must not reseal')), \
             patch.object(fw, 'staged_file', side_effect=AssertionError('must not write')), \
             patch.object(fw.os, 'replace', side_effect=AssertionError('must not rename')), \
             patch.object(fw.os, 'open', side_effect=read_only_open):
            self.assertTrue(self.tool.run()[0])
        self.assertEqual(before, self.snapshot())
        self.config.write_text('positioning=n\n')
        self.assertTrue(self.tool.run()[0])
        self.assert_stock()
        self.assertFalse(self.tool.owner.exists())

    def test_parsing_matches_positioning_switch(self):
        for text, want in [('positioning=Y', True), (' positioning = "y" ', True),
                           ("positioning='y'", True), ('positioning=yes', False),
                           ('# positioning=y', False), ('positioning=y\npositioning=n', False),
                           ('positioning=y # comment', False), ('', False)]:
            with self.subTest(text=text):
                self.config.write_text(text)
                self.assertEqual(fw.enabled(self.config), want)
        self.config.unlink()
        self.assertFalse(fw.enabled(self.config))

    def test_wrong_stock_hashes_refused(self):
        for name in fw.NAMES:
            with self.subTest(name=name):
                path = self.tool.assets / 'firmware/mt7916' / name
                path.write_bytes(b'wrong firmware')
                ok, message = self.tool.run()
                self.assertFalse(ok)
                self.assertIn('hash mismatch', message)
                self.assert_stock()
                path.write_bytes(self.blobs[name])

    def test_failed_repair_removes_owned_set(self):
        self.assertTrue(self.tool.run()[0])
        (self.tool.updates / fw.NAMES[1]).unlink()
        (self.tool.assets / 'firmware/mt7916' / fw.NAMES[0]).write_bytes(b'wrong WM')
        self.assertFalse(self.tool.run()[0])
        self.assert_stock()

    def test_repair_does_not_rewrite_correct_files(self):
        self.assertTrue(self.tool.run()[0])
        before = self.snapshot()
        (self.tool.updates / fw.NAMES[1]).unlink()
        with patch.object(fw, 'patch_wm', side_effect=AssertionError('must not reseal')):
            self.assertTrue(self.tool.run()[0])
        after = self.snapshot()
        for n in (fw.NAMES[0], fw.NAMES[2]):
            self.assertEqual(before[n], after[n])

    def test_output_hash_failure_leaves_stock(self):
        self.manifest['wm_patch']['replacements'][0]['hex'] = 'a6'
        self.write_manifest()
        self.assertFalse(self.tool.run()[0])
        self.assert_stock()

    def test_foreign_file_and_link_are_never_removed(self):
        self.tool.updates.mkdir(parents=True)
        foreign = self.tool.updates / fw.NAMES[0]
        foreign.write_bytes(b'operator file')
        link = self.tool.updates / fw.NAMES[1]
        link.symlink_to(self.base / fw.NAMES[1])
        self.assertFalse(self.tool.run()[0])
        self.config.write_text('positioning=n')
        ok, message = self.tool.run()
        self.assertTrue(ok)
        self.assertIn('unowned overrides preserved', message)
        self.assertEqual(foreign.read_bytes(), b'operator file')
        self.assertTrue(link.is_symlink())

    def test_identical_foreign_companion_is_not_adopted(self):
        self.tool.updates.mkdir(parents=True)
        foreign = self.tool.updates / fw.NAMES[1]
        foreign.write_bytes(self.blobs[fw.NAMES[1]])
        before = foreign.stat().st_mtime_ns
        self.assertTrue(self.tool.run()[0])
        self.assertNotIn(fw.NAMES[1], self.tool.owners())
        self.config.write_text('positioning=n')
        self.assertTrue(self.tool.run()[0])
        self.assertEqual(foreign.stat().st_mtime_ns, before)
        self.assertEqual(foreign.read_bytes(), self.blobs[fw.NAMES[1]])
        self.assertFalse((self.tool.updates / fw.NAMES[0]).exists())
        self.assertFalse((self.tool.updates / fw.NAMES[2]).exists())

    def test_admin_replacement_with_identical_bytes_is_preserved(self):
        self.assertTrue(self.tool.run()[0])
        foreign = self.tool.updates / fw.NAMES[1]
        replacement = self.tool.updates / 'admin-copy'
        replacement.write_bytes(self.blobs[fw.NAMES[1]])
        os.replace(replacement, foreign)
        self.config.write_text('positioning=n')
        self.assertTrue(self.tool.run()[0])
        self.assertEqual(foreign.read_bytes(), self.blobs[fw.NAMES[1]])
        self.assertFalse((self.tool.updates / fw.NAMES[2]).exists())

    def test_interrupted_temporary_write_exposes_no_partial_file(self):
        real_stage = fw.staged_file
        def interrupt(path, data):
            if path.name == fw.NAMES[0]:
                path.write_bytes(data[:len(data) // 2])
                self.assertFalse(os.path.lexists(self.tool.updates / path.name))
                raise OSError('injected interrupted write')
            return real_stage(path, data)
        with patch.object(fw, 'staged_file', side_effect=interrupt):
            self.assertFalse(self.tool.run()[0])
        self.assert_stock()
        self.assertFalse(list(self.tool.updates.iterdir()))

    def test_rename_failure_removes_published_companion(self):
        real_replace = os.replace
        def fail_second(source, destination):
            if Path(destination).name == fw.NAMES[2]:
                self.assertEqual((self.tool.updates / fw.NAMES[1]).read_bytes(), self.blobs[fw.NAMES[1]])
                self.assertFalse((self.tool.updates / fw.NAMES[0]).exists())
                raise OSError('injected rename failure')
            return real_replace(source, destination)
        with patch.object(fw.os, 'replace', side_effect=fail_second):
            self.assertFalse(self.tool.run()[0])
        self.assert_stock()

    def test_crash_after_first_publication_is_recovered_at_next_boot(self):
        real_replace = os.replace
        def crash(source, destination):
            if Path(destination).name == fw.NAMES[2]:
                raise SystemExit('simulated abrupt process termination')
            return real_replace(source, destination)
        with patch.object(fw.os, 'replace', side_effect=crash), self.assertRaises(SystemExit):
            self.tool.run()
        self.assertEqual((self.tool.updates / fw.NAMES[1]).read_bytes(), self.blobs[fw.NAMES[1]])
        self.assertFalse((self.tool.updates / fw.NAMES[0]).exists())
        self.config.write_text('positioning=n')
        self.assertTrue(fw.Firmware(self.root).run()[0])
        self.assert_stock()

    def test_disabled_boot_retries_failed_removal(self):
        self.assertTrue(self.tool.run()[0])
        self.config.write_text('positioning=n')
        real_unlink = Path.unlink
        def deny(path, *args, **kwargs):
            if path == self.tool.updates / fw.NAMES[0]:
                raise PermissionError('injected read-only filesystem')
            return real_unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', deny):
            ok, message = self.tool.run()
        self.assertFalse(ok)
        self.assertIn('retry at next boot', message)
        self.assertTrue(self.tool.owner.exists())
        self.assertTrue(fw.Firmware(self.root).run()[0])
        self.assert_stock()

    def test_persistence_and_offline_config_change(self):
        self.assertTrue(self.tool.run()[0])
        before = self.snapshot()
        with patch.object(fw, 'patch_wm', side_effect=AssertionError('must not reseal')):
            self.assertTrue(fw.Firmware(self.root).run()[0])
        self.assertEqual(before, self.snapshot())
        self.config.write_text('positioning=n')
        self.assertTrue(fw.Firmware(self.root).run()[0])
        self.assert_stock()

    def test_merged_usr_files(self):
        shutil.move(self.root / 'lib', self.root / 'usr/lib')
        (self.root / 'lib').symlink_to('usr/lib')
        self.assertTrue(self.tool.run()[0])
        self.assertEqual((self.tool.updates / fw.NAMES[0]).read_bytes(), self.expected)

    def test_missing_intent_disables_previous_override(self):
        self.assertTrue(self.tool.run()[0])
        (self.tool.assets / 'mt7916-firmware.json').unlink()
        self.assertFalse(self.tool.run()[0])
        self.assert_stock()

    def test_unreadable_config_disables_previous_override(self):
        self.assertTrue(self.tool.run()[0])
        self.config.write_bytes(b'positioning=\xff')
        self.assertFalse(self.tool.run()[0])
        self.assert_stock()

    def test_cli_logs_one_line(self):
        self.config.write_text('positioning=n')
        result = subprocess.run([sys.executable, str(Path(fw.__file__)), '--root', str(self.root)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn('disabled', result.stderr)
        self.assertFalse(self.tool.updates.exists())

    def test_missing_driver_tag_removes_previous_override(self):
        self.assertTrue(self.tool.run()[0])
        self.gate.return_value = False
        ok, message = self.tool.run('boot')
        self.assertTrue(ok)
        self.assertIn('lacks manet_timing=1', message)
        self.assert_stock()
        self.assertFalse(self.tool.pending.exists())

    def test_absent_primary_skips_preparation_and_boot_marker(self):
        shutil.rmtree(self.primary)
        for mode in ('apply', 'boot', 'rearm'):
            with self.subTest(mode=mode), \
                 patch.object(fw, 'patch_wm', side_effect=AssertionError('must not seal')), \
                 patch.object(self.tool, 'write_state', side_effect=AssertionError('must not arm')):
                ok, message = self.tool.run(mode)
                self.assertTrue(ok)
                self.assertIn('no MT7916 primary PCI function', message)
                self.assert_stock()
                self.assertFalse(self.tool.pending.exists())
                self.assertFalse(self.tool.owner.exists())
        self.gate.assert_not_called()

    def test_secondary_only_and_wrong_vendor_count_as_absent(self):
        shutil.rmtree(self.primary)
        auxiliary = self.make_pci('0000:01:00.0', '0x790a', 'mt7915e_hif')
        wrong_vendor = self.make_pci('0000:02:00.0')
        (wrong_vendor / 'vendor').write_text('0x1234\n')
        self.assertIn('no MT7916 primary PCI function', self.tool.run('boot')[1])
        self.assert_stock()
        self.assertFalse(self.tool.pending.exists())
        self.assertTrue(auxiliary.exists())

    def test_absent_primary_removes_prepared_owned_overrides(self):
        self.assertTrue(self.tool.run()[0])
        shutil.rmtree(self.primary)
        self.assertIn('no MT7916 primary PCI function', self.tool.run('boot')[1])
        self.assert_stock()
        self.assertFalse(self.tool.owner.exists())
        self.assertFalse(self.tool.pending.exists())

    def test_present_unbound_primary_prepares_before_coldplug(self):
        (self.primary / 'driver').unlink()
        shutil.rmtree(self.primary / 'ieee80211')
        self.assertTrue(self.tool.run('boot')[0])
        self.assertEqual((self.tool.updates / fw.NAMES[0]).read_bytes(), self.expected)
        self.assertTrue(self.tool.pending.exists())
        self.gate.assert_called_once_with(self.root)

    def test_absent_primary_cli_logs_one_line(self):
        shutil.rmtree(self.primary)
        result = subprocess.run([sys.executable, str(Path(fw.__file__)), '--root',
                                 str(self.root), '--boot'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn('no MT7916 primary PCI function', result.stderr)
        self.assert_stock()
        self.assertFalse(self.tool.pending.exists())

    def test_boot_marker_durable_before_preparation(self):
        # First boot and ready boot both persist the marker before preparation
        # can publish/retain WM. A ready boot never rewrites the firmware set.
        for ready in (False, True):
            with self.subTest(ready=ready):
                if ready:
                    self.tool.remove_state(self.tool.pending)
                before = self.snapshot() if ready else None
                events = []
                real_fsync, real_replace, real_prepare = os.fsync, os.replace, self.tool.prepare
                def fsync(fd):
                    real_fsync(fd)
                    events.append(('fsync', os.readlink('/proc/self/fd/' + str(fd))))
                def replace(source, dest):
                    real_replace(source, dest)
                    events.append(('replace', str(dest)))
                def prepare(enable):
                    record = self.tool.read_state(self.tool.pending)
                    self.assertEqual(record['boot_id'], 'boot-one')
                    self.assertEqual(record['wm_sha256'], fw.WM_SHA256)
                    renamed = events.index(('replace', str(self.tool.pending)))
                    self.assertTrue(any(kind == 'fsync' and path.endswith('/mt7916-probe-pending')
                                        for kind, path in events[:renamed]))
                    self.assertIn(('fsync', str(self.tool.state)), events[renamed + 1:])
                    return real_prepare(enable)
                with patch.object(fw.os, 'fsync', side_effect=fsync), \
                     patch.object(fw.os, 'replace', side_effect=replace), \
                     patch.object(self.tool, 'prepare', side_effect=prepare):
                    self.assertTrue(self.tool.run('boot')[0])
                if ready:
                    self.assertEqual(before, self.snapshot())

    def test_marker_sync_failure_removes_existing_patch(self):
        self.assertTrue(self.tool.run()[0])
        real_sync = fw.sync_directory
        def fail_state(directory):
            if directory == self.tool.state:
                raise OSError('injected marker directory fsync failure')
            return real_sync(directory)
        with patch.object(fw, 'sync_directory', side_effect=fail_state):
            self.assertFalse(self.tool.run('boot')[0])
        self.assert_stock()

    def make_pci(self, address, device_id='0x7906', driver='mt7915e', with_phy=True):
        device = self.pci / address
        device.mkdir()
        (device / 'vendor').write_text('0x14c3\n')
        (device / 'device').write_text(device_id + '\n')
        if driver:
            target = self.root / 'sys/bus/pci/drivers' / driver
            target.mkdir(parents=True, exist_ok=True)
            (device / 'driver').symlink_to(target)
            if with_phy:
                (device / 'ieee80211/phy0').mkdir(parents=True)
        return device

    def test_survival_requires_multiuser_and_all_primary_functions_bound(self):
        self.assertTrue(self.tool.run('boot')[0])
        with patch.object(fw.subprocess, 'run', return_value=SimpleNamespace(returncode=3)):
            self.assertIn('retained', self.tool.run('survived')[1])
        self.assertTrue(self.tool.pending.exists())
        unbound = self.make_pci('0000:02:00.0', driver=None)
        with patch.object(fw.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as command:
            self.assertIn('retained', self.tool.run('survived')[1])
            shutil.rmtree(unbound)
            before = self.snapshot()
            with patch.object(fw, 'sync_directory', wraps=fw.sync_directory) as sync:
                self.assertIn('cleared', self.tool.run('survived')[1])
                sync.assert_called_with(self.tool.state)
            command.assert_called_with(['systemctl', 'is-active', '--quiet', 'multi-user.target'],
                                       capture_output=True, timeout=10)
        self.assertFalse(self.tool.pending.exists())
        self.assertEqual(before, self.snapshot())
        self.boot_file.write_text('boot-two')
        self.assertTrue(self.tool.run('boot')[0])
        self.assertFalse(self.tool.disabled.exists())

    def test_survival_no_mt7916_present_and_auxiliary_only(self):
        self.assertTrue(self.tool.run('boot')[0])
        shutil.rmtree(self.primary)
        auxiliary = self.make_pci('0000:01:00.0', '0x790a', 'mt7915e_hif')
        with patch.object(fw.subprocess, 'run', return_value=SimpleNamespace(returncode=0)):
            self.assertIn('retained', self.tool.run('survived')[1])
            shutil.rmtree(auxiliary)
            self.assertIn('cleared', self.tool.run('survived')[1])
        self.assertFalse(self.tool.pending.exists())

    def test_survival_driver_link_before_firmware_init_is_not_enough(self):
        self.assertTrue(self.tool.run('boot')[0])
        shutil.rmtree(self.primary / 'ieee80211')
        with patch.object(fw.subprocess, 'run', return_value=SimpleNamespace(returncode=0)):
            self.assertIn('retained', self.tool.run('survived')[1])
        self.assertTrue(self.tool.pending.exists())

    def fail_boot(self):
        self.assertTrue(self.tool.run('boot')[0])
        self.boot_file.write_text('boot-two')
        ok, message = self.tool.run('boot')
        self.assertTrue(ok)
        self.assertIn('AUTO-DISABLED after failed boot', message)
        self.assertIn(fw.WM_SHA256, message)
        self.assert_stock()
        self.assertEqual(self.tool.read_state(self.tool.disabled)['boot_id'], 'boot-one')
        self.assertFalse(self.tool.pending.exists())

    def test_stale_marker_disables_across_boots_and_unrelated_config_edits(self):
        original = self.config.read_bytes()
        self.fail_boot()
        self.assertEqual(self.config.read_bytes(), original)
        before = self.tool.disabled.stat().st_mtime_ns
        for boot in ('boot-three', 'boot-four'):
            self.boot_file.write_text(boot)
            self.config.write_text('positioning=y\nvoice=n\n')
            for mode in ('boot', 'apply', 'survived'):
                self.assertIn('AUTO-DISABLED', self.tool.run(mode)[1])
                self.assert_stock()
        self.assertEqual(before, self.tool.disabled.stat().st_mtime_ns)

    def test_observed_positioning_change_rearms_but_does_not_edit_config(self):
        self.fail_boot()
        self.config.write_text('positioning=n\n')
        self.assertTrue(self.tool.run()[0])
        self.assertFalse(self.tool.disabled.exists())
        self.assert_stock()
        self.assertEqual(self.config.read_text(), 'positioning=n\n')
        self.config.write_text('positioning=y\n')
        self.assertTrue(self.tool.run()[0])
        self.assertEqual((self.tool.updates / fw.NAMES[0]).read_bytes(), self.expected)

    def test_explicit_rearm_still_requires_tag_and_retains_current_pending(self):
        self.fail_boot()
        self.gate.return_value = False
        self.assertIn('lacks manet_timing', self.tool.run('rearm')[1])
        self.assert_stock()
        self.assertFalse(self.tool.disabled.exists())
        self.gate.return_value = True
        self.assertTrue(self.tool.run('boot')[0])
        before = self.tool.pending.read_bytes()
        self.assertTrue(self.tool.run('rearm')[0])
        self.assertEqual(before, self.tool.pending.read_bytes())

    def test_same_boot_apply_and_boot_retry_do_not_consume_pending(self):
        self.assertTrue(self.tool.run('boot')[0])
        before = self.tool.pending.stat().st_mtime_ns
        for mode in ('apply', 'boot'):
            self.assertIn('already prepared', self.tool.run(mode)[1])
        self.assertEqual(before, self.tool.pending.stat().st_mtime_ns)
        self.assertFalse(self.tool.disabled.exists())

    def test_clean_reboot_before_survival_conservatively_disables(self):
        # No shutdown shortcut clears the marker. Even an orderly early
        # reboot remains unproven and uses the same conservative recovery.
        self.fail_boot()

    def test_crash_during_recovery_preserves_latch_and_evidence(self):
        self.assertTrue(self.tool.run('boot')[0])
        self.boot_file.write_text('boot-two')
        with patch.object(self.tool, 'deactivate', side_effect=SystemExit('power loss')):
            with self.assertRaises(SystemExit):
                self.tool.run('boot')
        self.assertTrue(self.tool.disabled.exists())
        self.assertTrue(self.tool.pending.exists())
        self.boot_file.write_text('boot-three')
        self.assertIn('AUTO-DISABLED', self.tool.run('boot')[1])
        self.assert_stock()

    def test_failed_recovery_record_keeps_pending_and_removes_override(self):
        self.assertTrue(self.tool.run('boot')[0])
        self.boot_file.write_text('boot-two')
        with patch.object(self.tool, 'write_state', side_effect=OSError('cannot persist failure')):
            self.assertFalse(self.tool.run('boot')[0])
        self.assertTrue(self.tool.pending.exists())
        self.assert_stock()
        self.assertIn('AUTO-DISABLED', self.tool.run('boot')[1])


class DriverGateTests(unittest.TestCase):
    def test_installed_running_kernel_module_tag(self):
        for code, tag, expected in ((0, '1\n', True), (0, '', False), (1, '1\n', False),
                                    (0, '0\n', False), (0, '1\n1\n', False)):
            with self.subTest(code=code, tag=tag), \
                 patch.object(fw.os, 'uname', return_value=SimpleNamespace(release='test-kernel')), \
                 patch.object(fw.subprocess, 'run', return_value=SimpleNamespace(
                     returncode=code, stdout=tag)) as command:
                self.assertEqual(fw.timing_driver(Path('/offline')), expected)
                command.assert_called_once_with(
                    ['modinfo', '-b', '/offline', '-k', 'test-kernel', '-F', 'manet_timing', 'mt7915e'],
                    capture_output=True, text=True, timeout=10)

    def test_missing_modinfo_and_timeout_fail_closed(self):
        for error in (FileNotFoundError(), subprocess.TimeoutExpired('modinfo', 10)):
            with patch.object(fw.subprocess, 'run', side_effect=error):
                self.assertFalse(fw.timing_driver(Path('/')))


class UnitSafetyTests(unittest.TestCase):
    SYSTEMD = Path(__file__).resolve().parent.parent / 'systemd'

    def test_coldplug_never_requires_the_firmware_tool(self):
        # A failure here must not stop every other device from loading.
        self.assertFalse((self.SYSTEMD / 'systemd-udev-trigger.service.d').exists())
        unit = (self.SYSTEMD / 'manet-mt7916-firmware.service').read_text()
        self.assertIn('Before=systemd-udev-trigger.service', unit)
        self.assertNotIn('RequiredBy=', unit)

    def test_boot_check_does_not_wait_for_coldplug_created_mounts(self):
        # local-fs.target includes /boot/firmware, whose by-partuuid device
        # only appears after coldplug: waiting for it here deadlocks the boot
        # into emergency mode.
        unit = (self.SYSTEMD / 'manet-mt7916-firmware.service').read_text()
        after = ' '.join(line.split('=', 1)[1] for line in unit.splitlines()
                         if line.startswith('After='))
        self.assertNotIn('local-fs.target', after)
        self.assertIn('systemd-remount-fs.service', after)

    def test_survival_check_only_runs_with_a_pending_marker(self):
        unit = (self.SYSTEMD / 'manet-mt7916-firmware-survived.service').read_text()
        self.assertIn('ConditionPathExists=/var/lib/manet/mt7916-probe-pending', unit)


class CachedFirmwareTests(unittest.TestCase):
    def test_hardware_proven_image_reproduction(self):
        path = CACHE / 'mt7916_wm.bin'
        if not path.exists():
            self.skipTest(f'cached stock WM absent: {path}; run either firmware package builder')
        output = fw.patch_wm(path.read_bytes(), MANIFEST['wm_patch'])
        self.assertEqual(hashlib.sha256(output).hexdigest(),
                         '18a2e1d03f17913ef0450a905536d5defcdf78ee1d98a1232649c31a70a63e1e')
        # Exact expected hash above is independent of the configurable manifest.

    def test_real_wrong_input_hash(self):
        with self.assertRaisesRegex(ValueError, 'stock WM hash mismatch'):
            fw.patch_wm(b'not stock firmware', MANIFEST['wm_patch'])

class ConfigPathTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'linux', 'Linux inotify is required')
    def test_atomic_config_rename_matches_pathchanged_watch(self):
        import ctypes
        import select
        from manet_config_io import atomic_write
        libc = ctypes.CDLL(None, use_errno=True)
        libc.inotify_init1.argtypes = [ctypes.c_int]
        libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        self.assertGreaterEqual(fd, 0, os.strerror(ctypes.get_errno()))
        self.addCleanup(os.close, fd)
        # systemd v257 src/core/path.c: PATH_CHANGED includes ATTRIB,
        # DELETE_SELF, MOVE_SELF, CLOSE_WRITE, CREATE, DELETE and both MOVED bits.
        # path_spec_fd_event triggers for an event on this primary watch.
        mask = 0x4 | 0x400 | 0x800 | 0x8 | 0x100 | 0x200 | 0x40 | 0x80
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'mesh.conf'
            path.write_text('positioning=n\n')
            for value in ('y', 'n', 'y'):
                watch = libc.inotify_add_watch(fd, os.fsencode(path), mask)
                self.assertGreaterEqual(watch, 0)
                old_inode = path.stat().st_ino
                atomic_write(path, 'positioning=' + value + '\n')
                self.assertNotEqual(path.stat().st_ino, old_inode)
                self.assertTrue(select.select([fd], [], [], 1)[0])
                data, offset, events = os.read(fd, 65536), 0, []
                while offset < len(data):
                    wd, event, cookie, length = struct.unpack_from('iIII', data, offset)
                    events.append((wd, event))
                    offset += 16 + length
                self.assertTrue(any(wd == watch and event & mask for wd, event in events), events)


if __name__ == '__main__':
    unittest.main()
