"""ACS snapshots retain observations through expiry and failed reads."""
import json
import os
from unittest.mock import patch

from test_acs import AcsHarness, OWN, PEER, THIRD, runtime
from test_acs_agreement import status


class AcsStatusTests(AcsHarness):
    def setUp(self):
        super().setUp()
        self.configure(2462, None)
        self.now = 945
        env = patch.dict(os.environ, self.env)
        env.start()
        self.addCleanup(env.stop)
        self.command('alfred', 'sys.stdin.read()')
        self.runner = runtime.Runtime()
        self.runner.state['clock_boot'] = self.runner.boot
        self.runner.request_path.write_text(json.dumps({'round': 5}))
        records = {mac: {'status': status(boot, current={'2.4': 2462})}
                   for mac, boot in ((PEER, 'b'), (THIRD, 'c'))}
        for seam in (patch.object(runtime.time, 'time', side_effect=lambda: self.now),
                     patch.object(self.runner, 'receive', return_value=records),
                     patch.object(self.runner, 'members', return_value=[OWN, PEER, THIRD]),
                     patch.object(self.runner, 'score', return_value=({'2.4': 2462}, False))):
            seam.start()
            self.addCleanup(seam.stop)

    def snapshot(self):
        return json.loads((self.root / 'manet-acs-status.json').read_text())

    def assert_observations(self, snapshot):
        self.assertEqual(snapshot['current'], {'2.4': 2462})
        self.assertEqual(set(snapshot['members']),
                         {'NODE_' + mac.replace(':', '') for mac in (OWN, PEER, THIRD)})
        self.assertEqual(snapshot['reachable'], 2)

    def expire(self):
        self.runner.tick(self.now)
        self.assertEqual(self.snapshot()['phase'], 'prepared')
        self.assert_observations(self.snapshot())
        self.now = self.runner.state['protocol']['plan']['deadline']
        self.runner.tick(self.now)
        self.assertEqual(self.runner.state['protocol']['expiry_reason'], 'no-majority')
        self.assertEqual(self.snapshot()['phase'], 'expired')
        self.assert_observations(self.snapshot())

    def test_expiry_and_contended_lock_keep_observations(self):
        self.expire()
        self.now += 1
        with self.runner.channel_lock(), self.assertRaises(BlockingIOError):
            self.runner.tick(self.now)
        snapshot = self.snapshot()
        self.assertEqual(snapshot['phase'], 'expired')
        self.assert_observations(snapshot)
        self.assertIn('error', snapshot)
        self.assertNotIn('observed_boot', snapshot)
        self.now += 1
        self.runner.tick(self.now)
        self.assert_observations(self.snapshot())
        self.assertNotIn('error', self.snapshot())
        self.assertIn('observed_boot', self.snapshot())

    def test_observations_expire_a_minute_after_the_last_radio_read(self):
        self.expire()
        self.runner.ui_observed_at -= 61
        self.runner.ui_members_at -= 61
        with patch.object(self.runner, 'status', side_effect=ValueError('radio unavailable')):
            with self.assertRaisesRegex(ValueError, 'radio unavailable'):
                self.runner.tick(self.now)
        snapshot = self.snapshot()
        self.assertEqual((snapshot['current'], snapshot['members'], snapshot['reachable']),
                         ({}, [], None))

    def test_peers_expire_while_only_membership_reads_fail(self):
        self.expire()
        self.runner.ui_members_at -= 61
        with patch.object(self.runner, 'members', side_effect=ValueError('no registry')):
            try:
                self.runner.tick(self.now)
            except ValueError:
                pass
        snapshot = self.snapshot()
        self.assertEqual(snapshot['current'], {'2.4': 2462})
        self.assertEqual((snapshot['members'], snapshot['reachable']), ([], None))

    def test_failed_radio_read_keeps_observations_then_refreshes(self):
        self.expire()
        with patch.object(self.runner, 'status', side_effect=ValueError('radio unavailable')):
            with self.assertRaisesRegex(ValueError, 'radio unavailable'):
                self.runner.tick(self.now)
        self.assert_observations(self.snapshot())
        self.assertNotIn('observed_boot', self.snapshot())
        self.configure(2437, None)
        with patch.object(self.runner, 'members', return_value=[OWN]):
            self.runner.tick(self.now)
        snapshot = self.snapshot()
        self.assertEqual(snapshot['current'], {'2.4': 2437})
        self.assertEqual(snapshot['members'], ['NODE_' + OWN.replace(':', '')])
        self.assertEqual(snapshot['reachable'], 0)

    def test_first_tick_failure_still_has_unknown_observation_fields(self):
        with self.runner.channel_lock(), self.assertRaises(BlockingIOError):
            self.runner.tick(self.now)
        snapshot = self.snapshot()
        self.assertEqual(snapshot['current'], {})
        self.assertEqual(snapshot['members'], [])
        self.assertIsNone(snapshot['reachable'])
        self.assertNotIn('observed_boot', snapshot)
