"""Exercise AP/DNS setup scripts in isolated roots with recorded service calls."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parent
SYSTEMD = TOOLS.parent / 'systemd'


class PreparationTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='manet-network-prep-')
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for path in ('etc/dnsmasq.d', 'etc/systemd', 'run/systemd/resolve',
                     'var/lib/misc', 'usr/local/bin', 'tmp'):
            (self.root / path).mkdir(parents=True, exist_ok=True)
        self.events = self.root / 'events'
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        TEST_EVENTS=str(self.events), TEST_FAIL='', TEST_AP_ACTIVE='0')
        for name in ('systemctl', 'ip', 'iw', 'unblock-wifi-rfkill.sh'):
            target = self.bin / name
            if name == 'unblock-wifi-rfkill.sh':
                target = self.root / 'usr/local/bin' / name
            target.write_text(f'#!{sys.executable}\n' + '''
import os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = ' '.join(sys.argv[1:])
with open(os.environ['TEST_EVENTS'], 'a') as out:
    out.write(name + ' ' + args + '\\n')
if name + ' ' + args == os.environ['TEST_FAIL']:
    sys.exit(1)
if name == 'systemctl' and args == 'is-active --quiet hostapd.service':
    sys.exit(0 if os.environ['TEST_AP_ACTIVE'] == '1' else 3)
if name == 'systemctl' and args == 'is-enabled dnsmasq.service':
    print('enabled')
''')
            target.chmod(0o755)
        (self.root / 'var/lib/ap_interface').write_text('wlan3\n')

    def isolated(self, text):
        # Only file paths are redirected. The real shell logic is unchanged.
        return re.sub(r'/(?:etc|run|var|usr/local/bin|tmp)/',
                      lambda match: str(self.root) + match[0], text)

    def run_script(self, name):
        script = self.root / name
        script.write_text(self.isolated((TOOLS / name).read_text()))
        return subprocess.run(['bash', str(script)], env=self.env, text=True,
                              capture_output=True, timeout=15)

    def history(self):
        return self.events.read_text().splitlines() if self.events.exists() else []

    def test_ap_stops_supplicant_and_detaches_before_changing_mode(self):
        result = self.run_script('prepare-ap-iface.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.history(), [
            'systemctl is-active --quiet hostapd.service',
            'unblock-wifi-rfkill.sh ',
            'systemctl stop wpa_supplicant@wlan3.service',
            'ip link set wlan3 down', 'ip link set wlan3 nomaster',
            'iw dev wlan3 set type managed', 'ip link set wlan3 up'])
        # A restart must prepare again even if the old preparation unit remains
        # active; hostapd's ExecStartPre is independent of that oneshot state.
        result = self.run_script('prepare-ap-iface.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.history().count('iw dev wlan3 set type managed'), 2)

    def test_optional_preparation_cannot_disrupt_a_running_ap(self):
        self.env['TEST_AP_ACTIVE'] = '1'
        self.assertEqual(self.run_script('prepare-ap-iface.sh').returncode, 0)
        self.assertEqual(self.history(), ['systemctl is-active --quiet hostapd.service'])

    def test_failed_preparation_stops_before_later_operations(self):
        for failed, forbidden in [
            ('systemctl stop wpa_supplicant@wlan3.service', 'ip link set wlan3 down'),
            ('iw dev wlan3 set type managed', 'ip link set wlan3 up')]:
            with self.subTest(failed=failed):
                self.events.write_text('')
                self.env['TEST_FAIL'] = failed
                self.assertNotEqual(self.run_script('prepare-ap-iface.sh').returncode, 0)
                self.assertNotIn(forbidden, self.history())

    def test_invalid_ap_role_does_not_touch_a_device(self):
        (self.root / 'var/lib/ap_interface').write_text('wlan3 bad\n')
        self.assertNotEqual(self.run_script('prepare-ap-iface.sh').returncode, 0)
        self.assertFalse(any(row.startswith(('iw ', 'ip ')) for row in self.history()))

    def test_dns_setup_replaces_static_resolver_once_and_preserves_upstream_data(self):
        (self.root / 'etc/resolv.conf').write_text('nameserver 1.1.1.1\n')
        upstream = self.root / 'run/systemd/resolve/resolv.conf'
        upstream.write_text('nameserver 192.0.2.53\n')
        result = self.run_script('manet-dns-setup.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.root / 'etc/systemd/resolved.conf.d/60-manet-dns.conf'
        self.assertIn('DNS=\nFallbackDNS=1.1.1.1 8.8.8.8\n', config.read_text())
        self.assertEqual((self.root / 'etc/resolv.conf').resolve(), upstream)
        self.assertEqual(upstream.read_text(), 'nameserver 192.0.2.53\n')
        original_stat = config.stat().st_mtime_ns
        self.assertEqual(self.run_script('manet-dns-setup.sh').returncode, 0)
        self.assertEqual(config.stat().st_mtime_ns, original_stat)
        self.assertEqual(self.history().count('systemctl restart systemd-resolved.service'), 1)
        self.assertFalse(list(config.parent.glob('*.??????')))
        # A renewed DHCP lease is immediately reflected in host lookups.
        upstream.write_text('nameserver 192.0.2.54\n')
        self.assertEqual((self.root / 'etc/resolv.conf').read_text(), 'nameserver 192.0.2.54\n')

    def test_dns_start_failure_is_reported(self):
        self.env['TEST_FAIL'] = 'systemctl restart systemd-resolved.service'
        self.assertNotEqual(self.run_script('manet-dns-setup.sh').returncode, 0)

    def test_generated_dnsmasq_config_stays_current_after_lease_change(self):
        source = (TOOLS / 'mesh-ip-manager.sh').read_text()
        function = re.search(r'^configure_dnsmasq\(\) \{\n.*?^\}', source, re.M | re.S)[0]
        start = source.index('                # Only reconfigure dnsmasq if the config has changed')
        end = source.index('                # The web UI is restricted', start)
        check = source[start:end]
        for mumble, mtx in [('', ''), ('10.30.0.2', '10.30.0.3')]:
            with self.subTest(mumble=mumble, mtx=mtx):
                self.events.write_text('')
                body = '''
log() { :; }
configure_ebtables_dhcp_isolation() { echo UNEXPECTED_DHCP_RESET; }
MUMBLE_VIP="$TEST_MUMBLE"; MTX_VIP="$TEST_MTX"
BR0_PRIMARY=10.30.0.6; BR0_SECONDARY=10.30.0.7
DHCP_START=10.30.0.8; DHCP_END=10.30.0.15
''' + function + '''
configure_dnsmasq "$BR0_PRIMARY" "$BR0_SECONDARY" "$DHCP_START" "$DHCP_END"
echo KEEP_LEASE > /var/lib/misc/dnsmasq.leases
echo 'nameserver 192.0.2.54' > /run/systemd/resolve/resolv.conf
''' + check + '''
echo "$NEEDS_DNSMASQ_UPDATE"
cat /var/lib/misc/dnsmasq.leases
'''
                result = subprocess.run(['bash', '-c', self.isolated(body)], capture_output=True,
                                        text=True, timeout=10,
                                        env=dict(self.env, TEST_MUMBLE=mumble, TEST_MTX=mtx))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, 'false\nKEEP_LEASE\n')
                self.assertEqual(self.history().count('systemctl restart dnsmasq.service'), 1)
                config = self.root / 'etc/dnsmasq.d/mesh-eud.conf'
                self.assertIn('address=/manet.local/10.30.0.7', config.read_text())
                self.assertNotRegex(config.read_text(), r'(?m)^server=')
                dnsmasq = shutil.which('dnsmasq')
                if dnsmasq:
                    result = subprocess.run([dnsmasq, '--test', '-C', str(config)],
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_service_dropins_run_preparation_on_every_start(self):
        for unit, script in [('hostapd', 'prepare-ap-iface.sh'), ('dnsmasq', 'manet-dns-setup.sh')]:
            files = list((SYSTEMD / (unit + '.service.d')).glob('*.conf'))
            text = ''.join(path.read_text() for path in files)
            self.assertIn(f'ExecStartPre=/usr/local/bin/{script}\n', text)
            # No '-' prefix: a failed preparation must prevent service startup.
            self.assertNotIn(f'ExecStartPre=-/usr/local/bin/{script}', text)

    def test_both_templates_keep_dynamic_dns_through_reboot(self):
        for name in ('firstrun.sh.template', 'rock3a-provision.sh.template'):
            text = (TOOLS.parent / 'provisioning' / name).read_text()
            self.assertIn('/usr/local/bin/manet-dns-setup.sh || exit 1', text)
            self.assertNotRegex(text, r'(?m)^[^#\n]*nameserver 1\.1\.1\.1')


if __name__ == '__main__':
    unittest.main()
