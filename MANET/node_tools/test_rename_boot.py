"""Role persistence, failed radio units and bounded BATMAN startup waits."""
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest


TOOLS = Path(__file__).resolve().parent
SETUP = (TOOLS / 'radio-setup.sh').read_text()
BATMAN = (TOOLS / 'batman-if-setup.sh').read_text()
ROLES = ('mesh_if', 'mesh_24_if', 'mesh_5_if', 'halow_if', 'no_mesh_if',
         'iface_map', 'ap_interface')


def section(source, start, end):
    return source[source.index(start):source.index(end, source.index(start))]


def function(source, name):
    return re.search(r'^' + name + r'\(\) \{.*?^\}', source, re.M | re.S)[0]


class RenameBootTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        for directory in ('var/lib', 'etc/default', 'etc/systemd/network',
                          'before', 'at-reboot'):
            (self.root / directory).mkdir(parents=True)

    def shell(self, body):
        for path in ('/var/lib', '/etc/default', '/etc/systemd/network'):
            body = body.replace(path, str(self.root) + path)
        result = subprocess.run(['bash', '-c', body], cwd=self.root,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def roles(self, directory='var/lib'):
        return {role: (self.root / directory / role).read_text().splitlines()
                for role in ROLES}

    def setup_run(self, mesh=(), halow=(), nonmesh=(), band24='', band5='',
                  ap=False, enable=True, reboot=True):
        body = 'RADIO_SETUP_BOOT_ID=boot-one\n'
        for name, values in [('mesh_ifaces', mesh), ('halow_ifaces', halow),
                             ('nonmesh_ifaces', nonmesh)]:
            body += name + '=(' + shlex.join(values) + ')\n'
        body += 'mesh_24=' + shlex.quote(band24) + '\n'
        body += 'mesh_5=' + shlex.quote(band5) + '\n'
        body += 'eud=' + ('auto' if ap else 'wired') + '\n'
        body += '''
systemctl() { return ''' + ('0' if enable else '1') + '''; }
iface_mac() { echo "02:00:00:00:00:${1#wlan}"; }
iw() { echo 'wiphy 0 5180'; }
sleep() { :; }
provision_try() { shift; "$@"; }
reboot() {
    cp /var/lib/* at-reboot/
    echo REBOOT
    return ''' + ('0' if reboot else '1') + '''
}
'''
        body += section(SETUP, '# Create directory and files', '# Log what we found')
        body += section(SETUP, 'AP_INTERFACE=""', '# === CLEANUP STALE PER-INTERFACE')
        body += section(SETUP, 'declare -A pinned_names=()', '# Mesh (SAE) supplicant config')
        body += '\ncp /var/lib/* before/\n'
        body += section(SETUP, 'declare -A runtime_roles=()', '# Start the services enabled above')
        body += '\necho COMPLETED\n'
        return self.shell(body)

    def test_rename_roles_match_pins_for_detected_hardware(self):
        cases = [
            ({'mesh': ('wlan2', 'wlan3'), 'band24': 'wlan2', 'band5': 'wlan3'},
             ['wlan0', 'wlan1'], ['wlan0'], ['wlan1'], [], []),
            ({'halow': ('wlan0',)}, [], [], [], ['wlan2'], []),
            ({'mesh': ('wlan1', 'wlan2'), 'halow': ('wlan0',), 'nonmesh': ('wlan3',),
              'band24': 'wlan1', 'band5': 'wlan2'},
             ['wlan0', 'wlan1'], ['wlan0'], ['wlan1'], ['wlan2'], ['wlan3']),
            ({'mesh': ('wlan4',), 'band24': 'wlan4'}, ['wlan0'], ['wlan0'], [], [], []),
            ({'mesh': ('wlan4',), 'band5': 'wlan4'}, ['wlan0'], [], ['wlan0'], [], []),
            ({'nonmesh': ('wlan0',)}, [], [], [], [], ['wlan3']),
            ({'mesh': ('wlan5', 'wlan6', 'wlan7'), 'halow': ('wlan8', 'wlan9'),
              'nonmesh': ('wlan10', 'wlan11'), 'band24': 'wlan5', 'band5': 'wlan6'},
             ['wlan0', 'wlan1', 'wlan7'], ['wlan0'], ['wlan1'],
             ['wlan2', 'wlan9'], ['wlan3', 'wlan11']),
        ]
        for args, mesh, band24, band5, halow, nonmesh in cases:
            with self.subTest(hardware=args):
                for pin in (self.root / 'etc/systemd/network').glob('*.link'):
                    pin.unlink()
                result = self.setup_run(**args)
                self.assertIn('REBOOT', result.stdout)
                self.assertNotIn('COMPLETED', result.stdout)
                roles = self.roles('at-reboot')
                expected = dict(zip(ROLES[:5], (mesh, band24, band5, halow, nonmesh)))
                for role, values in expected.items():
                    self.assertEqual(roles[role], values)
                self.assertEqual(roles['iface_map'],
                                 [f'{name}:{name}' for name in mesh + halow + nonmesh])
                for original, target in zip(args.get('mesh', ())[:2], ('wlan0', 'wlan1')):
                    pin = (self.root / f'etc/systemd/network/10-{target}.link').read_text()
                    self.assertIn('MACAddress=02:00:00:00:00:' + original[4:], pin)
                    self.assertIn('Name=' + target, pin)
                self.assertEqual(self.roles(), roles)
                marker = self.root / 'var/lib/radio-setup-reboot-pending'
                self.assertEqual(marker.read_text(), 'boot-one\n')

    def test_ap_reservation_and_band_survive_rewrite(self):
        for dedicated in (False, True):
            with self.subTest(dedicated=dedicated):
                self.setup_run(mesh=('wlan4', 'wlan5'), band24='wlan4', band5='wlan5',
                               nonmesh=('wlan6',) if dedicated else (), ap=True)
                roles = self.roles('at-reboot')
                self.assertEqual(roles['ap_interface'], ['wlan3' if dedicated else 'wlan0'])
                self.assertEqual(roles['mesh_if'], ['wlan0', 'wlan1'] if dedicated else ['wlan1'])
                self.assertEqual(roles['mesh_24_if'], ['wlan0'] if dedicated else [])
                self.assertEqual(roles['mesh_5_if'], ['wlan1'])
                self.assertEqual((self.root / 'var/lib/ap_mesh_band').read_text().strip(),
                                 '' if dedicated else '2.4')

    def test_no_rename_keeps_runtime_roles_and_does_not_reboot(self):
        for args in ({}, {'mesh': ('wlan0',), 'band5': 'wlan0'},
                     {'halow': ('wlan2',)}, {'nonmesh': ('wlan3',)},
                     {'mesh': ('wlan0', 'wlan1'), 'halow': ('wlan2',),
                      'nonmesh': ('wlan3',), 'band24': 'wlan0', 'band5': 'wlan1'}):
            with self.subTest(hardware=args):
                result = self.setup_run(**args)
                self.assertNotIn('REBOOT', result.stdout)
                self.assertIn('COMPLETED', result.stdout)
                self.assertEqual(self.roles(), self.roles('before'))

    def test_skipped_or_failed_reboot_keeps_runtime_names_including_ap(self):
        for enable, reboot in ((False, True), (True, False)):
            with self.subTest(enable=enable, reboot=reboot):
                result = self.setup_run(mesh=('wlan4', 'wlan5'), halow=('wlan0',),
                                        band24='wlan4', band5='wlan5', ap=True,
                                        enable=enable, reboot=reboot)
                self.assertIn('COMPLETED', result.stdout)
                self.assertEqual(self.roles(), self.roles('before'))
                self.assertFalse((self.root / 'var/lib/radio-setup-reboot-pending').exists())
                if enable:
                    self.assertEqual(self.roles('at-reboot')['halow_if'], ['wlan2'])

    def test_role_rewrite_follows_all_live_consumers(self):
        start = SETUP.index('if [ "$needs_rerun" -eq 1 ] && [ -f /var/lib/radio-setup-reboot-pending ]; then')
        self.assertLess(SETUP.index('systemctl restart alfred.service'), start)
        self.assertLess(SETUP.index('provision_try "MT7916 firmware setup failed"'), start)
        between = section(SETUP, 'provision_try "cannot stage renamed radio roles"', 'exit 0\n    fi\n    restore_runtime_roles')
        self.assertNotRegex(between, r'/var/lib/|(?:mesh|halow|nonmesh)_ifaces')

    def test_partial_role_write_failure_restores_runtime_names_without_reboot(self):
        body = '''
declare -A pinned_names=([wlan4]=wlan0)
needs_rerun=1
printf 'boot-one\n' > /var/lib/radio-setup-reboot-pending
sleep() { :; }
provision_try() { shift; "$@"; }
reboot() { echo REBOOT; }
printf() {
    if [[ "$*" == *wlan0* ]]; then
        return 1
    fi
    builtin printf "$@"
}
'''
        for role in ROLES:
            value = 'wlan4:wlan4\n' if role == 'iface_map' else 'wlan4\n'
            (self.root / 'var/lib' / role).write_text(value)
        before = self.roles()
        body += section(SETUP, 'declare -A runtime_roles=()', '# Start the services enabled above')
        result = self.shell(body)
        self.assertNotIn('REBOOT', result.stdout)
        self.assertEqual(self.roles(), before)
        self.assertFalse((self.root / 'var/lib/radio-setup-reboot-pending').exists())

    def test_only_unchanged_pre_restart_failures_are_reset_after_rename_boot(self):
        snapshot = section(SETUP, '# Keep only failures', 'current_mesh=')
        self.assertLess(SETUP.index('systemctl restart alfred.service'),
                        SETUP.index('\nreset_rename_failures\n'))
        self.assertLess(SETUP.index('manet-wait-radios.py'), SETUP.index('# Keep only failures'))
        self.assertLess(SETUP.index('# Keep only failures'), SETUP.index("cleanup_iface_service '"))
        for marker in (None, 'boot-two', 'boot-one'):
            with self.subTest(marker=marker):
                pending = self.root / 'var/lib/radio-setup-reboot-pending'
                pending.unlink(missing_ok=True)
                if marker:
                    pending.write_text(marker + '\n')
                boot_id = self.root / 'boot-id'
                boot_id.write_text('boot-two\n')
                body = '''
restarted=0
systemctl() {
    case "$1" in
        list-units)
            printf '%s loaded failed failed radio\n' wpa_supplicant@wlan2.service \\
                wpa_supplicant-s1g-wlan0.service hostapd.service wpa_supplicant@wlan1.service \\
                wpa_supplicant@wlan4.service wpa_supplicant-s1g-wlan5.service
            ;;
        show)
            if [[ $restarted == 1 && $2 == wpa_supplicant@wlan1.service ]]; then
                printf 'ActiveState=failed\nStateChangeTimestampMonotonic=200\n'
            elif [[ $restarted == 1 && $2 == wpa_supplicant@wlan4.service ]]; then
                printf 'ActiveState=active\nStateChangeTimestampMonotonic=200\n'
            elif [[ $restarted == 1 && $2 == wpa_supplicant-s1g-wlan5.service ]]; then
                return 1
            else
                printf 'ActiveState=failed\nStateChangeTimestampMonotonic=100\n'
            fi
            ;;
        reset-failed) echo "RESET:$2" ;;
        *) exit 99 ;;
    esac
}
'''
                body += snapshot.replace('/proc/sys/kernel/random/boot_id', str(boot_id))
                body += '\nrestarted=1\nreset_rename_failures\n'
                result = self.shell(body)
                cleared = set(result.stdout.splitlines())
                expected = {'RESET:wpa_supplicant@wlan2.service',
                            'RESET:wpa_supplicant-s1g-wlan0.service', 'RESET:hostapd.service'}
                self.assertEqual(cleared, expected if marker == 'boot-one' else set())


class BatmanWaitTests(unittest.TestCase):
    def shell(self, body):
        result = subprocess.run(['bash', '-c', body], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_missing_role_netdev_waits_ten_seconds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'mesh_if').write_text('absent\n')
            for role in ('halow_if', 'no_mesh_if'):
                (root / role).write_text('')
            body = function(BATMAN, 'refresh_interfaces')
            body = body.replace('/var/lib', directory).replace('/sys/class/net', directory)
            result = self.shell('set -e\nsleep() { echo "SLEEP:$1"; }\n' + body +
                                '\nrefresh_interfaces\n')
            self.assertEqual(result.stdout.count('SLEEP:1'), 10)
            self.assertIn('after 10s', result.stderr)

    def run_start(self, ready):
        body = '''
set -e
refresh_interfaces() { STANDARD_MESH_INTERFACES=wifi; HALOW_INTERFACES=halow; NONMESH_INTERFACES=; }
radio_iface_enabled() { return 0; }
systemctl() { return 0; }
networkctl() { :; }
sleep() { echo "SLEEP:$1"; }
cat() { return 1; }
batctl() { echo "BATCTL:$*"; [[ "$*" != *'if add'* ]]; }
ip() { [[ "$*" != 'link show halow' ]] || return ''' + ('0' if ready else '1') + '''; }
iw() { ''' + ("echo 'type mesh point'" if ready else 'return 1') + '''; }
'''
        return self.shell(body + function(BATMAN, 'start') + '\nstart\n')

    def test_netdev_and_mesh_mode_timeouts_each_wait_ten_seconds_and_skip_add(self):
        result = self.run_start(False)
        self.assertEqual(result.stdout.count('SLEEP:1'), 20)
        self.assertEqual(result.stderr.count('after 10s'), 2)
        self.assertNotIn('BATCTL:bat0 if add', result.stdout)
        self.assertNotIn('SLEEP:2', result.stdout)

    def test_healthy_radios_skip_waits_and_keep_settle_and_batctl_retries(self):
        result = self.run_start(True)
        self.assertEqual(result.stdout.count('SLEEP:2'), 1)
        for radio in ('wifi', 'halow'):
            self.assertEqual(result.stdout.count('BATCTL:bat0 if add ' + radio), 5)
        self.assertEqual(result.stdout.count('SLEEP:1'), 10)
        self.assertNotIn('Timed out', result.stderr)


if __name__ == '__main__':
    unittest.main()
