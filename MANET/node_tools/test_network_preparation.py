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
    """Check local bridge, address and DNS preparation."""
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
        for name in ('systemctl', 'ip', 'iw', 'batctl', 'unblock-wifi-rfkill.sh'):
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
if name == 'systemctl' and os.environ.get('TEST_ROOT'):
    active = Path(os.environ['TEST_ROOT']) / 'run/dnsmasq-active'
    if args == 'is-active --quiet dnsmasq.service':
        sys.exit(0 if active.exists() else 3)
    if args in ('start dnsmasq.service', 'restart dnsmasq.service'):
        active.touch()
    if args == 'stop dnsmasq.service':
        active.unlink(missing_ok=True)
''')
            target.chmod(0o755)
        (self.root / 'var/lib/ap_interface').write_text('wlan3\n')
        (self.root / 'etc/mesh.conf').write_text('eud=auto\nacs=n\n')
        (self.root / 'var/lib/mesh_if').write_text('')
        helper = self.root / 'usr/local/bin/manet_ap_mesh.py'
        helper.write_text(self.isolated((TOOLS / 'manet_ap_mesh.py').read_text()))
        self.env.update(PYTHONPATH=str(TOOLS),
                        MANET_IFACE_STATE_DIR=str(self.root / 'var/lib'),
                        MANET_ACS_LOCK_FILE=str(self.root / 'run/channel-election.lock'),
                        MANET_MESH_CONF=str(self.root / 'etc/mesh.conf'))

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
            'batctl if',
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
        """Refresh static DNS bindings after the local allocation changes."""
        source = (TOOLS / 'mesh-ip-manager.sh').read_text()
        source += (TOOLS / 'manet-common.sh').read_text()
        function = '\n'.join(re.search(r'^' + name + r'\(\) \{\n.*?^\}', source, re.M | re.S)[0]
                             for name in ('ip_to_int', 'int_to_ip', 'valid_eud_gateway',
                                          'update_avahi_host', 'configure_dnsmasq', 'eud_ready', 'ensure_dnsmasq_running'))
        start = source.index('                # Only reconfigure dnsmasq if the config has changed')
        end = source.index('                # The web UI is restricted', start)
        check = source[start:end]
        for mumble, mtx in [('', ''), ('10.30.0.2', '10.30.0.3')]:
            with self.subTest(mumble=mumble, mtx=mtx):
                self.events.write_text('')
                body = '''
