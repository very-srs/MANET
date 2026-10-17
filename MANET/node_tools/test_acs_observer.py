"""Observer correction, acquisition ownership and the real ACS trouble gate."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import manet_bgobs as obs
import test_acs_home as home_tests
from test_acs import AcsHarness, PEER


def window(*, lower=25000, top=6250, own=50000, **extra):
    """Construct a one-second window with a known foreign/own mixture."""
    bins = [125000 - lower - top, 0, lower] + [0] * 7 + [top, 0]
    result = dict(schema=1, sequence=4, active=False, reserved=False,
                  rx_reserved=True, valid=True, error=0, restore_error=0,
                  freq_mhz=5200, center_mhz=5200, width_mhz=20,
                  threshold_bucket=2, counter_policy='modulo23_no_clear',
                  elapsed_us=1000000, sample_count=125000, delta=bins,
                  tx_first_us=1000000, tx_last_us=1000000 + own,
                  tx_read_max_us=1000)
    result.update(extra)
    return result


class CorrectionTests(unittest.TestCase):
    """Distinguish foreign energy from local TX without inventing clean data."""

    def test_foreign_energy_and_own_blanking(self):
        self.assertEqual(obs.occupancy(window(lower=0, top=0, own=0),
                                       5200), 0)
        self.assertEqual(obs.occupancy(window(lower=62500, top=0, own=0),
                                       5200), 50)
        self.assertEqual(obs.occupancy(window(lower=59375, top=6250),
                                       5200), 50)
        self.assertEqual(obs.occupancy(window(lower=0, top=6250),
                                       5200), 0)
        # Strong foreign energy in bin 10 exceeds the independent own TX.
        self.assertEqual(obs.occupancy(window(lower=12500, top=50000,
                                              own=20000), 5200), 49)

    def test_local_tx_limit_does_not_erase_strong_foreign_energy(self):
        """Gate measured local TX without treating high bin 10 as blanking."""
        # Large bin 10 alone is not a reason to subtract or reject foreign RF.
        self.assertEqual(obs.occupancy(window(lower=0, top=112500,
                                              own=0), 5200), 90)
        self.assertEqual(obs.occupancy(window(lower=0, top=12500,
                                              own=99999), 5200), 0)
        for own in (100000, 500000, 899999, 900000):
            with self.subTest(own=own), self.assertRaisesRegex(
                    ValueError, 'local TX exceeds'):
                obs.occupancy(window(lower=0, top=112500, own=own), 5200)

    def test_invalid_and_mostly_blanked_windows_are_absent(self):
        cases = [dict(valid=False), dict(active=True), dict(reserved=True),
                 dict(error=-5), dict(restore_error=-5), dict(schema=2),
                 dict(freq_mhz=5220), dict(width_mhz=80),
                 dict(threshold_bucket=3), dict(sample_count=125001),
                 dict(elapsed_us=10000), dict(elapsed_us=6000001),
                 dict(tx_last_us=999999), dict(tx_read_max_us=10001),
                 dict(own=950000, top=118750, lower=100),
                 dict(own=500000, top=0), dict(delta=[])]
        for fields in cases:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                obs.occupancy(window(**fields), 5200)

    def test_matching_sequence_only_and_cleanup(self):
        before = dict(schema=1, sequence=3, active=False, reserved=False)
        running = dict(window(), active=True, reserved=True)
        path = Path('/unused')
        with patch.object(obs, 'read_status', side_effect=[
                before, running, window(), window()]), \
             patch.object(obs, 'write_command') as write, \
             patch.object(obs.time, 'sleep'):
            self.assertEqual(obs.measure(path, 5200), 21)
            self.assertEqual(write.call_count, 1)
        broken = dict(window(), valid=False, reserved=True)
        with patch.object(obs, 'read_status', side_effect=[
                before, broken, broken]), \
             patch.object(obs, 'write_command') as write:
            with self.assertRaises(ValueError):
                obs.measure(path, 5200)
            self.assertEqual(write.call_args.args, (path, 'stop'))
        other = dict(window(), sequence=5, active=True, reserved=True)
        with patch.object(obs, 'read_status', side_effect=[
                before, other, other]), \
             patch.object(obs, 'write_command') as write:
            with self.assertRaises(ValueError):
                obs.measure(path, 5200)
            self.assertEqual(write.call_count, 1)

    def test_busy_owner_is_never_stopped(self):
        with patch.object(obs, 'read_status', return_value=dict(
                schema=1, sequence=1, active=True, reserved=True)), \
             patch.object(obs, 'write_command') as write:
            with self.assertRaises(ValueError):
                obs.measure(Path('/unused'), 5200)
            write.assert_not_called()

    def test_phy_resolution_and_passive_dfs_candidates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            phy = root / 'net/wlan1/phy80211'
            phy.mkdir(parents=True)
            (phy / 'name').write_text('phy7\n')
            path = root / 'debug/ieee80211/phy7/mt76/background_scan'
            path.parent.mkdir(parents=True)
            with patch.dict(obs.os.environ, MANET_SYS_NET=str(root / 'net'),
                            MANET_DEBUGFS=str(root / 'debug'),
                            MANET_ACS_RUN_DIR=str(root)):
                self.assertIsNone(obs.observer_path('wlan1'))
                path.write_text('{}')
                self.assertEqual(obs.observer_path('wlan1'), path)
                with patch.object(obs, 'measure', return_value=20) as measure:
                    result = obs.collect_candidates(path, [5180, 5200, 5500], 'wlan1')
                self.assertEqual([c.args for c in measure.call_args_list],
                                 [(path, 5200), (path, 5500)])
                self.assertEqual(result[0]['busy_pct'], 20)
                self.assertNotIn('noise_floor', result[0])
                self.assertTrue(result[0]['_observer'])


class CapturedCorrectionTests(unittest.TestCase):
    """Replay reservation-v2 histograms, including quiet heavy local TX."""

    @classmethod
    def setUpClass(cls):
        """Load raw snapshots independently of the ignored bench directory."""
        cls.captures = json.loads((Path(__file__).parent / 'testdata/bgobs'
                                  / 'reservation-v2.json').read_text())

    def test_heavy_tx_is_missing_even_when_old_correction_looked_clean(self):
        """Reject every high-TX window, including apparently plausible ones."""
        for capture in self.captures:
            if '/tx-' not in capture['source']:
                continue
            record = capture['record']
            with self.subTest(source=capture['source']):
                with self.assertRaises(ValueError):
                    obs.occupancy(record, record['freq_mhz'])

    def test_idle_and_receive_controls_keep_their_measured_energy(self):
        """Preserve low-TX replay values without trusting confounded labels."""
        ranges = {'quiet/idle': (0, 1), 'quiet/rx': (1, 2),
                  '100/idle': (19, 20), '100/rx': (20, 20),
                  # Pacing-confounded campaign retained for replay only.
                  '250/idle': (68, 68), '250/rx': (49, 49),
                  '488/idle': (94, 95), '488/rx': (94, 95),
                  '250clean/idle': (49, 49), 'quiet-test': (1, 1)}
        for capture in self.captures:
            source = capture['source']
            if '/tx-' in source:
                continue
            group = (source[:-5] if source == 'quiet-test.json'
                     else source.rsplit('-', 1)[0])
            low, high = ranges[group]
            record = capture['record']
            with self.subTest(source=source):
                value = obs.occupancy(record, record['freq_mhz'])
                self.assertGreaterEqual(value, low)
                self.assertLessEqual(value, high)

    def test_rejected_capture_does_not_publish_a_candidate(self):
        """Exercise acquisition through publication with a biased quiet record."""
        record = next(c['record'] for c in self.captures
                      if c['source'] == 'quiet/tx-06.json')
        before = dict(record, sequence=record['sequence'] - 1)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'phy1/mt76/background_scan'
            with patch.dict(obs.os.environ, MANET_ACS_RUN_DIR=temp), \
                 patch.object(obs, 'read_status', side_effect=[
                     before, record, record]), \
                 patch.object(obs, 'write_command') as write:
                result = obs.collect_candidates(path, [5200], 'wlan1')
                self.assertEqual(result, [])
                write.assert_called_once()


class ObserverGateTests(AcsHarness):
    """Run node-manager's existing home monitor with an observer command stub."""
    collect = home_tests.HomeMonitorTests.collect
    row = staticmethod(home_tests.HomeMonitorTests.row)

    def setUp(self):
        """Record helper calls and use the production shell collector."""
        super().setUp()
        helper = self.root / 'observer.py'
        helper.write_text('''import json, os, sys
from pathlib import Path
root = Path(os.environ['TEST_ROOT'])
if '--policy' in sys.argv:
    with (root / 'policy-calls').open('a') as stream:
        stream.write(json.dumps(sys.argv[1:]) + '\\n')
    sys.exit(0)
with (root / 'observer-calls').open('a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
results = [dict(channel=int(os.environ.get('TEST_OBSERVER_FREQ', '5200')), busy_pct=20,
    bss_count=0, _observer=True, _survey_iface=sys.argv[1],
    _survey_boot=10000)]
print(json.dumps({'results': [] if os.environ.get('TEST_OBSERVER_EMPTY') else results}))
sys.exit(int(os.environ.get('TEST_OBSERVER_RC', '0')))
''')
        self.env['MANET_BGOBS_HELPER'] = str(helper)

    def test_closed_gate_never_calls_observer(self):
        self.collect([self.row(0), self.row(1)])
        self.assertFalse((self.root / 'observer-calls').exists())
        calls = (self.root / 'policy-calls').read_text().splitlines()
        self.assertEqual([json.loads(c)[-1] for c in calls], ['clear', 'clear'])

    def test_peer_trouble_uses_observer_and_no_main_radio_scan(self):
        reports, commands = self.collect([self.row(0)], peer=True,
                                        peer_channel=5220)
        candidate = next(r for r in reports[0]['results']
                         if r['channel'] == 5200)
        self.assertEqual(candidate['busy_pct'], 20)
        self.assertTrue(candidate['_observer'])
        self.assertFalse(any(c[1] == 'wlan1' and c[2] in
                             ('scan', 'offchannel') for c in commands))
        self.assertEqual(json.loads((self.root / 'observer-calls').read_text()),
                         ['wlan1', '5200'])
        self.assertEqual(json.loads((self.root / 'policy-calls').read_text()),
                         ['wlan1', '--policy', 'clear'])

    def test_absent_falls_back_failed_present_omits(self):
        for code in (3, 1):
            with self.subTest(code=code):
                self.env['TEST_OBSERVER_RC'] = str(code)
                reports, commands = self.collect([self.row(0)], peer=True,
                                                peer_channel=5220)
                scanned = any(c[1] == 'wlan1' and c[2] == 'scan'
                              for c in commands)
                self.assertEqual(scanned, code == 3)
                if code == 1:
                    self.assertFalse(any(r['channel'] == 5200
                                         for r in reports[0]['results']))

    def test_passive_dfs_result_is_published_without_a_main_radio_scan(self):
        self.env['TEST_OBSERVER_FREQ'] = '5260'
        reports, commands = self.collect([self.row(0)], peer=True,
                                        peer_channel=5220)
        candidate = next(r for r in reports[0]['results'] if r['channel'] == 5260)
        self.assertEqual(candidate['busy_pct'], 20)
        self.assertTrue(candidate['_observer'])
        self.assertFalse(any(c[1] == 'wlan1' and c[2] in
                             ('scan', 'offchannel') for c in commands))

    def test_deferred_observer_omits_candidates_without_fallback_scan(self):
        self.env['TEST_OBSERVER_EMPTY'] = '1'
        reports, commands = self.collect([self.row(0)], peer=True,
                                        peer_channel=5220)
        self.assertFalse(any(r.get('_observer') for r in reports[0]['results']))
        self.assertFalse(any(c[1] == 'wlan1' and c[2] in
                             ('scan', 'offchannel') for c in commands))

    def test_energy_only_roundtrip_retains_absent_noise(self):
        """Energy-only candidates cannot acquire a false zero dBm penalty."""
        import base64
        import decoder
        import subprocess
        import sys
        from test_acs import TOOLS
        results = [dict(channel=5200, busy_pct=20, bss_count=0),
                   dict(channel=5260, busy_pct=12, bss_count=0)]
        encoded = subprocess.check_output([
            sys.executable, str(TOOLS / 'encoder.py'), 'telemetry',
            '--timestamp', '1000', '--mesh-config', '/dev/null',
            '--channel-report-json', json.dumps({'results': results})],
            text=True)
        decoded = {}
        decoder.decode_telemetry(base64.b64decode(encoded),
            emit=lambda key, value: decoded.update({key: value}),
            emit_raw=lambda key, value: None)
        self.assertEqual(json.loads(decoded['CHANNEL_REPORT_JSON']),
                         {'results': results})
