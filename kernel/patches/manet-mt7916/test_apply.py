"""Replay the paired stack on both pinned bases and exercise its real C."""
from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
spec = importlib.util.spec_from_file_location('paired_apply', HERE / 'apply.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
PATCHES, PATHS = helper.inventory()
DRIVER = 'drivers/net/wireless/mediatek/mt76/mt7915'


def run(tree, *args, check=True):
    return subprocess.run(list(map(str, args)), cwd=tree, capture_output=True,
                          text=True, check=check)


def snapshot(tree):
    return {p: (tree / p).read_bytes() if (tree / p).exists() else None for p in PATHS}


class ApplyCases:
    @contextmanager
    def tree(self):
        source = Path(os.environ.get('MANET_KERNEL_WORK', ROOT / 'kernel-work')) / 'real_work' / self.kernel
        if not (source / '.git').exists():
            self.skipTest('pinned kernel objects unavailable: ' + str(source))
        with tempfile.TemporaryDirectory(prefix='manet-paired-test-') as temp:
            tree = Path(temp)
            run(tree, 'git', 'init', '-q')
            objects = run(source, 'git', 'rev-parse', '--git-path', 'objects').stdout.strip()
            (tree / '.git/objects/info/alternates').write_text(str((source / objects).resolve()) + '\n')
            (tree / '.git/HEAD').write_text(self.base + '\n')
            # Main-RDD proof also extracts the unchanged mt76 state selector.
            for p in [*PATHS, 'drivers/net/wireless/mediatek/mt76/mac80211.c']:
                result = run(source, 'git', 'show', self.base + ':' + p, check=False)
                if result.returncode == 0:
                    (tree / p).parent.mkdir(parents=True, exist_ok=True)
                    (tree / p).write_text(result.stdout)
            (tree / 'unrelated.txt').write_text('Preserve this local change.\n')
            yield tree

    def test_clean_apply_idempotence_and_reversal(self):
        with self.tree() as tree:
            original = snapshot(tree)
            helper.apply(tree)
            final = snapshot(tree)
            helper.apply(tree)
            self.assertEqual(final, snapshot(tree))
            self.assertEqual((tree / 'unrelated.txt').read_text(), 'Preserve this local change.\n')
            self.assertEqual((tree / 'net/wireless/hold-api.h').read_bytes(),
                             (tree / DRIVER / 'hold-api.h').read_bytes())
            for patch in reversed(PATCHES):
                run(tree, 'git', 'apply', '-R', patch)
            self.assertEqual(original, snapshot(tree))

    def test_partial_stacks_rejected_without_writes(self):
        # Includes cfg80211 alone, an incomplete driver, and the old observer
        # without its paired hold driver. No mixed private ABI may be blessed.
        for prefix in (1, 3, 4, 10, 14, 15):
            with self.subTest(prefix=prefix), self.tree() as tree:
                for patch in PATCHES[:prefix]:
                    run(tree, 'git', 'apply', patch)
                before = snapshot(tree)
                with self.assertRaisesRegex(ValueError, 'partial or conflicting'):
                    helper.apply(tree)
                self.assertEqual(before, snapshot(tree))

    def test_local_conflict_and_unknown_head_rejected(self):
        with self.tree() as tree:
            path = tree / 'net/wireless/core.h'
            path.write_text(path.read_text() + '\n/* local change */\n')
            before = snapshot(tree)
            with self.assertRaisesRegex(ValueError, 'partial or conflicting'):
                helper.apply(tree)
            self.assertEqual(before, snapshot(tree))
            parent = run(tree, 'git', 'rev-parse', self.base + '^').stdout
            (tree / '.git/HEAD').write_text(parent)
            with self.assertRaisesRegex(ValueError, 'requires kernel HEAD'):
                helper.apply(tree)
            self.assertEqual(before, snapshot(tree))

    def test_malformed_final_patch_is_atomic(self):
        with self.tree() as tree, tempfile.TemporaryDirectory() as temp:
            series = Path(temp) / 'series'
            shutil.copytree(HERE, series)
            patch = series / 'mt76/0011-continuous-hold-v2.patch'
            patch.write_text(patch.read_text() +
                             '\ndiff --git a/net/wireless/core.c b/net/wireless/core.c\n'
                             '--- a/net/wireless/core.c\n+++ b/net/wireless/core.c\n'
                             '@@ -1 +1 @@\n-no such source line\n+invalid replacement\n')
            before = snapshot(tree)
            with self.assertRaises(subprocess.CalledProcessError):
                helper.apply(tree, series)
            self.assertEqual(before, snapshot(tree))

    def test_combined_c_behavior(self):
        if not shutil.which('cc'):
            self.skipTest('host C compiler unavailable')
        with self.tree() as tree:
            helper.apply(tree)
            checks = [(HERE.parent / 'dfs-follow-ap/test_kernel.py', [tree])]
            checks += [(HERE / 'tests' / ('check_' + name + '.py'), [tree, tree])
                       for name in ('hold', 'driver', 'monitor', 'timing', 'transition', 'command', 'transfer', 'csa', 'main', 'background')]
            checks += [(HERE.parent / 'mt76-bgobs' / name, [tree / DRIVER, tree])
                       for name in ('check_reservation.py', 'check_initialization.py')]
            checks += [(HERE / 'tests/check_observer.py', [tree / DRIVER, tree])]
            for script, args in checks:
                with self.subTest(check=script.name):
                    result = run(tree, 'python3', script, *args, check=False)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class CM4Tests(ApplyCases, unittest.TestCase):
    kernel = 'linux-rpi-6.18'
    base = '95b85bebbedcaedfa7ca79116ed38b7376fba412'


class R3ATests(ApplyCases, unittest.TestCase):
    kernel = 'linux-6.18'
    base = '7d0a66e4bb9081d75c82ec4957c50034cb0ea449'


if __name__ == '__main__':
    unittest.main()
