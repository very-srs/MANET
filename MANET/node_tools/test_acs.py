#!/usr/bin/env python3
"""ACS with temporary role/config files and simulated radios/Alfred."""

import importlib.util
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import time
from manet_admin import AdminTransport
import manet_acs_agreement as agreement


TOOLS = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('tourguide_election', TOOLS / 'mesh-tourguide-election.py')
tourguide = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tourguide)
RUNTIME_SPEC = importlib.util.spec_from_file_location('acs_runtime', TOOLS / 'mesh-channel-agreement.py')
runtime = importlib.util.module_from_spec(RUNTIME_SPEC)
RUNTIME_SPEC.loader.exec_module(runtime)
OWN = '02:00:00:00:00:01'
PEER = '02:00:00:00:00:02'
THIRD = '02:00:00:00:00:03'
RADIO = '02:00:00:00:01:02'


def node(mac, timestamp=0, service=False, aliases='', freqs=(2437, 5200)):
    fields = {
        'MAC_ADDRESSES': ','.join(filter(None, [mac, aliases])),
        'LAST_TOURGUIDE_TIMESTAMP': str(timestamp),
        'IS_MEDIAMTX_SERVER': 'true' if service else 'false',
        'IS_MUMBLE_SERVER': 'false',
        'INTERFACES_JSON': json.dumps([{'role': 'mesh', 'state': 'UP', 'freq_mhz': f}
                                       for f in freqs]),
    }
    return ''.join(f"NODE_{mac.replace(':', '')}_{key}='{value}'\n" for key, value in fields.items())


