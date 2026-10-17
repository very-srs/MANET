"""Legacy wrapper validation and the real new updater's trial/recovery path."""

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import types
import unittest
from unittest.mock import Mock

import test_kernel_update as fixtures
from test_node_update import update


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REPO = Path(__file__).resolve().parents[2]
bootstrap = load(
    'release_bootstrap', Path(__file__).with_name('manet-release-bootstrap.py')
)
packaging = load(
    'bootstrap_packaging', REPO / 'MANET/packaging/stage-release-bootstrap.py'
)


class BootstrapTests(unittest.TestCase):
    command = fixtures.KernelUpdateTests.command
    make_archive = fixtures.KernelUpdateTests.make_archive
    hash_archive = fixtures.KernelUpdateTests.hash_archive
    history = fixtures.KernelUpdateTests.history
    run_fake = fixtures.KernelUpdateTests.run_fake
    kernel_archive = fixtures.KernelUpdateTests.kernel_archive
    trial_boot = fixtures.KernelUpdateTests.trial_boot

    def setUp(self):
        fixtures.KernelUpdateTests.setUp(self)
        stage = self.work / 'outer'
        for name, (body, mode) in self.members.items():
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            target.chmod(mode)
        packaging.stage(self.package, stage)
        self.base = self.root / 'usr/local/share/manet/bootstrap'
        shutil.copytree(stage / 'usr/local/share/manet/bootstrap', self.base)
        # Model the successful legacy first step, with both markers committed.
        for name in update.MARKERS:
            (self.root / name).write_text(self.new_version)
        self.stage = stage
        self.external = Mock()

    def test_legacy_installs_outer_then_new_updater_trials_and_cleans(self):
        self.assertFalse(
            bootstrap.apply_embedded_update(self.root, update, self.external)
        )
        self.assertEqual(self.updater.kernel().read()['phase'], 'attempted')
        self.assertEqual((self.boot / 'config.txt').read_text(), self.config)
        self.assertTrue((self.base / 'manifest.json').exists())
        self.trial_boot()
        self.assertTrue(
            bootstrap.apply_embedded_update(self.root, update, self.external)
        )
        self.assertFalse((self.base / 'manifest.json').exists())
        self.assertFalse(self.updater.pending.exists())
        self.assertTrue(
            (self.updater.state / 'bootstrap-complete.json').exists()
        )

    def test_outer_pending_defers_without_touching_kernel(self):
        self.updater.state.mkdir(parents=True)
        self.updater.pending.touch()
        self.assertFalse(
            bootstrap.apply_embedded_update(self.root, update, self.external)
        )
        self.assertFalse((self.boot / 'tryboot.txt').exists())
        self.assertFalse(self.calls)

    def test_wrong_board_ignores_corrupt_cm4_payload(self):
        (self.base / 'manifest.json').write_text('corrupt')
        for model in ('Radxa ROCK 3A', 'Raspberry Pi 5 Model B'):
            (self.root / 'proc/device-tree/model').write_text(model)
            self.assertFalse(
                bootstrap.apply_embedded_update(
                    self.root, update, self.external
                )
            )
        self.assertFalse(self.calls)

    def test_corrupt_inner_archive_fails_before_arming(self):
        (self.base / 'cm4-tools.tar.gz').write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'hash changed'):
            bootstrap.apply_embedded_update(self.root, update, self.external)
        self.assertFalse(self.updater.pending.exists())

    def test_fallback_is_retained_and_never_retried(self):
        bootstrap.apply_embedded_update(self.root, update, self.external)
        self.trial_boot(good=False)
        self.calls.clear()
        for _ in range(2):
            with self.assertRaises(update.KernelError):
                bootstrap.apply_embedded_update(
                    self.root, update, self.external
                )
        self.assertEqual((self.boot / 'config.txt').read_text(), self.config)
        self.assertTrue((self.base / 'manifest.json').exists())
        self.assertFalse(any(call[0] == 'reboot' for call in self.calls))

    def test_explicit_cancellation_stops_automatic_bootstrap(self):
        """An operator cancelling a failed trial must not rearm its timer."""
        bootstrap.apply_embedded_update(self.root, update, self.external)
        self.trial_boot(good=False)
        self.updater.update(cancel_kernel=True)
        self.calls.clear()
        self.assertFalse(
            bootstrap.apply_embedded_update(self.root, update, self.external)
        )
        self.assertFalse(self.updater.pending.exists())
        self.assertFalse(any(call[0] == 'reboot' for call in self.calls))
        self.assertTrue((self.base / 'manifest.json').exists())
        self.external.assert_called_once_with(
            ['systemctl', 'disable', '--now', 'manet-release-bootstrap.timer'],
            check=True,
            timeout=30,
        )

    def test_real_v0569_validator_accepts_wrapper_and_rejects_raw_kernel(self):
        source = subprocess.check_output(
            ['git', 'show', 'v0.569:MANET/node_tools/node-update.py'],
            cwd=REPO,
            text=True,
        )
        legacy = types.ModuleType('legacy_update')
        source_path = self.work / 'legacy/node-update.py'
        source_path.parent.mkdir()
        source_path.write_text(source)
        shutil.copyfile(
            REPO / 'MANET/node_tools/manet_release.py',
            source_path.with_name('manet_release.py'),
        )
        legacy.__file__ = str(source_path)
        exec(compile(source, 'v0.569/node-update.py', 'exec'), legacy.__dict__)
        old = legacy.Updater(self.root)
        with self.assertRaisesRegex(legacy.UpdateError, 'Unsafe archive path'):
            old.validate(
                self.package, self.checksum, self.package.name, '0.550'
            )
        self.members = {
            str(p.relative_to(self.stage)): (
                p.read_bytes(),
                p.stat().st_mode & 0o777,
            )
            for p in self.stage.rglob('*')
            if p.is_file() and not p.is_symlink()
        }
        self.make_archive()
        accepted = old.validate(
            self.package, self.checksum, self.package.name, '0.550'
        )
        self.assertIn(
            'usr/local/share/manet/bootstrap/cm4-tools.tar.gz', accepted
        )
        self.assertNotIn('manet-kernel/manifest.json', accepted)
        # Execute the actual tagged updater in the fake root as well. Its
        # downloaded package has only old-compatible paths and limits.
        for name in update.MARKERS:
            (self.root / name).write_text(self.old_version)
        old.update()
        self.assertFalse(old.pending.exists())
        self.assertEqual(old.marker.read_text(), self.new_version)
        self.assertEqual((self.boot / 'config.txt').read_text(), self.config)
        self.assertFalse(
            bootstrap.apply_embedded_update(self.root, update, self.external)
        )
        self.trial_boot()
        self.assertTrue(
            bootstrap.apply_embedded_update(self.root, update, self.external)
        )


if __name__ == '__main__':
    unittest.main()
