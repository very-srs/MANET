"""Passive DFS observations must reach the real, capability-gated scorer."""
import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import manet_bgobs as observer
import manet_dfs as dfs
from test_acs import AcsHarness, OWN, PEER, runtime
from test_acs_dfs import dfs_view


PHY = '''Band 2:
    Frequencies:
        * 5200.0 MHz [40] (30.0 dBm)
        * 5260.0 MHz [52] (24.0 dBm) (no IR, radar detection)
          DFS state: usable (for 120 sec)
        * 5280 MHz [56] (24.0 dBm) (radar detection)
          DFS state: unavailable (for 10 sec)
        * 5300.0 MHz [60] (disabled) (radar detection)
        * 5320.0 MHz [64] (24.0 dBm) (radar detection)
          DFS state: available (for 65 sec)
        * 5500.0 MHz [100] (24.0 dBm)
        * 5600.0 MHz [120] (24.0 dBm) (radar detection)
          DFS state: usable (for 120 sec)
        * 5745.0 MHz [149] (30.0 dBm)
'''


class DfsCandidateTests(AcsHarness):
    def setUp(self):
        super().setUp()
        self.configure(None, 5200)
        self.view = {m: s for m, s in dfs_view().items() if m in (OWN, PEER)}
        for s in self.view.values():
            s.pop('dfs_hold', None)
            s['dfs_allowed'] = []
            s['current'] = {'5': 5200}
            s['allowed'] = {'5': [5200, 5220]}
            s['dfs_candidates'] = [5260, 5320, 5600]
        self.runner = object.__new__(runtime.Runtime)
        self.runner.state = {}
        self.runner.run_dir = self.root

    def score(self, **kwargs):
        with patch.dict(runtime.os.environ, self.env):
            return self.runner.score(self.view, candidates=True, **kwargs)

    def test_decimal_channels_and_adjacent_nop_disabled_blocks(self):
        for text in (PHY, PHY.replace('.0 MHz', ' MHz')):
            with self.subTest(text=text):
                self.assertEqual(dfs.dfs_frequencies(text), [5260, 5320, 5600])
        self.assertEqual(dfs.dfs_frequencies('* 5260.5 MHz (radar detection)'), [])

    def test_observer_cli_adds_dfs_and_real_scorer_uses_its_measurement(self):
        path = self.root / 'phy7/mt76/background_scan'
        path.parent.mkdir(parents=True)
        (path.parent.parent / 'background_hold').write_text(json.dumps(
            dict(schema=2, dfs_mesh_v1=True, owner=False)))
        output = io.StringIO()
        original_read = Path.read_text
        def read(p, *a, **kw):
            if str(p) == '/sys/module/mt7915e/parameters/dfs_mesh_v1':
                return 'Y\n'
            return original_read(p, *a, **kw)
        with patch.dict(observer.os.environ, self.env), \
                patch.object(observer, 'observer_path', return_value=path), \
                patch.object(observer.subprocess, 'run') as iw, \
                patch.object(dfs, 'cm4_enabled', return_value=True), \
                patch.object(Path, 'read_text', read), \
                patch.object(observer, 'measure', side_effect=lambda p, f: 12 if f == 5260 else 70), \
                patch('sys.argv', ['manet_bgobs.py', 'wlan1', '5200']), \
                contextlib.redirect_stdout(output):
            iw.return_value.stdout = PHY
            self.assertEqual(observer.observer_main(), 0)
        samples = json.loads(output.getvalue())['results']
        self.assertEqual([s['channel'] for s in samples], [5200, 5260, 5320, 5600])
        self.reports([[dict(channel=5200, foreign_pct=80, rf_trouble=True),
                       *[s for s in samples if s['channel'] != 5200]]], [OWN])
        self.assertEqual(self.score()[0], {'5': 5260})

    def test_every_member_must_allow_dfs_but_only_one_must_measure(self):
        self.reports([[dict(channel=5200, foreign_pct=80, rf_trouble=True),
                       dict(channel=5260, busy_pct=12, bss_count=0)]], [OWN])
        self.assertEqual(self.score()[0], {'5': 5260})
        self.view[PEER]['dfs_capable'] = False
        self.assertEqual(self.score()[0], {'5': 5200})
        self.view[PEER]['dfs_capable'] = True
        self.view[PEER]['dfs_candidates'].remove(5260)
        self.assertEqual(self.score()[0], {'5': 5200})

    def test_unmeasured_dfs_cannot_win_from_bss_or_noise(self):
        for fields in ({}, *[dict(busy_pct=v) for v in (None, True, '0', -1, 101)]):
            with self.subTest(fields=fields):
                self.reports([[dict(channel=5200, foreign_pct=90, rf_trouble=True),
                               dict(channel=5260, bss_count=0, noise_floor=-95, **fields)]], [OWN])
                self.assertEqual(self.score()[0], {'5': 5200})

    def test_zero_occupancy_is_measured_and_missing_reporters_do_not_veto(self):
        self.reports([[dict(channel=5200, foreign_pct=80, rf_trouble=True),
                       dict(channel=5260, busy_pct=0, bss_count=0)],
                      [dict(channel=5260, noise_floor=-20, bss_count=100)]], [OWN, PEER])
        self.assertEqual(self.score()[0], {'5': 5260})

    def test_non_dfs_fallback_and_disconnected_measurements(self):
        reports = [dict(channel=5200, foreign_pct=80, rf_trouble=True),
                   dict(channel=5220, busy_pct=40, bss_count=0),
                   dict(channel=5260, busy_pct=12, bss_count=0)]
        self.reports([reports], [OWN])
        self.assertEqual(self.score()[0], {'5': 5260})
        self.assertEqual(self.score(fallback=True)[0], {'5': 5220})
        # A cached measurement from outside the connected component cannot win.
        self.reports([reports[:2], reports[2:]], [OWN, '02:00:00:00:00:ff'])
        self.assertEqual(self.score()[0], {'5': 5220})

    def test_deferred_reservation_does_not_attempt_observer(self):
        path = self.root / 'phy7/mt76/background_scan'
        path.parent.mkdir(parents=True)
        state = dict(schema=1, reserved=False, failed=False, chainmask=28)
        transition = path.with_name('rx_transition_probe')
        transition.write_text(json.dumps(state))
        with patch.dict(observer.os.environ, self.env), \
                patch.object(observer, 'measure', return_value=12) as measure:
            self.assertEqual(observer.collect_candidates(path, [5260], 'wlan1'), [])
            measure.assert_not_called()
            transition.write_text(json.dumps(dict(state, reserved=True, chainmask=12)))
            self.assertEqual(observer.collect_candidates(path, [5260], 'wlan1')[0]['busy_pct'], 12)
            measure.reset_mock()
            transition.write_text(json.dumps(dict(state, failed=True)))
            with self.assertRaisesRegex(ValueError, 'invalid RX reservation'):
                observer.collect_candidates(path, [5260], 'wlan1')
            measure.assert_not_called()


if __name__ == '__main__':
    unittest.main()
