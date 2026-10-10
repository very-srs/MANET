"""Pin shared helper contracts at their runtime callers."""

import importlib.util
import os
import re
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import manet_manage as manage

TOOLS = Path(__file__).resolve().parent


def load_script(name):
    """Import a flat installed script without starting its CLI."""
    spec = importlib.util.spec_from_file_location(name, TOOLS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


status = load_script('mesh-status')


class AssetHelperTests(unittest.TestCase):
    """Keep both web route implementations on the same asset contract."""

    def test_asset_paths_and_types(self):
        """Pin complete lookup results, including rejected paths."""
        urls = ['/assets/fer-logo', '/assets/fer-logo-white.svg',
                '/assets/fer-logo-black.png', '/assets/fer-logo.jpeg',
                '/assets/../fer-logo', '/manage/assets/fer-logo',
                '/assets/unknown', '/assets/fer-logo.PNG']
        black = '/usr/local/share/manet/fer-logo-black.png'
        white = '/usr/local/share/manet/fer-logo-white.png'
        for module in (manage, status):
            self.assertEqual([module.logo_asset_file(p) for p in urls],
                             [black, white, black, black, None, None,
                              None, black])
            self.assertEqual(
                [module.asset_content_type(p) for p in
                 ('x.svg', 'x.PNG', 'x.jpg', 'x.jpeg', 'x.webp', 'x', 'x.gif')],
                ['image/svg+xml', 'image/png', 'image/jpeg', 'image/jpeg',
                 'image/webp', 'application/octet-stream',
                 'application/octet-stream'])

    def test_asset_cache_token(self):
        """Pin metadata ordering and the missing-asset fallback."""
        for module in (manage, status):
            with patch('os.stat', side_effect=[
                    SimpleNamespace(st_mtime_ns=123, st_size=45), OSError()]):
                self.assertEqual(module.logo_asset_token(), '314886b9')
            with patch('os.stat', side_effect=OSError()):
                self.assertEqual(module.logo_asset_token(), '90c4715d')


class MotdHelperTests(unittest.TestCase):
    """Exercise the shared separator through actual symlink launch paths."""

    def test_motd_symlink_output(self):
        """Pin the full banner when provisioning has just begun."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'state'
            state.write_text('STATE=running\nPHASE=radio-setup\n')
            link = root / '50-manet-provision'
            link.symlink_to(TOOLS / 'manet-provision-status.sh')
            result = subprocess.run(
                ['bash', str(link)], capture_output=True, text=True,
                env={'PATH': '/usr/bin:/bin',
                     'MANET_PROVISION_STATE': str(state)}, timeout=5)
            rule = '  ' + '─' * 60 + '\n'
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout,
                             '\n' + rule +
                             '  MANET PROVISIONING IN PROGRESS: '
                             'do not disconnect\n' + rule +
                             '  Stage    : radio-setup\n'
                             '  Running  : unknown\n'
                             '  Keep Ethernet and power connected. '
                             'The node reboots itself\n'
                             '  several times; this can take about '
                             'ten minutes.\n'
                             '  Watch    : sudo tail -f '
                             '/var/log/radio-setup.log\n' + rule + '\n')
            self.assertEqual(result.stderr, '')

    def test_power_symlink_output(self):
        """Keep JSON and the complete power warning behind the MOTD link."""
        import os
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            probe = root / 'vcgencmd'
            probe.write_text('#!/bin/sh\nprintf "throttled=0x1\\n"\n')
            probe.chmod(0o755)
            link = root / '55-manet-power'
            link.symlink_to(TOOLS / 'manet-power-status.sh')
            env = dict(os.environ, PATH=str(root) + ':/usr/bin:/bin')
            result = subprocess.run(['bash', str(link), '--json'], env=env,
                                    text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout,
                             '{"available": true, "raw": "0x1", '
                             '"state": "critical", '
                             '"undervoltage_now": true, '
                             '"undervoltage_ever": false, '
                             '"throttled_now": false, '
                             '"throttled_ever": false, '
                             '"capped_now": false, "capped_ever": false, '
                             '"soft_temp_now": false, '
                             '"soft_temp_ever": false}\n')
            result = subprocess.run(['bash', str(link)], env=env,
                                    text=True, capture_output=True, timeout=5)
            rule = '  ' + '─' * 60 + '\n'
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, '')
            self.assertEqual(result.stdout,
                             '\n' + rule +
                             '  ** UNDER-VOLTAGE RIGHT NOW: THIS BOARD '
                             'IS NOT GETTING ENOUGH POWER **\n' + rule +
                             '  Under-voltage : NOW\n'
                             '  Raw           : 0x1\n\n'
                             '  Expect radios to misbehave in ways '
                             'that look like driver faults:\n'
                             '  a HaLow card that stops answering, '
                             'a Wi-Fi interface that will\n'
                             '  not associate, or a board that resets '
                             'with nothing in the log.\n'
                             '  Check the PSU and the cable before '
                             'suspecting the software.\n' + rule + '\n')


class RegistryHelperTests(unittest.TestCase):
    """Pin web registry parsing and tolerant JSON state readers."""

    def test_registry_at_both_callers(self):
        """Keep casing, duplicate assignment and quoted-value behavior."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'registry'
            path.write_text(
                "NODE_Aa12_HOSTNAME='first'\n"
                "NODE_Aa12_HOSTNAME='last'\n"
                "NODE_Aa12_DATA='a=b'\nNODE_ff00_EMPTY=''\n"
                "NODE_badZ_NAME='ignored'\nNODE_Aa12_lower='ignored'\n"
                "NODE_1234_NAME='can'\\''t'\n")
            expected = {
                'Aa12': {'id': 'Aa12', 'HOSTNAME': 'last', 'DATA': 'a=b'},
                'ff00': {'id': 'ff00', 'EMPTY': ''},
                '1234': {'id': '1234', 'NAME': 'can'},
            }
            for module in (manage, status):
                with patch.object(module, 'REGISTRY_FILE', str(path)):
                    self.assertEqual(
                        module.parse_registry(module.REGISTRY_FILE), expected)
            path.unlink()
            for module in (manage, status):
                with patch.object(module, 'REGISTRY_FILE', str(path)):
                    self.assertEqual(
                        module.parse_registry(module.REGISTRY_FILE), {})

    def test_dict_json_readers(self):
        """Keep every JSON shape and read failure at both users."""
        import manet_recovery_status as recovery
        import manet_registry_builder as builder
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state'
            for module in (recovery, builder):
                for raw, expected in (
                        ('{"x": [1, null], "ok": true}',
                         {'x': [1, None], 'ok': True}),
                        ('[]', {}), ('null', {}), ('1', {}),
                        ('"text"', {}), ('invalid', {}), ('', {})):
                    path.write_text(raw)
                    self.assertEqual(module.read_json(path), expected)
                path.unlink()
                self.assertEqual(module.read_json(path), {})
                self.assertEqual(module.read_json(Path(directory)), {})


class NodeInfoHelperTests(unittest.TestCase):
    """Pin node facts and both intentionally distinct display formats."""

    def test_key_value_files(self):
        """Keep quotes, repeated keys, ignored lines and read failures."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config'
            path.write_text(" # comment\na = 'one'\na=two\n"
                            'b="x=y"\nignored\nempty=\n')
            for module in (manage, status):
                self.assertEqual(module.load_kv_file(path),
                                 {'a': 'two', 'b': 'x=y', 'empty': ''})
            path.unlink()
            for module in (manage, status):
                self.assertEqual(module.load_kv_file(path), {})

    def test_hostname(self):
        """Return the kernel hostname or the same failure placeholder."""
        for module in (manage, status):
            with patch('socket.gethostname', return_value='cm4') as call:
                self.assertEqual(module.get_my_hostname(), 'cm4')
                call.assert_called_once_with()
            with patch('socket.gethostname', side_effect=OSError()):
                self.assertEqual(module.get_my_hostname(), 'unknown')

    def test_uptime_formats(self):
        """Keep management compact and topology spaced, including errors."""
        values = [0, 59, 60, 3599, 3600, 90061, -1, '61.5', None, 'bad']
        self.assertEqual([manage.fmt_uptime(v, compact=True) for v in values],
                         ['0s', '59s', '1m', '59m', '1h00m', '1d01h',
                          '-1s', '', '', ''])
        self.assertEqual([status.fmt_uptime(v) for v in values],
                         ['0m 0s', '0m 59s', '1m 0s', '59m 59s',
                          '1h 0m', '25h 1m', '59m 59s', '1m 1s', None,
                          'bad'])

    def test_local_uptime(self):
        """Read procfs once, keeping every caller's full formatted output."""
        from unittest.mock import mock_open
        for module, expected in ((manage, '1h01m'), (status, '1h 1m')):
            with patch('builtins.open', mock_open(read_data='3661.5 50\n')):
                self.assertEqual(
                    module.get_local_uptime(compact=module is manage), expected)
            for raw in ('', 'bad 2\n'):
                with patch('builtins.open', mock_open(read_data=raw)):
                    self.assertEqual(
                    module.get_local_uptime(compact=module is manage), '')
            with patch('builtins.open', side_effect=OSError()):
                self.assertEqual(
                    module.get_local_uptime(compact=module is manage), '')


def shell_functions(caller, names):
    """Read helper bodies through the caller's actual sourced libraries."""
    sources = {}

    def read(name):
        """Follow node library source paths without executing startup code."""
        if name in sources:
            return
        source = (TOOLS / name).read_text()
        sources[name] = source
        for library in re.findall(
                r'^\. .*?/(\w[\w-]*\.sh)"', source, re.M):
            if (TOOLS / library).is_file():
                read(library)

    read(caller)
    result = []
    for name in names:
        for source in sources.values():
            match = re.search(r'^' + re.escape(name) +
                              r'\(\) \{.*?^\}', source, re.M | re.S)
            if match:
                result.append(match[0])
                break
        else:
            raise AssertionError(f'{caller} does not define/source {name}')
    return '\n\n'.join(result) + '\n'


class ManagerHelperTests(unittest.TestCase):
    """Pin manager probes, scheduling globals and service subprocesses."""

    def test_manager_helpers(self):
        """Run both callers' helpers against an isolated node filesystem."""
        for caller in ('node-manager-acs.sh', 'node-manager-static.sh'):
            with self.subTest(caller=caller), \
                    tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'mesh.conf').write_text(
                    'ipv4_network=10.30.0.0/24\nmtx=y\nmumble=y\n')
                (root / 'chunk').write_text('1\n')
                (root / 'gateway').touch()
                (root / 'mesh-ntp.state').touch()
                (root / 'uptime').write_text('100.50 9\n')
                dispatch = root / 'dispatch'
                dispatch.write_text('#!/bin/bash\n'
                                    'printf "dispatch %s\\n" "$*"\n')
                dispatch.chmod(0o755)
                elections = root / 'elections'
                elections.mkdir()
                for service in ('channel', 'mediamtx', 'mumble', 'time'):
                    script = elections / (service + '-election.sh')
                    script.write_text('#!/bin/bash\n'
                                      'echo run >> "' + str(root / service) +
                                      '"\n')
                    script.chmod(0o755)
                names = ('detect_and_update_gateway_state',
                         'is_ntp_time_source', 'run_service_elections',
                         'is_hosting_service', 'is_hosting_mumble_service')
                source = shell_functions(caller, names)
                source = source.replace('/etc/mesh.conf',
                                        str(root / 'mesh.conf'))
                source = source.replace('/var/run/my_ipv4_chunk',
                                        str(root / 'chunk'))
                source = source.replace('/usr/local/bin/'
                                        'manet-uplink-dispatch.sh',
                                        str(dispatch))
                body = '''
CONTROL_IFACE=br0
LAST_GW_CHECK=90
GW_CHECK_INTERVAL=60
ELECTION_INTERVAL=15
LAST_ELECTION_RUN=''
uptime_now() { echo 100; }
systemctl() { printf 'systemctl %s\\n' "$*" >&2; }
manet-ipcalc.sh() { printf 'HostMin: 10.30.0.1\\n'; }
ip() {
    printf 'ip %s\\n' "$*" >&2
    printf 'inet 10.30.0.2/24\\ninet 10.30.0.3/24\\n'
}
detect_and_update_gateway_state
printf 'gateway=%s\\n' "$LAST_GW_CHECK"
LAST_GW_CHECK=0
detect_and_update_gateway_state
printf 'gateway=%s\\n' "$LAST_GW_CHECK"
is_ntp_time_source; printf 'ntp=%s\\n' "$?"
is_hosting_service; printf 'mtx=%s\\n' "$?"
is_hosting_mumble_service; printf 'mumble=%s\\n' "$?"
run_service_elections
wait
run_service_elections
wait
printf 'election=%s\\n' "$LAST_ELECTION_RUN"
'''
                env = dict(os.environ, MANET_TIME_RUN_DIR=str(root),
                           MESH_UPTIME_FILE=str(root / 'uptime'),
                           GATEWAY_STATE_FILE=str(root / 'gateway'),
                           ELECTION_DIR=str(elections))
                result = subprocess.run(['bash', '-c', source + body],
                                        env=env, text=True,
                                        capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout,
                                 'gateway=90\ngateway=100\nntp=0\nmtx=0\n'
                                 'mumble=0\nelection=100\n')
                self.assertEqual(result.stderr,
                                 'systemctl is-active --quiet '
                                 'mediamtx.service\nip addr show dev br0\n'
                                 'systemctl is-active --quiet '
                                 'mumble-server.service\n'
                                 'ip addr show dev br0\n')
                self.assertFalse((root / 'channel').exists())
                for service in ('mediamtx', 'mumble', 'time'):
                    self.assertEqual((root / service).read_text(), 'run\n')


