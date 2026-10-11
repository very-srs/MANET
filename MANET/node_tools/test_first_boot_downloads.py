"""Exercise first-boot downloads without network access or changes to the host."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

PROVISIONING = Path(__file__).resolve().parents[1] / 'provisioning'
TEMPLATES = ('firstrun.sh.template', 'rock3a-provision.sh.template')
PINS = {
    'YQ_VERSION': 'v4.54.1',
    'YQ_SHA256': '189088da0c6429ec5178dfaab1a114805f6cab0b61b165ab236efedf1d57a71b',
    'MEDIAMTX_VERSION': 'v1.15.3',
    'MEDIAMTX_SHA256': '5f3cb84ef42952a82b1ff5764fd06dc13697d21b06eb179d83663ec55d15ed0c',
}
# No unverified executable/archive downloads are allowed. APT authenticates
# changing metadata and packages with repository signatures and hashes. The
# HTTP HEAD request supplies only a best-effort logging clock, not code/data.
UNVERIFIED_ARTIFACT_ALLOWLIST = set()
NON_ARTIFACT_FETCHES = {'apt', 'apt-get', 'http://google.com'}
APT_PATH = '/etc/apt/apt.conf.d/99no-time-checks.conf'
APT_START = '# Temporarily allow APT metadata'
APT_END = '# End temporary APT clock setup.'
APT_CLEANUP = '# Restore normal APT date checks before completing this phase.'


def block(content, start, end):
    return content.split(start, 1)[1].split(end, 1)[0]


def artifact_blocks(content):
    start = content.index('# The flasher pins setup')
    end = content.index('\ndone\n', start) + len('\ndone\n')
    return {
        'MANET': content[start:end],
        'YQ': '# Download and' + block(content, '# Download and', '# These add network traffic'),
        'MEDIAMTX': block(content, 'echo "Installing MediaMTX"', 'groupadd --system mediamtx'),
    }


class FirstBootDownloadTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='manet-first-boot-')
        self.addCleanup(scratch.cleanup)
        self.work = Path(scratch.name)
        self.bin = self.work / 'bin'
        self.bin.mkdir()
        self.events = self.work / 'events'
        self.payload = self.work / 'payload'
        self.payload.write_bytes(b'first boot download fixture\n')
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        TMPDIR=str(self.work), TEST_PAYLOAD=str(self.payload),
                        TEST_EVENTS=str(self.events))
        downloader = f'''#!{sys.executable}
import json, os, shutil, sys
args = sys.argv[1:]
with open(os.environ['TEST_EVENTS'], 'a') as log:
    log.write(json.dumps(args) + '\\n')
if '--head' in args or '-sI' in args:
    if os.environ.get('TEST_DATE_FAIL'): sys.exit(22)
    print('Date: Sun, 11 Oct 2026 12:00:00 GMT')
    sys.exit(0)
if os.environ.get('TEST_DOWNLOAD_FAIL'): sys.exit(22)
flag = '-o' if '-o' in args else '-O'
if flag in args:
    destination = args[args.index(flag) + 1]
else:
    destination = args[-1].rsplit('/', 1)[-1]
shutil.copyfile(os.environ['TEST_PAYLOAD'], destination)
'''
        for name in ('curl', 'wget'):
            self.command(name, downloader)
        self.command('tar', '#!/bin/bash\nif [[ "$1" == -xzOf ]]; then echo 0.551; fi\necho USED >> "$TEST_EVENTS"\n')
        self.command('install', '#!/bin/bash\necho USED >> "$TEST_EVENTS"\n')
        self.command('date', '#!/bin/bash\necho DATE >> "$TEST_EVENTS"\nexit "${TEST_DATE_SET_FAIL:-0}"\n')

    def command(self, name, source):
        path = self.bin / name
        path.write_text(source)
        path.chmod(0o755)

    def run_shell(self, script, **env):
        return subprocess.run(['bash', '-c', script], cwd=self.work,
                              env=dict(self.env, **env), text=True,
                              capture_output=True, timeout=10)

    def templates(self):
        for name in TEMPLATES:
            yield name, (PROVISIONING / name).read_text()

    def assert_pins(self, content):
        for key, value in PINS.items():
            self.assertIn(f"{key}='{value}'", content)

    def test_templates_share_third_party_pins_and_verification(self):
        sources = [content for _, content in self.templates()]
        for content in sources:
            self.assert_pins(content)
        for name in ('YQ', 'MEDIAMTX'):
            self.assertEqual(artifact_blocks(sources[0])[name], artifact_blocks(sources[1])[name])

    def test_linux_renderers_preserve_pins(self):
        # Execute the actual sed commands used by each Linux path, without
        # calling the disk-writing functions that surround them.
        source = (PROVISIONING / 'linux-flasher.sh').read_text()
        pi_start = source.index('        sed -e ', source.index('flash_rpi() {'))
        pi_end = source.index('\n\n', pi_start)
        pi_render = source[pi_start:pi_end]
        rock_start = source.index('        cp "$ROCK3A_TEMPLATE" "$TEMP_PROVISION_SCRIPT"')
        rock_end = source.index('\n\n        # Same embedding', rock_start)
        rock_render = source[rock_start:rock_end]
        manifest = {'schema': 1, 'version': '0.551', 'tag': 'v0.551', 'commit': 'a' * 40,
                    'assets': {f'{board}-{kind}.tar.gz': {'size': 1, 'sha256': 'b' * 64}
                               for board in ('cm4', 'rpi5', 'r3a') for kind in ('install', 'tools')}}
        manifest_path = self.work / 'manifest.json'
        manifest_path.write_text(json.dumps(manifest))
        for board, name in (('rpi4', TEMPLATES[0]), ('rpi5', TEMPLATES[0]), ('r3a', TEMPLATES[1])):
            with self.subTest(board=board):
                values = subprocess.run([sys.executable, str(PROVISIONING / 'prepare-release.py'),
                                         str(manifest_path), 'cm4' if board == 'rpi4' else board],
                                        text=True, capture_output=True, check=True).stdout.splitlines()
                output = self.work / 'rendered.sh'
                content = (PROVISIONING / name).read_text()
                env = {token: 'y' for token in re.findall(r'__([A-Z0-9_]+)__', content)}
                env.update(HARDWARE_MODEL=board, RELEASE_VERSION=values[0], INSTALL_URL=values[1],
                           INSTALL_SHA256=values[2], TEMPLATE_FILE=str(PROVISIONING / name),
                           ROCK3A_TEMPLATE=str(PROVISIONING / name), TEMP_SCRIPT_FILE=str(output),
                           TEMP_PROVISION_SCRIPT=str(output))
                result = self.run_shell(rock_render if board == 'r3a' else pi_render, **env)
                self.assertEqual(result.returncode, 0, result.stderr)
                rendered = output.read_text()
                self.assert_pins(rendered)
                self.assertIn(f"MANET_INSTALL_SHA256='{'b' * 64}'", rendered)
                self.assertNotRegex(rendered, r'__[A-Z0-9_]+__')
                parsed = subprocess.run(['bash', '-n', str(output)], capture_output=True, text=True)
                self.assertEqual(parsed.returncode, 0, parsed.stderr)

    def test_every_fetch_is_verified_or_explicitly_exempt(self):
        for name, content in self.templates():
            with self.subTest(template=name):
                blocks = artifact_blocks(content)
                remaining = content
                for source in blocks.values():
                    remaining = remaining.replace(source, '')
                    self.assertEqual(len(re.findall(r'\bcurl\s', source)), 1)
                    self.assertIn('--insecure', source)
                    self.assertIn("--proto '=https' --proto-redir '=https'", source)
                    self.assertIn('sha256sum -c - || exit 1', source)
                # Scan all logical lines, including functions and optional
                # blocks. Removing only reviewed fetches catches added ones.
                unmatched = set()
                exceptions = set()
                for line in remaining.replace('\\\n', ' ').splitlines():
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if re.match(r'(?:DEBIAN_FRONTEND=noninteractive )?apt(?:-get)? ', line):
                        exceptions.add('apt-get' if 'apt-get ' in line else 'apt')
                        self.assertNotRegex(line, r'\b(?:curl|wget|pip[0-9]?)\s|https?://')
                    elif re.search(r'\b(?:curl|wget|pip[0-9]?|git)\s|https?://', line):
                        if line.startswith('http_date=$(curl ') and 'http://google.com' in line:
                            self.assertIn('--head', line)
                            self.assertNotIn('--insecure', line)
                            self.assertNotRegex(line, r'(?:-o|--output)\s')
                            exceptions.add('http://google.com')
                        else:
                            unmatched.add(line)
                self.assertEqual(unmatched, UNVERIFIED_ARTIFACT_ALLOWLIST)
                self.assertEqual(exceptions, NON_ARTIFACT_FETCHES)
                self.assertNotIn('--insecure', remaining)
                self.assertNotRegex(content, r'--no-check-certificate|--allow-unauthenticated|AllowInsecure|Verify-Peer|Verify-Host')
                self.assertNotRegex(content, r'(?i)curlrc|wgetrc|CURL_HOME|WGETRC|APT_CONFIG')

    def test_hash_mismatch_aborts_before_use(self):
        self.exercise_artifacts('corrupt')

    def test_download_failure_aborts_before_use(self):
        self.exercise_artifacts('failed')

    def test_verified_downloads_ignore_certificate_clock(self):
        self.exercise_artifacts('valid')

    def exercise_artifacts(self, mode):
        digest = hashlib.sha256(self.payload.read_bytes()).hexdigest()
        for name, content in self.templates():
            for artifact, source in artifact_blocks(content).items():
                with self.subTest(template=name, artifact=artifact, mode=mode):
                    self.events.write_text('')
                    script = source.replace('__INSTALL_URL__', 'https://github.com/very-srs/MANET/releases/download/v0.551/cm4-install.tar.gz')
                    script = script.replace('__RELEASE_VERSION__', '0.551')
                    checksum = digest if mode != 'corrupt' else '0' * 64
                    script = script.replace('__INSTALL_SHA256__', checksum)
                    if artifact != 'MANET':
                        script = script.replace(PINS[f'{artifact}_SHA256'], checksum)
                    script = script.replace('/root/morse-pi-install.tar.gz', str(self.work / 'install.tar.gz'))
                    script = script.replace('/usr/bin/yq', str(self.work / 'yq'))
                    result = self.run_shell(script + '\necho COMPLETE\n',
                                            TEST_DOWNLOAD_FAIL='1' if mode == 'failed' else '')
                    events = self.events.read_text().splitlines()
                    if mode == 'valid':
                        self.assertEqual(result.returncode, 0, result.stderr)
                        args = json.loads(events[0])
                        self.assertIn('--insecure', args)
                        self.assertEqual(args[args.index('--proto') + 1], '=https')
                        self.assertEqual(args[args.index('--proto-redir') + 1], '=https')
                        self.assertIn('COMPLETE', result.stdout)
                    else:
                        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertNotIn('USED', events)
                        self.assertNotIn('COMPLETE', result.stdout)
                        if mode == 'corrupt':
                            self.assertIn('FAILED', result.stdout)

    def test_http_date_is_bounded_and_optional(self):
        for name, content in self.templates():
            with self.subTest(template=name):
                source = block(content, '# Best-effort HTTP Date', '\nfi\n')
                source = source[source.index('http_date='):] + '\nfi\n'
                self.assertIn('--max-time 10', source)
                self.assertEqual(len(re.findall(r'\bdate -s\b', content)), 1)
                for env, sets_date in (({'TEST_DATE_FAIL': '1'}, False),
                                       ({'TEST_DATE_SET_FAIL': '1'}, True), ({}, True)):
                    self.events.write_text('')
                    result = self.run_shell(source + '\necho CONTINUE\n', **env)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('CONTINUE', result.stdout)
                    self.assertEqual('DATE' in self.events.read_text().splitlines(), sets_date)

    def test_apt_override_lifecycle_and_syntax(self):
        for name, content in self.templates():
            if name == TEMPLATES[0]:
                wrapper, rest = content.split("<< 'PROVISIONEOF'\n", 1)
                inner, tail = rest.split('\nPROVISIONEOF\n', 1)
                phases = (wrapper + '\n' + tail, inner)
            else:
                phases = (content,)
            for number, phase in enumerate(phases):
                with self.subTest(template=name, phase=number):
                    setup = APT_START + block(phase, APT_START, APT_END) + APT_END
                    cleanup = APT_CLEANUP + block(phase, APT_CLEANUP, 'trap - EXIT HUP INT TERM') + 'trap - EXIT HUP INT TERM\n'
                    apt_uses = list(re.finditer(r'^\s*(?:DEBIAN_FRONTEND=noninteractive )?apt(?:-get)? ', phase, re.M))
                    self.assertTrue(apt_uses)
                    self.assertLess(phase.index(setup), apt_uses[0].start())
                    self.assertGreater(phase.index(cleanup), apt_uses[-1].start())
                    for boundary in ('# >>> MANET_ADDITIONAL_SCRIPTS <<<', 'systemctl disable mesh-provision', '\nreboot\n'):
                        if boundary in phase:
                            self.assertLess(phase.index(cleanup), phase.index(boundary))
                    path = self.work / '99no-time-checks.conf'
                    setup = setup.replace(APT_PATH, str(path))
                    cleanup = cleanup.replace(APT_PATH, str(path))
                    for ending, code in ((cleanup + f'\ntest ! -e {path}\n', 0), ('exit 7\n', 7), ('kill -TERM $$\n', 1)):
                        result = self.run_shell(setup + f'\ncat {path}\napt-config -c {path} shell CHECK Acquire::Check-Date\n' + ending)
                        self.assertEqual(result.returncode, code, result.stderr)
                        self.assertIn('Acquire::Check-Date "false";', result.stdout)
                        self.assertIn("CHECK='false'", result.stdout)
                        self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
