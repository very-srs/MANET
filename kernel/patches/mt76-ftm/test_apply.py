"""Exercise the real series on disposable trees using local kernel objects.

Run: python3 -m unittest discover -s kernel/patches/mt76-ftm -v
The two source trees in kernel-work/real_work are read only to these tests.
An unavailable source tree skips its tests.
"""
from contextlib import contextmanager
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SERIES = Path(__file__).resolve().parent
ROOT = SERIES.parents[2]
DRIVER = 'drivers/net/wireless/mediatek/mt76/mt7915/'
FILES = ('Makefile', 'debugfs.c', 'init.c', 'mac.c', 'mt7915.h', 'pci.c')
PATCHES = [SERIES / name for name in (SERIES / 'series').read_text().splitlines()]
TXPOWER = SERIES.parent / 'mt76-txpower/0001-mt7915-channel-power.patch'
DTS = 'arch/arm64/boot/dts/rockchip/rk3568-rock-3a.dts'


def run(tree, *args, check=True):
    return subprocess.run(args, cwd=tree, capture_output=True, text=True,
                          check=check)


def snapshot(tree):
    return {str(p.relative_to(tree)): p.read_bytes()
            for p in tree.rglob('*') if p.is_file()
            and '.git' not in p.relative_to(tree).parts
            and p.name != '.manet-ftm-apply.lock'}


class ApplyCases:
    @contextmanager
    def tree(self):
        source = ROOT / 'kernel-work/real_work' / self.kernel
        if not (source / '.git').exists():
            self.skipTest(f'kernel objects unavailable: {source}')
        with tempfile.TemporaryDirectory(prefix='manet-ftm-test-') as temp:
            tree = Path(temp)
            run(tree, 'git', 'init', '-q')
            objects = run(source, 'git', 'rev-parse', '--git-path', 'objects').stdout.strip()
            (tree / '.git/objects/info/alternates').write_text(
                str((source / objects).resolve()) + '\n')
            (tree / '.git/HEAD').write_text(self.base + '\n')
            paths = [DRIVER + name for name in FILES]
            if self.kernel == 'linux-6.18':
                paths.append(DTS)
            for path in paths:
                data = run(source, 'git', 'show', f'{self.base}:{path}').stdout
                (tree / path).parent.mkdir(parents=True, exist_ok=True)
                (tree / path).write_text(data)
            run(tree, 'git', 'apply', str(TXPOWER))
            if self.kernel == 'linux-6.18':
                run(tree, 'git', 'apply', str(SERIES.parent /
                    'r3a-pcie/0001-rock-3a-pcie-startup-delay.patch'))
            (tree / 'unrelated.txt').write_text('Keep this local change.\n')
            yield tree

    def helper(self, tree, script=SERIES / 'apply.py'):
        return run(tree, 'python3', str(script), str(tree), check=False)

    def test_apply_skip_and_preserve_local_changes(self):
        with self.tree() as tree:
            before = snapshot(tree)
            result = self.helper(tree)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('applied complete series', result.stdout)
            applied = snapshot(tree)
            self.assertIn('MODULE_INFO(manet_timing, "1")',
                          (tree / DRIVER / 'pci.c').read_text())
            result = self.helper(tree)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('complete series already applied', result.stdout)
            self.assertEqual(snapshot(tree), applied)
            for patch in reversed(PATCHES):
                run(tree, 'git', 'apply', '-R', str(patch))
            self.assertEqual(snapshot(tree), before)

    def test_partial_series_rejected_without_changes(self):
        for subset in [PATCHES[:n] for n in range(1, len(PATCHES))] + [PATCHES[-1:]]:
            with self.subTest(patches=[p.name for p in subset]), self.tree() as tree:
                for patch in subset:
                    run(tree, 'git', 'apply', str(patch))
                before = snapshot(tree)
                result = self.helper(tree)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('partial or conflicting series', result.stderr)
                self.assertEqual(snapshot(tree), before)

    def test_conflict_rejected_without_changes(self):
        with self.tree() as tree:
            path = tree / DRIVER / 'Makefile'
            path.write_text(path.read_text().replace('debugfs.o mmio.o',
                                                    'debugfs.o conflict.o'))
            before = snapshot(tree)
            result = self.helper(tree)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('partial or conflicting series', result.stderr)
            self.assertEqual(snapshot(tree), before)

    def test_unknown_base_rejected_without_changes(self):
        with self.tree() as tree:
            parent = run(tree, 'git', 'rev-parse', self.base + '^').stdout
            (tree / '.git/HEAD').write_text(parent)
            before = snapshot(tree)
            result = self.helper(tree)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('requires kernel HEAD', result.stderr)
            self.assertEqual(snapshot(tree), before)

    def test_paths_outside_driver_rejected(self):
        with self.tree() as tree, tempfile.TemporaryDirectory() as temp:
            series = Path(temp) / 'series'
            shutil.copytree(SERIES, series)
            patch = series / PATCHES[-1].name
            patch.write_text(patch.read_text().replace('mt76/mt7915/', 'mt76/other/'))
            before = snapshot(tree)
            result = self.helper(tree, series / 'apply.py')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('unexpected path', result.stderr)
            self.assertEqual(snapshot(tree), before)


class CM4Tests(ApplyCases, unittest.TestCase):
    kernel = 'linux-rpi-6.18'
    base = '95b85bebbedcaedfa7ca79116ed38b7376fba412'


class R3ATests(ApplyCases, unittest.TestCase):
    kernel = 'linux-6.18'
    base = '7d0a66e4bb9081d75c82ec4957c50034cb0ea449'


if __name__ == '__main__':
    unittest.main()
