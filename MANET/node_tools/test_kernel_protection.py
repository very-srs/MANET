"""Kernel ownership, first-boot ordering and an isolated real APT resolver."""
import fnmatch
import fcntl
import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parent
MANET = TOOLS.parent
POLICY = MANET / 'etc/apt/preferences.d/00-manet-kernel'
HELPER = TOOLS / 'manet-kernel-protect.py'


def load_helper():
    spec = importlib.util.spec_from_file_location('kernel_protect', HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class KernelProtectionTests(unittest.TestCase):
    def test_cm4_initramfs_policy_is_idempotent_and_keeps_pi5(self):
        text = (MANET / 'provisioning/firstrun.sh.template').read_text()
        start = text.index('    if [ "__HARDWARE_MODEL__" = "rpi4" ]; then', text.index('for _cfg in /boot/firmware/config.txt'))
        block = text[start:text.index('\n    fi', start) + len('\n    fi')]
        for model in ('rpi4', 'rpi5'):
            for initial in ('[all]\nauto_initramfs=1\n[cm5]\nauto_initramfs=1\n', '[all]\n'):
                with self.subTest(model=model, initial=initial), tempfile.TemporaryDirectory() as tmp:
                    config = Path(tmp) / 'config.txt'
                    config.write_text(initial)
                    command = '_cfg="$1"\n' + block.replace('__HARDWARE_MODEL__', model)
                    subprocess.run(['bash', '-eu', '-c', command, 'test', str(config)], check=True)
                    first = config.read_text()
                    subprocess.run(['bash', '-eu', '-c', command, 'test', str(config)], check=True)
                    self.assertEqual(first, config.read_text())
                    if model == 'rpi4':
                        self.assertNotIn('auto_initramfs=1', first)
                        self.assertIn('[cm4]\nauto_initramfs=0\n[all]', first)
                    else:
                        self.assertEqual(first, initial)

    def test_pin_coverage_and_userland_exclusions(self):
        text = POLICY.read_text()
        patterns = []
        for stanza in text.split('\n\n'):
            packages = re.search(r'^Package: (.+)$', stanza, re.M)
            if packages:
                self.assertIn('Pin: version *', stanza)
                self.assertIn('Pin-Priority: -1', stanza)
                patterns.extend(packages[1].split())
        blocked = lambda p: any(fnmatch.fnmatchcase(p, g) for g in patterns)
        for package in (
                'linux-image-rpi-v8', 'linux-image-rpi-2712',
                'linux-image-6.18.50+rpt-rpi-v8', 'linux-image-arm64',
                'linux-headers-rpi-v8', 'linux-headers-6.18.50+rpt-rpi-2712',
                'linux-base-rpi-v8', 'linux-base-6.18.50+rpt-rpi-2712',
                'raspi-firmware', 'raspberrypi-kernel',
                'raspberrypi-kernel-headers', 'raspberrypi-bootloader',
                'linux-image-vendor-rk35xx', 'linux-dtb-vendor-rk35xx',
                'linux-u-boot-rock-3a-vendor', 'armbian-bsp-cli-rock-3a-vendor',
                'flash-kernel', 'u-boot-menu', 'dracut', 'dracut-core'):
            with self.subTest(package=package):
                self.assertTrue(blocked(package))
        for package in ('linux-libc-dev', 'linux-base', 'libc6', 'dpkg',
                        'build-essential', 'libssl-dev', 'firmware-mediatek',
                        'rpi-eeprom', 'armbian-firmware', 'initramfs-tools',
                        'initramfs-tools-core', 'dracut-install'):
            with self.subTest(package=package):
                self.assertFalse(blocked(package))

    def test_templates_protect_before_apt_and_full_extraction(self):
        for template in ('firstrun.sh.template', 'rock3a-provision.sh.template'):
            text = (MANET / 'provisioning' / template).read_text()
            start = text.index('# Install kernel protection from the verified archive.')
            end = text.index('# End kernel protection.', start)
            block = text[start:end]
            self.assertIn('./etc/apt/preferences.d/00-manet-kernel', block)
            self.assertIn('./usr/local/bin/manet-kernel-protect.py', block)
            self.assertIn('python3 /usr/local/bin/manet-kernel-protect.py', block)
            self.assertIn('|| exit 1', block)
            self.assertLess(text.index('sha256sum -c -'), start)
            for command in re.finditer(r'^\s*(?:DEBIAN_FRONTEND=\S+ )?apt(?:-get)?\s+(?:update|upgrade|install)', text, re.M):
                self.assertLess(end, command.start())

    def test_all_builders_stage_policy(self):
        for board in ('cm4', 'rpi5', 'r3a', 'tools'):
            builder = MANET / 'packaging' / f'build-{board}-tarball.sh'
            if not builder.exists():
                self.skipTest('Local archive builders are not in this checkout')
            text = builder.read_text()
            self.assertIn('stage_kernel_protection "$STAGE" "$REPO_ROOT"', text)
        with tempfile.TemporaryDirectory() as root:
            subprocess.run(['bash', '-euc',
                            '. "$1"; stage_kernel_protection "$2" "$3"',
                            'test', str(MANET / 'packaging/lib-kernel-protection.sh'),
                            root, str(MANET.parent)], check=True)
            self.assertEqual((Path(root) / 'etc/apt/preferences.d/00-manet-kernel').read_bytes(), POLICY.read_bytes())
            self.assertTrue((Path(root) / 'usr/local/bin/manet-kernel-protect.py').stat().st_mode & 0o111)

    def test_bootstrap_failure_prevents_apt(self):
        for template in ('firstrun.sh.template', 'rock3a-provision.sh.template'):
            text = (MANET / 'provisioning' / template).read_text()
            start = text.index('# Install kernel protection from the verified archive.')
            block = text[start:text.index('# End kernel protection.', start)]
            for failing, expected in (('', 'tar\nprotect\napt\n'),
                                      ('tar', 'tar\n'), ('protect', 'tar\nprotect\n')):
                with self.subTest(template=template, failing=failing):
                    result = subprocess.run(['bash', '-c', '''
tar() { echo tar; [ "$FAIL" != tar ]; }
python3() { echo protect; [ "$FAIL" != protect ]; }
''' + block + '\necho apt\n'], env=dict(os.environ, FAIL=failing),
                                            capture_output=True, text=True)
                    self.assertEqual(result.stdout, expected)
                    self.assertEqual(result.returncode, 1 if failing else 0)

    def test_apply_is_idempotent_and_neutralizes_hooks_per_board(self):
        module = load_helper()
        for board in ('cm4', 'rpi5', 'r3a'):
            with self.subTest(board=board), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                held, diversions, events = set(), {}, []
                packages = ['linux-image-rpi-v8', 'linux-image-rpi-2712',
                            'linux-base-rpi-v8', 'linux-image-vendor-rk35xx',
                            'linux-dtb-vendor-rk35xx', 'raspi-firmware',
                            'armbian-bsp-cli-rock-3a-vendor', 'libssl-dev',
                            'linux-headers-rpi-v8']
                def command(args):
                    events.append(args)
                    if args[0] == 'dpkg-query':
                        return ''.join(p + ':arm64\tinstalled\n' for p in packages) + 'linux-image-old\tconfig-files\n'
                    if args[:2] == ['apt-mark', 'showhold']:
                        return '\n'.join(held)
                    if args[:2] == ['apt-mark', 'hold']:
                        held.update(args[2:])
                    if args[0] == 'dpkg-divert':
                        if '--listpackage' in args:
                            return 'LOCAL\n' if args[-1] in diversions else ''
                        if '--truename' in args:
                            return diversions[args[-1]] + '\n'
                        if '--add' in args:
                            diversions[args[-1]] = args[args.index('--divert') + 1]
                    return ''
                with patch.object(module, 'kernel_package_command', side_effect=command):
                    module.protect(root, board, POLICY)
                    first_files = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
                    module.protect(root, board, POLICY)
                self.assertEqual(len([e for e in events if e[:2] == ['apt-mark', 'hold']]), 1)
                self.assertIn('linux-image-rpi-v8:arm64', held)
                self.assertNotIn('libssl-dev:arm64', held)
                self.assertNotIn('linux-headers-rpi-v8:arm64', held)
                self.assertNotIn('linux-image-old', held)
                self.assertEqual(first_files, {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()})
                stub = root / 'usr/sbin/update-initramfs'
                for mode in ('-u', '-c', '-d'):
                    self.assertEqual(subprocess.run([str(stub), mode, '-k', 'all'], capture_output=True).returncode, 0)
                self.assertEqual((root / 'etc/flash-kernel/machine').read_text(), 'none\n')
                if board == 'r3a':
                    self.assertIn('/etc/initramfs/post-update.d/99-uboot', diversions)
                else:
                    config = (root / 'etc/default/raspi-firmware').read_text()
                    self.assertIn('KERNEL=none', config)
                    self.assertIn('INITRAMFS=none', config)

    def test_conflicting_diversion_fails_closed(self):
        module = load_helper()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(module, 'kernel_package_command', return_value='another-package\n'):
                with self.assertRaises(RuntimeError):
                    module.protect(Path(tmp), 'cm4', POLICY)

    def test_unconfigured_boot_package_requires_offline_repair(self):
        module = load_helper()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(module, 'divert'), patch.object(module, 'kernel_package_command',
                    return_value='linux-image-vendor-rk35xx\tunpacked\n'):
                with self.assertRaisesRegex(RuntimeError, 'repair offline'):
                    module.protect(Path(tmp), 'r3a', POLICY)

    @unittest.skipUnless(shutil.which('dpkg-divert'), 'dpkg-divert required')
    def test_real_diversion_preserves_package_copy_and_skips_backup(self):
        module = load_helper()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'var/lib/dpkg').mkdir(parents=True)
            name = '/etc/initramfs/post-update.d/99-uboot'
            hook = root / name.lstrip('/')
            hook.parent.mkdir(parents=True)
            original = '#!/bin/sh\nexit 19\n'
            hook.write_text(original)
            hook.chmod(0o755)
            module.divert(root, name)
            backup = hook.with_name(hook.name + '.manet-distrib')
            self.assertEqual(backup.read_text(), original)
            # dpkg writes an upgraded payload to the diverted path.
            backup.write_text('#!/bin/sh\nexit 27\n')
            module.divert(root, name)
            self.assertEqual(backup.read_text(), '#!/bin/sh\nexit 27\n')
            self.assertEqual(subprocess.run([str(hook)], capture_output=True).returncode, 0)
            selected = subprocess.check_output(['run-parts', '--test', str(hook.parent)], text=True)
            self.assertEqual(selected.strip(), str(hook))

    @unittest.skipUnless(shutil.which('apt-mark'), 'APT required')
    def test_real_holds_work_under_frontend_lock_and_are_idempotent(self):
        module = load_helper()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / 'var/lib/dpkg'
            db.mkdir(parents=True)
            (db / 'status').write_text(
                'Package: linux-image-manet-fixture\n'
                'Status: install ok installed\nArchitecture: amd64\n'
                'Version: 1\nMaintainer: Test <test@example.invalid>\n'
                'Description: fixture\n')
            empty = root / 'empty'
            empty.mkdir()
            config = root / 'apt.conf'
            config.write_text(
                f'Dir::Etc::sourcelist "/dev/null";\n'
                f'Dir::Etc::sourceparts "{empty}";\n'
                f'Dir::Etc::parts "{empty}";\n'
                f'Dir::Etc::main "/dev/null";\n'
                f'Dir::Etc::preferences "/dev/null";\n'
                f'Dir::Etc::preferencesparts "{empty}";\n'
                f'Dir::Cache "{root / "cache"}";\n'
                'DPkg::Options { "--force-not-root"; };\n')
            with (db / 'lock-frontend').open('a') as stream, \
                    patch.dict(os.environ, APT_CONFIG=str(config)), \
                    patch.object(module, 'kernel_package_command', wraps=module.kernel_package_command) as run:
                fcntl.lockf(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                module.protect(root, 'cm4', POLICY)
                module.protect(root, 'cm4', POLICY)
                self.assertEqual(sum(c.args[0][:2] == ['apt-mark', 'hold']
                                     for c in run.call_args_list), 1)
            self.assertIn('Status: hold ok installed', (db / 'status').read_text())


@unittest.skipUnless(shutil.which('apt-get'), 'APT is needed for resolver checks')
class AptResolverTests(unittest.TestCase):
    def test_userland_install_and_upgrade_keep_kernel_but_reject_new_dependency(self):
        # Only metadata is used. No packages are built, downloaded or installed.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / 'repo'
            repo.mkdir()
            def package(name, version='2', depends=''):
                return (f'Package: {name}\nVersion: {version}\nArchitecture: amd64\n'
                        f'Maintainer: Test <test@example.invalid>\nDescription: fixture\n'
                        + (f'Depends: {depends}\n' if depends else ''))
            entries = [('build-essential', 'libc6-dev'),
                       ('libc6-dev', 'linux-libc-dev, libc6 (= 2)'),
                       ('libc6', ''), ('linux-libc-dev', ''), ('libssl-dev', ''),
                       ('linux-image-rpi-v8', 'linux-image-new'),
                       ('linux-image-new', ''), ('kernel-client', 'linux-image-new')]
            (repo / 'Packages').write_text('\n'.join(package(n, depends=d) + f'Filename: {n}.deb\nSize: 1\n' for n, d in entries))
            (root / 'status').write_text('\n'.join(package(n, '1') + 'Status: install ok installed\n' for n in ('linux-image-rpi-v8', 'libc6')))
            (root / 'sources.list').write_text(f'deb [trusted=yes] file:{repo} ./\n')
            (root / 'preferences').write_bytes(POLICY.read_bytes())
            for directory in ('lists/partial', 'cache/archives/partial', 'empty'):
                (root / directory).mkdir(parents=True)
            options = []
            for key, value in {
                    'Dir::Etc::sourcelist': root / 'sources.list',
                    'Dir::Etc::sourceparts': root / 'empty',
                    'Dir::Etc::main': '/dev/null', 'Dir::Etc::parts': root / 'empty',
                    'Dir::Etc::preferences': root / 'preferences',
                    'Dir::Etc::preferencesparts': root / 'empty',
                    'Dir::State::status': root / 'status',
                    'Dir::State::lists': root / 'lists',
                    'Dir::State::extended_states': root / 'extended_states',
                    'Dir::Cache': root / 'cache', 'Debug::NoLocking': '1',
                    'APT::Architecture': 'amd64', 'APT::Sandbox::User': '',
                    }.items():
                options += ['-o', f'{key}={value}']
            env = dict(os.environ, LC_ALL='C', APT_CONFIG='/dev/null')
            def apt(*args):
                return subprocess.run(['apt-get', *options, *args], env=env,
                                      capture_output=True, text=True, timeout=30)
            result = apt('update')
            self.assertEqual(result.returncode, 0, result.stderr)
            for args in (('install', '--no-install-recommends', 'build-essential', 'libssl-dev'), ('upgrade',), ('dist-upgrade',)):
                result = apt('-s', *args)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotRegex(result.stdout, r'(?m)^(Inst|Remv) linux-image')
                if args[0] == 'install':
                    self.assertIn('Inst linux-libc-dev', result.stdout)
                    self.assertIn('Inst libssl-dev', result.stdout)
            self.assertNotEqual(apt('-s', 'install', 'kernel-client').returncode, 0)
            (root / 'preferences').write_text('')
            self.assertIn('Inst linux-image-new', apt('-s', 'dist-upgrade').stdout)


if __name__ == '__main__':
    unittest.main()