class AcsHarness(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='manet-acs-test-')
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        (self.root / 'initial_time_synced').touch()
        clock_env = patch.dict(os.environ, MANET_TIME_RUN_DIR=str(self.root))
        clock_env.start(); self.addCleanup(clock_env.stop)
        self.bin = self.root / 'bin'
        self.roles = self.root / 'roles'
        self.wpa = self.root / 'wpa'
        for path in (self.bin, self.roles, self.wpa):
            path.mkdir()
        self.registry = self.root / 'registry'
        self.registry.write_text('')
        (self.root / 'mac').write_text(OWN)
        (self.root / 'peers').write_text('[]')
        self.env = dict(os.environ,
                        PATH=str(self.bin) + os.pathsep + str(Path(sys.executable).parent)
                             + os.pathsep + os.environ.get('PATH', ''),
                        MANET_TOOLS_DIR=str(TOOLS), MANET_IFACE_STATE_DIR=str(self.roles),
                        MANET_WPA_DIR=str(self.wpa), MANET_RADIO_STATE_FILE=str(self.root / 'radio-state'),
                        REGISTRY_FILE=str(self.registry), OUTPUT_FILE=str(self.root / 'election'),
                        LOCK_FILE=str(self.root / 'election.lock'),
                        MANET_ACS_LOCK_FILE=str(self.root / 'election.lock'),
                        MANET_ACS_RUN_DIR=str(self.root), MANET_ACS_STATE_DIR=str(self.root / 'acs-state'),
                        MANET_MESH_CONF=str(self.root / 'mesh.conf'), MANET_SYS_NET=str(self.root / 'net'),
                        MANET_BOOT_ID_FILE=str(self.root / 'boot'),
                        MANET_ADMIN_STATE_DIR=str(self.root / 'admin'), BATCTL_PATH=str(self.bin / 'batctl'),
                        TEST_ROOT=str(self.root), TEST_NOW='1000', TEST_BATCTL_RC='0',
                        # Boot clock pinned at 10000 s for registry observation ages.
                        MESH_UPTIME_FILE=str(self.root / 'uptime'))
        (self.root / 'uptime').write_text('10000.50 1.00\n')
        (self.root / 'mesh.conf').write_text('acs=y\nadmin_password=test-secret\n')
        (self.root / 'boot').write_text('a' * 32)
        (self.root / 'net/br0').mkdir(parents=True)
        (self.root / 'net/br0/address').write_text(OWN)
        self.transport = AdminTransport(str(self.root / 'mesh.conf'), str(self.root / 'admin'))
        self.command('date', '''
clock = Path(os.environ['TEST_ROOT'], 'clock')
print((clock.read_text() if clock.exists() else os.environ['TEST_NOW']) if sys.argv[1:] == ['+%s'] else 'test-clock')
''')
        self.command('systemd-cat', 'sys.stderr.write(sys.stdin.read())')
        self.command('sleep', '''
clock = Path(os.environ['TEST_ROOT'], 'clock')
seconds = max(1, int(float(sys.argv[1])))
if clock.exists():
    clock.write_text(str(int(clock.read_text()) + seconds))
uptime = Path(os.environ['MESH_UPTIME_FILE'])
uptime.write_text(str(float(uptime.read_text().split()[0]) + seconds) + ' 1.00\\n')
''')
        self.command('batctl', '''
if sys.argv[1:] != ['meshif', 'bat0', 'originators_json']:
    sys.exit(99)
print(Path(os.environ['TEST_ROOT'], 'peers').read_text())
sys.exit(int(os.environ['TEST_BATCTL_RC']))
''')
        self.command('wpa_cli', '''
with open(Path(os.environ['TEST_ROOT'], 'commands'), 'a') as log:
    log.write('wpa_cli ' + ' '.join(sys.argv[1:]) + '\\n')
''')
        self.command('systemctl', '''
with open(Path(os.environ['TEST_ROOT'], 'commands'), 'a') as log:
    log.write('systemctl ' + ' '.join(sys.argv[1:]) + '\\n')
''')
        self.command('iw', '''
with open(Path(os.environ['TEST_ROOT'], 'commands'), 'a') as log:
    log.write('iw ' + ' '.join(sys.argv[1:]) + '\\n')
if sys.argv[1] == 'dev' and sys.argv[3] == 'info':
    import re
    conf = Path(os.environ['MANET_WPA_DIR'], 'wpa_supplicant-' + sys.argv[2] + '.conf')
    freq = re.search(r'frequency=(\\d+)', conf.read_text())[1]
    print('wiphy 0\\nchannel 1 (' + freq + ' MHz)')
elif sys.argv[1] == 'phy':
    for freq in (2412, 2437, 2462, 5180, 5200, 5220, 5240, 5745, 5765, 5785, 5805, 5825):
        print('* ' + str(freq) + '.0 MHz [1] (20.0 dBm)')
''')
        self.configure(2412, 5180)

    def command(self, name, body):
        path = self.bin / name
        path.write_text(f'#!{sys.executable}\nimport os, sys\nfrom pathlib import Path\n' + body + '\n')
        path.chmod(0o755)

    def configure(self, freq24=None, freq5=None):
        (self.root / 'manet-acs-busy').unlink(missing_ok=True)
        (self.root / 'manet-rendezvous.json').unlink(missing_ok=True)
        for role, iface, freq in [('mesh_24_if', 'wlan0', freq24), ('mesh_5_if', 'wlan1', freq5)]:
            (self.roles / role).write_text(iface if freq else '')
            conf = self.wpa / f'wpa_supplicant-{iface}.conf'
            if freq:
                conf.write_text('mesh_max_inactivity=10\n'
                                f'network={{\nfrequency={freq}\n}}\n')
            else:
                conf.unlink(missing_ok=True)
        (self.roles / 'mesh_if').write_text(''.join(
            iface + '\n' for iface, freq in [('wlan0', freq24), ('wlan1', freq5)] if freq))

    def run_command(self, args):
        return subprocess.run(args, env=self.env, text=True, capture_output=True, timeout=20)

    def shell(self, code):
        return self.run_command(['bash', '-c', code])

    def definitions(self, script):
        marker = '# === MAIN SETUP ===' if script == 'node-manager-acs.sh' else '# === MAIN EXECUTION ==='
        return (TOOLS / script).read_text().split(marker, 1)[0] + '''
BATCTL_PATH="$TEST_ROOT/bin/batctl"
PEER_COUNTER="$MANET_TOOLS_DIR/mesh-peer-count.sh"
REGISTRY_STATE_FILE="$TEST_ROOT/registry"
log() { echo "$1" >&2; }
load_mesh_roles
'''

    def reports(self, reports, macs=None):
        macs = macs or [f'02:00:00:00:00:{i:02x}' for i in range(len(reports))]
        self.registry.write_text(''.join(
            f"NODE_{mac.replace(':', '')}_CHANNEL_REPORT_JSON='{json.dumps({'results': report})}'\n"
            f"NODE_{mac.replace(':', '')}_OBSERVED_AT_UPTIME='10000'\n"
            for mac, report in zip(macs, reports)))

    def election(self):
        return self.run_command(['bash', str(TOOLS / 'channel-election.sh'), '--score'])