class AddressHelperTests(unittest.TestCase):
    """Pin conversion output at both shell consumers."""

    def test_integer_addresses(self):
        """Keep octet boundaries, signed input and arithmetic expressions."""
        for caller in ('manet-ipcalc.sh', 'mesh-ip-manager.sh'):
            result = subprocess.run(
                ['bash', '-c', shell_functions(caller, ('int_to_ip',)) +
                 '\nfor n in 0 1 255 256 167772161 4294967295 -1; do\n'
                 'int_to_ip "$n"\ndone\nint_to_ip "1+256"\n'],
                capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout,
                             '0.0.0.0\n0.0.0.1\n0.0.0.255\n0.0.1.0\n'
                             '10.0.0.1\n255.255.255.255\n255.255.255.255\n'
                             '0.0.1.1\n')
            self.assertEqual(result.stderr, '')

    def test_arithmetic_alias(self):
        """Pin the symbolic alias affected by a caller-local variable name."""
        for caller in ('manet-ipcalc.sh', 'mesh-ip-manager.sh'):
            result = subprocess.run(
                ['bash', '-c', shell_functions(caller, ('int_to_ip',)) +
                 '\nip_int=16909060\nint_to_ip ip_int\n'],
                capture_output=True, text=True, timeout=5)
            self.assertEqual((result.returncode, result.stdout, result.stderr),
                             (0, '1.2.3.4\n', ''))

    def test_ipcalc_full_output(self):
        """Keep the complete field layout parsed by service elections."""
        result = subprocess.run(
            ['bash', str(TOOLS / 'manet-ipcalc.sh'), '10.30.0.7/24'],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout,
                         'Address:   10.30.0.7\nNetmask:   255.255.255.0 = 24\n'
                         'Network:   10.30.0.0/24\nHostMin:   10.30.0.1\n'
                         'HostMax:   10.30.0.254\nBroadcast: 10.30.0.255\n'
                         'Hosts/Net: 254\n')
        self.assertEqual(result.stderr, '')


