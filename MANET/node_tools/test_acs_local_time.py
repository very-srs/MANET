"""Local ACS deadlines survive wall-clock steps and same-boot restarts."""
import json
import os
from unittest.mock import patch

import manet_acs_agreement as protocol
from test_acs import AcsHarness, OWN, PEER, runtime
from test_acs_agreement import status


STEPS = (3600, -3600, 137)


class LocalTimeTests(AcsHarness):
    def setUp(self):
        super().setUp()
        self.configure(2437, None)
        self.wall = 1800000045
        self.elapsed = 10000.5
        self.monotonic = 700.0
        self.frequencies = (2412, 2437, 2462)
        for seam in (patch.dict(os.environ, self.env),
                     patch.object(runtime.time, 'time', side_effect=lambda: self.wall),
                     patch.object(runtime.time, 'monotonic', side_effect=lambda: self.monotonic),
                     patch.object(runtime.time, 'clock_gettime', side_effect=self.boot_time),
                     patch.object(runtime, 'command', side_effect=self.command_output),
                     patch.object(runtime.Runtime, 'score', return_value=None),
                     patch.object(runtime.Runtime, 'discovery_step', return_value=False)):
            seam.start(); self.addCleanup(seam.stop)
        publisher = patch.object(runtime.AdminTransport, 'seal', return_value={})
        self.publisher = publisher.start(); self.addCleanup(publisher.stop)
        self.runner = runtime.Runtime()
        self.runner.state['clock_boot'] = self.runner.boot

    def boot_time(self, clock):
        self.assertEqual(clock, runtime.time.CLOCK_BOOTTIME)
        return self.elapsed

    def command_output(self, args, **kwargs):
        if args[-1] == 'originators_json':
            return '[]'
        if args[:2] == ['iw', 'dev']:
            return 'wiphy 0\nchannel 6 (2437 MHz)'
        if args[:2] == ['iw', 'phy']:
            return '\n'.join(f'* {f}.0 MHz [1] (20 dBm)' for f in self.frequencies)
        return ''

    def ready(self):
        self.runner.request_path.write_text(json.dumps({'round': self.wall // 180}))
        return self.runner.status(self.wall)['ready']

    def tick(self, result=None):
        # Keep the shared protocol fixed: this suite tests only local clocks.
        result = result or ({'phase': 'idle', 'round': self.wall // 180}, {}, None)
        with patch.object(protocol, 'advance', return_value=result):
            self.runner.tick(self.wall)

    def plan(self, frequency=2462):
        plan = protocol.make_plan({OWN: status()}, {'2.4': frequency}, False,
                                  1800000045 - 7200, 'e' * 32)
        commit = {'plan': protocol.digest(plan), 'approvals': {OWN: 'a' * 32},
                  'issued_at': plan['deadline']}
        return plan, commit

    def test_hold_and_ui_remaining_duration_survive_steps_and_restart(self):
        deadline = self.elapsed + 60
        self.runner.state['hold_until'] = deadline
        self.runner.save()
        for step in STEPS:
            with self.subTest(step=step):
                self.wall = 1800000045 + step
                self.assertFalse(self.ready())
                self.tick()
                shown = json.loads((self.root / 'manet-acs-status.json').read_text())
                self.assertEqual(shown['hold_until'] - self.wall, 60)
                self.runner = runtime.Runtime()
                self.assertEqual(self.runner.state['hold_until'], deadline)
                self.assertFalse(self.ready())
        self.elapsed += 60
        self.runner = runtime.Runtime()
        self.assertTrue(self.ready())

    def test_capability_cache_refreshes_only_after_monotonic_minute(self):
        original = self.runner.status(self.wall)['allowed']
        self.frequencies = (2462,)
        self.elapsed += 600  # BOOTTIME can also advance independently during suspend.
        for step in STEPS:
            self.wall = 1800000045 + step
            self.assertEqual(self.runner.status(self.wall)['allowed'], original)
        self.monotonic += 59
        self.assertEqual(self.runner.status(self.wall)['allowed'], original)
        self.monotonic += 1
        self.assertEqual(self.runner.status(self.wall)['allowed'], {'2.4': [2462]})

    def test_publish_and_probe_cadences_follow_monotonic_seconds(self):
        plan, commit = self.plan(2437)
        self.runner.state['destination'] = {'plan': plan, 'commit': commit}
        with patch.object(self.runner, 'answer_probes') as answer:
            self.tick()
            self.assertEqual((self.publisher.call_count, answer.call_count), (1, 1))
            for step in STEPS:
                self.wall = 1800000045 + step
                self.tick()
                self.assertEqual((self.publisher.call_count, answer.call_count), (1, 1))
            self.elapsed += 600
            self.monotonic += 4.99
            self.tick()
            self.assertEqual((self.publisher.call_count, answer.call_count), (1, 1))
            self.monotonic += .01
            self.tick()
            self.assertEqual((self.publisher.call_count, answer.call_count), (2, 2))

    def test_failed_apply_retry_and_repair_window_follow_boot_time(self):
        plan, commit = self.plan()
        deadline = self.elapsed + 120
        with patch.object(self.runner, 'apply', side_effect=OSError('radio unavailable')) as apply:
            with self.assertRaises(OSError):
                self.tick(({'phase': 'attempted', 'plan': plan, 'commit': commit}, {}, plan))
            self.assertEqual(self.runner.state['repair_until'], deadline)
            self.assertEqual(self.runner.state['retry_at'], self.elapsed + 30)
            self.assertEqual(self.runner.state['hold_until'], self.elapsed + protocol.RECOVERY_SECONDS)
            self.assertEqual(self.runner.state['timer_boot'], self.runner.boot)
            # The failed reconfigure left the desired config ahead of the radio.
            (self.wpa / 'wpa_supplicant-wlan0.conf').write_text('frequency=2462\n')
            for step in STEPS:
                self.wall = 1800000045 + step
                self.tick()
            self.assertEqual(apply.call_count, 1)
        self.runner = runtime.Runtime()
        with patch.object(self.runner, 'apply', side_effect=OSError('radio unavailable')) as apply:
            self.elapsed += 29.9
            self.tick()
            apply.assert_not_called()
            self.elapsed += .1
            with self.assertRaises(OSError):
                self.tick()
            self.assertEqual(apply.call_count, 1)
            self.elapsed = deadline + .01
            for step in STEPS:
                self.wall = 1800000045 + step
                self.tick()
            self.assertEqual(apply.call_count, 1)

    def test_successful_apply_settles_for_thirty_boot_seconds(self):
        plan, commit = self.plan()
        with patch.object(self.runner, 'apply'):
            self.tick(({'phase': 'attempted', 'plan': plan, 'commit': commit}, {}, plan))
        deadline = self.elapsed + 30
        self.assertEqual(self.runner.state['settle_until'], deadline)
        for step in STEPS:
            self.wall = 1800000045 + step
            self.runner = runtime.Runtime()
            self.tick()
            self.assertTrue(self.runner.busy())
            self.assertEqual(self.runner.state['settle_until'], deadline)
        self.elapsed = deadline + .01
        self.assertFalse(self.runner.busy())

    def test_connected_recovery_cannot_retry_early_after_wall_steps(self):
        plan, commit = self.plan()
        records = {PEER: {'status': status('b', current={'2.4': 2462}),
                          'destination': {'plan': plan, 'commit': commit}}}
        with patch.object(self.runner, 'receive', return_value=records), \
                patch.object(self.runner, 'members', return_value=[OWN, PEER]), \
                patch.object(self.runner, 'apply', side_effect=OSError('radio unavailable')) as apply:
            with self.assertRaises(OSError):
                self.tick()
            self.assertEqual(self.runner.state['hold_until'], self.elapsed + protocol.RECOVERY_SECONDS)
            self.assertEqual(self.runner.state['repair_until'], self.elapsed + 120)
            for step in STEPS:
                self.wall = 1800000045 + step
                self.tick()
            self.assertEqual(apply.call_count, 1)
            self.elapsed += 30
            with self.assertRaises(OSError):
                self.tick()
            self.assertEqual(apply.call_count, 2)

    def test_plan_busy_conversion_is_pinned_across_steps_and_restart(self):
        plan = protocol.make_plan({OWN: status()}, {'2.4': 2462}, False, self.wall, 'e' * 32)
        result = ({'phase': 'prepared', 'plan': plan}, {}, None)
        self.tick(result)
        deadline = self.elapsed + plan['activate_at'] + protocol.APPLY_GRACE - self.wall
        self.assertEqual(self.runner.state['busy_until'], deadline)
        original = self.runner.busy_path.read_text()
        for step in STEPS:
            self.wall = 1800000045 + step
            self.elapsed += 1
            self.runner = runtime.Runtime()
            self.tick(result)
            self.assertTrue(self.runner.busy())
            self.assertEqual(self.runner.busy_path.read_text(), original)
        self.elapsed = deadline + .01
        self.tick(result)
        self.assertFalse(self.runner.busy())  # Polling cannot renew the deadline.

    def test_busy_conversion_uses_fresh_clock_samples_after_slow_work(self):
        plan = protocol.make_plan({OWN: status()}, {'2.4': 2462}, False, self.wall, 'e' * 32)
        deadline = self.elapsed + plan['activate_at'] + protocol.APPLY_GRACE - self.wall
        def slow_advance(*args):
            self.wall += 7
            self.elapsed += 7
            return {'phase': 'prepared', 'plan': plan}, {}, None
        with patch.object(protocol, 'advance', side_effect=slow_advance):
            self.runner.tick(self.wall)
        self.assertEqual(self.runner.state['busy_until'], deadline)

    def test_unknown_boot_restarts_only_hold_and_persists_it_before_sync(self):
        for boot in (None, 'f' * 32):
            with self.subTest(boot=boot):
                self.runner.state.update({key: 9999999999 for key in runtime.LOCAL_TIMERS})
                self.runner.state['timer_boot'] = boot
                self.runner.save()
                (self.root / 'initial_time_synced').unlink(missing_ok=True)
                self.wall += 3600
                self.runner = runtime.Runtime()
                hold = self.elapsed + protocol.RECOVERY_SECONDS
                self.assertEqual(self.runner.state['hold_until'], hold)
                self.assertEqual(self.runner.state['timer_boot'], self.runner.boot)
                for key in runtime.LOCAL_TIMERS[1:]:
                    self.assertNotIn(key, self.runner.state)
                self.elapsed += 10
                self.wall -= 7200
                self.runner = runtime.Runtime()
                self.assertEqual(self.runner.state['hold_until'], hold)

    def test_boot_reconciliation_under_a_held_lock_stays_in_memory(self):
        # The tourguide holds the channel lock while it runs members and
        # helper-encode, which construct Runtime: they must not fail.
        self.runner.state.update(timer_boot='f' * 32, hold_until=9999999999)
        self.runner.save()
        original = self.runner.path.read_text()
        with self.runner.channel_lock():
            for step in STEPS:
                self.wall = 1800000045 + step
                helper = runtime.Runtime()
                self.assertEqual(helper.state['hold_until'], self.elapsed + protocol.RECOVERY_SECONDS)
                self.assertEqual(self.runner.path.read_text(), original)
        self.runner = runtime.Runtime()
        self.assertEqual(self.runner.state['hold_until'], self.elapsed + protocol.RECOVERY_SECONDS)
        self.assertNotEqual(self.runner.path.read_text(), original)

    def test_shell_and_python_share_busy_deadlines_across_wall_steps(self):
        source = '. "$MANET_TOOLS_DIR/mesh-acs-common.sh"\n'
        (self.root / 'boot').write_text('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa\n')
        self.runner.write_busy(self.elapsed + 30)
        for step in STEPS:
            self.wall = 1800000045 + step
            self.env['TEST_NOW'] = str(self.wall)
            self.assertTrue(self.runner.busy())
            self.assertEqual(self.shell(source + 'acs_agreement_busy').returncode, 0)
        self.elapsed += 30.01
        (self.root / 'uptime').write_text(f'{self.elapsed} 1.00\n')
        self.assertFalse(self.runner.busy())
        self.assertEqual(self.shell(source + 'acs_agreement_busy').returncode, 1)
        for step in STEPS:
            self.wall = 1800000045 + step
            self.env['TEST_NOW'] = str(self.wall)
            self.assertEqual(self.shell(source + 'acs_mark_busy').returncode, 0)
            self.assertTrue(self.runner.busy())
            self.assertAlmostEqual(float(self.runner.busy_path.read_text().split()[1]), self.elapsed + 30)
        # The process that owned a foreign-boot marker no longer exists.
        for value in (f'{"f" * 32} {self.elapsed + 30}', 'invalid',
                      f'{self.runner.boot} nan', f'{self.runner.boot} {self.elapsed + 30} extra'):
            self.runner.busy_path.write_text(value + '\n')
            self.assertFalse(self.runner.busy())
            self.assertEqual(self.shell(source + 'acs_agreement_busy').returncode, 1)