class RoleTests(AcsHarness):
    def test_one_or_two_bands_can_leave_and_return_to_lobby(self):
        for freq24, freq5 in [(2412, None), (None, 5180), (2412, 5180)]:
            with self.subTest(freq24=freq24, freq5=freq5):
                self.configure(freq24, freq5)
                (self.root / 'uptime').write_text('10000.50 1.00\n')
                body = self.definitions('node-manager-acs.sh') + '''
is_in_lobby
acs_write_channels 2437 5200 || exit
is_in_lobby
echo "10031.50 1.00" > "$MESH_UPTIME_FILE"
return_to_lobby
is_in_lobby
'''
                result = self.shell(body)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, 'true\nfalse\ntrue\n')
                self.assertFalse((self.wpa / 'wpa_supplicant-.conf').exists())

    def test_empty_role_is_not_replaced_by_interface_array_position(self):
        self.configure(None, 5200)
        result = self.shell(self.definitions('node-manager-acs.sh') +
                            '\nprintf "%s|%s\\n" "$WPA_IFACE_2_4" "$WPA_IFACE_5_0"\nis_in_lobby\n')
        self.assertEqual(result.stdout, '|wlan1\nfalse\n')

    def test_no_radios_or_unready_config_cannot_bootstrap(self):
        for freq24, freq5 in [(None, None), (2412, None)]:
            self.configure(freq24, freq5)
            (self.wpa / 'wpa_supplicant-wlan0.conf').unlink(missing_ok=True)
            result = self.shell(self.definitions('node-manager-acs.sh') + '\nacs_configs_ready\n')
            self.assertNotEqual(result.returncode, 0)

    def test_disabled_band_does_not_block_the_enabled_band(self):
        self.configure(2437, 5180)
        (self.root / 'radio-state').write_text(json.dumps({'desired': {'wlan1': 'down'}}))
        result = self.shell(self.definitions('node-manager-acs.sh') + '\nis_in_lobby\n')
        self.assertEqual(result.stdout, 'false\n')

    def test_channel_adoption_requires_overlap_and_preserves_unsupplied_band(self):
        self.configure(2412, 5180)
        result = self.shell(self.definitions('node-manager-acs.sh') + '\nacs_write_channels 2462 ""\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('frequency=2462', (self.wpa / 'wpa_supplicant-wlan0.conf').read_text())
        self.assertIn('frequency=5180', (self.wpa / 'wpa_supplicant-wlan1.conf').read_text())
        for iface in ('wlan0', 'wlan1'):
            content = (self.wpa / f'wpa_supplicant-{iface}.conf').read_text()
            self.assertTrue(content.startswith('mesh_max_inactivity=10\n'))
        self.configure(None, 5180)
        result = self.shell(self.definitions('node-manager-acs.sh') + '\nacs_write_channels 2462 ""\n')
        self.assertNotEqual(result.returncode, 0)

    def test_provisioning_assigns_capabilities_even_for_one_radio(self):
        source = (TOOLS / 'radio-setup.sh').read_text()
        selection = source.split('# Assign standard mesh roles', 1)[1].split('# Create directory', 1)[0]
        selection = selection[selection.index('mesh_24=""'):]
        for interfaces, expected in [('w24', 'w24|'), ('w5', '|w5'), ('dual0', 'dual0|'),
                                     ('dual0 dual1', 'dual0|dual1'), ('w5 w24', 'w24|w5')]:
            with self.subTest(interfaces=interfaces):
                body = '''iface_supports_freq() {
case "$1:$2" in w24:2412|w5:5180|dual*:2412|dual*:5180) return 0 ;; *) return 1 ;; esac
}
mesh_ifaces=(''' + interfaces + ')\n' + selection + '\necho "$mesh_24|$mesh_5"\n'
                result = self.shell(body)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)

    def test_provisioning_dual_band_radio_starts_on_its_assigned_5ghz_role(self):
        source = (TOOLS / 'radio-setup.sh').read_text()
        function = re.search(r'^iface_mesh_freq\(\) \{\n.*?^\}', source, re.M | re.S)[0]
        function = function.replace('/var/lib/mesh_5_if', str(self.roles / 'mesh_5_if'))
        result = self.shell(function + '\niface_mesh_freq wlan1\n')
        self.assertEqual(result.stdout, '5180\n')


