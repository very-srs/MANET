#!/usr/bin/env python3
"""Clock epochs from simulated wall and boot clocks."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import manet_clock


class ClockEpochTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        (self.root / 'boot_id').write_text('boot-a\n')
        env = patch.dict(os.environ, MANET_TIME_RUN_DIR=str(self.root),
                         MANET_BOOT_ID_FILE=str(self.root / 'boot_id'))
        env.start(); self.addCleanup(env.stop)
        self.wall, self.boot = 1_800_000_000.0, 100.0
        for name, value in [('time', lambda: self.wall), ('time_ns', lambda: int(self.wall * 10**9)),
                            ('clock_gettime', lambda clock: self.boot)]:
            clock = patch.object(manet_clock.time, name, side_effect=value)
            clock.start(); self.addCleanup(clock.stop)

    def advance(self, seconds, wall_extra=0.0):
        self.boot += seconds
        self.wall += seconds + wall_extra

    def test_jump_beyond_the_threshold_starts_a_new_epoch(self):
        self.assertEqual(manet_clock.epoch_id(), ['boot-a', 0])
        self.advance(5, wall_extra=-(manet_clock.JUMP_SECONDS + 1))
        state = manet_clock.observe()
        self.assertEqual(state['epoch'], 1)
        self.assertEqual(state['jump_ns'], int(self.wall * 10**9))
        self.advance(5)
        self.assertEqual(manet_clock.observe()['epoch'], 1)

    def test_slewing_and_small_steps_keep_the_epoch(self):
        manet_clock.observe()
        for _ in range(100):
            # chrony's fastest slew, 83 ms/s, observed every 15 s.
            self.advance(15, wall_extra=15 * 0.083)
            self.assertEqual(manet_clock.observe()['epoch'], 0)
        self.advance(1, wall_extra=manet_clock.JUMP_SECONDS - 1)
        self.assertEqual(manet_clock.observe()['epoch'], 0)

    def test_new_boot_restarts_the_count(self):
        manet_clock.observe()
        self.advance(1, wall_extra=3600)
        self.assertEqual(manet_clock.observe()['epoch'], 1)
        (self.root / 'boot_id').write_text('boot-b\n')
        self.boot = 10.0
        self.assertEqual(manet_clock.epoch_id(), ['boot-b', 0])

    def test_damaged_state_starts_over_without_a_jump(self):
        (self.root / 'manet-clock-epoch.json').write_text('{')
        self.assertEqual(manet_clock.observe()['epoch'], 0)


if __name__ == '__main__':
    unittest.main()
