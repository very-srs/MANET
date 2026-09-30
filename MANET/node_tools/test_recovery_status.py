"""Recovery descriptions distinguish evidence, missing state and stale state."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from manet_recovery_status import recovery_status


class RecoveryStatusTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        env = patch.dict(os.environ, MANET_ACS_RUN_DIR=scratch.name,
                         MANET_TIME_RUN_DIR=scratch.name, MANET_IFACE_STATE_DIR=scratch.name)
        env.start()
        self.addCleanup(env.stop)
        self.snapshot = {'monotonic': time.monotonic(), 'current': {'2.4': 2437},
                         'target': {'2.4': 2462}, 'clock_ready': True,
                         'halow_ready': True, 'reachable': 2,
                         'route_interfaces': ['wlan2'], 'phase': 'idle'}
        (self.root / 'halow_if').write_text('wlan2\n')

    def describe(self):
        (self.root / 'manet-acs-status.json').write_text(json.dumps(self.snapshot))
        return recovery_status({'acs': 'y'}, {}, 'self')

    def values(self, result):
        return {row['label']: row['value'] for row in result['details']}

    def test_halow_connected_mesh_is_not_reported_as_isolated(self):
        result = self.describe()
        self.assertEqual(result['summary'], 'Connected to 2 mesh nodes via HaLow')
        rows = self.values(result)
        self.assertIn('2437', rows['Observed Wi-Fi channels'])
        self.assertIn('2462', rows['Agreed Wi-Fi channels'])
        self.assertIn('suppressed', rows['Recollection'])

    def test_ready_radio_alone_is_not_a_connection(self):
        self.snapshot['reachable'] = 0
        self.assertIn('No other mesh nodes', self.describe()['summary'])
        self.snapshot['reachable'] = None
        self.assertIn('unknown', self.describe()['summary'])

    def test_stale_observation_never_claims_current_connectivity(self):
        self.snapshot['monotonic'] -= 60
        result = self.describe()
        self.assertFalse(result['fresh'])
        self.assertIn('stale', result['summary'])
        self.assertNotIn('Observed Wi-Fi channels', self.values(result))

    def test_missing_and_broken_state_have_a_readable_fallback(self):
        for content in ('{', '[]'):
            (self.root / 'manet-acs-status.json').write_text(content)
            result = recovery_status({'acs': 'y'}, {}, 'self')
            self.assertIn('unavailable', result['summary'])

    def test_clock_wait_agreement_reconciliation_and_hold(self):
        cases = [({'clock_ready': False}, 'GPS or NTP'),
                 ({'phase': 'prepared', 'votes': 2, 'participants': 3}, '(2/3)'),
                 ({'phase': 'committed', 'activate_at': time.time() + 20}, 'scheduled'),
                 ({'conflicting': True}, 'reconciling'),
                 ({'hold_until': time.time() + 500}, 'stragglers'),
                 ({'error': 'Radio read failed'}, 'retrying')]
        original = dict(self.snapshot)
        for extra, expected in cases:
            self.snapshot = dict(original, **extra)
            with self.subTest(extra=extra):
                self.assertIn(expected, self.values(self.describe())['Recovery'])

    def test_clock_source_event_and_registry_ages(self):
        (self.root / 'initial_time_synced').touch()
        (self.root / 'mesh-time-client.json').write_text(json.dumps(
            {'last_source': 'GPS', 'last_sync': time.monotonic() - 120}))
        (self.root / 'manet-last-channel-change.json').write_text(json.dumps(
            {'reason': 'Returning to the connected mesh channel plan',
             'channels': {'2.4': 2462}, 'monotonic': time.monotonic() - 20}))
        # Freshness comes from local observation. A sender clock far behind
        # (LAST_SEEN_TIMESTAMP) must not make a current peer look stale.
        boot = time.clock_gettime(time.CLOCK_BOOTTIME)
        registry = {'one': {'OBSERVED_AT_UPTIME': str(int(boot - 20)),
                            'LAST_SEEN_TIMESTAMP': '100', 'LAST_REGISTRY_UPDATE': '100'},
                    'two': {'OBSERVED_AT_UPTIME': str(int(boot - 400)),
                            'LAST_REGISTRY_UPDATE': str(time.time())},
                    'three': {'LAST_REGISTRY_UPDATE': str(time.time())},
                    'self': {'HOSTNAME': 'self'}}
        rows = self.values(recovery_status({'acs': 'y'}, registry, 'self'))
        self.assertIn('GPS, 2 minutes ago', rows['Clock'])
        self.assertIn('2462', rows['Last Wi-Fi change'])
        self.assertEqual(rows['Peer metadata'], '1 fresh, 1 stale, 1 age unknown')


if __name__ == '__main__':
    unittest.main()