class ChannelElectionTests(AcsHarness):
    def test_single_band_scoring_does_not_change_a_config(self):
        report = [{'channel': f, 'noise_floor': -95, 'busy_pct': 10, 'bss_count': 0}
                  for f in (2437, 2462, 5200, 5220)]
        for freq24, freq5, winner in [(2412, None, '2437'), (None, 5180, '5200')]:
            with self.subTest(freq24=freq24, freq5=freq5):
                self.configure(freq24, freq5)
                self.reports([report])
                result = self.election()
                self.assertEqual(result.returncode, 0, result.stderr)
                iface = 'wlan0' if freq24 else 'wlan1'
                self.assertIn('frequency=' + str(freq24 or freq5), (self.wpa / f'wpa_supplicant-{iface}.conf').read_text())
                self.assertIn('WINNER_' + ('2_4' if freq24 else '5_0') + '=' + winner, (self.root / 'election').read_text())
                self.assertFalse((self.wpa / 'wpa_supplicant-.conf').exists())

    def test_report_freshness_uses_local_observation_not_sender_clock(self):
        # One report aged past the 240 s window by our own observation, one
        # from a node whose clock is days behind but whose record is current.
        stale = [{'channel': 2437, 'noise_floor': -95, 'busy_pct': 5}]
        fresh = [{'channel': 2462, 'noise_floor': -95, 'busy_pct': 5}]
        self.configure(2412, None)
        self.reports([stale, fresh])
        text = self.registry.read_text().splitlines()
        text[1] = text[1].replace("'10000'", "'9700'")   # observed 300 s ago
        text.append("NODE_020000000001_LAST_SEEN_TIMESTAMP='100'")
        self.registry.write_text('\n'.join(text) + '\n')
        result = self.election()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Scoring 1 scan report(s)', result.stderr)
        self.assertIn('WINNER_2_4=2462', (self.root / 'election').read_text())

    def test_identical_reports_and_incumbents_ignore_registry_order(self):
        reports = [[{'channel': 2437, 'noise_floor': -95,
                     'busy_pct': 70, 'foreign_pct': 70},
                    {'channel': 2462, 'noise_floor': -95, 'busy_pct': 10}],
                   [{'channel': 2437, 'noise_floor': -95,
                     'busy_pct': 50, 'foreign_pct': 50},
                    {'channel': 2462, 'noise_floor': -95, 'busy_pct': 20}]]
        outputs = []
        for ordered in [reports, reports[::-1]]:
            self.configure(2437, None)
            self.reports(ordered)
            result = self.election()
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs.append((self.root / 'election').read_text())
        self.assertEqual(outputs[0], outputs[1])
        self.assertIn('WINNER_2_4=2462', outputs[0])

    def test_missing_band_data_holds_without_limp_mode(self):
        self.reports([[{'channel': 2437, 'noise_floor': -95, 'busy_pct': 10}]])
        result = self.election()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('frequency=5180', (self.wpa / 'wpa_supplicant-wlan1.conf').read_text())
        self.assertIn('LIMP_MODE=false', (self.root / 'election').read_text())

    def test_all_disqualified_uses_a_measured_data_channel(self):
        self.configure(2412, None)
        self.reports([[{'channel': 2437, 'noise_floor': -60, 'busy_pct': 90},
                       {'channel': 2462, 'noise_floor': -65, 'busy_pct': 99}]])
        result = self.election()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('WINNER_2_4=2437', (self.root / 'election').read_text())
        self.assertIn('LIMP_MODE=true', (self.root / 'election').read_text())


