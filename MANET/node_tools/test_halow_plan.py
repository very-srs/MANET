"""Region-aware HaLow channel plan.

The plans are transcribed from the Morse driver's dot11ah tables, which are
not in this repo, so these tests guard the properties the rest of the code
relies on rather than re-deriving the numbers.
"""

import unittest

import manet_radio as mr


class ChannelPlanTests(unittest.TestCase):
    def test_eu_stops_at_2mhz_and_us_reaches_8mhz(self):
        # EU has 5 MHz of allocation in total, so there is no room for a 4 or
        # 8 MHz channel; the US 902-928 plan has both.
        self.assertEqual(sorted(mr.HALOW_CHANNEL_PLANS['EU']), ['1MHz', '2MHz'])
        self.assertEqual(sorted(mr.HALOW_CHANNEL_PLANS['US']),
                         ['1MHz', '2MHz', '4MHz', '8MHz'])

    def test_us_8mhz_channels_match_the_shipped_supplicant_template(self):
        # radio-setup.sh's US template joins on channel 12, and the bench node
        # reports 908 MHz / 8 MHz there.
        self.assertEqual(mr.HALOW_CHANNEL_PLANS['US']['8MHz'][12], 908000)

    def test_eu_1mhz_channel_1_matches_the_non_us_template(self):
        self.assertEqual(mr.HALOW_CHANNEL_PLANS['EU']['1MHz'][1], 863500)

    def test_centre_frequencies_are_unique_within_a_region(self):
        # halow_channel_for_frequency() resolves a frequency to exactly one
        # (channel, bandwidth); a collision would make it ambiguous.
        for region, plan in mr.HALOW_CHANNEL_PLANS.items():
            seen = {}
            for bw, channels in plan.items():
                for chan, khz in channels.items():
                    self.assertNotIn(khz, seen,
                                     f'{region}: {khz} kHz is both {seen.get(khz)} '
                                     f'and {bw} ch{chan}')
                    seen[khz] = f'{bw} ch{chan}'

    def test_every_channel_round_trips_through_the_frequency_lookup(self):
        for region, plan in mr.HALOW_CHANNEL_PLANS.items():
            for bw, channels in plan.items():
                for chan, khz in channels.items():
                    self.assertEqual(mr.halow_channel_for_frequency(khz, region),
                                     (str(chan), bw))

    def test_unknown_frequency_resolves_to_nothing(self):
        self.assertEqual(mr.halow_channel_for_frequency(5180000, 'US'), ('', ''))

    def test_every_region_and_bandwidth_has_an_operating_class(self):
        # A missing class would mean the apply path could not write a config.
        for region, plan in mr.HALOW_CHANNEL_PLANS.items():
            for bw in plan:
                self.assertIn((region, bw), mr.HALOW_OP_CLASS)

    def test_operating_classes_are_distinct(self):
        classes = list(mr.HALOW_OP_CLASS.values())
        self.assertEqual(len(classes), len(set(classes)))


class ChannelOptionTests(unittest.TestCase):
    def test_bandwidths_are_ordered_by_width(self):
        self.assertEqual(mr.halow_channel_options('US')['bandwidths'],
                         ['1MHz', '2MHz', '4MHz', '8MHz'])

    def test_channels_carry_a_display_frequency_in_mhz(self):
        opts = mr.halow_channel_options('US')['channels']['8MHz']
        self.assertEqual(opts[0], {'channel': 12, 'mhz': 908.0})
        self.assertEqual([c['channel'] for c in opts], [12, 28, 44])

    def test_eu_offers_no_4mhz_option(self):
        self.assertNotIn('4MHz', mr.halow_channel_options('EU')['channels'])

    def test_unknown_region_falls_back_to_the_eu_plan(self):
        # radio-setup.sh generates the EU supplicant template for every region
        # that is not US, so the menu must not offer anything else.
        self.assertEqual(mr.halow_channel_options('JP')['bandwidths'],
                         mr.halow_channel_options('EU')['bandwidths'])


class BandwidthFormatTests(unittest.TestCase):
    def test_eight_mhz_is_recognised_in_every_form(self):
        for value in (8, '8', '8MHz', '8mhz', 8000, 8000000):
            self.assertEqual(mr._format_halow_bw(value), '8MHz')


class ApplyValidationTests(unittest.TestCase):
    def setUp(self):
        self._region = mr.halow_region
        mr.halow_region = lambda: 'EU'

    def tearDown(self):
        mr.halow_region = self._region

    def test_bandwidth_absent_from_the_region_is_refused(self):
        # Refused before anything is written, so a bad request cannot leave a
        # half-applied config behind.
        with self.assertRaises(ValueError) as cm:
            mr.apply_halow_channel(1, '8MHz')
        self.assertIn('not available on the EU', str(cm.exception))

    def test_channel_from_another_bandwidth_is_refused(self):
        with self.assertRaises(ValueError) as cm:
            mr.apply_halow_channel(2, '1MHz')   # ch 2 is a 2 MHz channel
        self.assertIn('not a 1MHz channel', str(cm.exception))

    def test_missing_channel_is_refused(self):
        with self.assertRaises(ValueError):
            mr.apply_halow_channel('', '1MHz')


class ChannelToBandwidthTests(unittest.TestCase):
    def test_channel_numbers_are_unique_within_a_region(self):
        # halow_bandwidth_for_channel() reads the width off the number alone.
        for region, plan in mr.HALOW_CHANNEL_PLANS.items():
            seen = {}
            for bw, channels in plan.items():
                for chan in channels:
                    self.assertNotIn(chan, seen,
                                     f'{region}: ch{chan} is both {seen.get(chan)} and {bw}')
                    seen[chan] = bw

    def test_width_comes_from_the_channel_number(self):
        self.assertEqual(mr.halow_bandwidth_for_channel(12, 'US'), '8MHz')
        self.assertEqual(mr.halow_bandwidth_for_channel(8, 'US'), '4MHz')
        self.assertEqual(mr.halow_bandwidth_for_channel(5, 'EU'), '1MHz')

    def test_unknown_channel_reports_nothing(self):
        self.assertEqual(mr.halow_bandwidth_for_channel(99, 'US'), '')
        self.assertEqual(mr.halow_bandwidth_for_channel('not-a-number'), '')


if __name__ == '__main__':
    unittest.main()
