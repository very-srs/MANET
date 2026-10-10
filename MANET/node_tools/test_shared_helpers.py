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


if __name__ == '__main__':
    unittest.main()
