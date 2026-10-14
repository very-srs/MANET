"""Exercise coordinator hold-downs against the real stateless RF scorer."""
from contextlib import redirect_stdout
from io import StringIO
import os
from unittest.mock import patch

import manet_acs_agreement as protocol
from test_acs import AcsHarness, OWN, runtime
from test_acs_agreement import status


def sample(frequency, busy=10, noise=-95):
    """Build one measured candidate with a predictable raw score."""
    return {'channel': frequency, 'busy_pct': busy, 'noise_floor': noise}


class HoldDownTests(AcsHarness):
    """Keep preferences local while passing urgent moves through immediately."""

    def setUp(self):
        """Use real scoring with temporary radios and a controlled round."""
        super().setUp()
        self.configure(2437, 5200)
        self.now = 945
        for seam in (patch.dict(os.environ, self.env),
                     patch.object(runtime.time, 'time',
                                  side_effect=lambda: self.now)):
            seam.start()
            self.addCleanup(seam.stop)
        self.runner = runtime.Runtime()
        self.runner.state['clock_boot'] = self.runner.boot
        self.view = {OWN: status(current={'2.4': 2437})}
        self.measurements = [sample(2437, 45), sample(2462, 20)]

    def score(self, round_offset=0):
        """Score one round and return its channel plan and journal text."""
        self.now = 945 + round_offset * protocol.ROUND_SECONDS
        self.reports([self.measurements], [OWN])
        journal = StringIO()
        with redirect_stdout(journal):
            result = self.runner.score(self.view)
        return result, journal.getvalue()

    def test_preference_waits_three_rounds_and_counts_each_round_once(self):
        """Repeated passes cannot advance the streak or repeat held logs."""
        for offset, frequency in enumerate((2437, 2437, 2462)):
            result, journal = self.score(offset)
            self.assertEqual(result, ({'2.4': frequency}, False))
            expected = (f'ACS holding 2.4 GHz 2437: 2462 preferred '
                        f'({offset + 1}/3)\n') if offset < 2 else ''
            self.assertEqual(journal, expected)
            self.assertEqual(self.score(offset), (result, ''))
            self.assertEqual(self.runner.state['challenge']['2.4']['count'],
                             offset + 1)

    def test_nonconsecutive_round_restarts_streak(self):
        """A missing or backwards round cannot preserve consecutive wins."""
        for offset in (0, 2, 1):
            result, journal = self.score(offset)
            self.assertEqual(result, ({'2.4': 2437}, False))
            self.assertIn('(1/3)', journal)

    def test_incumbent_win_clears_streak(self):
        """A quiet incumbent breaks an otherwise consecutive challenge."""
        self.score()
        self.measurements = [sample(2437, 10), sample(2462, 20)]
        self.assertEqual(self.score(1), (({'2.4': 2437}, False), ''))
        self.assertNotIn('2.4', self.runner.state['challenge'])
        self.measurements = [sample(2437, 45), sample(2462, 20)]
        self.assertIn('(1/3)', self.score(2)[1])

    def test_incumbent_change_restarts_streak(self):
        """Wins against one incumbent cannot justify leaving another."""
        self.score()
        self.score(1)
        self.view[OWN]['current']['2.4'] = 2462
        self.measurements = [sample(2437, 20), sample(2462, 45)]
        result, journal = self.score(2)
        self.assertEqual(result, ({'2.4': 2462}, False))
        self.assertIn('2462: 2437 preferred (1/3)', journal)

    def test_alternating_5_ghz_challengers_move_on_third_round(self):
        """Any preferred channel counts against the same incumbent."""
        self.view = {OWN: status(current={'5': 5200})}
        for offset, winner in enumerate((5220, 5240, 5220)):
            self.measurements = [sample(5200, 45), sample(winner, 10)]
            result, _ = self.score(offset)
            self.assertEqual(result,
                             ({'5': 5200 if offset < 2 else winner}, False))

    def test_bands_advance_and_reset_independently(self):
        """Resetting 5 GHz does not erase a 2.4 GHz preference streak."""
        self.view = {OWN: status(current={'2.4': 2437, '5': 5200})}
        for offset, busy5 in enumerate((45, 10, 45)):
            self.measurements = [sample(2437, 45), sample(2462, 10),
                                 sample(5200, busy5), sample(5220, 20)]
            result, _ = self.score(offset)
            self.assertEqual(result, ({'2.4': 2437 if offset < 2 else 2462,
                                       '5': 5200}, False))
        self.assertEqual(self.runner.state['challenge']['5']['count'], 1)

    def test_streak_and_same_round_log_survive_runtime_reload(self):
        """Persisted same-boot state prevents recounting after a restart."""
        self.score()
        self.runner.save()
        self.runner = runtime.Runtime()
        self.assertEqual(self.score(), (({'2.4': 2437}, False), ''))
        self.assertIn('(2/3)', self.score(1)[1])
        self.runner.save()
        self.runner = runtime.Runtime()
        self.assertEqual(self.score(2), (({'2.4': 2462}, False), ''))

    def test_urgency_output_and_immediate_moves_for_each_rf_cause(self):
        """Both bands export urgency from incumbent RF, including raw score."""
        for band, suffix, home, away in (('2.4', '2_4', 2437, 2462),
                                         ('5', '5_0', 5200, 5220)):
            for cause in ('preference', 'threshold', 'dq', 'nodata',
                          'above_threshold', 'excluded', 'empty'):
                with self.subTest(band=band, cause=cause):
                    current = {} if cause == 'empty' else {band: home}
                    self.view = {OWN: status(current=current)}
                    allowed = [away] if cause == 'excluded' else [home, away]
                    self.view[OWN]['allowed'] = {band: allowed}
                    home_sample = sample(home, 45)
                    if cause == 'dq':
                        # Disqualification can occur below the limp threshold.
                        home_sample = sample(home, 10, -60)
                    elif cause == 'threshold':
                        home_sample = sample(home, 60)
                    elif cause == 'above_threshold':
                        # Raw 61 is urgent even though the biased score is 51.
                        home_sample = sample(home, 55, -83)
                    self.measurements = [sample(away)]
                    if cause != 'nodata':
                        self.measurements.append(home_sample)
                    self.reports([self.measurements], [OWN])
                    self.env['ACS_CHANNELS_' + suffix] = ' '.join(
                        map(str, allowed))
                    self.env['ACS_CURRENT_' + suffix] = (
                        '' if cause == 'empty' else str(home))
                    result = self.election()
                    self.assertEqual(result.returncode, 0, result.stderr)
                    output = (self.root / 'election').read_text().splitlines()
                    values = dict(line.split('=', 1) for line in output)
                    urgent = cause not in ('preference', 'threshold')
                    self.assertEqual(values['URGENT_' + suffix],
                                     str(urgent).lower())
                    self.assertEqual(values['WINNER_' + suffix], str(away))
                    self.runner.state['challenge'] = {
                        band: {'incumbent': home, 'round': 4, 'count': 1}}
                    scored, journal = self.score()
                    self.assertEqual(
                        scored, ({band: away if urgent else home}, False))
                    if urgent:
                        self.assertEqual(journal, '')
                        self.assertNotIn(band, self.runner.state['challenge'])

    def test_urgent_band_preserves_limp_and_other_band_hold(self):
        """Limp mode passes through while a healthy band holds its channel."""
        self.view = {OWN: status(current={'2.4': 2437, '5': 5200})}
        self.measurements += [sample(5200, 99), sample(5220, 90)]
        result, journal = self.score()
        self.assertEqual(result, ({'2.4': 2437, '5': 5200}, True))
        self.assertIn('ACS holding 2.4 GHz', journal)
        self.assertNotIn('5 GHz', journal)
        self.assertNotIn('5', self.runner.state['challenge'])
