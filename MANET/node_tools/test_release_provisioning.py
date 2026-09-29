"""Run release launchers against local download fixtures without touching disks."""
import hashlib
import io
import json
import os
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

    @unittest.skipUnless(PWSH, 'PowerShell is required for Windows template execution tests')
    def test_windows_templates_receive_pinned_release_and_parse(self):
        self.save()
        script = self.work / 'tokens.ps1'
        script.write_text(r'''
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($env:TEST_ENGINE, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$function = $ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Expand-ProvisioningTokens'}, $true)
Invoke-Expression $function.Extent.Text
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
                parsed = subprocess.run(['bash', '-n'], input=result.stdout, text=True, capture_output=True)
                self.assertEqual(parsed.returncode, 0, parsed.stderr)


if __name__ == '__main__':
    unittest.main()
