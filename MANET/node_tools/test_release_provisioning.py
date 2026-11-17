"""Run release launchers against local download fixtures without touching disks."""
import hashlib
import io
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

from manet_release import API, DOWNLOADS, PACKAGES, STABLE_MANIFEST

PROVISIONING = Path(__file__).resolve().parents[1] / 'provisioning'
PWSH = os.environ.get('MANET_PWSH') or shutil.which('pwsh')


class ProvisioningReleaseTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='manet-flasher-test-')
        self.addCleanup(scratch.cleanup)
        self.work = Path(scratch.name)
        self.host = self.work / 'host with spaces'
        self.host.mkdir()
        self.launcher = self.host / 'flash-a-radio.sh'
        shutil.copyfile(PROVISIONING / 'flash-a-radio.sh', self.launcher)
        self.cmd = self.host / 'Flash a Radio.cmd'
        shutil.copyfile(PROVISIONING / 'Flash a Radio.cmd', self.cmd)
        self.bin = self.work / 'bin'
        self.bin.mkdir()
        self.map = self.work / 'downloads.json'
        self.events = self.work / 'events'
        curl = self.bin / 'curl'
        curl.write_text(f'''#!{sys.executable}
import json, os, shutil, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['TEST_EVENTS'], 'a') as out: out.write(args[-1] + '\\n')
mapping = json.loads(Path(os.environ['TEST_DOWNLOADS']).read_text())
if args[-1] not in mapping: sys.exit(22)
shutil.copyfile(mapping[args[-1]], args[args.index('--output') + 1])
''')
        curl.chmod(0o755)
        self.environment = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                                TEST_DOWNLOADS=str(self.map), TEST_EVENTS=str(self.events))
        self.bundle = self.work / 'manet-flasher.zip'
        with zipfile.ZipFile(self.bundle, 'w') as archive:
            archive.writestr('linux-flasher.sh', '''#!/bin/bash
read -r answer
python3 -c 'import json,os; print("RAN", json.load(open(os.environ["MANET_RELEASE_FILE"]))["tag"], os.environ["MANET_FLASHER_WORK"])'
echo "INPUT=$answer"
echo "OFFLINE=$MANET_OFFLINE KIT=$MANET_KIT_DIR IMAGE=$MANET_OS_IMAGE"
''')
            archive.writestr('manet-flasher.ps1', '# fixture GUI')
        self.manifest = {'schema': 1, 'version': '0.551', 'tag': 'v0.551', 'commit': 'a' * 40,
                         'assets': {name: {'size': 1, 'sha256': 'b' * 64} for name in PACKAGES}}
        self.manifest['assets']['manet-flasher.zip'] = {
            'size': self.bundle.stat().st_size, 'sha256': hashlib.sha256(self.bundle.read_bytes()).hexdigest()}
        self.manifest_path = self.work / 'manet-release.json'
        self.mapping = {STABLE_MANIFEST: str(self.manifest_path),
                        DOWNLOADS + '/v0.551/manet-flasher.zip': str(self.bundle)}

    def save(self):
        self.map.write_text(json.dumps(self.mapping))
        self.manifest_path.write_text(json.dumps(self.manifest))

    def run_linux(self, *args):
        self.save()
        return subprocess.run(['bash', str(self.launcher), *args], input='interactive answer\n',
                              text=True, capture_output=True, env=self.environment, timeout=15)

    def development(self):
        rows = self.work / 'releases.json'
        rows.write_text(json.dumps([
            {'id': 9, 'tag_name': 'v0.999', 'draft': True, 'prerelease': True,
             'published_at': '2026-09-30T00:00:00Z', 'assets': [{'name': 'manet-release.json'}]},
            {'id': 1, 'tag_name': 'v0.551', 'draft': False, 'prerelease': True,
             'published_at': '2026-09-28T00:00:00Z', 'assets': [{'name': 'manet-release.json'}]},
        ]))
        self.mapping[API + '/releases?per_page=100&page=1'] = str(rows)
        self.mapping[DOWNLOADS + '/v0.551/manet-release.json'] = str(self.manifest_path)

    def test_linux_defaults_to_stable_and_preserves_interactive_input(self):
        result = self.run_linux()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('RAN v0.551', result.stdout)
        self.assertIn('INPUT=interactive answer', result.stdout)
        self.assertEqual(self.events.read_text().splitlines(), [STABLE_MANIFEST, DOWNLOADS + '/v0.551/manet-flasher.zip'])
        self.assertFalse(list(self.host.glob('manet-flasher/.release-*')))

    def test_linux_development_selects_newest_published_bundle(self):
        self.development()
        result = self.run_linux('--development')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('development release 0.551', result.stdout)
        self.assertNotIn(STABLE_MANIFEST, self.events.read_text())

    def test_linux_bad_checksum_stops_before_executing_download(self):
        self.manifest['assets']['manet-flasher.zip']['sha256'] = '0' * 64
        result = self.run_linux()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('RAN', result.stdout)

    def test_linux_checkout_defaults_to_released_scripts_and_local_is_explicit(self):
        local = self.host / 'linux-flasher.sh'
        local.write_text('echo LOCAL\n')
        result = self.run_linux()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('LOCAL', result.stdout)
        result = self.run_linux('--local-scripts')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('LOCAL', result.stdout)
        self.assertEqual(local.read_text(), 'echo LOCAL\n')

    @unittest.skipUnless(PWSH, 'PowerShell is required for Windows launcher execution tests')
    def test_windows_stable_development_and_checksum_rejection(self):
        source = self.cmd.read_text().split('# MANET_POWERSHELL_START\n', 1)[1]
        # Elevation is a Windows OS operation. Exercise the remaining launcher
        # with real PowerShell JSON, hashing and ZIP extraction on this host.
        start = source.index('    $admin =')
        end = source.index('    $work =', start)
        source = source[:start] + source[end:]
        mocks = r'''
function Invoke-RestMethod {
    param($Headers, $TimeoutSec, $Uri)
    $map = Get-Content $env:TEST_DOWNLOADS -Raw | ConvertFrom-Json
    $file = $map.PSObject.Properties[$Uri].Value
    if (-not $file) { throw "Unexpected URL: $Uri" }
    Get-Content $file -Raw | ConvertFrom-Json
}
function Invoke-WebRequest {
    param([switch]$UseBasicParsing, $Headers, $TimeoutSec, $Uri, $OutFile)
    $map = Get-Content $env:TEST_DOWNLOADS -Raw | ConvertFrom-Json
    $file = $map.PSObject.Properties[$Uri].Value
    if (-not $file) { throw "Unexpected URL: $Uri" }
    Copy-Item -LiteralPath $file -Destination $OutFile
}
function powershell.exe {
    $m = Get-Content $env:MANET_RELEASE_FILE -Raw | ConvertFrom-Json
    Write-Output "RAN $($m.tag)"
    $global:LASTEXITCODE = 0
}
'''
        script = self.work / 'launcher-test.ps1'
        script.write_text(mocks + source)
        self.development()
        for development, corrupt in ((False, False), (True, False), (False, True)):
            with self.subTest(development=development, corrupt=corrupt):
                if corrupt:
                    self.manifest['assets']['manet-flasher.zip']['sha256'] = '0' * 64
                self.save()
                env = dict(self.environment, MANET_LAUNCHER=str(self.cmd),
                           MANET_DEVELOPMENT='1' if development else '0', MANET_LOCAL_SCRIPTS='0')
                result = subprocess.run([PWSH, '-NoProfile', '-File', str(script)], env=env,
                                        text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode == 0, not corrupt, result.stdout + result.stderr)
                self.assertEqual('RAN v0.551' in result.stdout, not corrupt)

    def test_first_boot_rejects_bad_checksum_and_wrong_version(self):
        blocks = []
        for name in ('firstrun.sh.template', 'rock3a-provision.sh.template'):
            content = (PROVISIONING / name).read_text()
            start = content.index('# The flasher pins setup')
            end = content.index('\ndone\n', start) + len('\ndone\n')
            blocks.append(content[start:end])
        self.assertEqual(blocks[0], blocks[1])
        package = self.work / 'cm4-install.tar.gz'
        url = DOWNLOADS + '/v0.551/cm4-install.tar.gz'
        self.mapping[url] = str(package)
        for wrong_hash, version in ((False, '0.551'), (True, '0.551'), (False, '0.550')):
            with self.subTest(wrong_hash=wrong_hash, version=version):
                with tarfile.open(package, 'w:gz') as archive:
                    for name in ('./etc/manet_version.txt', './usr/local/bin/version.txt'):
                        data = (version + '\n09/2026\n').encode()
                        info = tarfile.TarInfo(name)
                        info.size = len(data)
                        archive.addfile(info, io.BytesIO(data))
                checksum = '0' * 64 if wrong_hash else hashlib.sha256(package.read_bytes()).hexdigest()
                script = blocks[0].replace('__INSTALL_URL__', url).replace('__RELEASE_VERSION__', '0.551')
                script = script.replace('__INSTALL_SHA256__', checksum).replace('/root/morse-pi-install.tar.gz', str(self.work / 'download.tar.gz'))
                # The first-boot command uses -o; fixture curl accepts --output.
                script = script.replace(' -o ', ' --output ')
                # Unlike updater downloads, the URL precedes the output option.
                script = script.replace('"$MANET_INSTALL_URL" --output ' + str(self.work / 'download.tar.gz'),
                                        '--output ' + str(self.work / 'download.tar.gz') + ' "$MANET_INSTALL_URL"')
                self.save()
                result = subprocess.run(['bash', '-c', script], env=self.environment,
                                        text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode == 0, not wrong_hash and version == '0.551', result.stderr)

    def linux_functions(self, *names):
        """The named functions from the Linux flasher, without its main flow."""
        source = (PROVISIONING / 'linux-flasher.sh').read_text()
        parts = []
        for name in names:
            start = source.index(f'\n{name}() {{\n') + 1
            parts.append(source[start:source.index('\n}\n', start) + 3])
        return '\n'.join(parts)

    def run_bash(self, script, cwd, **env):
        return subprocess.run(['bash', '-c', script], cwd=cwd, text=True, capture_output=True,
                              stdin=subprocess.DEVNULL, env=dict(self.environment, **env), timeout=30)

    def command(self, name, source):
        path = self.bin / name
        path.write_text(source)
        path.chmod(0o755)

    def kit_release(self, offline=True):
        """A release with an offline bundle and image, served by the fixture curl."""
        image_url = 'https://downloads.raspberrypi.com/raspios_lite_arm64/images/test/test-lite.img.xz'
        files = {'cm4-install.tar.gz': b'install package\n', 'cm4-offline.tar': b'offline bundle\n' * 8,
                 'flash-a-radio.sh': (PROVISIONING / 'flash-a-radio.sh').read_bytes(),
                 'Flash-a-Radio.cmd': (PROVISIONING / 'Flash a Radio.cmd').read_bytes()}
        source = self.work / 'release-files'
        source.mkdir(exist_ok=True)
        for name, data in files.items():
            if name == 'cm4-offline.tar' and not offline:
                continue
            (source / name).write_bytes(data)
            self.manifest['assets'][name] = {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            self.mapping[DOWNLOADS + f'/v0.551/{name}'] = str(source / name)
        image = source / 'test-lite.img.xz'
        image.write_bytes(b'raspberry pi os image\n' * 16)
        tools = self.kit_tools(source)
        with zipfile.ZipFile(self.bundle, 'a') as archive:
            archive.writestr('flasher-tools.json', json.dumps({'files': tools}))
        self.manifest['assets']['manet-flasher.zip'] = {
            'size': self.bundle.stat().st_size, 'sha256': hashlib.sha256(self.bundle.read_bytes()).hexdigest()}
        if offline:
            self.manifest['os_image'] = {'url': image_url, 'size': image.stat().st_size,
                                         'sha256': hashlib.sha256(image.read_bytes()).hexdigest()}
        self.mapping[image_url] = str(image)
        return files, image

    def kit_tools(self, source):
        """Pins for two programs, as flasher-tools.json lists them."""
        tools = []
        for tool, platform, name in (('rpi-imager', 'linux-x86_64', 'imager-test.AppImage'),
                                     ('rpi-imager', 'windows', 'imager-test.exe')):
            path = source / name
            path.write_bytes(name.encode() * 8)
            url = f'https://github.com/raspberrypi/rpi-imager/releases/download/v9.9/{name}'
            self.mapping[url] = str(path)
            tools.append({'tool': tool, 'version': '9.9', 'platform': platform, 'name': name, 'url': url,
                          'size': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        return tools

    def test_linux_offline_is_opt_in(self):
        for args, value in (((), '0'), (('--offline',), '1')):
            with self.subTest(args=args):
                result = self.run_linux(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'OFFLINE={value} KIT= IMAGE=\n', result.stdout)

    def test_linux_kit_is_filled_once_and_runs_without_network(self):
        files, image = self.kit_release()
        kit = self.work / 'usb stick' / 'manet-kit'
        result = self.run_linux('--make-kit', str(kit))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('RAN', result.stdout)
        for name, data in files.items():
            self.assertEqual((kit / name).read_bytes(), data)
        self.assertEqual((kit / 'test-lite.img.xz').read_bytes(), image.read_bytes())
        for name in ('imager-test.AppImage', 'imager-test.exe'):
            self.assertEqual((kit / 'tools' / name).read_bytes(), name.encode() * 8)
        self.assertEqual(json.loads((kit / 'manet-release.json').read_text())['tag'], 'v0.551')
        self.assertIn('release 0.551', (kit / 'MANET-OFFLINE-KIT.txt').read_text())
        self.assertIn('1.9 or later', (kit / 'MANET-OFFLINE-KIT.txt').read_text())
        self.assertFalse(list(kit.glob('**/*.partial')))
        # A second run checks what is there and downloads only the manifest.
        self.events.write_text('')
        result = self.run_linux('--make-kit', str(kit))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.events.read_text().splitlines(), [STABLE_MANIFEST])
        # On the offline computer any network use fails the run.
        self.command('curl', '#!/bin/bash\necho NETWORK >> "$TEST_EVENTS"\nexit 7\n')
        self.events.write_text('')
        run = subprocess.run(['bash', str(kit / 'flash-a-radio.sh')], input='answer\n', text=True,
                             capture_output=True, env=self.environment, timeout=15)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('RAN v0.551', run.stdout)
        self.assertIn(f'OFFLINE=1 KIT={kit} IMAGE={kit}/test-lite.img.xz\n', run.stdout)
        self.assertEqual(self.events.read_text(), '')
        # A damaged image stops the kit before the flasher starts.
        (kit / 'test-lite.img.xz').write_bytes(b'damaged')
        run = subprocess.run(['bash', str(kit / 'flash-a-radio.sh')], input='answer\n', text=True,
                             capture_output=True, env=self.environment, timeout=15)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('missing or damaged', run.stderr)
        self.assertNotIn('RAN', run.stdout)

    def test_linux_kit_needs_a_release_with_an_offline_bundle(self):
        self.kit_release(offline=False)
        kit = self.work / 'kit'
        result = self.run_linux('--make-kit', str(kit))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('has no offline bundle', result.stderr)
        self.assertFalse((kit / 'MANET-OFFLINE-KIT.txt').exists())

    def test_linux_kit_needs_pinned_programs(self):
        self.kit_release()
        source = self.work / 'release-files'
        bad = self.kit_tools(source)
        bad[0]['url'] = 'https://example.com/imager-test.AppImage'
        for pins, message in ((None, 'does not name the programs'), (bad, 'Invalid program')):
            with self.subTest(message=message):
                with zipfile.ZipFile(self.bundle, 'w') as archive:
                    archive.writestr('linux-flasher.sh', '#!/bin/bash\n')
                    if pins:
                        archive.writestr('flasher-tools.json', json.dumps({'files': pins}))
                self.manifest['assets']['manet-flasher.zip'] = {
                    'size': self.bundle.stat().st_size,
                    'sha256': hashlib.sha256(self.bundle.read_bytes()).hexdigest()}
                kit = self.work / ('kit-' + message.split()[0])
                result = self.run_linux('--make-kit', str(kit))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertFalse((kit / 'MANET-OFFLINE-KIT.txt').exists())
                self.assertNotIn('example.com', self.events.read_text())

    def test_linux_release_files_are_verified_cached_and_taken_from_a_kit(self):
        package = self.work / 'cm4-offline.tar'
        package.write_bytes(b'offline bundle fixture\n')
        digest = hashlib.sha256(package.read_bytes()).hexdigest()
        url = DOWNLOADS + '/v0.551/cm4-offline.tar'
        self.mapping[url] = str(package)
        self.save()
        script = (self.linux_functions('fetch_release_file') +
                  f'\nOFFLINE_FILE_DIR=packages\nfetch_release_file {url} "$DIGEST" || exit 1\n'
                  'echo "FILE=$RELEASE_FILE"\n')
        for checksum, expect_ok in (('0' * 64, False), (digest, True), (digest, True)):
            with self.subTest(checksum=checksum):
                self.events.write_text('')
                result = self.run_bash(script, self.host, DIGEST=checksum, RELEASE_VERSION='0.551')
                self.assertEqual(result.returncode == 0, expect_ok, result.stdout + result.stderr)
                self.assertFalse(list((self.host / 'packages').glob('*.partial')))
                cached = self.host / 'packages/cm4-offline.tar'
                self.assertEqual(cached.exists(), expect_ok)
                if expect_ok:
                    self.assertIn(f'FILE={cached}', result.stdout)
        # The first good run downloaded. The second used the verified copy.
        self.assertEqual(self.events.read_text(), '')
        kit = self.work / 'kit'
        kit.mkdir()
        shutil.copyfile(package, kit / 'cm4-offline.tar')
        for data, expect_ok in ((None, True), (b'damaged', False)):
            with self.subTest(kit_copy=data):
                if data:
                    (kit / 'cm4-offline.tar').write_bytes(data)
                result = self.run_bash(script, self.host, DIGEST=digest, MANET_KIT_DIR=str(kit),
                                       KIT_DIR=str(kit), RELEASE_VERSION='0.551')
                self.assertEqual(result.returncode == 0, expect_ok, result.stdout + result.stderr)
                if expect_ok:
                    self.assertIn(f'FILE={kit}/cm4-offline.tar', result.stdout)
                else:
                    self.assertIn('Make the kit again', result.stderr)
                self.assertEqual(self.events.read_text(), '')


    def test_linux_kit_run_offers_no_installs(self):
        """A computer running a kit is told what to install, never offered a download."""
        stub = 'required_tools_for_board() { echo manet-missing-tool; }\ndetect_pkg_manager() { PKG_MGR=apt; PKG_INSTALL="sudo apt-get install -y"; }\n'
        script = (self.linux_functions('preflight_dependencies', 'pkg_for_tool', 'hint_for_tool',
                                       'why_tool_is_needed') + stub +
                  'preflight_dependencies cm4 && echo PASSED || echo STOPPED\n')
        result = self.run_bash(script, self.host, KIT_DIR=str(self.work))
        self.assertIn('STOPPED', result.stdout)
        self.assertIn('Running from an offline kit', result.stdout)
        self.assertNotIn('[Y/n]', result.stdout)

    def card_fixture(self):
        """Block-device commands that act on directories instead of a card."""
        self.card = self.work / 'card'
        self.card.mkdir()
        self.command('sudo', '#!/bin/bash\nexec "$@"\n')
        for name in ('blockdev', 'udevadm'):
            self.command(name, '#!/bin/bash\nexit 0\n')
        self.command('lsblk', '#!/bin/bash\nprintf "/dev/sdz disk\\n${TEST_PART-/dev/sdz1 part}\\n"\n')
        self.command('blkid', '#!/bin/bash\necho "${TEST_FSTYPE:-vfat}"\n')
        self.command('mount', '#!/bin/bash\necho "MOUNT ${@: -1}" >> "$TEST_EVENTS"\n')
        # Unmounting moves what was written to the card, leaving the mount
        # point empty for the caller to remove.
        self.command('umount', '#!/bin/bash\necho UMOUNT >> "$TEST_EVENTS"\n'
                               'find "$1" -mindepth 1 -maxdepth 1 -exec mv -t "$TEST_CARD" {} +\n')
        self.command('df', '#!/bin/bash\necho Avail\necho "${TEST_AVAIL:-500000000}"\n')
        # A copy whose destination names TEST_FAIL fails, as a full card would.
        self.command('cp', '#!/bin/bash\n[ -n "$TEST_FAIL" ] && [[ "${@: -1}" == *"$TEST_FAIL"* ]] && exit 1\n'
                           'exec /bin/cp "$@"\n')
        files = []
        for name in ('cm4-install.tar.gz', 'cm4-offline.tar'):
            path = self.work / name
            path.write_bytes(name.encode() * 64)
            files.append(path)
        return files

    def test_linux_card_copy_lands_whole_or_not_at_all(self):
        files = self.card_fixture()
        digests = [hashlib.sha256(path.read_bytes()).hexdigest() for path in files]
        script = (self.linux_functions('copy_files_to_card') +
                  f'\ncopy_files_to_card /dev/sdz "{digests[0]} {files[0]}" "${{SECOND:-{digests[1]}}} {files[1]}"\n')
        cases = (({}, True, True), ({'TEST_AVAIL': '1000'}, False, True),
                 ({'TEST_FAIL': 'cm4-offline.tar'}, False, True),
                 ({'SECOND': '0' * 64}, False, True),
                 ({'TEST_FSTYPE': 'ext4'}, False, False), ({'TEST_PART': ''}, False, False))
        for env, copied, mounted in cases:
            with self.subTest(env=env):
                for item in self.card.iterdir():
                    item.unlink()
                self.events.write_text('')
                temp = self.work / 'tmp'
                shutil.rmtree(temp, ignore_errors=True)
                temp.mkdir()
                result = self.run_bash(script, self.host, TEST_CARD=str(self.card),
                                       TMPDIR=str(temp), **env)
                self.assertEqual(result.returncode == 0, copied, result.stdout + result.stderr)
                names = sorted(item.name for item in self.card.iterdir())
                self.assertEqual(names, ['cm4-install.tar.gz', 'cm4-offline.tar'] if copied else [])
                for path in files if copied else ():
                    self.assertEqual((self.card / path.name).read_bytes(), path.read_bytes())
                events = self.events.read_text()
                self.assertEqual('MOUNT' in events, mounted)
                self.assertEqual('UMOUNT' in events, mounted)
                self.assertEqual(list(temp.iterdir()), [])

    def test_linux_imager_eject_flag_follows_imager_support(self):
        imager = self.work / 'rpi-imager'
        # linux-flasher.sh runs under set -e, and the probe always exits nonzero.
        script = ('set -e\n' + self.linux_functions('choose_imager_eject_args', 'imager_ask',
                                                     'imager_keeps_card') +
                  f'\nRPI_IMAGER_CMD={imager}\nchoose_imager_eject_args\n'
                  'echo "ARGS=${RPI_IMAGER_ARGS[*]}"\n')
        for reply, args in (("Unknown option 'disable-eject'.", ''), ('Usage: rpi-imager', '--disable-eject')):
            with self.subTest(reply=reply):
                imager.write_text(f'#!/bin/bash\necho "{reply}" >&2\nexit 1\n')
                imager.chmod(0o755)
                result = self.run_bash(script, self.host)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'ARGS={args}\n', result.stdout)

    def fake_imager(self, path, version, keeps_card):
        """An rpi-imager that answers version and option probes like the real one."""
        reject = '' if keeps_card else (
            'case " $* " in *" --disable-eject "*) echo "Unknown option \'disable-eject\'." >&2; exit 1;; esac\n')
        path.write_text('#!/bin/bash\n'
                        f'[ "$1" = --version ] && {{ echo "rpi-imager version {version}" >&2; exit 0; }}\n'
                        + reject + 'echo "Usage: rpi-imager" >&2\nexit 1\n')
        path.chmod(0o755)

    def test_linux_offline_needs_an_imager_that_keeps_the_card(self):
        """Offline setup refuses an imager that ejects the card. A kit run
        unpacks the kit's checked imager into the local cache, using no
        network. A normal flash keeps the system imager."""
        system = self.bin / 'rpi-imager'
        self.fake_imager(system, '1.8.5', keeps_card=False)
        # The kit's AppImage unpacks to an imager that keeps the card.
        appimage = self.work / 'imager-test.AppImage'
        inner = self.work / 'inner-imager'
        self.fake_imager(inner, '2.0.12', keeps_card=True)
        appimage.write_text('#!/bin/bash\n[ "$1" = --appimage-extract ] || exit 1\n'
                            f'mkdir -p squashfs-root && cp {inner} squashfs-root/AppRun\n')
        kit = self.work / 'kit'
        (kit / 'tools').mkdir(parents=True)
        shutil.copyfile(appimage, kit / 'tools' / appimage.name)
        pins = self.work / 'flasher-tools.json'
        sha = hashlib.sha256(appimage.read_bytes()).hexdigest()
        pins.write_text(json.dumps({'files': [
            {'tool': 'rpi-imager', 'version': '2.0.12', 'platform': platform, 'name': appimage.name,
             'url': f'https://github.com/raspberrypi/rpi-imager/releases/download/v2.0.12/{appimage.name}',
             'size': appimage.stat().st_size, 'sha256': sha}
            for platform in ('linux-x86_64', 'linux-aarch64')]}))
        names = ('resolve_rpi_imager', 'install_imager', 'tool_pin', 'imager_platform', 'imager_need',
                 'imager_ask', 'imager_version_of', 'imager_keeps_card', 'imager_state_of',
                 'pkg_candidate_version', 'choose_imager_eject_args')
        one_liners = ''.join(line + '\n' for line in (PROVISIONING / 'linux-flasher.sh').read_text().splitlines()
                             if line.startswith(('local_imager_path() {', 'version_ge() {')))
        script = ('set -e\n' + self.linux_functions(*names) + one_liners +
                  '\nfetch_url() { echo NETWORK >> "$TEST_EVENTS"; return 1; }\n'
                  'RPI_IMAGER_MIN_VERSION=1.8.0\nRPI_IMAGER_OFFLINE_MIN_VERSION=1.9.0\n'
                  'resolve_rpi_imager || { echo STOPPED; exit 0; }\n'
                  '[ "$OFFLINE" != 1 ] || choose_imager_eject_args\n'
                  'echo "CMD=$RPI_IMAGER_CMD ARGS=${RPI_IMAGER_ARGS[*]}"\n')
        cache = self.work / 'cache' / 'imager'
        cases = (
            ('normal flash keeps the system imager', dict(OFFLINE='0'), f'CMD={system} ARGS=\n', False),
            ('kit unpacks its own imager', dict(OFFLINE='1', KIT_DIR=str(kit)),
             f'CMD={cache}/squashfs-root/AppRun ARGS=--disable-eject\n', True),
            ('unpacked copy is reused', dict(OFFLINE='1', KIT_DIR=str(kit)),
             f'CMD={cache}/squashfs-root/AppRun ARGS=--disable-eject\n', False),
            ('connected run without a terminal stops', dict(OFFLINE='1', KIT_DIR=''), 'STOPPED', True),
        )
        for label, env, expected, explained in cases:
            with self.subTest(label):
                self.events.write_text('')
                result = self.run_bash(script, self.host, FLASHER_TOOLS_FILE=str(pins),
                                       RPI_IMAGER_LOCAL_DIR=str(cache if env.get('KIT_DIR') else self.host / 'imager'),
                                       **env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(expected, result.stdout)
                self.assertEqual(self.events.read_text(), '')
                self.assertEqual('ejects the card' in result.stdout, explained, result.stdout)
        # A damaged kit copy is refused and nothing is left unpacked.
        shutil.rmtree(cache)
        (kit / 'tools' / appimage.name).write_text('damaged')
        result = self.run_bash(script, self.host, FLASHER_TOOLS_FILE=str(pins), RPI_IMAGER_LOCAL_DIR=str(cache),
                               OFFLINE='1', KIT_DIR=str(kit))
        self.assertIn('STOPPED', result.stdout)
        self.assertIn('does not match its checksum. Make the kit again', result.stdout)
        self.assertIn('Install rpi-imager 1.9.0 or newer', result.stdout)
        self.assertFalse(cache.exists())
        self.assertEqual(self.events.read_text(), '')

    def test_linux_flash_without_option_is_unchanged(self):
        """Without --offline the imager runs as it always has. With it, a card
        that cannot take the offline files fails the run."""
        imager = self.work / 'rpi-imager'
        self.command('sudo', '#!/bin/bash\nprintf "%s\\n" "$*" >> "$TEST_EVENTS"\n')
        stubs = ''.join(f'{name}() {{ :; }}\n' for name in
                        ('validate_ssh_username', 'prepare_release', 'append_additional_scripts',
                         'prepare_flash_target'))
        stubs += ('copy_files_to_card() { echo "COPY $*" >> "$TEST_EVENTS"; '
                  '[ -z "$TEST_COPY_FAIL" ]; }\n')
        script = 'set -e\n' + self.linux_functions('flash_rpi') + stubs + 'flash_rpi /dev/sdz\n'
        script_file = self.work / 'firstrun.sh'
        env = dict(RPI_IMAGER_CMD=str(imager), PI_OS_IMAGE_URL='https://os.example/image.img.xz',
                   TEMPLATE_FILE=str(PROVISIONING / 'firstrun.sh.template'),
                   TEMP_SCRIPT_FILE=str(script_file))
        base = f'{imager} --cli https://os.example/image.img.xz /dev/sdz --first-run-script {script_file}'
        offline = ('RPI_IMAGER_ARGS=(--disable-eject)\nOFFLINE_FILES=("1 /k/a.tar.gz" "2 /k/b.tar")\n'
                   f"OFFLINE_TOKEN={'c' * 64}\n")
        for option, prefix, fail in (('0', '', ''), ('1', offline, ''), ('1', offline, '1')):
            with self.subTest(option=option, fail=fail):
                self.events.write_text('')
                result = self.run_bash(prefix + script, self.host, OFFLINE=option,
                                       TEST_COPY_FAIL=fail, **env)
                self.assertEqual(result.returncode == 0, not fail, result.stdout + result.stderr)
                events = self.events.read_text().splitlines()
                if option == '0':
                    self.assertEqual(events, [base])
                    self.assertNotIn('offline', result.stdout.lower())
                    self.assertNotIn('c' * 64, script_file.read_text())
                else:
                    self.assertEqual(events, [base.replace('--cli', '--cli --disable-eject'),
                                              'COPY /dev/sdz 1 /k/a.tar.gz 2 /k/b.tar'])
                    self.assertIn("'" + 'c' * 64 + "'", script_file.read_text())
                    if fail:
                        self.assertIn('NOT ready for offline setup', result.stderr)
                        self.assertNotIn('Flash complete', result.stdout)
                    else:
                        self.assertIn('Offline setup: the card carries everything', result.stdout)

    def windows_functions(self, *names):
        return r'''
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($env:TEST_ENGINE, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($name in @(''' + ', '.join(f"'{name}'" for name in names) + r''')) {
    $function = $ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name}, $true)
    Invoke-Expression $function.Extent.Text
}
'''

    def run_pwsh(self, script, **env):
        path = self.work / 'engine-test.ps1'
        path.write_text(script)
        return subprocess.run([PWSH, '-NoProfile', '-File', str(path)], text=True,
                              capture_output=True, timeout=60,
                              env=dict(self.environment, TEST_ENGINE=str(PROVISIONING / 'windows.ps1'),
                                       MANET_RELEASE_FILE=str(self.manifest_path), **env))

    @unittest.skipUnless(PWSH, 'PowerShell is required for Windows engine execution tests')
    def test_windows_offline_files_are_verified_cached_and_taken_from_a_kit(self):
        files, _ = self.kit_release()
        script = self.windows_functions('Get-ReleaseInstallAsset', 'Get-ReleaseFile', 'Get-OfflineFiles') + r'''
function Invoke-WebRequest {
    param([switch]$UseBasicParsing, $Uri, $OutFile)
    Add-Content -LiteralPath $env:TEST_EVENTS -Value $Uri
    $map = Get-Content $env:TEST_DOWNLOADS -Raw | ConvertFrom-Json
    Copy-Item -LiteralPath $map.PSObject.Properties[$Uri].Value -Destination $OutFile
}
$OFFLINE_FILE_DIR = Join-Path $env:TEST_WORK 'packages'
$Script:HARDWARE_MODEL = $env:TEST_BOARD
try { Write-Output "FILES=$((Get-OfflineFiles | ForEach-Object { $_.Path + '=' + $_.Sha256 }) -join '|')" } catch { Write-Output "FAILED: $($_.Exception.Message)" }
'''
        packages = self.host / 'packages'
        good = self.manifest['assets']['cm4-offline.tar']['sha256']
        cases = (('0' * 64, 'rpi4', '', 'does not match'), (good, 'rpi4', '', 'FILES='),
                 (good, 'rpi4', '', 'FILES='), (good, 'r3a', '', 'Compute Module 4'))
        for index, (checksum, board, kit, expected) in enumerate(cases):
            with self.subTest(case=index):
                self.manifest['assets']['cm4-offline.tar']['sha256'] = checksum
                self.save()
                self.events.write_text('')
                result = self.run_pwsh(script, TEST_WORK=str(self.host), TEST_BOARD=board)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(expected, result.stdout)
                self.assertFalse(list(packages.glob('*.partial')))
                if index == 2:
                    # The second good run found both verified copies.
                    self.assertEqual(self.events.read_text(), '')
                    self.assertIn(str(packages / 'cm4-offline.tar'), result.stdout)
        kit = self.work / 'kit'
        kit.mkdir()
        for name in ('cm4-install.tar.gz', 'cm4-offline.tar'):
            (kit / name).write_bytes(files[name])
        self.events.write_text('')
        result = self.run_pwsh(script, TEST_WORK=str(self.host), TEST_BOARD='rpi4', MANET_KIT_DIR=str(kit))
        install, offline = (self.manifest['assets'][n]['sha256'] for n in ('cm4-install.tar.gz', 'cm4-offline.tar'))
        self.assertIn(f'FILES={kit}/cm4-install.tar.gz={install}|{kit}/cm4-offline.tar={offline}', result.stdout)
        (kit / 'cm4-offline.tar').write_bytes(b'damaged')
        result = self.run_pwsh(script, TEST_WORK=str(self.host), TEST_BOARD='rpi4', MANET_KIT_DIR=str(kit))
        self.assertIn('Make the kit again', result.stdout)
        self.assertEqual(self.events.read_text(), '')

    @unittest.skipUnless(PWSH, 'PowerShell is required for Windows engine execution tests')
    def test_windows_card_copy_and_eject_flag(self):
        card = self.work / 'card'
        card.mkdir()
        files = []
        for name in ('cm4-install.tar.gz', 'cm4-offline.tar'):
            path = self.work / name
            path.write_bytes(name.encode() * 64)
            files.append(path)
        self.save()
        script = self.windows_functions('Get-RpiImagerExtraArgs', 'Copy-FilesToCard') + r'''
# Storage cmdlets over a directory standing in for the card's boot partition.
New-PSDrive -Name Z -PSProvider FileSystem -Root $env:TEST_CARD | Out-Null
$global:Lettered = $env:TEST_LETTERED -eq '1'
$global:Removed = $false
function Update-HostStorageCache { }
function Start-Sleep { }
function Write-VolumeCache { }
function Get-Partition {
    if ($env:TEST_NO_PARTITION -eq '1') { return }
    [pscustomobject]@{ PartitionNumber = 1; DriveLetter = $(if ($global:Lettered) { [char]'Z' } else { [char]0 }) }
}
function Add-PartitionAccessPath { $global:Lettered = $true }
function Remove-PartitionAccessPath { $global:Removed = $true }
function Get-Volume { [pscustomobject]@{ FileSystem = $env:TEST_FS; SizeRemaining = [int64]$env:TEST_AVAIL } }
$files = @([pscustomobject]@{ Path = $env:TEST_FILE1; Sha256 = $env:TEST_SHA1 },
           [pscustomobject]@{ Path = $env:TEST_FILE2; Sha256 = $env:TEST_SHA2 })
if ($env:TEST_MISSING -eq '1') { $files += [pscustomobject]@{ Path = (Join-Path $env:TEST_CARD 'no-such-file'); Sha256 = '' } }
$problem = Copy-FilesToCard -DiskNumber 3 -Files $files
Write-Output "PROBLEM=[$problem] REMOVED=$global:Removed"
$cli = Join-Path $env:TEST_CARD 'rpi-imager-cli.cmd'
Write-Output "CMD=$(Get-RpiImagerExtraArgs $cli)"
Write-Output "OLD=$(Get-RpiImagerExtraArgs (Join-Path $env:TEST_CARD 'missing.exe'))"
'''
        cases = ((dict(TEST_LETTERED='1'), '', 'False'),
                 (dict(TEST_LETTERED='0'), '', 'True'),
                 (dict(TEST_LETTERED='0', TEST_AVAIL='1000'), 'too little free space', 'True'),
                 (dict(TEST_LETTERED='1', TEST_FS='NTFS'), 'not the boot partition', 'False'),
                 (dict(TEST_LETTERED='1', TEST_MISSING='1'), 'no-such-file', 'False'),
                 (dict(TEST_LETTERED='1', TEST_SHA2='0' * 64), 'does not match the release checksum', 'False'),
                 (dict(TEST_NO_PARTITION='1'), 'ejected', 'False'))
        for env, problem, removed in cases:
            with self.subTest(env=env):
                for item in card.iterdir():
                    item.unlink()
                env = dict(dict(TEST_FS='FAT32', TEST_AVAIL=str(10 ** 9),
                                TEST_SHA1=hashlib.sha256(files[0].read_bytes()).hexdigest(),
                                TEST_SHA2=hashlib.sha256(files[1].read_bytes()).hexdigest()), **env)
                result = self.run_pwsh(script, TEST_CARD=str(card), TEST_FILE1=str(files[0]),
                                       TEST_FILE2=str(files[1]), **env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f'REMOVED={removed}', result.stdout)
                self.assertIn('CMD=--disable-eject', result.stdout)
                self.assertIn('OLD=\n', result.stdout + '\n')
                names = sorted(item.name for item in card.iterdir())
                if problem:
                    self.assertIn(problem, result.stdout)
                    self.assertEqual(names, [])
                else:
                    self.assertIn('PROBLEM=[]', result.stdout)
                    self.assertEqual(names, ['cm4-install.tar.gz', 'cm4-offline.tar'])
                    for path in files:
                        self.assertEqual((card / path.name).read_bytes(), path.read_bytes())

    @unittest.skipUnless(PWSH, 'PowerShell is required for Windows engine execution tests')
    def test_windows_flash_without_option_is_unchanged(self):
        """Without --offline the imager gets the same arguments as before."""
        imager = self.work / 'rpi-imager'
        imager.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$TEST_EVENTS"\n')
        imager.chmod(0o755)
        script = self.windows_functions('Invoke-RpiImagerFlash') + r'''
function Get-RpiImagerExtraArgs { @('--disable-eject') }
function Remove-CachedToolPath { }
$OS_IMAGE_URL = 'https://os.example/image.img.xz'
$Script:RPI_IMAGER_PATH = $env:TEST_IMAGER
$Script:OFFLINE = $env:TEST_OPTION -eq '1'
$result = Invoke-RpiImagerFlash -DiskNumber 3 -ScriptPath '/tmp/firstrun.sh'
Write-Output "OK=$($result.Ok)"
'''
        base = r'--cli https://os.example/image.img.xz \\.\PhysicalDrive3 --first-run-script /tmp/firstrun.sh'
        for option, expected in (('0', base), ('1', base.replace('--cli', '--cli --disable-eject'))):
            with self.subTest(option=option):
                self.events.write_text('')
                result = self.run_pwsh(script, TEST_IMAGER=str(imager), TEST_OPTION=option)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('OK=True', result.stdout)
                self.assertEqual(self.events.read_text().splitlines(), [expected])

    def windows_launcher(self, mocks):
        """The Windows launcher's PowerShell part, without the UAC relaunch."""
        source = self.cmd.read_text().split('# MANET_POWERSHELL_START\n', 1)[1]
        start = source.index('    $admin =')
        end = source.index('    $work =', start)
        script = self.work / 'launcher-test.ps1'
        script.write_text(mocks + source[:start] + source[end:])
        return script

    @unittest.skipUnless(PWSH, 'PowerShell is required for Windows launcher execution tests')
    def test_windows_kit_is_filled_once_and_runs_without_network(self):
        files, image = self.kit_release()
        self.save()
        script = self.windows_launcher(r'''
function Invoke-RestMethod {
    param($Headers, $TimeoutSec, $Uri)
    Add-Content -LiteralPath $env:TEST_EVENTS -Value $Uri
    if ($env:TEST_OFFLINE -eq '1') { throw 'network used' }
    $map = Get-Content $env:TEST_DOWNLOADS -Raw | ConvertFrom-Json
    Get-Content $map.PSObject.Properties[$Uri].Value -Raw | ConvertFrom-Json
}
function Invoke-WebRequest {
    param([switch]$UseBasicParsing, $Headers, $TimeoutSec, $Uri, $OutFile)
    Add-Content -LiteralPath $env:TEST_EVENTS -Value $Uri
    if ($env:TEST_OFFLINE -eq '1') { throw 'network used' }
    $map = Get-Content $env:TEST_DOWNLOADS -Raw | ConvertFrom-Json
    Copy-Item -LiteralPath $map.PSObject.Properties[$Uri].Value -Destination $OutFile
}
function powershell.exe {
    Write-Output "RAN OFFLINE=$env:MANET_OFFLINE KIT=$env:MANET_KIT_DIR IMAGE=$env:MANET_OS_IMAGE"
    $global:LASTEXITCODE = 0
}
''')
        kit = self.work / 'usb stick' / 'manet-kit'

        def launch(launcher, **env):
            return subprocess.run([PWSH, '-NoProfile', '-File', str(script)], text=True, capture_output=True,
                                  timeout=60, env=dict(self.environment, MANET_LAUNCHER=str(launcher),
                                                       MANET_DEVELOPMENT='0', MANET_LOCAL_SCRIPTS='0',
                                                       MANET_OFFLINE='0', **env))

        result = launch(self.cmd, MANET_MAKE_KIT=str(kit))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name, data in files.items():
            self.assertEqual((kit / name).read_bytes(), data)
        self.assertEqual((kit / 'test-lite.img.xz').read_bytes(), image.read_bytes())
        for name in ('imager-test.AppImage', 'imager-test.exe'):
            self.assertEqual((kit / 'tools' / name).read_bytes(), name.encode() * 8)
        self.assertIn('release 0.551', (kit / 'MANET-OFFLINE-KIT.txt').read_text())
        self.assertIn('1.9 or later', (kit / 'MANET-OFFLINE-KIT.txt').read_text())
        self.events.write_text('')
        result = launch(self.cmd, MANET_MAKE_KIT=str(kit))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.events.read_text().splitlines(), [STABLE_MANIFEST])
        self.events.write_text('')
        result = launch(kit / 'Flash-a-Radio.cmd', TEST_OFFLINE='1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f'RAN OFFLINE=1 KIT={kit} IMAGE={kit}/test-lite.img.xz', result.stdout)
        self.assertEqual(self.events.read_text(), '')
        (kit / 'test-lite.img.xz').write_bytes(b'damaged')
        result = launch(kit / 'Flash-a-Radio.cmd', TEST_OFFLINE='1')
        self.assertNotIn('RAN', result.stdout)
        self.assertIn('missing or damaged', result.stdout)

    @unittest.skipUnless(PWSH, 'PowerShell is required for Windows engine execution tests')
    def test_windows_kit_tools_are_checked_before_use(self):
        kit = self.work / 'kit'
        (kit / 'tools').mkdir(parents=True)
        tools = self.kit_tools(kit / 'tools')
        pins = self.work / 'flasher-tools.json'
        pins.write_text(json.dumps({'files': tools}))
        self.save()
        script = self.windows_functions('Get-KitToolPath') + r'''
$FLASHER_TOOLS_FILE = $env:TEST_PINS
$found = Get-KitToolPath -Tool $env:TEST_TOOL
Write-Output "FOUND=$(if ($found) { $found.Path + '|' + $found.Version } else { 'none' })"
'''
        exe = kit / 'tools' / 'imager-test.exe'
        good = exe.read_bytes()
        cases = (('rpi-imager', str(kit), None, f'FOUND={exe}|9.9'),
                 ('rpiboot', str(kit), None, 'FOUND=none'),
                 ('rpi-imager', '', None, 'FOUND=none'),
                 ('rpi-imager', str(kit), b'damaged', 'damaged. Make the kit again'))
        for tool, kit_dir, data, expected in cases:
            with self.subTest(tool=tool, kit=bool(kit_dir), damaged=bool(data)):
                exe.write_bytes(data or good)
                result = self.run_pwsh(script, TEST_PINS=str(pins), TEST_TOOL=tool, MANET_KIT_DIR=kit_dir)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(expected, result.stdout)
                if data:
                    self.assertIn('FOUND=none', result.stdout)

    @unittest.skipUnless(PWSH, 'PowerShell is required for Windows front end tests')
    def test_windows_window_offers_an_imager_update_for_offline_setup(self):
        """An imager that ejects the card is fine for a normal flash but gets
        an Update button, and counts as missing when started for offline setup."""
        script = self.work / 'prereq.ps1'
        script.write_text(r'''
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($env:TEST_GUI, [ref]$tokens, [ref]$errors)
$function = $ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Test-Prereq'}, $true)
Invoke-Expression $function.Extent.Text
function Find-InstalledProgram { 'C:\Imager\rpi-imager.exe' }
function Get-RpiImagerExtraArgs { if ($env:TEST_KEEPS -eq '1') { @('--disable-eject') } else { @() } }
$item = [pscustomobject]@{ Key = 'rpi-imager'; Found = $false; Upgrade = $false; Detail = ''; Path = $null }
[void](Test-Prereq $item)
Write-Output "FOUND=$($item.Found) UPGRADE=$($item.Upgrade) DETAIL=$($item.Detail)"
''')
        cases = (('1', '0', 'FOUND=True UPGRADE=False'), ('0', '0', 'FOUND=True UPGRADE=True'),
                 ('0', '1', 'FOUND=False UPGRADE=True'), ('1', '1', 'FOUND=True UPGRADE=False'))
        for keeps, offline, expected in cases:
            with self.subTest(keeps=keeps, offline=offline):
                result = subprocess.run([PWSH, '-NoProfile', '-File', str(script)], text=True, capture_output=True,
                                        timeout=60, env=dict(self.environment, TEST_KEEPS=keeps, MANET_OFFLINE=offline,
                                                             TEST_GUI=str(PROVISIONING / 'manet-flasher.ps1')))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(expected, result.stdout)
                self.assertEqual('1.9 or later' in result.stdout, keeps == '0')

    @unittest.skipUnless(PWSH, 'PowerShell is required for Windows template execution tests')
    def test_windows_offline_token_follows_the_option(self):
        self.kit_release()
        self.save()
        script = self.work / 'offline-token.ps1'
        script.write_text(r'''
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($env:TEST_ENGINE, [ref]$tokens, [ref]$errors)
foreach ($name in @('Test-SshUsername', 'Get-ReleaseInstallAsset', 'Expand-ProvisioningTokens')) {
    $function = $ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name}, $true)
    Invoke-Expression $function.Extent.Text
}
$Script:RADIO_USER = 'radio'
$Script:HARDWARE_MODEL = 'rpi4'
$Script:OFFLINE = $env:TEST_OPTION -eq '1'
Expand-ProvisioningTokens "pin='__OFFLINE_SHA256__'"
''')
        for option, expected in (('0', "pin=''"), ('1', f"pin='{self.manifest['assets']['cm4-offline.tar']['sha256']}'")):
            with self.subTest(option=option):
                result = subprocess.run([PWSH, '-NoProfile', '-File', str(script)], text=True, capture_output=True,
                                        timeout=60, env=dict(self.environment, TEST_OPTION=option,
                                                             MANET_RELEASE_FILE=str(self.manifest_path),
                                                             TEST_ENGINE=str(PROVISIONING / 'windows.ps1')))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)

    @unittest.skipUnless(PWSH, 'PowerShell is required for Windows template execution tests')
    def test_windows_templates_receive_pinned_release_and_parse(self):
        self.save()
        script = self.work / 'tokens.ps1'
        script.write_text(r'''
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($env:TEST_ENGINE, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($name in @('Test-SshUsername', 'Get-ReleaseInstallAsset', 'Expand-ProvisioningTokens')) {
    $function = $ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name}, $true)
    Invoke-Expression $function.Extent.Text
}
$Script:RADIO_USER = 'field_ops-2'
$Script:HARDWARE_MODEL = $env:TEST_BOARD
Expand-ProvisioningTokens (Get-Content $env:TEST_TEMPLATE -Raw)
''')
        for board, template in (('rpi4', 'firstrun.sh.template'), ('rpi5', 'firstrun.sh.template'), ('r3a', 'rock3a-provision.sh.template')):
            with self.subTest(board=board):
                env = dict(self.environment, MANET_RELEASE_FILE=str(self.manifest_path),
                           TEST_ENGINE=str(PROVISIONING / 'windows.ps1'), TEST_BOARD=board,
                           TEST_TEMPLATE=str(PROVISIONING / template))
                result = subprocess.run([PWSH, '-NoProfile', '-File', str(script)], env=env,
                                        text=True, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                package_board = 'cm4' if board == 'rpi4' else board
                self.assertIn(DOWNLOADS + f'/v0.551/{package_board}-install.tar.gz', result.stdout)
                self.assertNotIn('__INSTALL_', result.stdout)
                self.assertNotIn('__RELEASE_', result.stdout)
                self.assertNotIn('__OFFLINE_', result.stdout)
                self.assertNotIn('__RADIO_USER__', result.stdout)
                self.assertIn('ssh_user=field_ops-2', result.stdout)
                pins = re.findall(r"^\s*((?:YQ|MEDIAMTX)_(?:VERSION|SHA256)='[^']+')$",
                                  (PROVISIONING / template).read_text(), re.M)
                self.assertEqual(len(pins), 4)
                for pin in pins:
                    self.assertIn(pin, result.stdout)
                parsed = subprocess.run(['bash', '-n'], input=result.stdout, text=True, capture_output=True)
                self.assertEqual(parsed.returncode, 0, parsed.stderr)


if __name__ == '__main__':
    unittest.main()
