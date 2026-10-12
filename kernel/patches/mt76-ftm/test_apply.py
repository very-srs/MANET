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
FILES = ('Makefile', 'debugfs.c', 'init.c', 'mac.c', 'main.c', 'mt7915.h', 'pci.c')
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
        # The module-tag patch also applies alone; the probe-status patch
        # depends on earlier timing additions.
        for subset in [PATCHES[:n] for n in range(1, len(PATCHES))] + [PATCHES[3:4]]:
            with self.subTest(patches=[p.name for p in subset]), self.tree() as tree:
                for patch in subset:
                    run(tree, 'git', 'apply', str(patch))
                before = snapshot(tree)
                result = self.helper(tree)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('partial or conflicting series', result.stderr)
                self.assertEqual(snapshot(tree), before)

    def test_explicit_suffix_upgrades_older_trees(self):
        for prefix in (4, 5):
            with self.subTest(prefix=prefix), self.tree() as tree:
                for patch in PATCHES[:prefix]:
                    run(tree, 'git', 'apply', str(patch))
                before = snapshot(tree)
                result = self.helper(tree)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(snapshot(tree), before)
                # Check each dependent suffix against its preceding result.
                for patch in PATCHES[prefix:]:
                    run(tree, 'git', 'apply', '--check', str(patch))
                    run(tree, 'git', 'apply', str(patch))
                result = self.helper(tree)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('complete series already applied', result.stdout)

    def test_suffix_conflict_leaves_four_patch_tree_untouched(self):
        with self.tree() as tree:
            for patch in PATCHES[:4]:
                run(tree, 'git', 'apply', str(patch))
            path = tree / DRIVER / 'mac.c'
            path.write_text(path.read_text().replace(
                'mt76_connac2_mac_add_txs_skb(&dev->mt76, wcid, pid, txs_data);',
                'local_txs_handler(&dev->mt76, wcid, pid, txs_data);'))
            before = snapshot(tree)
            result = self.helper(tree)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(snapshot(tree), before)
            result = run(tree, 'git', 'apply', '--check', *map(str, PATCHES[4:]),
                         check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(snapshot(tree), before)

    def test_null_suffix_conflict_leaves_five_patch_tree_untouched(self):
        with self.tree() as tree:
            for patch in PATCHES[:5]:
                run(tree, 'git', 'apply', str(patch))
            path = tree / DRIVER / 'mac.c'
            path.write_text(path.read_text().replace(
                '\tbool tmr;', '\tbool local_timing;'))
            before = snapshot(tree)
            self.assertNotEqual(self.helper(tree).returncode, 0)
            self.assertNotEqual(run(tree, 'git', 'apply', '--check',
                                    str(PATCHES[5]), check=False).returncode, 0)
            self.assertEqual(snapshot(tree), before)

    def test_probe_status_behavior(self):
        # Execute the patched C helper with status/lifecycle stubs. These
        # cannot verify firmware behavior or real DMA ordering on hardware.
        compiler = shutil.which('cc')
        if compiler is None:
            self.skipTest('C compiler unavailable')
        with self.tree() as tree:
            result = self.helper(tree)
            self.assertEqual(result.returncode, 0, result.stderr)
            mac = (tree / DRIVER / 'mac.c').read_text()
            start = mac.index('static void mt7915_mac_add_txs_skb(')
            status = mac[start:mac.index('\nstatic void mt7915_mac_add_txs(', start)]
            flag = next(line for line in mac.splitlines()
                        if line.startswith('#define MT7915_TX_CB_TMR '))
            start = mac.index('static bool mt7915_tmr_selected(')
            txwi = mac[start:mac.index('\nint mt7915_tx_prepare_skb(', start)]
            probe = (tree / DRIVER / 'tmr.c').read_text()
            start = probe.index('#define MT7915_TMR_PROBE_SKIPPED ')
            helpers = probe[start:probe.index('\nstruct mt7915_tmr_lookup', start)]
            start = probe.index('static ssize_t mt7915_tmr_probe_write(')
            io = probe[start:probe.index('\nconst struct file_operations', start)]
            harness = (SERIES / 'timing_test.c').read_text().replace(
                '/* DRIVER_HELPERS */', flag + '\n' + status).replace(
                '/* PROBE_HELPERS */', txwi + helpers + io)
            source = tree / 'timing_test.c'
            source.write_text(harness)
            run(tree, compiler, '-std=gnu11', '-Wall', '-Werror',
                '-Wno-unused-parameter', '-o', 'timing_test', str(source))
            run(tree, str(tree / 'timing_test'))

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
