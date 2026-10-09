"""Package setup keeps shipped conffiles and recovers interrupted provisioning."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parent
PROVISIONING = TOOLS.parent / 'provisioning'

STUBS = '''
repairs=0
installs=0
broken=1
record() { printf '%s|%s|%s\\n' "$1" "${DEBIAN_FRONTEND:-}" "${*:2}" >> "$EVENTS"; }
dpkg() {
    [ "$1" = -s ] && return 1
    record dpkg "$@"
    repairs=$((repairs + 1))
    if [ "$FAIL_STEP" = repair ] && [ "$repairs" -eq 1 ]; then return 1; fi
    broken=0
}
apt-get() {
    record apt-get "$@"
    if [ "$1" = update ]; then
        [ "$FAIL_STEP" != update ]
        return $?
    fi
    installs=$((installs + 1))
    [ "$broken" -eq 0 ] || return 1
    if [ "$FAIL_STEP" = install ] && [ "$installs" -eq 1 ]; then return 1; fi
    return 0
}
apt() { record apt "$@"; return 1; }
getent() { return "${NETWORK_RC:-0}"; }
ping() { return 0; }
python3() { [ "$installs" -gt 0 ]; }
sed() { :; }
service() { :; }
grep() { :; }
'''


class PackageSetupTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.events = self.root / 'events'
        self.failures = self.root / 'failures'

    def run_shell(self, source, fail='', network=0):
        self.events.write_text('')
        self.failures.write_text('')
        env = dict(os.environ, EVENTS=str(self.events),
                   PROVISION_FAIL_FILE=str(self.failures), FAIL_STEP=fail,
                   NETWORK_RC=str(network), DEBIAN_FRONTEND='dialog')
        result = subprocess.run(['bash', '-c', STUBS + source], env=env,
                                stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return [line.split('|') for line in self.events.read_text().splitlines()]

    def radio_source(self):
        source = (TOOLS / 'radio-setup.sh').read_text()
        functions = [re.search(r'^' + name + r'\(\) \{\n.*?^\}', source,
                               re.M | re.S).group() for name in
                     ('provision_fail', 'provision_try', 'install_packages',
                      'have_package_network')]
        blocks = re.findall(r'^if have_package_network; then\n.*?^fi', source,
                            re.M | re.S)
        self.assertEqual(len(blocks), 3)
        return '\n'.join(functions + blocks) + '\necho continued\n'

    def assert_install_options(self, events, recommends=False):
        installs = [event for event in events if event[2].startswith('install ')]
        self.assertTrue(installs)
        for command, frontend, args in installs:
            self.assertEqual(command, 'apt-get')
            self.assertEqual(frontend, 'noninteractive')
            options = args.split()
            self.assertIn('-y', options)
            self.assertEqual('--no-install-recommends' in options, not recommends)
            self.assertIn('-o Dpkg::Options::=--force-confdef', args)
            self.assertIn('-o Dpkg::Options::=--force-confold', args)
        return installs

    def test_radio_repairs_before_each_install_and_keeps_conffiles(self):
        events = self.run_shell(self.radio_source())
        self.assertEqual([event[0] for event in events],
                         ['dpkg', 'apt-get', 'dpkg', 'apt-get', 'apt-get',
                          'dpkg', 'apt-get'])
        for event in (events[0], events[2], events[5]):
            self.assertEqual(event, ['dpkg', 'noninteractive',
                                     '--force-confdef --force-confold --configure -a'])
        self.assertEqual(events[3][2], 'update -qq')
        installs = self.assert_install_options(events)
        self.assertEqual(len(installs), 3)
        self.assertEqual(self.failures.read_text(), '')

    def test_radio_failures_are_recorded_and_later_steps_continue(self):
        for failure, packages, installed in (
                ('repair', 'avahi-daemon iperf3 traceroute sqlite3 python3-zeroconf python3-cryptography', 2),
                ('install', 'avahi-daemon iperf3 traceroute sqlite3 python3-zeroconf python3-cryptography', 3),
                ('update', 'python3-smbus i2c-tools', 2)):
            with self.subTest(failure=failure):
                events = self.run_shell(self.radio_source(), fail=failure)
                installs = self.assert_install_options(events)
                self.assertEqual(len(installs), installed)
                self.assertTrue(installs[-1][2].endswith(' gpsd gpsd-tools'))
                self.assertEqual(self.failures.read_text(),
                                 'apt install failed: ' + packages + '\n')
        self.run_shell(self.radio_source())
        self.assertEqual(self.failures.read_text(), '')

    def test_radio_offline_records_existing_messages_without_package_commands(self):
        events = self.run_shell(self.radio_source(), network=1)
        self.assertEqual(events, [])
        self.assertEqual(self.failures.read_text().splitlines(), [
            'no network: cannot install avahi-daemon iperf3 traceroute sqlite3 python3-zeroconf python3-cryptography',
            'no network: cannot install python3-smbus i2c-tools',
            'no network: cannot install gpsd gpsd-tools'])

    def test_update_helpers_keep_options_on_retry(self):
        for name in ('manet-admin-setup.sh', 'manet-voice-setup.sh'):
            with self.subTest(script=name):
                source = (TOOLS / name).read_text()
                if name == 'manet-voice-setup.sh':
                    source = source[source.index('PKGS='):source.index('# The OpenVLM rule')]
                events = self.run_shell('broken=0\n' + source, fail='install')
                self.assertEqual([event[2].split()[0] for event in events],
                                 ['install', 'update', 'install'])
                self.assertEqual(len(self.assert_install_options(events)), 2)

    def test_templates_keep_conffiles_for_optional_post_archive_installs(self):
        for name, packages in (('firstrun.sh.template', 'mumble-server'),
                               ('rock3a-provision.sh.template', 'sqlite3 mumble-server')):
            with self.subTest(template=name):
                source = (PROVISIONING / name).read_text()
                start = source.index('if [ "__INSTALL_MUMBLE__"')
                self.assertLess(source.index('tar -zxf /root/morse-pi-install.tar.gz'), start)
                block = source[start:source.index('\nfi', start) + 3]
                block = block.replace('__INSTALL_MUMBLE__', 'y').replace(
                    '/root/mumble_pw', str(self.root / 'mumble_pw'))
                installs = self.assert_install_options(
                    self.run_shell('broken=0\n' + block), recommends=True)
                self.assertEqual(len(installs), 1)
                self.assertTrue(installs[0][2].endswith(' ' + packages))


if __name__ == '__main__':
    unittest.main()
