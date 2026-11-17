"""Exercise first-boot downloads without network access or changes to the host."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
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
                # Reviewed HEAD-only readiness probes consume no artifact.
                readiness = '# BEGIN bounded provisioning connectivity'
                end_readiness = '# END bounded provisioning connectivity'
                probe = block(content, readiness, end_readiness)
                self.assertIn('--head --location --insecure', probe)
                self.assertIn('--output /dev/null', probe)
                remaining = remaining.replace(probe, '')
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
                    result = self.run_shell(
                        script + '\necho COMPLETE\n',
                        MEDIAMTX_VERSION=PINS['MEDIAMTX_VERSION'],
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


    def readiness_source(self, template, media='y', checksum='0' * 64, offline=''):
        """Extract the identical early gate and isolate all of its writes."""
        content = (PROVISIONING / template).read_text()
        source = block(content, '# BEGIN bounded provisioning connectivity',
                       '# END bounded provisioning connectivity')
        source = source.replace('__INSTALL_MEDIAMTX__', media).replace(
            '__INSTALL_URL__',
            'https://github.com/very-srs/MANET/releases/download/'
            'v0.551/cm4-install.tar.gz').replace('__INSTALL_SHA256__', checksum)
        source = source.replace('__OFFLINE_SHA256__', offline)
        return re.sub(r'/(?:var/lib|usr/local/bin|etc|sys|proc|boot)(?=/|\s|$)',
                      lambda m: str(self.work / 'image') + m[0], source)

    def card_copy_source(self, template, checksum):
        """The early gate, then the package step, as one provisioning run."""
        package = artifact_blocks((PROVISIONING / template).read_text())['MANET']
        package = package.replace(
            '__INSTALL_URL__',
            'https://github.com/very-srs/MANET/releases/download/'
            'v0.551/cm4-install.tar.gz').replace('__RELEASE_VERSION__', '0.551')
        package = package.replace('__INSTALL_SHA256__', checksum).replace(
            '/root/morse-pi-install.tar.gz', str(self.work / 'install.tar.gz'))
        return (self.readiness_source(template, 'n', checksum) + '\n' + package +
                '\necho COMPLETE\n')

    def test_card_copy_replaces_github_download(self):
        """A matching copy on the card needs no GitHub access at first boot."""
        digest = hashlib.sha256(self.payload.read_bytes()).hexdigest()
        cases = [(name, place, linked) for name in TEMPLATES
                 for place in ('boot/firmware', 'boot') for linked in (True, False)]
        for name, place, linked in cases:
            with self.subTest(template=name, place=place, linked=linked):
                # A FAT boot partition cannot hold a link into the root
                # filesystem, which this models by making ln fail.
                if linked:
                    (self.bin / 'ln').unlink(missing_ok=True)
                else:
                    self.command('ln', '#!/bin/bash\nexit 1\n')
                root = self.readiness_fixture()
                shutil.rmtree(root / 'boot', ignore_errors=True)
                (root / place).mkdir(parents=True)
                shutil.copyfile(self.payload, root / place / 'cm4-install.tar.gz')
                self.events.write_text('')
                # The fixture curl only answers HEAD probes, so a download
                # attempt in the package step fails the run.
                result = self.run_shell(self.card_copy_source(name, digest),
                                        TEST_RESOURCE_FAIL='/very-srs/')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('COMPLETE', result.stdout)
                self.assertIn('Install package found on the card', result.stdout)
                urls = [json.loads(row)[-1] for row in
                        self.events.read_text().splitlines() if row.startswith('[')]
                self.assertFalse([u for u in urls if 'cm4-install' in u])
                self.assertEqual((self.work / 'install.tar.gz').read_bytes(),
                                 self.payload.read_bytes())
                card = root / place / 'cm4-install.tar.gz'
                self.assertEqual(os.stat(card).st_ino == os.stat(self.work / 'install.tar.gz').st_ino,
                                 linked)
                # The card keeps its copy until provisioning succeeds.
                self.assertTrue(card.exists())

    def test_mismatched_card_copy_is_ignored(self):
        """A stale or damaged copy leaves first boot waiting for GitHub."""
        for name in TEMPLATES:
            with self.subTest(template=name):
                root = self.readiness_fixture()
                (root / 'boot/firmware').mkdir(parents=True, exist_ok=True)
                (root / 'boot/firmware/cm4-install.tar.gz').write_bytes(b'other release\n')
                digest = hashlib.sha256(self.payload.read_bytes()).hexdigest()
                result = self.run_shell(self.card_copy_source(name, digest),
                                        TEST_RESOURCE_FAIL='/very-srs/')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Ignoring', result.stdout)
                self.assertIn('MANET GitHub release', result.stdout)
                self.assertNotIn('COMPLETE', result.stdout)

    def test_card_copy_is_removed_only_after_success(self):
        """A retry after a failed run still finds the card's copies."""
        cleanup = 'rm -f "/boot/firmware/${MANET_INSTALL_URL##*/}" "/boot/${MANET_INSTALL_URL##*/}"'
        offline = 'rm -f "/boot/firmware/$offline_name" "/boot/$offline_name"'
        for name, content in self.templates():
            with self.subTest(template=name):
                self.assertEqual(content.count(cleanup), 1)
                self.assertEqual(content.count(offline), 1)
                self.assertLess(content.index(cleanup), content.index(offline))
                # Only after the disable is on disk, so a power cut cannot strand a retry.
                disable = content.index('systemctl disable mesh-provision')
                position = content.index(cleanup)
                self.assertGreater(position, content.index('# The flasher pins setup'))
                self.assertGreater(position, content.index(APT_CLEANUP))
                self.assertGreater(position, disable)
                self.assertIn('\nsync\n', content[disable:position])

    def readiness_fixture(self):
        """Model monotonic time, resource replies and base-image LEDs."""
        root = self.work / 'image'
        for name in ('proc', 'sys/class/leds/PWR', 'sys/class/leds/ACT'):
            (root / name).mkdir(parents=True, exist_ok=True)
        (root / 'proc/uptime').write_text('0.00 0\n')
        for led in ('PWR', 'ACT'):
            for name, value in (('trigger', 'default-on'),
                                ('max_brightness', '1'), ('brightness', '0')):
                (root / 'sys/class/leds' / led / name).write_text(value)
        self.env.update(TEST_IMAGE=str(root),
                        BOOTSTRAP_LOG='/boot/firmware/provision.log')
        self.command('date', '#!/bin/bash\necho 1700000000\n')
        self.command('ping',
                     '#!/bin/bash\necho PING >> "$TEST_EVENTS"\nexit 1\n')
        self.command('sleep', f'#!{sys.executable}\n' + '''
import os, sys
from pathlib import Path
p = Path(os.environ['TEST_IMAGE']) / 'proc/uptime'
p.write_text(str(int(p.read_text().split()[0]) + int(sys.argv[1])) + ' 0')
''')
        self.command('curl', f'#!{sys.executable}\n' + '''
import json, os, sys
from pathlib import Path
root = Path(os.environ['TEST_IMAGE'])
args = sys.argv[1:]
with open(os.environ['TEST_EVENTS'], 'a') as out:
    out.write(json.dumps(args) + '\\n')
assert (root / 'sys/class/leds/PWR/trigger').read_text().strip() == 'timer'
assert (root / 'sys/class/leds/PWR/delay_on').read_text().strip() == '100'
assert '--head' in args and '--insecure' in args
assert args[args.index('--output') + 1] == '/dev/null'
budget = int(args[args.index('--max-time') + 1])
p = root / 'proc/uptime'
now = int(float(p.read_text().split()[0]))
p.write_text(str(now + min(3, budget)) + ' 0')
if os.environ.get('TEST_RESOURCE_FAIL', 'NOT_A_URL') in args[-1]:
    sys.exit(22)
print('200', end='')
''')
        return root

    def test_bootstrap_gate_allows_icmp_filtered_cold_clock(self):
        """HTTP resources, not ping or certificate time, admit provisioning."""
        sources = [self.readiness_source(name) for name in TEMPLATES]
        self.assertEqual(sources[0], sources[1])
        for name in TEMPLATES:
            with self.subTest(template=name):
                root = self.readiness_fixture()
                self.events.write_text('')
                result = self.run_shell(self.readiness_source(name))
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = [json.loads(row) for row in
                         self.events.read_text().splitlines()]
                urls = [args[-1] for args in calls]
                self.assertTrue(any('/trixie/InRelease' in u for u in urls))
                self.assertTrue(any('cm4-install.tar.gz' in u for u in urls))
                self.assertTrue(any('mediamtx_v1.15.3_linux_arm64.tar.gz'
                                    in u for u in urls))
                self.assertIn('http://google.com', urls)
                state = root / 'var/lib/manet-provision.state'
                self.assertIn('STATE=running', state.read_text())

    def test_bootstrap_timeout_records_failure_leds_and_reboot_retry(self):
        """Each required resource failure stops at 300s and permits retry."""
        for name in TEMPLATES:
            for missing in ('deb.debian.org', '/very-srs/', '/mediamtx/'):
                with self.subTest(template=name, missing=missing):
                    root = self.readiness_fixture()
                    result = self.run_shell(self.readiness_source(name),
                                            TEST_RESOURCE_FAIL=missing)
                    self.assertNotEqual(result.returncode, 0)
                    state = root / 'var/lib/manet-provision.state'
                    self.assertIn('STATE=incomplete', state.read_text())
                    self.assertIn('PHASE=bootstrap-network', state.read_text())
                    self.assertEqual(int((root / 'proc/uptime').read_text()
                                         .split()[0]), 300)
                    self.assertIn('Timed out after 300s', result.stdout)
                    self.assertIn('reboot to retry', result.stdout)
                    self.assertIn('restart mesh-provision.service',
                                  result.stdout)
                    red = root / 'sys/class/leds/PWR/trigger'
                    green = root / 'sys/class/leds/ACT/brightness'
                    self.assertEqual(red.read_text().strip(), 'heartbeat')
                    self.assertEqual(green.read_text().strip(), '1')
                    banner = root / 'etc/update-motd.d/50-manet-provision'
                    self.assertTrue(banner.is_symlink())
                    # A later invocation is the enabled service's reboot retry.
                    self.readiness_fixture()
                    result = self.run_shell(self.readiness_source(name))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('STATE=running', state.read_text())
                    failures = root / 'var/lib/manet-provision.failures'
                    self.assertEqual(failures.read_text(), '')

    def test_date_failure_and_unselected_mediamtx_do_not_block_bootstrap(self):
        """The logging clock and unselected downloads are optional."""
        for missing, media in (('google.com', 'y'), ('/mediamtx/', 'n')):
            self.readiness_fixture()
            result = self.run_shell(self.readiness_source(TEMPLATES[0], media),
                                    TEST_RESOURCE_FAIL=missing)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_bootstrap_failure_uses_full_status_and_existing_led_scheme(self):
        """Installed readers understand the early state without migration."""
        root = self.readiness_fixture()
        result = self.run_shell(self.readiness_source(TEMPLATES[0]),
                                TEST_RESOURCE_FAIL='deb.debian.org')
        self.assertNotEqual(result.returncode, 0)
        tools = PROVISIONING.parent / 'node_tools'
        state = root / 'var/lib/manet-provision.state'
        failures = root / 'var/lib/manet-provision.failures'
        env = dict(self.env,
                   MANET_PROVISION_STATE=str(state),
                   MANET_PROVISION_FAILURES=str(failures),
                   MANET_PROVISION_DONE=str(root / 'absent'),
                   MANET_LED_DIR=str(root / 'sys/class/leds'))
        status = tools / 'manet-provision-status.sh'
        result = subprocess.run(['bash', str(status)],
                                env=env, capture_output=True, text=True)
        self.assertIn('PROVISIONING DID NOT COMPLETE', result.stdout)
        self.assertIn('bootstrap-network', result.stdout)
        self.assertIn('/boot/firmware/provision.log', result.stdout)
        self.assertIn('restart mesh-provision.service', result.stdout)
        self.assertNotIn('sudo radio-setup.sh', result.stdout)
        subprocess.run(['bash', str(tools / 'manet-led-status.sh')],
                       env=env, check=True)
        self.assertEqual((root / 'sys/class/leds/PWR/trigger')
                         .read_text().strip(), 'heartbeat')
        self.assertEqual((root / 'sys/class/leds/ACT/brightness')
                         .read_text().strip(), '1')

    def test_bootstrap_failure_leaves_next_boot_service_enabled(self):
        """Neither platform reaches its success-only disable/flag removal."""
        for name, content in self.templates():
            begin = content.index('# BEGIN bounded provisioning connectivity')
            finish = content.index('# END bounded provisioning connectivity')
            self.assertIn('bootstrap_wait || exit 1', content[begin:finish])
            disable = content.index('systemctl disable mesh-provision')
            self.assertLess(finish, disable)
        for name in ('linux-flasher.sh', 'windows.ps1'):
            source = (PROVISIONING / name).read_text()
            self.assertIn(
                'ExecStartPost=/bin/rm -f /root/.mesh-not-provisioned',
                          source)
            self.assertNotIn(
                'ExecStopPost=/bin/rm -f /root/.mesh-not-provisioned',
                             source)


    def card_files(self, root, place='boot/firmware', bundle=b'offline bundle\n'):
        """The install package and offline bundle on the card, and their digests."""
        (root / place).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.payload, root / place / 'cm4-install.tar.gz')
        (root / place / 'cm4-offline.tar').write_bytes(bundle)
        return (hashlib.sha256(self.payload.read_bytes()).hexdigest(),
                hashlib.sha256(bundle).hexdigest())

    def test_offline_card_skips_every_network_check(self):
        """With both files on the card, first boot waits for nothing."""
        for name in TEMPLATES:
            for place in ('boot/firmware', 'boot'):
                with self.subTest(template=name, place=place):
                    root = self.readiness_fixture()
                    shutil.rmtree(root / 'boot', ignore_errors=True)
                    package, bundle = self.card_files(root, place)
                    self.events.write_text('')
                    result = self.run_shell(
                        self.readiness_source(name, 'y', package, bundle) +
                        '\necho "BUNDLE=$MANET_OFFLINE_BUNDLE"\n',
                        TEST_RESOURCE_FAIL='deb.debian.org')
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn('Offline setup: every installation file is on the card', result.stdout)
                    self.assertIn(f'BUNDLE={root / place}/cm4-offline.tar', result.stdout)
                    # No probe at all: Debian, GitHub, MediaMTX, or the clock.
                    self.assertEqual(self.events.read_text(), '')

    def test_offline_bundle_alone_or_unpinned_is_not_offline(self):
        """Offline setup needs both files, and an online flash pins no bundle."""
        for case in ('no package', 'no pin', 'wrong bundle'):
            with self.subTest(case=case):
                root = self.readiness_fixture()
                package, bundle = self.card_files(root)
                if case == 'no package':
                    (root / 'boot/firmware/cm4-install.tar.gz').unlink()
                pin = {'no pin': '', 'wrong bundle': '0' * 64}.get(case, bundle)
                result = self.run_shell(
                    self.readiness_source(TEMPLATES[0], 'n', package, pin) +
                    '\necho "BUNDLE=[$MANET_OFFLINE_BUNDLE]"\n',
                    TEST_RESOURCE_FAIL='deb.debian.org')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Debian archive', result.stdout)
                self.assertNotIn('BUNDLE=', result.stdout)

    def offline_source(self):
        """The Raspberry Pi template's offline apt block, writing into image/."""
        content = (PROVISIONING / TEMPLATES[0]).read_text()
        self.assertNotIn('# BEGIN offline package source', (PROVISIONING / TEMPLATES[1]).read_text())
        source = block(content, '# BEGIN offline package source', '# END offline package source')
        return re.sub(r'/(?:var/lib|etc)(?=/|\s|$)',
                      lambda m: str(self.work / 'image') + m[0], source)

    def offline_bundle(self, version='0.551'):
        stage = self.work / 'bundle-stage'
        (stage / 'debs').mkdir(parents=True, exist_ok=True)
        (stage / 'debs/Packages').write_text('Package: fixture\n')
        (stage / 'VERSION').write_text(version + '\n11/2026\n')
        (stage / 'yq_linux_arm64').write_bytes(b'yq')
        bundle = self.work / 'cm4-offline.tar'
        subprocess.run(['tar', '-cf', str(bundle), '-C', str(stage), '.'], check=True,
                       env=dict(os.environ))
        return bundle

    def test_offline_apt_source_is_the_bundle_alone(self):
        """apt reads only the unpacked bundle, and only for the pinned release."""
        root = self.work / 'image'
        (root / 'etc/apt/apt.conf.d').mkdir(parents=True, exist_ok=True)
        (root / 'var/lib').mkdir(parents=True, exist_ok=True)
        source = self.offline_source()
        # Last: a download fallback after the successful offline attempt above.
        for version, offline in (('0.551', True), ('0.550', True), ('0.551', True), ('0.551', False)):
            with self.subTest(version=version, offline=offline):
                for leftover in (root / 'etc/apt/apt.conf.d/99manet-offline', root / 'etc/apt/manet-offline.list'):
                    leftover.unlink(missing_ok=True)
                bundle = self.offline_bundle(version)
                env = dict(MANET_LOCAL_PACKAGE='/card/cm4-install.tar.gz' if offline else '',
                           MANET_OFFLINE_BUNDLE=str(bundle) if offline else '',
                           MANET_INSTALL_VERSION='0.551')
                # The real tar: the setUp fixture only stands in for package reads.
                (self.bin / 'tar').unlink(missing_ok=True)
                script = ('MANET_LOCAL_PACKAGE=$MANET_LOCAL_PACKAGE\n' + source +
                          '\necho "DIR=$MANET_OFFLINE_DIR"\n')
                result = self.run_shell(script, **env)
                conf = root / 'etc/apt/apt.conf.d/99manet-offline'
                if not offline:
                    # A download fallback also drops what an earlier offline attempt left.
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('DIR=\n', result.stdout)
                    self.assertFalse(conf.exists())
                    self.assertFalse((root / 'etc/apt/manet-offline.list').exists())
                    self.assertFalse((root / 'var/lib/manet-offline').exists())
                elif version != '0.551':
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('does not match the selected release', result.stdout)
                    self.assertFalse(conf.exists())
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    unpacked = root / 'var/lib/manet-offline'
                    self.assertIn(f'DIR={unpacked}', result.stdout)
                    self.assertTrue((unpacked / 'debs/Packages').is_file())
                    self.assertTrue((unpacked / 'no-sources').is_dir())
                    self.assertEqual(list((unpacked / 'no-sources').iterdir()), [])
                    listing = (root / 'etc/apt/manet-offline.list').read_text()
                    self.assertEqual(listing, f'deb [trusted=yes] file:{unpacked}/debs ./\n')
                    settings = conf.read_text()
                    self.assertIn(f'Dir::Etc::SourceList "{root}/etc/apt/manet-offline.list";', settings)
                    self.assertIn(f'Dir::Etc::SourceParts "{unpacked}/no-sources";', settings)
                    self.assertIn('Acquire::Languages "none";', settings)
                    check = subprocess.run(['apt-config', '-c', str(conf), 'shell', 'LIST', 'Dir::Etc::SourceList'],
                                           capture_output=True, text=True)
                    self.assertIn(f"LIST='{root}/etc/apt/manet-offline.list'", check.stdout)

    def test_offline_yq_and_mediamtx_come_from_the_bundle(self):
        """Both pinned downloads read the unpacked bundle when it is there."""
        digest = hashlib.sha256(self.payload.read_bytes()).hexdigest()
        offline = self.work / 'offline'
        offline.mkdir()
        shutil.copyfile(self.payload, offline / 'yq_linux_arm64')
        shutil.copyfile(self.payload, offline / f"mediamtx_{PINS['MEDIAMTX_VERSION']}_linux_arm64.tar.gz")
        for name, content in self.templates():
            for artifact in ('YQ', 'MEDIAMTX'):
                for mode in ('valid', 'corrupt'):
                    with self.subTest(template=name, artifact=artifact, mode=mode):
                        self.events.write_text('')
                        script = artifact_blocks(content)[artifact]
                        script = script.replace(PINS[f'{artifact}_SHA256'], digest if mode == 'valid' else '0' * 64)
                        script = script.replace('/usr/bin/yq', str(self.work / 'yq'))
                        result = self.run_shell(script + '\necho COMPLETE\n',
                                                MEDIAMTX_VERSION=PINS['MEDIAMTX_VERSION'],
                                                MANET_OFFLINE_DIR=str(offline))
                        self.assertEqual(result.returncode == 0, mode == 'valid', result.stdout + result.stderr)
                        self.assertEqual('COMPLETE' in result.stdout, mode == 'valid')
                        # Nothing was fetched over the network.
                        self.assertFalse([e for e in self.events.read_text().splitlines() if e.startswith('[')])
                        if mode == 'corrupt':
                            self.assertNotIn('USED', self.events.read_text())

    def test_radio_setup_installs_offline_and_then_restores_apt(self):
        """radio-setup needs no network while the offline source is in place,
        and removes the source once setup is complete."""
        setup = (PROVISIONING.parent / 'node_tools/radio-setup.sh').read_text()
        start = setup.index('have_package_network() {')
        function = setup[start:setup.index('\n}\n', start) + 3]
        self.command('getent', '#!/bin/bash\nexit 2\n')
        conf = self.work / '99manet-offline'
        script = (f'MANET_OFFLINE_APT={conf}\nMANET_TOOLS_DIR={self.work}\nPACKAGE_UPLINK_CHECKED=1\n' +
                  function + 'have_package_network && echo NETWORK || echo NONE\n')
        result = self.run_shell(script)
        self.assertIn('NONE', result.stdout)
        conf.write_text('Dir::Etc::SourceList "/x";\n')
        result = self.run_shell(script)
        self.assertIn('NETWORK', result.stdout)
        complete = setup.index('provision_state complete "$(date +%s)"')
        cleanup = setup.index('rm -f "$MANET_OFFLINE_APT" /etc/apt/manet-offline.list')
        self.assertLess(complete, cleanup)
        self.assertIn('rm -rf /var/lib/manet-offline', setup[cleanup:cleanup + 200])
        self.assertLess(cleanup, setup.index('systemctl disable radio-setup-run-once.service'))
        self.assertEqual(setup.count('MANET_OFFLINE_APT=/etc/apt/apt.conf.d/99manet-offline'), 1)


if __name__ == '__main__':
    unittest.main()