class RadioShellHelperTests(unittest.TestCase):
    """Pin radio detection, service operations and bounded uplink probes."""

    def test_driver_and_phy_probes(self):
        """Pin complete driver and PHY command sequences at every caller."""
        for caller in ('batman-if-setup.sh', 'radio-setup.sh',
                       'manet-uplink-dispatch.sh', 'usb-wifi-uplink.sh'):
            source = shell_functions(caller, ('iface_driver',))
            for sysfs in (False, True):
                with tempfile.TemporaryDirectory() as directory:
                    trace = Path(directory) / 'trace'
                    body = '''
readlink() {
    printf 'readlink %s\\n' "$*" >> "$TRACE"
    [ "$SYSFS" = yes ] || return 1
    echo /sys/bus/usb/drivers/morse_usb
}
ethtool() {
    printf 'ethtool %s\\n' "$*" >> "$TRACE"
    printf 'driver: morse_usb\\nversion: 1\\n'
}
iface_driver wlan5
'''
                    result = subprocess.run(
                        ['bash', '-c', source + body], capture_output=True,
                        text=True, timeout=5,
                        env=dict(os.environ, TRACE=str(trace),
                                 SYSFS='yes' if sysfs else 'no'))
                    self.assertEqual(
                        (result.returncode, result.stdout, result.stderr),
                        (0, 'morse_usb\n', ''))
                    self.assertEqual(
                        trace.read_text(),
                        'readlink -f /sys/class/net/wlan5/device/driver\n' +
                        ('' if sysfs else 'ethtool -i wlan5\n'))
        for caller in ('batman-if-setup.sh', 'radio-setup.sh',
                       'node-manager-acs.sh'):
            source = shell_functions(caller, ('iface_phy',))
            with tempfile.TemporaryDirectory() as directory:
                trace = Path(directory) / 'trace'
                body = '''
iw() {
    printf 'iw %s\\n' "$*" >> "$TRACE"
    printf 'wiphy 3\\nwiphy 8\\n'
}
iface_phy wlan5
'''
                result = subprocess.run(
                    ['bash', '-c', source + body], capture_output=True,
                    text=True, timeout=5,
                    env=dict(os.environ, TRACE=str(trace)))
                self.assertEqual(
                    (result.returncode, result.stdout, result.stderr),
                    (0, 'phy3\n', ''))
                self.assertEqual(trace.read_text(), 'iw dev wlan5 info\n')

    def test_mesh_capability_and_reserved_ap(self):
        """Keep mesh support and the brcmfmac AP exclusion independent."""
        for caller in ('batman-if-setup.sh', 'radio-setup.sh'):
            source = shell_functions(caller, ('iface_supports_mesh',
                                              'is_nonmesh_wifi'))
            result = subprocess.run(['bash', '-c', source + '''
iface_phy() { echo phy1; }
iw() { printf 'Supported interface modes:\\n * mesh point\\n'; }
iface_driver() { echo brcmfmac; }
iface_supports_mesh wlan0; printf 'mesh=%s\\n' "$?"
is_nonmesh_wifi wlan0; printf 'ap=%s\\n' "$?"
iface_phy() { :; }
iface_driver() { echo mt7915e; }
iface_supports_mesh wlan1; printf 'mesh=%s\\n' "$?"
is_nonmesh_wifi wlan1; printf 'ap=%s\\n' "$?"
'''], capture_output=True, text=True, timeout=5)
            self.assertEqual((result.returncode, result.stdout, result.stderr),
                             (0, 'mesh=0\nap=0\nmesh=1\nap=1\n', ''))

    def test_service_failure_under_errexit(self):
        """Characterize best-effort service helpers under both shell modes."""
        for caller in ('ethernet-autodetect.sh', 'manet-uplink-dispatch.sh'):
            for name in ('enable_if_disabled', 'unmask_if_masked'):
                for strict in (False, True):
                    source = shell_functions(caller, (name,))
                    body = '''
systemctl() {
    printf '%s\\n' "$*" >> "$TRACE"
    [ "$*" != "is-enabled sample.service" ] || printf 'masked\\n'
    return 1
}
''' + name + ' sample.service\nprintf "survived\\n"\n'
                    with tempfile.TemporaryDirectory() as directory:
                        trace = Path(directory) / 'trace'
                        result = subprocess.run(
                            ['bash', '-c', ('set -e\n' if strict else '') +
                             source + body], capture_output=True, text=True,
                            env=dict(os.environ, TRACE=str(trace)), timeout=5)
                        fatal = False
                        self.assertEqual(result.returncode, int(fatal))
                        self.assertEqual(result.stdout,
                                         '' if fatal else 'survived\n')
                        self.assertEqual(result.stderr, '')
                        operation = ('enable' if name == 'enable_if_disabled'
                                     else 'unmask')
                        query = ('is-enabled --quiet' if operation == 'enable'
                                 else 'is-enabled')
                        self.assertEqual(trace.read_text(),
                                         query + ' sample.service\n' +
                                         operation + ' sample.service\n')

    def test_usb_candidate_exclusions(self):
        """Pin assigned roles, driver exclusions and missing netdevs."""
        for caller in ('manet-uplink-dispatch.sh', 'usb-wifi-uplink.sh'):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'net/wlan5').mkdir(parents=True)
                (root / 'mesh_if').write_text('wlan6\n')
                source = shell_functions(caller, ('is_usb_wifi_uplink_iface',))
                source = source.replace('/sys/class/net', str(root / 'net'))
                source = source.replace('/var/lib/', str(root) + '/')
                result = subprocess.run(['bash', '-c', source + '''
readlink() { echo /sys/bus/usb; }
iface_driver() { echo mt76x2u; }
for iface in wlan0 wlan3 eth0 wlan5 wlan6 wlan7; do
    is_usb_wifi_uplink_iface "$iface"; printf '%s:%s\\n' "$iface" "$?"
done
iface_driver() { echo morse_usb; }
is_usb_wifi_uplink_iface wlan5; printf 'morse:%s\\n' "$?"
iface_driver() { echo brcmfmac; }
is_usb_wifi_uplink_iface wlan5; printf 'brcm:%s\\n' "$?"
'''], capture_output=True, text=True, timeout=5)
                missing = '1'
                self.assertEqual(result.stdout,
                                 'wlan0:1\nwlan3:1\neth0:1\nwlan5:0\n'
                                 'wlan6:1\nwlan7:' + missing +
                                 '\nmorse:1\nbrcm:1\n')
                self.assertEqual((result.returncode, result.stderr), (0, ''))

    def test_internet_probe_commands(self):
        """Pin the full timeout/argv sequence for curl and TCP fallbacks."""
        for caller in ('ethernet-autodetect.sh', 'manet-uplink-dispatch.sh'):
            names = ('internet_probe',)
            names += ('iface_ip',)
            source = shell_functions(caller, names)
            for curl in (True, False):
                with tempfile.TemporaryDirectory() as directory:
                    trace = Path(directory) / 'trace'
                    body = '''
ping() { printf 'ping %s\\n' "$*" >> "$TRACE"; return 1; }
curl() { printf 'curl %s\\n' "$*" >> "$TRACE"; echo 204; }
ip() {
    printf 'ip %s\\n' "$*" >> "$TRACE"
    printf '2: eth0 inet 192.0.2.5/24 scope global eth0\\n'
}
nc() { printf 'nc %s\\n' "$*" >> "$TRACE"; return 0; }
command() {
    if [ "$*" = '-v curl' ] && [ "$NO_CURL" = yes ]; then return 1; fi
    builtin command "$@"
}
internet_probe eth0
'''
                    env = dict(os.environ, TRACE=str(trace),
                               NO_CURL='no' if curl else 'yes')
                    result = subprocess.run(['bash', '-c', source + body],
                                            capture_output=True, text=True,
                                            env=env, timeout=5)
                    self.assertEqual(
                        (result.returncode, result.stdout, result.stderr),
                        (0, '', ''))
                    expected = ('ping -c 1 -W 2 -I eth0 1.1.1.1\n'
                                'ping -c 1 -W 2 -I eth0 8.8.8.8\n')
                    if curl:
                        expected += ('curl --interface eth0 -s -m 3 '
                                     '-o /dev/null -w %{http_code} '
                                     'http://connectivitycheck.gstatic.com/'
                                     'generate_204\n')
                    else:
                        flag = ' -o'
                        expected += ('ip -4' + flag + ' addr show dev eth0\n'
                                     'nc -z -w 3 -s 192.0.2.5 1.1.1.1 53\n')
                    self.assertEqual(trace.read_text(), expected)

    def test_frequency_config(self):
        """Pin the first match, absent file and unterminated final line."""
        for caller in ('mesh-acs-common.sh', 'node-manager-static.sh'):
            for raw in ('frequency=2412\nfrequency=5180\n', 'frequency=2462'):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'wpa'
                    path.write_text(raw)
                    result = subprocess.run(
                        ['bash', '-c', shell_functions(
                            caller, ('get_current_freq',)) +
                         '\nget_current_freq "$1"\n', 'test', str(path)],
                        capture_output=True, text=True, timeout=5)
                    expected = ('2412\n' if '\n' in raw else
                                '2462\n')
                    self.assertEqual(result.stdout, expected)
                    self.assertEqual(result.stderr, '')

    def test_boot_clock_validation(self):
        """Pin leading zero and malformed-decimal behavior at each caller."""
        for caller in ('manet-common.sh', 'gateway-route-manager.sh',
                       'mesh-config-rollback.sh'):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'uptime'
                source = shell_functions(caller, ('uptime_now',))
                source = source.replace('/proc/uptime', str(path))
                cases = [('0010.50 2\n', '10\n', '0010\n', '0010\n'),
                         ('10.bad 2\n', '', '10\n', '10\n'),
                         ('bad 2\n', '', 'bad\n', '')]
                index = 1
                for case in cases:
                    path.write_text(case[0])
                    result = subprocess.run(
                        ['bash', '-c', source + '\nuptime_now\n'],
                        capture_output=True, text=True, timeout=5,
                        env=dict(os.environ, UPTIME_FILE=str(path),
                                 MESH_UPTIME_FILE=str(path)))
                    self.assertEqual(result.stdout, case[index])
                    self.assertEqual(result.returncode,
                                     0 if case[index] else 1)
                    self.assertEqual(result.stderr, '')


