"""Clockless lobby recovery with real encryption and simulated Alfred/radios."""
import json
import os
import time
from unittest.mock import patch

from manet_admin import AdminError, AdminTransport
from test_acs import AcsHarness, OWN, PEER, THIRD, runtime


def frame(mac, envelope):
    return '{ "' + mac + '", ' + json.dumps(json.dumps(envelope)) + ' },\n'


class BootstrapTests(AcsHarness):
    def setUp(self):
        super().setUp()
        self.configure(2412, None)
        self.marker = self.root / 'initial_time_synced'
        self.marker.unlink()
        self.command('alfred', '''
path = Path(os.environ['TEST_ROOT'], ('wire-' if sys.argv[1] == '-r' else 'sent-') + sys.argv[-1])
if sys.argv[1] == '-r':
    print(path.read_text() if path.exists() else '')
else:
    path.write_text(sys.stdin.read())
''')
        (self.root / 'peers').write_text(json.dumps([{'orig_address': PEER}]))
        env = patch.dict(os.environ, self.env); env.start(); self.addCleanup(env.stop)
        self.runner = runtime.Runtime()
        self.remote = AdminTransport(self.root / 'mesh.conf', self.root / 'remote-admin')

    def select(self, monotonic=100):
        with patch.object(runtime.time, 'time', return_value=10000000000), patch.object(runtime.time, 'monotonic', return_value=monotonic):
            return self.runner.bootstrap_select()

    def reply(self, change=None):
        probe = json.loads((self.root / 'manet-acs-probe.json').read_text())
        payload = {'kind': 'acs_probe_reply', 'node': PEER, 'boot': 'b' * 32,
                   'channels': {'2.4': 2462}, 'size': 3,
                   'answers': {OWN: {'boot': self.runner.boot, 'nonce': probe['nonce']}}}
        if change:
            change(payload)
        envelope = self.remote.seal_challenge(77, payload)
        (self.root / 'wire-77').write_text(frame(PEER, envelope))
        return payload

    def test_live_challenge_recovers_without_setting_clock_or_admin_history(self):
        self.assertIsNone(self.select())
        wire = json.loads((self.root / 'sent-76').read_text())
        self.assertEqual(self.remote.open_challenge(76, wire).payload['node'], OWN)
        self.reply()
        self.assertEqual(self.select(101), {'2.4': 2462})
        self.assertIsNone(self.select(102))  # Consumed before a caller may move.
        self.assertFalse(self.marker.exists())
        self.assertFalse((self.root / 'admin/sender.json').exists())
        self.assertFalse(self.runner.path.exists())

    def test_cached_reply_expires_even_across_process_restart_or_clock_change(self):
        self.select(); self.reply()
        self.runner = runtime.Runtime()
        old_probe = json.loads((self.root / 'manet-acs-probe.json').read_text())
        self.assertIsNone(self.select(160))
        new_probe = json.loads((self.root / 'manet-acs-probe.json').read_text())
        self.assertNotEqual(new_probe['nonce'], old_probe['nonce'])
        self.assertIsNone(self.select(161))

    def test_old_boot_wrong_recipient_or_wrong_nonce_cannot_authorize_recovery(self):
        self.select()
        for change in [lambda p: p['answers'][OWN].update(boot='f' * 32),
                       lambda p: p['answers'][OWN].update(nonce='f' * 32),
                       lambda p: p['answers'].update({THIRD: p['answers'].pop(OWN)}),
                       lambda p: p.update(node=THIRD),
                       lambda p: p.update(channels={'2.4': 9999}),
                       lambda p: p.update(channels={'5': 5220})]:
            self.reply(change)
            self.assertIsNone(self.select(101))
        self.runner.boot = 'f' * 32
        self.assertIsNone(self.select(102))

    def test_slow_reply_read_cannot_extend_probe_deadline(self):
        self.select(); self.reply()
        original = runtime.command
        with patch.object(runtime.time, 'monotonic', return_value=159) as clock:
            def delayed_reply(args, **kwargs):
                result = original(args, **kwargs)
                if args == ['alfred', '-r', 77]:
                    clock.return_value = 161  # I/O crosses the outstanding probe's expiry.
                return result
            with patch.object(runtime, 'command', side_effect=delayed_reply):
                self.assertIsNone(self.runner.bootstrap_select())
        self.assertFalse(json.loads((self.root / 'manet-acs-probe.json').read_text())['used'])

    def test_reply_can_span_two_replication_periods_and_manager_wakeup(self):
        self.select(); self.reply()
        self.assertEqual(self.select(145), {'2.4': 2462})

    def test_failed_consumption_cannot_return_an_actionable_channel(self):
        self.select(); self.reply()
        with patch.object(runtime, 'private_json_write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.select(101)
        self.assertFalse(json.loads((self.root / 'manet-acs-probe.json').read_text())['used'])

    def test_no_peer_does_not_probe_but_halow_reachable_data_node_can(self):
        (self.root / 'peers').write_text('[]')
        self.assertIsNone(self.select())
        self.assertFalse((self.root / 'sent-76').exists())
        (self.root / 'peers').write_text(json.dumps([{'orig_address': PEER}]))
        self.configure(2437, None)
        self.assertIsNone(self.select())
        self.assertTrue((self.root / 'sent-76').exists())
        self.reply()
        self.assertEqual(self.select(101), {'2.4': 2462})

    def test_daemon_recovers_cold_data_radio_over_surviving_link(self):
        self.configure(2437, None)
        with patch.object(runtime.time, 'time', return_value=10000000000), patch.object(runtime.time, 'monotonic', return_value=100):
            self.runner.tick(10000000000)
        self.reply()
        with patch.object(runtime.time, 'time', return_value=10000000000), patch.object(runtime.time, 'monotonic', return_value=101):
            self.runner.tick(10000000000)
        self.assertIn('frequency=2462', (self.wpa / 'wpa_supplicant-wlan0.conf').read_text())
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.runner.path.exists())
        self.assertFalse((self.root / 'sent-74').exists())

    def test_matching_clockless_reply_finishes_discovery_without_retuning(self):
        self.configure(2437, None)
        self.runner.discovery.set_mode('search')
        self.select()
        self.reply(lambda p: p.update(channels={'2.4': 2437}))
        (self.root / 'commands').write_text('')
        with patch.object(runtime.time, 'time', return_value=10000000000), patch.object(runtime.time, 'monotonic', return_value=101):
            self.runner.tick(10000000000)
        self.assertEqual(self.runner.discovery.mode({'2.4': 2437}), 'data')
        self.assertNotIn('wpa_cli', (self.root / 'commands').read_text())

    def test_normal_helper_select_cli_uses_bootstrap_before_time_sync(self):
        self.select(time.monotonic()); self.reply()
        result = self.run_command(['python3', str(runtime.TOOLS / 'mesh-channel-agreement.py'), 'helper-select'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '2462|')

    def test_probe_traffic_is_limited_and_stops_after_sync(self):
        self.select()
        original = (self.root / 'sent-76').read_bytes()
        self.select(110); self.select(131); self.select(159)
        self.assertEqual((self.root / 'sent-76').read_bytes(), original)
        self.marker.touch()
        self.assertIsNone(self.select(160))
        self.assertEqual((self.root / 'sent-76').read_bytes(), original)

    def test_live_tourguide_answers_reachable_probe_once(self):
        self.select()
        (self.root / 'wire-76').write_text(frame(OWN, json.loads((self.root / 'sent-76').read_text())))
        (self.root / 'peers').write_text(json.dumps([{'orig_address': OWN}]))
        helper = runtime.Runtime()
        helper.own, helper.boot = PEER, 'b' * 32
        helper.run_dir = self.root / 'helper-run'; helper.run_dir.mkdir()
        helper.transport = self.remote
        with self.assertRaises(AdminError):
            helper.answer_probes({'2.4': 2462}, 3)
        self.marker.touch()
        with patch.object(runtime.time, 'monotonic', return_value=101):
            helper.answer_probes({'2.4': 2462}, 3)
        reply = (self.root / 'sent-77').read_bytes()
        (self.root / 'sent-77').unlink()
        with patch.object(runtime.time, 'monotonic', return_value=106):
            helper.answer_probes({'2.4': 2462}, 3)
        self.assertFalse((self.root / 'sent-77').exists())
        (self.root / 'wire-77').write_text(frame(PEER, json.loads(reply)))
        self.marker.unlink()
        self.assertEqual(self.select(107), {'2.4': 2462})

    def test_cold_manager_keeps_publication_and_recovery_but_defers_timed_work(self):
        result = self.shell(self.definitions('node-manager-acs.sh') + '''
NOW=1000
BOOTSTRAPPING=true
BOOTSTRAP_START_WINDOW=0
update_lobby_bootstrap
echo "$BOOTSTRAPPING"
should_perform_action SCAN 180 10 && exit 9
should_perform_action ELECTION 180 25 && exit 10
should_perform_tourguide && exit 11
should_perform_action PUBLISH 180 15
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'false')

    def test_cold_tourguide_cannot_hop_or_publish_timestamped_helpers(self):
        result = self.run_command(['bash', str(runtime.TOOLS / 'tourguide-manager.sh')])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / 'commands').exists())
        result = self.run_command(['python3', str(runtime.TOOLS / 'mesh-channel-agreement.py'),
                                   'helper-encode', '--channels', '{"2.4":2462}', '--size', '3'])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