class TourguideTests(AcsHarness):
    def test_agreement_and_tourguide_respect_the_same_lock(self):
        with open(self.env['MANET_ACS_LOCK_FILE'], 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.dict(os.environ, self.env):
                runner = runtime.Runtime()
                with self.assertRaises(BlockingIOError), runner.channel_lock():
                    pass
            result = self.run_command(['bash', str(TOOLS / 'tourguide-manager.sh')])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Another ACS channel operation', result.stderr)
            self.assertFalse((self.root / 'commands').exists())

    def test_manager_waits_while_tourguide_holds_channel_lock(self):
        source = (TOOLS / 'node-manager-acs.sh').read_text()
        gate = source.split('    # Tourguide temporarily rewrites a config to the lobby.', 1)[1]
        gate = '    # Tourguide temporarily rewrites a config to the lobby.' + gate
        gate = gate.split('    # === ALFRED RADIO STATE SYNC ===', 1)[0]
        body = 'STARTUP_MONITOR_INTERVAL=5\nfor pass in 1; do\n' + gate + '\necho RAN_LOOP\ndone\n'
        with open(self.env['MANET_ACS_LOCK_FILE'], 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.shell(body).stdout, '')
        self.assertEqual(self.shell(body).stdout, 'RAN_LOOP\n')

    def test_partition_comparison_uses_original_single_band_config_and_size(self):
        self.configure(2412, None)  # Already hopped to lobby.
        source = self.definitions('tourguide-manager.sh').replace(
            '"/sys/class/net/${CONTROL_IFACE}/address"', '"$TEST_ROOT/mac"').replace(
            '"/usr/local/bin/decoder.py"', f'"{TOOLS}/decoder.py"')
        body = source + '''
ELECTION_OUTPUT_FILE="$TEST_ROOT/election"
analyze_partition_data "$TEST_FOREIGN" 2437 "" 4
'''
        for freq, size, moves in [(2437, 5, False), (2462, 3, False), (2462, 5, True)]:
            with self.subTest(freq=freq, size=size):
                (self.root / 'election').unlink(missing_ok=True)
                envelope = self.transport.seal(75, {'kind': 'acs_helper', 'node': PEER,
                                                      'channels': {'2.4': freq}, 'size': size})
                self.env['TEST_FOREIGN'] = '{ "' + PEER + '", ' + json.dumps(json.dumps(envelope)) + ' },'
                self.env['TEST_BATCTL_RC'] = '1'  # Must not requery the lobby table.
                result = self.shell(body)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((self.root / 'election').exists(), moves)
                if moves:
                    output = (self.root / 'election').read_text()
                    self.assertIn('WINNER_2_4=2462\n', output)
                    self.assertIn('WINNER_5_0=\n', output)

    def test_radio_schedule_skips_missing_or_disabled_band_without_substitution(self):
        self.configure(2437, None)
        body = self.definitions('tourguide-manager.sh') + '\nselect_tourguide_radio\n'
        self.env['TEST_NOW'] = '960'
        self.assertEqual(self.shell(body).stdout, 'wlan0\n')
        self.env['TEST_NOW'] = '1080'
        result = self.shell(body)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.configure(2437, 5200)
        (self.root / 'radio-state').write_text(json.dumps({'desired': {'wlan1': 'down'}}))
        self.assertNotEqual(self.shell(body).returncode, 0)

    def test_election_maps_radio_macs_and_agrees_across_multi_hop_partition(self):
        registry = node(OWN, 30) + node(PEER, 10, aliases=RADIO) + node(THIRD, 20)
        self.assertEqual(tourguide.elect(OWN, [RADIO, THIRD], registry, '2.4'), PEER)
        self.assertEqual(tourguide.elect(PEER, [OWN, THIRD], registry, '2.4'), PEER)
        self.assertEqual(tourguide.elect(THIRD, [OWN, RADIO], registry, '2.4'), PEER)

    def test_service_hosts_and_nodes_missing_scheduled_band_are_excluded(self):
        registry = node(OWN, 30) + node(PEER, 0, freqs=(5200,)) + node(THIRD, 0, service=True)
        self.assertEqual(tourguide.elect(OWN, [PEER, THIRD], registry, '2.4'), OWN)
        self.assertEqual(tourguide.elect(OWN, [PEER, THIRD], registry, '5'), PEER)

    def test_ties_and_all_service_hosts_have_a_deterministic_winner(self):
        registry = node(OWN, 0, service=True) + node(PEER, 0, service=True)
        self.assertEqual(tourguide.elect(PEER, [OWN], registry, '2.4'), OWN)

    def test_missing_identity_defers_but_solo_can_heal(self):
        with self.assertRaises(ValueError):
            tourguide.elect(OWN, [RADIO], node(OWN), '2.4')
        self.assertEqual(tourguide.elect(OWN, [], '', '2.4'), OWN)

    def test_real_election_caller_uses_json_and_propagates_query_errors(self):
        (self.root / 'peers').write_text(json.dumps([{'orig_address': RADIO}, {'orig_address': RADIO}]))
        self.registry.write_text(node(OWN, 30) + node(PEER, 0, aliases=RADIO))
        body = self.definitions('tourguide-manager.sh') + '\nTOURGUIDE_BAND=2.4\nelect_tourguide ' + OWN
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, PEER + '\n')
        self.env['TEST_BATCTL_RC'] = '1'
        result = self.shell(body)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')

    def test_single_band_tourguide_encodes_beacon_and_restores_original_frequency(self):
        for freq24, freq5, now in [(2437, None, '990'), (None, 5200, '1110')]:
            with self.subTest(freq24=freq24, freq5=freq5):
                self.configure(freq24, freq5)
                self.env['TEST_NOW'] = now
                (self.root / 'clock').write_text(now)
                self.command('alfred', '''
if sys.argv[1:] == ['-s', '69']:
    Path(os.environ['TEST_ROOT'], 'beacon').write_text(sys.stdin.read())
''')
                source = (TOOLS / 'tourguide-manager.sh').read_text()
                source = source.replace('/var/run/tourguide_state', str(self.root / 'tourguide_state'))
                source = source.replace('"/sys/class/net/${CONTROL_IFACE}/address"', '"$TEST_ROOT/mac"')
                source = source.replace('BATCTL_PATH="/usr/sbin/batctl"', 'BATCTL_PATH="$TEST_ROOT/bin/batctl"')
                source = source.replace('ENCODER_PATH="/usr/local/bin/encoder.py"', f'ENCODER_PATH="{TOOLS}/encoder.py"')
                result = self.shell(source)
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = (self.root / 'beacon').read_text()
                decoded = self.run_command([sys.executable, str(TOOLS / 'decoder.py'), 'telemetry', payload])
                self.assertEqual(decoded.returncode, 0, decoded.stderr)
                self.assertIn(f"DATA_CHANNEL_2_4='{freq24 or ''}'", decoded.stdout)
                self.assertIn(f"DATA_CHANNEL_5_0='{freq5 or ''}'", decoded.stdout)
                for iface, freq in [('wlan0', freq24), ('wlan1', freq5)]:
                    if freq:
                        self.assertIn(f'frequency={freq}', (self.wpa / f'wpa_supplicant-{iface}.conf').read_text())