class HalowDetectionHelperTests(unittest.TestCase):
    """Keep role, sysfs and command-query contracts for both radio users."""

    def test_role_and_driver_queries(self):
        """Keep sysfs probes local and receiver ethtool calls bounded."""
        from unittest.mock import mock_open
        import manet_radio as radio
        receiver = load_script('mesh-radio-state')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'halow_if').write_text('wlan2\n')
            driver = root / 'net/wlan5/device/driver'
            driver.parent.mkdir(parents=True)
            (root / 'morse_usb').mkdir()
            driver.symlink_to(root / 'morse_usb')
            with patch.dict(os.environ, MANET_IFACE_STATE_DIR=str(root),
                            MANET_SYS_NET=str(root / 'net')):
                self.assertEqual([radio.is_halow_iface(i)
                                  for i in ('wlan2', 'wlan5', 'wlan0')],
                                 [True, True, False])
        with patch('builtins.open', mock_open(read_data='wlan2\n')), \
                patch.object(receiver, 'run') as run:
            self.assertTrue(receiver.is_halow_iface(
                'wlan2', halow_ifaces=receiver.load_halow_ifaces(),
                driver_query=receiver.run))
            run.assert_not_called()
            run.return_value = SimpleNamespace(stdout='driver: morse_usb\n')
            self.assertTrue(receiver.is_halow_iface(
                'wlan5', halow_ifaces=receiver.load_halow_ifaces(),
                driver_query=receiver.run))
            run.assert_called_once_with(['ethtool', '-i', 'wlan5'], timeout=3)
            run.reset_mock()
            run.side_effect = OSError()
            self.assertFalse(receiver.is_halow_iface(
                'wlan0', halow_ifaces=receiver.load_halow_ifaces(),
                driver_query=receiver.run))
            run.assert_called_once_with(['ethtool', '-i', 'wlan0'], timeout=3)


if __name__ == '__main__':
    unittest.main()
