"""HaLow readiness suppresses Wi-Fi tours without disabling live recovery."""
import json
import os
import subprocess
import time
from unittest.mock import patch

import manet_rendezvous as rendezvous
from test_acs import AcsHarness, OWN, PEER, THIRD, RADIO, TOOLS, node, runtime, tourguide
from test_acs_agreement import status
from test_acs_bootstrap import frame


class HaLowTourguideTests(AcsHarness):
    def setUp(self):
        super().setUp()
        self.configure(2462, None)
        self.registry.write_text(node(OWN))
        (self.root / 'clock').write_text('750')  # 2.4 GHz anchor visit.
        self.halow = self.root / 'net/wlan2'
        self.halow.mkdir()
        (self.halow / 'flags').write_text('0x1003')
        (self.roles / 'halow_if').write_text('wlan2\n')
        self.command('batctl', '''
with Path(os.environ['TEST_ROOT'], 'commands').open('a') as log:
    log.write('batctl ' + ' '.join(sys.argv[1:]) + '\\n')
if sys.argv[1:] == ['if']:
    count = Path(os.environ['TEST_ROOT'], 'health-checks')
    n = int(count.read_text()) + 1 if count.exists() else 1
    count.write_text(str(n))
    if n == 2 and os.environ.get('TEST_HALOW_DELAY_CLOCK'):
        clock = Path(os.environ['TEST_ROOT'], 'clock')
        clock.write_text(str(int(clock.read_text()) + int(os.environ['TEST_HALOW_DELAY_CLOCK'])))
    if os.environ.get('TEST_HALOW_BAT_FAIL') == '1':
        sys.exit(1)
    active = os.environ.get('TEST_HALOW_ACTIVE', 'active')
    if n <= int(os.environ.get('TEST_HALOW_UP_AFTER', '0')):
        active = 'inactive'
    print('wlan2: ' + active)
elif sys.argv[1:] == ['meshif', 'bat0', 'originators_json']:
    print(Path(os.environ['TEST_ROOT'], 'peers').read_text())
    sys.exit(int(os.environ['TEST_BATCTL_RC']))
else:
    sys.exit(99)
''')
        self.command('systemctl', '''
with Path(os.environ['TEST_ROOT'], 'commands').open('a') as log:
    log.write('systemctl ' + ' '.join(sys.argv[1:]) + '\\n')
if sys.argv[1] == 'is-active':
    sys.exit(int(os.environ.get('TEST_HALOW_SERVICE_RC', '0')))
''')
        original_iw = (self.bin / 'iw').read_text().split('from pathlib import Path\n', 1)[1]
        self.command('iw', '''
if sys.argv[1:3] == ['dev', 'wlan2']:
    if os.environ.get('TEST_HALOW_IW_FAIL') == '1':
        sys.exit(1)
    if sys.argv[3:] == ['info']:
        # Morse can expose an ordinary frequency through iw; the role identifies S1G.
        print('wiphy 2\\ntype ' + os.environ.get('TEST_HALOW_MODE', 'mesh point') + '\\nchannel 1 (2412 MHz)')
    elif sys.argv[3:] == ['get', 'mesh_param', 'mesh_plink_timeout']:
        print(os.environ.get('TEST_HALOW_JOINED', '0'))
    else:
        sys.exit(99)
    sys.exit(0)
''' + original_iw)
        self.command('alfred', '''
path = Path(os.environ['TEST_ROOT'], ('wire-' if sys.argv[1] == '-r' else 'sent-') + sys.argv[-1])
if sys.argv[1] == '-r':
    print(path.read_text() if path.exists() else '')
else:
    path.write_text(sys.stdin.read())
''')
        env = patch.dict(os.environ, self.env)
        env.start(); self.addCleanup(env.stop)

    def run_tourguide(self):
        source = (TOOLS / 'tourguide-manager.sh').read_text()
        source = source.replace('/var/run/tourguide_state', str(self.root / 'tourguide_state'))
        source = source.replace('"/sys/class/net/${CONTROL_IFACE}/address"', '"$TEST_ROOT/mac"')
        source = source.replace('BATCTL_PATH="/usr/sbin/batctl"', 'BATCTL_PATH="$TEST_ROOT/bin/batctl"')
        source = source.replace('REGISTRY_STATE_FILE="/var/run/mesh_node_registry"', 'REGISTRY_STATE_FILE="$TEST_ROOT/registry"')
        source = source.replace('ENCODER_PATH="/usr/local/bin/encoder.py"', f'ENCODER_PATH="{TOOLS}/encoder.py"')
        return self.shell(source)

    def test_working_halow_suppresses_tour_without_requiring_any_peer(self):
        self.env['TEST_BATCTL_RC'] = '1'  # Peer discovery is not needed for this decision.
        result = self.run_tourguide()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('HaLow mesh is ready', result.stderr)
        commands = (self.root / 'commands').read_text()
        self.assertNotIn('wpa_cli', commands)
        self.assertNotIn('originators_json', commands)
        self.assertFalse((self.root / 'sent-75').exists())
        self.assertFalse((self.root / 'tourguide_state').exists())
        self.assertIn('frequency=2462', (self.wpa / 'wpa_supplicant-wlan0.conf').read_text())

    def test_absent_halow_keeps_existing_rotating_tour(self):
        (self.roles / 'halow_if').unlink()
        result = self.run_tourguide()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'sent-75').exists())
        self.assertIn('wpa_cli -i wlan0 reconfigure', (self.root / 'commands').read_text())
        self.assertIn('frequency=2462', (self.wpa / 'wpa_supplicant-wlan0.conf').read_text())

    def test_halow_failure_reenables_the_tour_without_restarting_manager(self):
        self.assertEqual(self.run_tourguide().returncode, 0)
        self.env['TEST_HALOW_ACTIVE'] = 'inactive'
        result = self.run_tourguide()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'sent-75').exists())
        self.assertIn('wpa_cli -i wlan0 reconfigure', (self.root / 'commands').read_text())

    def test_halow_recovering_during_preparation_cancels_the_hop(self):
        self.env['TEST_HALOW_UP_AFTER'] = '1'
        result = self.run_tourguide()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(int((self.root / 'health-checks').read_text()), 2)
        self.assertNotIn('wpa_cli', (self.root / 'commands').read_text())
        self.assertFalse((self.root / 'sent-75').exists())

    def test_slow_final_health_check_cannot_make_a_guide_leave_after_entry_window(self):
        self.env.update(TEST_HALOW_ACTIVE='inactive', TEST_HALOW_DELAY_CLOCK='25')
        result = self.run_tourguide()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('wpa_cli', (self.root / 'commands').read_text())
        self.assertFalse((self.root / 'sent-75').exists())

    def test_interface_role_alone_is_not_proof_of_working_halow(self):
        self.assertTrue(rendezvous.halow_ready())
        for setting in [{'TEST_HALOW_ACTIVE': 'inactive'}, {'TEST_HALOW_BAT_FAIL': '1'},
                        {'TEST_HALOW_SERVICE_RC': '3'}, {'TEST_HALOW_IW_FAIL': '1'},
                        {'TEST_HALOW_MODE': 'managed'}, {'TEST_HALOW_JOINED': ''},
                        {'TEST_HALOW_JOINED': 'not joined'}]:
            with self.subTest(setting=setting), patch.dict(os.environ, setting):
                self.assertFalse(rendezvous.halow_ready())
        for flags in ('0x1002', 'bad flags'):
            (self.halow / 'flags').write_text(flags)
            self.assertFalse(rendezvous.halow_ready())
        (self.halow / 'flags').unlink()
        self.assertFalse(rendezvous.halow_ready())

    def test_disabled_or_missing_halow_does_not_suppress_fallback(self):
        (self.root / 'radio-state').write_text(json.dumps({'desired': {'wlan2': 'down'}}))
        self.assertFalse(rendezvous.halow_ready())
        (self.root / 'radio-state').unlink()
        (self.roles / 'halow_if').write_text('wlan9\n')
        self.assertFalse(rendezvous.halow_ready())
        (self.roles / 'halow_if').write_text('')
        self.assertFalse(rendezvous.halow_ready())

    def test_an_unusable_first_halow_interface_does_not_hide_a_working_second(self):
        (self.roles / 'halow_if').write_text('missing\nwlan2\n')
        self.assertTrue(rendezvous.halow_ready())

    def test_health_timeouts_enable_fallback_and_every_probe_is_bounded(self):
        with patch.object(rendezvous.subprocess, 'run', side_effect=subprocess.TimeoutExpired('batctl', 2)) as run:
            self.assertFalse(rendezvous.halow_ready())
            self.assertEqual(run.call_args.kwargs['timeout'], 2)

    def test_readiness_rides_existing_encrypted_status_with_bounded_local_polling(self):
        runner = runtime.Runtime()
        now = int(time.time()) // 180 * 180 + 150
        with patch.object(runtime.time, 'monotonic', return_value=100), \
                patch.object(runtime.time, 'time', return_value=now), \
                patch.object(runtime.time, 'time_ns', return_value=now * 10**9):
            runner.tick(now)
            envelope = json.loads((self.root / 'sent-74').read_text())
            self.assertTrue(self.transport.open(74, envelope).payload['status']['halow_ready'])
        first = int((self.root / 'health-checks').read_text())
        (self.halow / 'flags').write_text('0x1002')
        with patch.object(runtime.time, 'monotonic', return_value=110):
            self.assertTrue(runner.status(now)['halow_ready'])
        self.assertEqual(int((self.root / 'health-checks').read_text()), first)
        with patch.object(runtime.time, 'monotonic', return_value=115):
            self.assertFalse(runner.status(now)['halow_ready'])
        self.assertFalse((self.root / 'sent-75').exists())
        self.assertFalse((self.root / 'sent-77').exists())

    def test_mixed_group_elects_only_a_node_needing_wifi_fallback(self):
        registry = node(OWN, 0) + node(PEER, 10, aliases=RADIO) + node(THIRD, 20)
        for own, peers in [(OWN, [RADIO, THIRD]), (PEER, [OWN, THIRD]), (THIRD, [OWN, RADIO])]:
            self.assertEqual(tourguide.elect(own, peers, registry, '2.4', [OWN]), PEER)
        self.assertIsNone(tourguide.elect(OWN, [RADIO, THIRD], registry, '2.4', [OWN, PEER, THIRD]))
        self.assertIsNone(tourguide.elect(OWN, [], '', '2.4', [OWN]))

    def test_real_tourguide_uses_fresh_peer_readiness_to_select_fallback_node(self):
        (self.roles / 'halow_if').unlink()
        self.registry.write_text(node(OWN, 20) + node(PEER, 0, aliases=RADIO))
        (self.root / 'peers').write_text(json.dumps([{'orig_address': RADIO}]))
        envelope = self.transport.seal(74, dict(kind='acs_state', node=PEER,
            status=dict(status('b'), halow_ready=True)))
        (self.root / 'wire-74').write_text(frame(PEER, envelope))
        result = self.run_tourguide()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'sent-75').exists(), result.stderr)
        self.assertIn('wpa_cli -i wlan0 reconfigure', (self.root / 'commands').read_text())

    def test_exclusions_require_fresh_authenticated_readiness_and_do_not_override_local_probe(self):
        runner = runtime.Runtime()
        now = int(time.time())
        def record(mac, ready=True):
            envelope = self.transport.seal(74, dict(kind='acs_state', node=mac,
                status=dict(status('b'), halow_ready=ready)))
            return frame(mac, envelope)
        (self.root / 'wire-74').write_text(record(PEER) + record(OWN) + record(THIRD, False))
        self.assertEqual(runner.tourguide_exclusions(now), [PEER])
        self.assertEqual(runner.tourguide_exclusions(now + 46), [])
        (self.root / 'wire-74').write_text(frame(PEER, {'status': {'halow_ready': True}}))
        self.assertEqual(runner.tourguide_exclusions(now), [])