class CadenceTests(unittest.TestCase):
    """Run the ACS pass scheduler against the real protocol on a fake clock."""
    BASE = 1000 * agreement.ROUND_SECONDS
    MACS = (OWN, PEER, THIRD)
    SCORE_SECONDS = 0.8

    @staticmethod
    def status(index, ready=True):
        return {'boot': 'abc'[index] * 32, 'acs': True, 'ready': ready, 'current': {'2.4': 2437},
                'allowed': {'2.4': sorted(agreement.CHANNELS['2.4'])}}

    def simulate(self, nodes, cost, delay, fail=None, legacy=False,
                 phases=(0, 0.37, 0.74), requests=(25, 25, 25)):
        """Return ({mac: apply second}, activate_at, pass count) for one round.

        Like _tick, a pass builds its view from replicated peer status plus
        live local status, reads records near its start, samples the decision
        time near its end, adds scoring time when proposing, and republishes
        on change or after 5 seconds. A node turns ready at its request
        second, which also wakes the new loop at once. fail=(mac, at) aborts
        that node's first pass starting at or after at before any state
        changes, as an early read error does.
        """
        macs = self.MACS[:nodes]
        request = {mac: self.BASE + requests[i] for i, mac in enumerate(macs)}
        runner = runtime.Runtime.__new__(runtime.Runtime)
        states = {mac: {} for mac in macs}
        published = {mac: [] for mac in macs}
        last = {mac: (None, None) for mac in macs}
        wake = {mac: self.BASE + phases[i] for i, mac in enumerate(macs)}
        applied, activate, passes, failed = {}, None, 0, False
        with patch.object(runtime, 'clock_ready', return_value=True):
            while min(wake.values()) < self.BASE + agreement.ROUND_SECONDS:
                mac = min(wake, key=wake.get)
                start = end = wake[mac]
                passes += 1
                if fail and not failed and fail[0] == mac and start >= fail[1]:
                    failed = True
                    end += cost
                else:
                    local = self.status(macs.index(mac), start >= request[mac])
                    view, records = {mac: local}, {}
                    for other in macs:
                        if other == mac:
                            continue
                        seen = [out for visible, out in published[other] if visible <= start + 0.05]
                        view[other] = seen[-1]['status'] if seen else None
                        if seen:
                            records[other] = seen[-1]['protocol']
                    saved = states[mac]
                    proposal = None
                    now = int(start + 0.9 * cost)
                    if (agreement.coordinator(view) == mac
                            and agreement.PROPOSE_FROM <= now % agreement.ROUND_SECONDS <= agreement.PROPOSE_UNTIL
                            and (saved.get('round', -1) < now // agreement.ROUND_SECONDS
                                 or saved.get('phase') == 'idle')):
                        now = int(start + 0.9 * cost + self.SCORE_SECONDS)
                        end += self.SCORE_SECONDS
                        if agreement.PROPOSE_FROM <= now % agreement.ROUND_SECONDS <= agreement.PROPOSE_UNTIL:
                            proposal = agreement.make_plan(view, {'2.4': 2462}, False, now, 'e' * 32)
                            activate = proposal['activate_at']
                    states[mac], outgoing, plan = agreement.advance(saved, mac, now, view, records,
                                                                    proposal, local)
                    if plan is not None:
                        applied[mac] = now
                    end += cost
                    record = {'protocol': outgoing, 'status': local}
                    # Same predicate as _tick: protocol or status changed, or 5 s.
                    if record != last[mac][0] or last[mac][1] is None or end - last[mac][1] >= 5:
                        published[mac].append((end + delay, record))
                        last[mac] = (record, end)
                runner.state = {'protocol': states[mac]}
                wake[mac] = end + (1 if legacy else runner.interval(end))
                if not legacy and request[mac] > start:
                    # The request signal ends the sleep, or the next one at once.
                    wake[mac] = min(wake[mac], max(end, request[mac]))
        return applied, activate, passes

    def test_every_node_applies_inside_the_supported_budget(self):
        # Supported: replication up to 15 s (Alfred syncs every 10 s), passes
        # up to 0.8 s plus scoring, any start phase, one early-aborted pass
        # anywhere in the plan, and manager requests by :28. Readiness must
        # reach every peer before :45, so request lateness plus replication
        # stays under 19 s whatever the pass cadence.
        checked = 0
        for nodes, costs in ((2, (0.096, 0.8)), (3, (0.3,))):
            for cost in costs:
                for phases in ((0.13, 4.6), (0.79, 0.13), (4.9, 0.0)):
                    phases += (1.9,)
                    for requests in ((25, 25, 25), (25, 28, 27), (28, 25, 25)):
                        for delay in (0.5, 15):
                            fails = [None] + [(mac, self.BASE + offset) for mac in self.MACS[:2]
                                              for offset in range(98, 142, 7)]
                            for fail in fails:
                                applied, activate, _ = self.simulate(nodes, cost, delay, fail,
                                                                     phases=phases, requests=requests)
                                case = (nodes, cost, delay, fail, phases, requests)
                                self.assertEqual(set(applied), set(self.MACS[:nodes]), case)
                                for second in applied.values():
                                    self.assertTrue(activate <= second <= activate + agreement.APPLY_GRACE, case)
                                checked += 1
        self.assertGreater(checked, 600)

    def test_request_wake_publishes_readiness_in_time(self):
        # Without the wake, readiness from :25 can reach the peer after :45
        # and both nodes propose. Cost 0.096 s, 15 s replication.
        applied, _, _ = self.simulate(2, 0.096, 15, phases=(4.6, 0.13))
        self.assertEqual(set(applied), {OWN, PEER})

    def test_request_signals_only_the_run_daemon(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'manet-acs-agreement.pid').write_text('4242\n')
            for cmdline, expected in [
                    (b'python3\0/usr/local/bin/mesh-channel-agreement.py\0run\0', True),
                    (b'python3\0/usr/local/bin/mesh-channel-agreement.py\0members\0', False),
                    (b'/usr/sbin/sshd\0', False)]:
                with patch.object(runtime.Path, 'read_bytes', return_value=cmdline), \
                        patch.object(runtime.os, 'pidfd_open', return_value=99), \
                        patch.object(runtime.os, 'close') as close, \
                        patch.object(runtime.signal, 'pidfd_send_signal') as kill:
                    runtime.wake_daemon(directory)
                close.assert_called_once_with(99)
                self.assertEqual(kill.called, expected, cmdline)
                if expected:
                    kill.assert_called_once_with(99, runtime.signal.SIGUSR1)
            # A process that exited before pidfd_open is never signalled.
            with patch.object(runtime.os, 'pidfd_open', side_effect=ProcessLookupError), \
                    patch.object(runtime.signal, 'pidfd_send_signal') as kill:
                runtime.wake_daemon(directory)
            kill.assert_not_called()
            Path(directory, 'manet-acs-agreement.pid').unlink()
            with patch.object(runtime.signal, 'pidfd_send_signal') as kill:
                runtime.wake_daemon(directory)
            kill.assert_not_called()

    def test_passes_per_round(self):
        _, _, legacy = self.simulate(2, 0.1, 3, legacy=True)
        _, _, new = self.simulate(2, 0.1, 3)
        # Two nodes, one live plan: about half the one-second loop.
        self.assertLess(new, legacy * 0.6)

    def test_stale_phase_uses_default_spacing(self):
        # A phase surviving a reboot or backward clock step names an old round.
        runner = runtime.Runtime.__new__(runtime.Runtime)
        plan = {'deadline': self.BASE + 110, 'activate_at': self.BASE + 140}
        runner.state = {'protocol': {'round': self.BASE // agreement.ROUND_SECONDS - 1,
                                     'phase': 'committed', 'plan': plan}}
        with patch.object(runtime, 'clock_ready', return_value=True):
            self.assertEqual(runner.interval(self.BASE + 120.2), runtime.IDLE_INTERVAL)
            runner.state['protocol']['round'] += 1
            self.assertEqual(runner.interval(self.BASE + 120.2), runtime.ACTIVE_INTERVAL)

    def test_clockless_node_uses_default_spacing(self):
        runner = runtime.Runtime.__new__(runtime.Runtime)
        runner.state = {}
        with patch.object(runtime, 'clock_ready', return_value=False):
            self.assertEqual(runner.interval(self.BASE + 50), runtime.IDLE_INTERVAL)

    def test_sleep_never_crosses_a_window_start(self):
        runner = runtime.Runtime.__new__(runtime.Runtime)
        runner.state = {}
        with patch.object(runtime, 'clock_ready', return_value=True):
            self.assertAlmostEqual(runner.interval(self.BASE + 43), 2 + runtime.WAKE_MARGIN)

if __name__ == '__main__':
    unittest.main()