log() { :; }
python3() { return 0; }
MUMBLE_VIP="$TEST_MUMBLE"; MTX_VIP="$TEST_MTX"
BR0_PRIMARY=10.30.0.6; BR0_SECONDARY=10.30.0.7
DHCP_START=10.30.0.8; DHCP_END=10.30.0.15
''' + function + '''
configure_dnsmasq "$BR0_PRIMARY" "$BR0_SECONDARY" "$DHCP_START" "$DHCP_END"
echo KEEP_LEASE > /var/lib/misc/dnsmasq.leases
configure_dnsmasq "$BR0_PRIMARY" "$BR0_SECONDARY" "$DHCP_START" "$DHCP_END"
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
                self.assertIn('\nbind-dynamic\n', config.read_text())
                self.assertNotIn('\nbind-interfaces\n', config.read_text())
                self.assertNotRegex(config.read_text(), r'(?m)^server=')
                dnsmasq = shutil.which('dnsmasq')
                if dnsmasq:
                    result = subprocess.run([dnsmasq, '--test', '-C', str(config)],
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_existing_static_dns_binding_is_reconfigured_once(self):
        """Reconcile a changed static DNS binding once."""
        source = (TOOLS / 'mesh-ip-manager.sh').read_text()
        source += (TOOLS / 'manet-common.sh').read_text()
        start = source.index('                # Only reconfigure dnsmasq if the config has changed')
        end = source.index('                # The web UI is restricted', start)
        check = source[start:end]
        config = self.root / 'etc/dnsmasq.d/mesh-eud.conf'
        config.write_text(self.isolated('''interface=br0
bind-interfaces
dhcp-range=10.30.0.8,10.30.0.15,4m
dhcp-option=3,10.30.0.7
address=/manet.local/10.30.0.7
resolv-file=/run/systemd/resolve/resolv.conf
clear-on-reload
'''))
        body = '''
log() { :; }
ensure_dnsmasq_running() { :; }
update_avahi_host() { :; }
configure_dnsmasq() { sed -i s/bind-interfaces/bind-dynamic/ "$DNSMASQ_CONF"; echo REBOUND; }
BR0_PRIMARY=10.30.0.6; BR0_SECONDARY=10.30.0.7
DHCP_START=10.30.0.8; DHCP_END=10.30.0.15
''' + check + check
        result = subprocess.run(['bash', '-c', self.isolated(body)], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'REBOUND\n')

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


    def cidr_fixture(self):
        """Prepare a saved allocation, live VIPs and a stateful ip fixture."""
        import json
        for name in ('etc/avahi', 'sys/class/net/br0/brif/wlan1',
                     'sys/class/net/wlan1', 'var/run'):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        mac = self.root / 'sys/class/net/br0/address'
        mac.write_text('02:00:00:00:00:01')
        (self.root / 'sys/class/net/br0/brif/wlan1/state').write_text('3')
        (self.root / 'sys/class/net/wlan1/carrier').write_text('1')
        (self.root / 'etc/mesh.conf').write_text(
            'ipv4_network=10.45.0.0/16\nmax_euds_per_node=5\neud=wireless\n')
        self.allocation = self.root / 'etc/mesh_ipv4_state'
        self.allocation.write_text('PERSISTENT_IPV4="10.44.0.13"\n'
                                   'PERSISTENT_CHUNK="1"\n'
                                   'PERSISTENT_NETWORK="10.44.0.0/16"\n')
        self.live = self.root / 'run/addresses.json'
        self.live.write_text(json.dumps({
            'br0': ['10.44.0.13/16', '10.44.0.14/16',
                    '10.44.0.2/16', '10.44.0.3/16',
                    '192.0.2.10/24', 'fd01:ed20:ecb4::1/64',
                    'fd01:ed20:ecb4::64/128', 'fe80::1/64'],
            'end0': ['192.168.1.10/24']}))
        (self.root / 'etc/dnsmasq.d/mesh-eud.conf').write_text(
            'dhcp-range=10.44.0.15,10.44.0.19,4m\n'
            'address=/mumble.local/10.44.0.3\n')
        (self.root / 'etc/avahi/hosts').write_text(
            '10.44.0.14 manet.local\n192.0.2.5 printer.local\n')
        (self.root / 'var/lib/misc/dnsmasq.leases').write_text('old leases')
        for name in ('my_ipv4_chunk', 'my_ipv4_chunk_size'):
            (self.root / 'var/run' / name).write_text('1')
        (self.root / 'claims').write_text('')
        (self.root / 'run/dnsmasq-active').touch()
        self.events = self.root / 'run/events'
        self.env['TEST_EVENTS'] = str(self.events)
        for interface in ('br0', 'all'):
            policy = self.root / 'proc/sys/net/ipv4/conf' / interface
            policy.mkdir(parents=True, exist_ok=True)
            (policy / 'promote_secondaries').write_text(
                '1\n' if interface == 'br0' else '0\n')
        (self.root / 'proc/self').mkdir(exist_ok=True)
        (self.root / 'proc/self/status').write_text('CapEff:\t0000300b\n')
        (self.root / 'proc/uptime').write_text('100.00 100.00\n')
        self.env.update(
            MANET_TOOLS_DIR=str(TOOLS), MANET_IP_CHECKED='1',
            MANET_IP_STARTUP_READY='1', MANET_IP_ISOLATED='1',
            MANET_IP_ADDRESS_OK='1', MANET_IP_PRIMARY='',
            MANET_SYS_NET=str(self.root / 'sys/class/net'),
            MESH_CLAIMED_CHUNKS_FILE=str(self.root / 'claims'),
            TEST_ROOT=str(self.root),
            PATH=str(self.bin) + os.pathsep + str(TOOLS) +
            os.pathsep + os.environ['PATH'])
        (self.bin / 'ip').write_text(f'#!{sys.executable}\n' + '''
import ipaddress, json, os, signal, sys
from pathlib import Path
root = Path(os.environ['TEST_ROOT'])
args = sys.argv[1:]
with (root / 'run/events').open('a') as out:
    out.write('ip ' + ' '.join(args) + '\\n')
p = root / 'run/addresses.json'
state = json.loads(p.read_text())
iface = args[args.index('dev') + 1]
if 'show' in args:
    seen = set()
    for a in state[iface]:
        if '-4' in args and ':' in a: continue
        family = 'inet6' if ':' in a else 'inet'
        network = str(ipaddress.ip_interface(a).network)
        flags = ' secondary' if network in seen else ''
        seen.add(network)
        print('2: ' + iface + ' ' + family + ' ' + a +
              ' scope global' + flags)
else:
    action = args[1]
    address = args[2]
    if os.environ.get('TEST_DELETE_FAIL') == address and action == 'del':
        sys.exit(1)
    if action == 'del':
        if address not in state[iface]: sys.exit(2)
        # Model Linux's implicit secondary deletion without promotion.
        net = ipaddress.ip_interface(address).network
        peers = [a for a in state[iface]
                 if ipaddress.ip_interface(a).network == net]
        policy = root / 'proc/sys/net/ipv4/conf'
        promote = any((policy / name / 'promote_secondaries').read_text()
                      .strip() == '1' for name in ('br0', 'all'))
        if not promote and peers[0] == address:
            state[iface] = [a for a in state[iface]
                            if ipaddress.ip_interface(a).network != net]
        else:
            state[iface].remove(address)
    elif action == 'add':
        if address in state[iface]: sys.exit(2)
        state[iface].append(address)
    p.write_text(json.dumps(state))
    crash = os.environ.get('TEST_CRASH_AFTER')
    if crash == address or (crash == 'first-add' and action == 'add'):
        os.kill(os.getppid(), signal.SIGKILL)
''')
        sysctl = self.bin / 'sysctl'
        sysctl.write_text('#!/bin/bash\n'
                         'echo "sysctl $*" >> "$TEST_EVENTS"\n'
                         'echo "kernel tunable writes forbidden" >&2\n'
                         'exit 1\n')
        sysctl.chmod(0o755)

    def allocate_cidr(self, advance=16, sandbox=False, **env):
        """Run the full shell allocator using its real primary-address rule."""
        import json
        from manet_node_ipv4 import primary_ipv4, read_values
        addresses = json.loads(self.live.read_text())['br0']
        self.env['MANET_IP_PRIMARY'] = primary_ipv4(
            read_values(self.allocation),
            read_values(self.root / 'etc/mesh.conf'),
            [a.split('/')[0] for a in addresses])
        script = self.root / 'allocator.sh'
        source = self.isolated((TOOLS / 'mesh-ip-manager.sh').read_text())
        source = re.sub(r'/(?:sys|proc)/',
                        lambda m: str(self.root) + m[0], source)
        script.write_text(source)
        uptime = self.root / 'proc/uptime'
        now = float(uptime.read_text().split()[0]) + advance
        uptime.write_text(f'{now:.2f} {now:.2f}\n')
        command = ['bash', str(script)]
        if sandbox:
            command = self.retirement_sandbox() + command
        return subprocess.run(command,
                              env=dict(self.env, **env), text=True,
                              capture_output=True, timeout=15)

    def retirement_sandbox(self):
        """Enforce the unit's filesystem policy and both capability lines."""
        if not shutil.which('bwrap'):
            self.skipTest('read-only runtime sandbox requires bubblewrap')
        unit = (SYSTEMD / 'mesh-channel-agreement.service').read_text()
        self.assertIn('ProtectSystem=strict\n', unit)
        self.assertIn('ProtectKernelTunables=yes\n', unit)
        self.assertIn('ProtectKernelModules=yes\n', unit)
        self.assertIn('NoNewPrivileges=yes\n', unit)
        command = ['bwrap', '--ro-bind', '/', '/', '--dev', '/dev',
                   '--proc', '/proc', '--ro-bind', '/proc/sys', '/proc/sys',
                   '--unshare-user', '--uid', '0', '--gid', '0',
                   '--cap-drop', 'ALL']
        for line in unit.splitlines():
            if line.startswith('CapabilityBoundingSet='):
                for capability in line.split('=', 1)[1].split():
                    command += ['--cap-add', capability]
            if line.startswith('ReadWritePaths='):
                for directory in line.split('=', 1)[1].split():
                    # systemd's '-' prefix permits an absent optional mount,
                    # such as the DFS debugfs ABI on an unsupported kernel.
                    optional = directory.startswith('-')
                    path = str(self.root / directory.lstrip('-').lstrip('/'))
                    if optional and not Path(path).exists():
                        continue
                    command += ['--bind', path, path]
        # /var/run aliases /run on Debian. The fixture uses a directory.
        path = str(self.root / 'var/run')
        command += ['--bind', path, path]
        return command

    def assert_old_dhcp(self):
        """A recoverable failure keeps the old gateway, pool and leases."""
        import json
        live = json.loads(self.live.read_text())['br0']
        for address in ('10.44.0.13', '10.44.0.14',
                        '10.44.0.2', '10.44.0.3'):
            self.assertIn(address + '/16', live)
        self.assertTrue((self.root / 'run/dnsmasq-active').exists())
        self.assertIn('dhcp-range=10.44.0.15,10.44.0.19,4m',
                      (self.root / 'etc/dnsmasq.d/mesh-eud.conf').read_text())
        self.assertEqual((self.root / 'var/lib/misc/dnsmasq.leases')
                         .read_text(), 'old leases')
        self.assertIn('10.44.0.14 manet.local',
                      (self.root / 'etc/avahi/hosts').read_text())
        self.assertIn('10.44.0.0/16', self.allocation.read_text())

    def test_retirement_under_runtime_filesystem_and_capability_sandbox(self):
        """Readonly proc/sys, modules and root must still allow retirement."""
        self.cidr_fixture()
        forbidden = self.root / 'proc/sys/net/ipv4/conf/br0'
        check = subprocess.run(
            self.retirement_sandbox() + ['bash', '-c',
                'printf 0 > "$1/promote_secondaries"', 'check',
                str(forbidden)], capture_output=True, text=True)
        self.assertNotEqual(check.returncode, 0)
        self.assertIn('Read-only file system', check.stderr)
        status = subprocess.run(
            self.retirement_sandbox() + ['cat', '/proc/self/status'],
            capture_output=True, text=True, check=True).stdout
        self.assertRegex(status, r'CapEff:\s+000000000000300b\n')
        self.assertRegex(status, r'NoNewPrivs:\s+1\n')
        result = self.allocate_cidr(sandbox=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_cidr_retired()
        self.assertFalse(any(s.startswith('sysctl ') for s in self.history()))

    def test_bench_subnet_change_without_hosted_services_in_sandbox(self):
        """Reproduce 10.30.2/24 to 10.31.2/24 with no local VIPs."""
        import json
        self.cidr_fixture()
        for name in ('etc/mesh.conf', 'etc/mesh_ipv4_state',
                     'etc/dnsmasq.d/mesh-eud.conf', 'etc/avahi/hosts',
                     'run/addresses.json'):
            path = self.root / name
            path.write_text(path.read_text().replace('10.44.0.', '10.30.2.')
                            .replace('10.45.0.', '10.31.2.')
                            .replace('/16', '/24'))
        live = json.loads(self.live.read_text())
        live['br0'].remove('10.30.2.2/24')
        live['br0'].remove('10.30.2.3/24')
        self.live.write_text(json.dumps(live))
        result = self.allocate_cidr(sandbox=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('10.30.2.', self.live.read_text())
        self.assertIn('10.31.2.', self.live.read_text())
        self.assertIn('10.31.2.0/24', self.allocation.read_text())
        self.assertIn('dhcp-range=10.31.2.',
                      (self.root / 'etc/dnsmasq.d/mesh-eud.conf').read_text())
        self.assertTrue((self.root / 'run/dnsmasq-active').exists())

    def test_retirement_without_promotion_deletes_secondaries_first(self):
        """An all-owned prefix needs no kernel tunable changes."""
        self.cidr_fixture()
        policy = self.root / 'proc/sys/net/ipv4/conf/br0/promote_secondaries'
        policy.write_text('0\n')
        result = self.allocate_cidr(sandbox=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_cidr_retired()
        deletes = [s for s in self.history() if s.startswith('ip addr del')]
        self.assertEqual(deletes[-1], 'ip addr del 10.44.0.13/16 dev br0')

    def test_unowned_primary_needs_no_promotion_and_all_can_enable_it(self):
        """Keep aliases with either an owned or unowned kernel primary."""
        import json
        for policy_all in (0, 1):
            with self.subTest(policy_all=policy_all):
                self.cidr_fixture()
                policy = self.root / 'proc/sys/net/ipv4/conf'
                (policy / 'br0/promote_secondaries').write_text('0\n')
                (policy / 'all/promote_secondaries').write_text(
                    f'{policy_all}\n')
                live = json.loads(self.live.read_text())
                position = 0 if policy_all == 0 else len(live['br0'])
                live['br0'].insert(position, '10.44.20.20/16')
                self.live.write_text(json.dumps(live))
                result = self.allocate_cidr(sandbox=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('10.44.20.20/16', self.live.read_text())
                self.assertNotIn('10.44.0.13/16', self.live.read_text())

    def test_unsafe_alias_and_missing_capability_defer_before_disruption(self):
        """Preflight leaves DHCP, leases and addresses completely untouched."""
        import json
        for condition in ('alias', 'capability', 'permission', 'ownership'):
            with self.subTest(condition=condition):
                self.cidr_fixture()
                if condition == 'alias':
                    policy = self.root / 'proc/sys/net/ipv4/conf/br0'
                    (policy / 'promote_secondaries').write_text('0\n')
                    live = json.loads(self.live.read_text())
                    live['br0'].append('10.44.20.20/16')
                    self.live.write_text(json.dumps(live))
                elif condition == 'capability':
                    (self.root / 'proc/self/status').write_text('CapEff: 0\n')
                elif condition == 'permission':
                    path = self.root / 'etc/dnsmasq.d/mesh-eud.conf'
                    path.chmod(0o444)
                else:
                    self.allocation.write_text(self.allocation.read_text()
                                               .replace('.13', '.3'))
                self.events.write_text('')
                result = self.allocate_cidr()
                self.assertNotEqual(result.returncode, 0)
                self.assert_old_dhcp()
                self.assertNotIn('systemctl stop dnsmasq.service',
                                 self.history())
                self.assertFalse(any(s.startswith('ip addr del')
                                     for s in self.history()))
                (self.root / 'etc/dnsmasq.d/mesh-eud.conf').chmod(0o644)
                retry = self.root / 'run/manet-ip-retirement.retry'
                retry.unlink(missing_ok=True)

    def test_retirement_failure_restores_dhcp_files_and_owned_addresses(self):
        """Failures after partial deletion or name withdrawal roll back."""
        failures = ({'TEST_DELETE_FAIL': '10.44.0.3/16'},
                    {'TEST_FAIL': 'systemctl reload avahi-daemon.service'},
                    {'TEST_FAIL': 'systemctl stop dnsmasq.service'})
        for failure in failures:
            with self.subTest(failure=failure):
                self.cidr_fixture()
                result = self.allocate_cidr(**failure)
                self.assertNotEqual(result.returncode, 0)
                self.assert_old_dhcp()
                result = self.allocate_cidr()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_cidr_retired()

    def test_retirement_commit_failure_restores_old_persistent_state(self):
        """Even a failure after the state rename restores the old DHCP pool."""
        self.cidr_fixture()
        sync = self.bin / 'sync'
        sync.write_text('#!/bin/bash\n'
                        'if [ "$2" = "$TEST_SYNC_FAIL" ]; then exit 1; fi\n'
                        'exec /usr/bin/sync "$@"\n')
        sync.chmod(0o755)
        result = self.allocate_cidr(TEST_SYNC_FAIL=str(self.root / 'etc'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('cannot commit retired allocation', result.stderr)
        self.assert_old_dhcp()
        self.assertEqual(self.allocate_cidr().returncode, 0)
        self.assert_cidr_retired()

    def test_failed_file_removal_reuses_intact_old_dhcp_configuration(self):
        """An unreplaceable but unchanged pool can still resume service."""
        self.cidr_fixture()
        for command in ('rm', 'mv'):
            stub = self.bin / command
            stub.write_text('#!/bin/bash\n'
                            'for argument in "$@"; do\n'
                            '  [ "$argument" != "$TEST_FIXED_FILE" ] || '
                            'exit 1\n'
                            'done\n'
                            f'exec /usr/bin/{command} "$@"\n')
            stub.chmod(0o755)
        result = self.allocate_cidr(
            TEST_FIXED_FILE=str(self.root / 'etc/dnsmasq.d/mesh-eud.conf'))
        self.assertNotEqual(result.returncode, 0)
        self.assert_old_dhcp()
        self.assertFalse((self.root / 'run/manet-ip-retirement/armed').exists())
        self.assertEqual(self.allocate_cidr().returncode, 0)
        self.assert_cidr_retired()

    def test_interrupted_retirement_recovers_when_config_is_reverted(self):
        """An armed checkpoint is recovered even with no pending CIDR change."""
        self.cidr_fixture()
        result = self.allocate_cidr(TEST_CRASH_AFTER='10.44.0.3/16')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.root / 'run/manet-ip-retirement/armed').exists())
        conf = self.root / 'etc/mesh.conf'
        conf.write_text(conf.read_text().replace('10.45.', '10.44.'))
        result = self.allocate_cidr(advance=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('10.44.0.13/16', self.live.read_text())
        self.assertIn('10.44.0.14/16', self.live.read_text())
        self.assertIn('10.44.0.0/16', self.allocation.read_text())
        self.assertTrue((self.root / 'run/dnsmasq-active').exists())
        self.assertFalse((self.root / 'run/manet-ip-retirement').exists())

    def test_failed_rollback_is_retained_and_retried_before_retirement(self):
        """A failed service restart cannot discard the recovery checkpoint."""
        self.cidr_fixture()
        result = self.allocate_cidr(
            TEST_DELETE_FAIL='10.44.0.3/16',
            TEST_FAIL='systemctl start dnsmasq.service')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('old DHCP not restored', result.stderr)
        self.assertTrue((self.root / 'run/manet-ip-retirement/armed').exists())
        self.events.write_text('')
        result = self.allocate_cidr()
        self.assertEqual(result.returncode, 0, result.stderr)
        history = self.history()
        self.assertLess(history.index('systemctl start dnsmasq.service'),
                        history.index('systemctl stop dnsmasq.service'))
        self.assert_cidr_retired()

    def test_retirement_backoff_and_distinct_failure_logging(self):
        """One-second discovery cannot repeat disruption or the same log."""
        self.cidr_fixture()
        failure = {'TEST_DELETE_FAIL': '10.44.0.3/16'}
        result = self.allocate_cidr(advance=0, **failure)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('cannot delete owned address', result.stderr)
        history = self.history()
        for _ in range(14):
            result = self.allocate_cidr(advance=1, **failure)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stderr, '')
            self.assertEqual(self.history(), history)
        result = self.allocate_cidr(advance=1, **failure)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotEqual(self.history(), history)
        self.assertEqual(result.stderr, '')
        self.assert_old_dhcp()
        result = self.allocate_cidr(TEST_DELETE_FAIL='10.44.0.2/16')
        self.assertIn('10.44.0.2/16', result.stderr)
        result = self.allocate_cidr()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_cidr_retired()

    def assert_cidr_retired(self):
        """Check addresses, their connected routes, DHCP, VIPs and state."""
        import ipaddress
        import json
        live = json.loads(self.live.read_text())
        self.assertFalse(any(a.startswith('10.44.') for a in live['br0']))
        routes = {str(ipaddress.ip_interface(a).network) for a in live['br0']}
        self.assertNotIn('10.44.0.0/16', routes)
        self.assertIn('10.45.0.0/16', routes)
        self.assertIn('192.0.2.10/24', live['br0'])
        self.assertIn('fd01:ed20:ecb4::1/64', live['br0'])
        self.assertIn('fd01:ed20:ecb4::64/128', live['br0'])
        self.assertIn('fe80::1/64', live['br0'])
        self.assertEqual(live['end0'], ['192.168.1.10/24'])
        dns = (self.root / 'etc/dnsmasq.d/mesh-eud.conf').read_text()
        self.assertNotIn('10.44.', dns)
        self.assertIn('dhcp-range=10.45.', dns)
        self.assertIn('address=/mumble.local/10.45.0.3', dns)
        self.assertIn('address=/mtx.local/10.45.0.2', dns)
        self.assertNotIn('10.44.', self.allocation.read_text())
        hosts = (self.root / 'etc/avahi/hosts').read_text()
        self.assertNotIn('10.44.', hosts)
        self.assertIn('192.0.2.5 printer.local', hosts)

    def test_cidr_change_retires_old_addresses_vips_routes_and_pool(self):
        """A /16 change preserves the new allocation and unrelated IPs."""
        self.cidr_fixture()
        result = self.allocate_cidr()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_cidr_retired()
        saved = self.live.read_text(), self.allocation.read_text()
        result = self.allocate_cidr()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(saved, (self.live.read_text(),
                                 self.allocation.read_text()))
        self.assert_cidr_retired()

    def test_cidr_retirement_retries_after_crash_or_failed_delete(self):
        """The old allocation survives until every owned old IP is gone."""
        for failure in ('TEST_CRASH_AFTER', 'TEST_DELETE_FAIL'):
            with self.subTest(failure=failure):
                self.cidr_fixture()
                result = self.allocate_cidr(**{failure: '10.44.0.14/16'})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('10.44.0.0/16', self.allocation.read_text())
                result = self.allocate_cidr()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_cidr_retired()

    def test_cidr_change_keeps_operator_alias_in_the_old_network(self):
        """Preserve unowned aliases and their kernel routes."""
        import json
        self.cidr_fixture()
        live = json.loads(self.live.read_text())
        live['br0'].append('10.44.20.20/16')
        self.live.write_text(json.dumps(live))
        result = self.allocate_cidr()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('10.44.20.20/16',
                      json.loads(self.live.read_text())['br0'])
        self.assertEqual(self.allocate_cidr().returncode, 0)
        self.assertIn('10.44.20.20/16',
                      json.loads(self.live.read_text())['br0'])

    def test_cidr_change_resumes_same_new_allocation_after_first_address(self):
        """New allocation intent survives interruption before gateway/DHCP."""
        from manet_node_ipv4 import read_values
        self.cidr_fixture()
        result = self.allocate_cidr(TEST_CRASH_AFTER='first-add')
        self.assertNotEqual(result.returncode, 0)
        saved = read_values(self.allocation)
        self.assertEqual(saved['PERSISTENT_NETWORK'], '10.45.0.0/16')
        result = self.allocate_cidr()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(read_values(self.allocation), saved)
        self.assert_cidr_retired()

    def test_cidr_retirement_waits_for_election_lock(self):
        """A running handover leaves durable state available for retry."""
        import fcntl
        self.cidr_fixture()
        with (self.root / 'var/run/mediamtx-election.lock').open('w') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            result = self.allocate_cidr()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('10.44.0.0/16', self.allocation.read_text())
        self.assertEqual(self.allocate_cidr().returncode, 0)
        self.assert_cidr_retired()


if __name__ == '__main__':
    unittest.main()
