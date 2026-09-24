"""Absolute rendezvous scheduling and discovery-to-data adoption regressions."""
import json
import os
import time
from unittest.mock import patch

import manet_acs_agreement as protocol
import manet_rendezvous as rendezvous
from test_acs import AcsHarness, OWN, PEER, RADIO, TOOLS, node, runtime
from test_acs_agreement import status
from test_acs_bootstrap import frame


class RendezvousTests(AcsHarness):
    def setUp(self):
        super().setUp()
        self.base = int(time.time()) // 720 * 720
        self.registry.write_text(node(OWN) + node(PEER, aliases=RADIO))
        self.command('alfred', '''
path = Path(os.environ['TEST_ROOT'], ('wire-' if sys.argv[1] == '-r' else 'sent-') + sys.argv[-1])
if sys.argv[1] == '-r':
    print(path.read_text() if path.exists() else '')
else:
    path.write_text(sys.stdin.read())
''')
        env = patch.dict(os.environ, self.env)
        env.start(); self.addCleanup(env.stop)
        self.runner = runtime.Runtime()

    def tick(self, offset, records=None):
        when = self.base + offset
        with patch.object(runtime.time, 'time', return_value=when), \
                patch.object(runtime.time, 'time_ns', return_value=when * 10**9), \
                patch.object(runtime.time, 'monotonic', return_value=when):
            if records is None:
                self.runner.tick(when)
            else:
                with patch.object(self.runner, 'receive', return_value=records):
                    self.runner.tick(when)

    def frequencies(self):
        return {b: f for b, (iface, path, f) in self.runner.interfaces().items()}

    def connect_halow(self):
        (self.root / 'peers').write_text(json.dumps([
            {'orig_address': RADIO, 'hard_ifname': 'wlan2', 'best': True}]))

    def test_common_rotation_contains_anchor_and_diverse_channels_in_each_band(self):
        expected = [('2.4', 2412), ('5', 5180), ('2.4', 2437),
                    ('5', 5220), ('2.4', 2462), ('5', 5745)]
        for i, pair in enumerate(expected):
            for cycle in (0, 1, 101):
                when = self.base + cycle * 720 + i * 120 + 30
                self.assertEqual(rendezvous.slot(when), pair)
                self.assertEqual(rendezvous.search_channels(when)[pair[0]], pair[1])
        self.assertGreaterEqual(protocol.RECOVERY_SECONDS, 2 * rendezvous.CYCLE_SECONDS + 120)

    def test_missing_radio_roles_do_not_seed_data_mode_before_setup(self):
        self.assertEqual(self.runner.discovery.mode({}), 'data')
        self.assertFalse(self.runner.discovery.path.exists())
        self.assertEqual(self.runner.discovery.mode(rendezvous.ANCHORS), 'search')

    def test_clockless_solo_remains_on_fixed_anchors_across_windows(self):
        (self.root / 'initial_time_synced').unlink()
        for offset in (270, 510, 750, 990):
            self.tick(offset)
            self.assertEqual(self.frequencies(), rendezvous.ANCHORS)
        self.assertNotIn('wpa_cli', (self.root / 'commands').read_text())
        self.assertFalse(self.runner.path.exists())
        self.assertFalse((self.root / 'sent-74').exists())

    def test_synchronized_search_rotates_and_preserves_mode_across_daemon_restart(self):
        self.tick(270, {})
        self.assertEqual(self.frequencies(), {'2.4': 2437, '5': 5220})
        self.assertEqual(self.runner.discovery.mode(self.frequencies()), 'search')
        self.runner = runtime.Runtime()
        self.tick(510, {})
        self.assertEqual(self.frequencies(), {'2.4': 2462, '5': 5745})
        self.assertEqual(self.runner.discovery.mode(self.frequencies()), 'search')

    def test_first_sync_enables_rotation_without_a_restart(self):
        marker = self.root / 'initial_time_synced'
        marker.unlink()
        self.tick(260)
        self.assertEqual(self.frequencies(), rendezvous.ANCHORS)
        marker.touch()
        self.tick(270, {})
        self.assertEqual(self.frequencies(), {'2.4': 2437, '5': 5220})

    def test_cold_restart_of_searcher_parks_at_anchors_when_disconnected(self):
        self.tick(270, {})
        (self.root / 'initial_time_synced').unlink()
        self.runner = runtime.Runtime()
        self.tick(310)
        self.assertEqual(self.frequencies(), rendezvous.ANCHORS)
        self.assertEqual(self.runner.discovery.mode(self.frequencies()), 'search')

    def test_halow_path_holds_search_channels_for_recovery_or_joint_bootstrap(self):
        self.connect_halow()
        for offset in (270, 510):
            self.tick(offset, {})
        self.assertEqual(self.frequencies(), rendezvous.ANCHORS)
        self.assertNotIn('wpa_cli', (self.root / 'commands').read_text())

    def test_failed_peer_query_does_not_turn_a_connected_node_into_a_searcher(self):
        with patch.dict(os.environ, TEST_BATCTL_RC='1'):
            self.tick(270, {})
        self.assertEqual(self.frequencies(), rendezvous.ANCHORS)
        self.assertNotIn('wpa_cli', (self.root / 'commands').read_text())

    def test_missing_band_follows_same_slots_without_substitution(self):
        self.configure(None, 5180)
        self.tick(270, {})
        self.assertEqual(self.frequencies(), {'5': 5220})
        self.assertEqual(rendezvous.slot(self.base + 270), ('2.4', 2437))
        self.tick(510, {})
        self.assertEqual(self.frequencies(), {'5': 5745})

    def test_unsupported_entry_is_skipped_without_renumbering_the_schedule(self):
        self.tick(30, {})  # Establish the boot before changing cached PHY capabilities.
        self.runner.capabilities[('wlan1', '5', '0')] = (self.base + 270, [5180, 5200])
        self.tick(270, {})
        self.assertEqual(self.frequencies(), {'2.4': 2437, '5': 5180})
        self.runner.capabilities[('wlan1', '5', '0')] = (self.base + 510, [5180, 5200])
        self.tick(510, {})
        self.assertEqual(self.frequencies(), {'2.4': 2462, '5': 5180})
        self.assertEqual(rendezvous.slot(self.base + 630), ('5', 5745))

    def test_discovery_rejects_disabled_no_ir_and_dfs_frequencies(self):
        text = '''* 2412.0 MHz [1] (20 dBm)
* 2437.0 MHz [6] (disabled)
* 2462.0 MHz [11] (no IR)
* 5180 MHz [36] (20 dBm)
* 5220 MHz [44] (radar detection)
* 5745 MHz [149] (passive scan)'''
        self.assertEqual(rendezvous.permitted_frequencies(text), {2412, 5180})

    def test_failed_discovery_hop_retries_at_most_once_per_thirty_seconds(self):
        with patch.object(self.runner, 'apply', side_effect=OSError('radio unavailable')) as apply:
            with self.assertRaises(OSError):
                self.tick(270, {})
            self.tick(271, {})
            self.tick(299, {})
            self.assertEqual(apply.call_count, 1)
            with self.assertRaises(OSError):
                self.tick(300, {})
            self.assertEqual(apply.call_count, 2)

    def test_data_channel_encounter_stops_hopping_without_a_radio_restart(self):
        self.tick(270, {})
        channels = self.frequencies()
        self.connect_halow()  # Any BATMAN path works, including the visited Wi-Fi channel.
        plan = protocol.make_plan({OWN: status('a'), PEER: status('b')},
                                  channels, False, self.base + 45, 'e' * 32)
        commit = {'plan': protocol.digest(plan), 'issued_at': plan['deadline'],
                  'approvals': {OWN: 'a' * 32, PEER: 'b' * 32}}
        destination = {'plan': plan, 'commit': commit}
        payload = {'kind': 'acs_state', 'node': PEER, 'aliases': [RADIO],
                   'status': status('b', ready=False, current=channels), 'destination': destination}
        with patch.object(runtime.time, 'time_ns', return_value=(self.base + 275) * 10**9):
            envelope = self.transport.seal(74, payload)
        (self.root / 'wire-74').write_text(frame(PEER, envelope))
        (self.root / 'commands').write_text('')
        self.tick(275)
        self.assertEqual(self.runner.discovery.mode(channels), 'data')
        self.assertEqual(self.runner.state['destination'], destination)
        commands = (self.root / 'commands').read_text()
        self.assertNotIn('wpa_cli', commands)
        self.assertNotIn('systemctl', commands)
        (self.root / 'peers').write_text('[]')
        self.tick(510, {})
        self.assertEqual(self.frequencies(), channels)  # The next rotation cannot pull it away.

    def test_search_frequency_alone_is_not_proof_of_the_data_channel(self):
        self.tick(270, {})
        self.connect_halow()
        self.tick(275, {})  # Peering alone is not an authenticated channel plan.
        self.assertEqual(self.runner.discovery.mode(self.frequencies()), 'search')
        (self.root / 'peers').write_text('[]')
        self.tick(510, {})
        self.assertEqual(self.frequencies(), {'2.4': 2462, '5': 5745})

    def test_matching_authenticated_helper_exits_search_but_not_partition_comparison(self):
        self.tick(270, {})
        local = self.runner.status(self.base + 270)
        channels = dict(local['current'])
        records = {PEER: {'channels': channels, 'size': 3}}
        self.assertEqual(runtime.recovery_destination(records, local, self.base + 270, OWN), channels)
        local['discovery'] = False
        self.assertIsNone(runtime.recovery_destination(records, local, self.base + 270, OWN))

    def test_searcher_cannot_advertise_old_certificate_while_visiting_its_frequency(self):
        self.tick(270, {})
        channels = self.frequencies()
        plan = protocol.make_plan({OWN: status('a')}, channels, False, self.base + 45, 'e' * 32)
        destination = {'plan': plan, 'commit': {'plan': protocol.digest(plan),
            'issued_at': plan['deadline'], 'approvals': {OWN: 'a' * 32}}}
        self.assertIsNone(protocol.live_destination(destination, self.runner.status(self.base + 270), self.base + 270))

    def test_busy_agreement_prevents_rotation(self):
        self.runner.busy_path.write_text(str(self.base + 290))
        self.tick(270, {})
        self.assertEqual(self.frequencies(), rendezvous.ANCHORS)

    def test_clockless_parking_waits_for_another_radio_operation(self):
        self.tick(270, {})
        (self.root / 'initial_time_synced').unlink()
        self.runner.busy_path.write_text(str(self.base + 310))
        self.tick(300)
        self.assertEqual(self.frequencies(), {'2.4': 2437, '5': 5220})
        self.tick(311)
        self.assertEqual(self.frequencies(), rendezvous.ANCHORS)

    def test_clockless_application_cannot_rotate_to_a_non_anchor(self):
        (self.root / 'initial_time_synced').unlink()
        with self.assertRaisesRegex(ValueError, 'incompatible'):
            self.runner.apply({'2.4': 2437}, discovering=True)

    def test_shell_helper_adoption_on_same_frequency_does_not_restart_supplicant(self):
        self.tick(270, {})
        (self.root / 'commands').write_text('')
        body = self.definitions('node-manager-acs.sh') + '''
LIMP_STATE_FILE="$TEST_ROOT/limp"
adopt_helper_channels 2437 5220
is_in_lobby
'''
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'false\n')
        self.assertNotIn('systemctl restart', (self.root / 'commands').read_text())

    def test_real_tourguide_visits_rotation_and_avoids_retuning_its_data_channel(self):
        for offset, expected in [(30, [2412, 2437]), (270, []), (510, [2462, 2437])]:
            with self.subTest(offset=offset):
                self.configure(2437, None)
                (self.root / 'clock').write_text(str(self.base + offset))
                (self.root / 'hops').write_text('')
                self.command('wpa_cli', '''
import re
conf = Path(os.environ['MANET_WPA_DIR'], 'wpa_supplicant-' + sys.argv[2] + '.conf').read_text()
with Path(os.environ['TEST_ROOT'], 'hops').open('a') as out:
    out.write(re.search(r'frequency=(\\d+)', conf)[1] + '\\n')
''')
                source = (TOOLS / 'tourguide-manager.sh').read_text()
                source = source.replace('/var/run/tourguide_state', str(self.root / 'tourguide_state'))
                source = source.replace('"/sys/class/net/${CONTROL_IFACE}/address"', '"$TEST_ROOT/mac"')
                source = source.replace('BATCTL_PATH="/usr/sbin/batctl"', 'BATCTL_PATH="$TEST_ROOT/bin/batctl"')
                source = source.replace('ENCODER_PATH="/usr/local/bin/encoder.py"', f'ENCODER_PATH="{TOOLS}/encoder.py"')
                result = self.shell(source)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([int(f) for f in (self.root / 'hops').read_text().split()], expected)
                self.assertEqual(self.frequencies(), {'2.4': 2437})
                self.assertEqual(self.runner.discovery.mode(self.frequencies()), 'data')

    def test_helper_keeps_matching_band_connected_while_recovering_other_band(self):
        self.tick(270, {})
        (self.root / 'commands').write_text('')
        body = self.definitions('node-manager-acs.sh') + '''
LIMP_STATE_FILE="$TEST_ROOT/limp"
adopt_helper_channels 2437 5200
'''
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = (self.root / 'commands').read_text()
        self.assertNotIn('systemctl restart wpa_supplicant@wlan0.service', commands)
        self.assertIn('systemctl restart wpa_supplicant@wlan1.service', commands)
