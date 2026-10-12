"""Idle Wi-Fi drift repairs respect radio operations and bounded retries."""
import contextlib
import io
import json
import os
import subprocess
from unittest.mock import patch

from test_acs import AcsHarness, runtime


class DriftTests(AcsHarness):
    """Exercise daemon ticks with independent configured and live MHz."""

    def setUp(self):
        """Provide two radios, a boot clock and an observable supplicant."""
        super().setUp()
        self.configure(2437, 5805)
        self.elapsed = 1000.0
        self.wall = 1800000150
        self.actual = {'wlan0': 2437, 'wlan1': 5785}
        self.calls = []
        self.answer = 'OK\n'
        self.lands = False
        self.read_failure = False
        self.log = io.StringIO()
        for seam in (
                patch.dict(os.environ, self.env),
                patch.object(runtime, 'boot_time',
                             side_effect=lambda: self.elapsed),
                patch.object(runtime.time, 'time',
                             side_effect=lambda: self.wall),
                patch.object(runtime.time, 'monotonic',
                             side_effect=lambda: self.elapsed),
                patch.object(runtime, 'command', side_effect=self.output),
                patch.object(runtime.AdminTransport, 'seal', return_value={}),
                patch.object(runtime.Runtime, 'score', return_value=None),
                contextlib.redirect_stderr(self.log)):
            seam.__enter__()
            self.addCleanup(seam.__exit__, None, None, None)
        self.runner = runtime.Runtime()

    def output(self, args, **kwargs):
        """Simulate live readback independently from the WPA config files."""
        self.calls.append((args, kwargs))
        if args[:2] == ['iw', 'dev']:
            if self.read_failure:
                raise subprocess.TimeoutExpired('iw', 2)
            return f'wiphy 0\nchannel 1 ({self.actual[args[2]]} MHz)'
        if args[:2] == ['iw', 'phy']:
            return '\n'.join(f'* {f}.0 MHz [1] (20 dBm)'
                             for f in (2412, 2437, 2462, 5180, 5785, 5805))
        if args[0] == 'wpa_cli':
            if isinstance(self.answer, Exception):
                raise self.answer
            if self.lands:
                for iface, path, freq in self.runner.interfaces().values():
                    if iface == args[2]:
                        self.actual[iface] = freq
            return self.answer
        if args[-1] == 'originators_json':
            return '[]'
        return ''

    def tick(self, seconds=0):
        """Advance local time and exercise the public daemon pass."""
        self.elapsed += seconds
        self.wall += seconds
        self.runner.request_path.write_text(json.dumps({
            'round': int(self.wall) // runtime.protocol.ROUND_SECONDS}))
        self.runner.tick(int(self.wall))

    def repairs(self):
        """Return only supplicant mutations, including their timeouts."""
        return [(args, kwargs) for args, kwargs in self.calls
                if args[0] in ('wpa_cli', 'systemctl')]

    def test_sustained_drift_reconfigures_wrong_radio_and_restores_vote(self):
        """The 5785/5805 mismatch heals after grace without a saved plan."""
        originals = {p: p.read_bytes() for p in self.wpa.iterdir()}
        self.lands = True
        self.tick()
        self.assertFalse(self.runner.status(self.wall)['stable'])
        self.assertFalse(self.runner.status(self.wall)['ready'])
        self.tick(59)
        self.assertEqual(self.repairs(), [])
        self.tick(1)
        self.assertEqual(self.repairs(), [
            (['wpa_cli', '-i', 'wlan1', 'reconfigure'], {'timeout': 2})])
        self.assertEqual({p: p.read_bytes() for p in originals}, originals)
        self.assertTrue(self.runner.status(self.wall)['stable'])
        self.assertTrue(self.runner.busy())
        self.tick(31)
        self.assertTrue(self.runner.status(self.wall)['ready'])
        self.tick(600)
        self.assertEqual(len(self.repairs()), 1)
        self.assertIn('wlan1 at 5785 MHz, configured 5805 MHz',
                      self.log.getvalue())

    def test_failed_landing_backs_off_across_restart_and_clock_steps(self):
        """Even accepted commands that fail to land cannot cause a loop."""
        self.tick()
        self.tick(60)
        self.assertEqual(len(self.repairs()), 1)
        self.runner = runtime.Runtime()
        self.tick(31)
        self.wall += 7200
        self.tick(60)
        self.wall -= 14400
        self.tick(208)
        self.assertEqual(len(self.repairs()), 1)
        self.tick(1)
        self.assertEqual(len(self.repairs()), 2)
        self.assertEqual(self.log.getvalue().count('backoff 300s'), 2)

    def test_rejected_and_timed_out_commands_also_back_off(self):
        """Record the cooldown before a failed command and never restart."""
        for answer in ('FAIL\n', '', subprocess.TimeoutExpired('wpa_cli', 2),
                       OSError('missing supplicant')):
            with self.subTest(answer=answer):
                self.runner.state.clear()
                self.runner.reset_timers()
                self.runner.drift_since.clear()
                self.runner.busy_path.unlink(missing_ok=True)
                self.calls.clear()
                self.answer = answer
                self.tick()
                self.tick(60)
                self.tick(31)
                self.tick(60)
                self.assertEqual(len(self.repairs()), 1)
                self.assertEqual(self.repairs()[0][0][0], 'wpa_cli')
                self.assertIn('wlan1 failed:', self.log.getvalue())

    def test_transient_match_and_config_change_restart_grace(self):
        """Only continuous mismatch against the same target qualifies."""
        self.tick()
        self.actual['wlan1'] = 5805
        self.tick(59)
        self.actual['wlan1'] = 5785
        self.tick(1)
        self.tick(59)
        self.assertEqual(self.repairs(), [])
        self.configure(2437, 5825)
        self.tick(1)
        self.tick(59)
        self.assertEqual(self.repairs(), [])
        self.tick(1)
        self.assertEqual(len(self.repairs()), 1)

    def test_disabled_and_absent_roles_never_repair(self):
        """Withheld mesh roles and explicitly disabled radios are excluded."""
        for absent in (False, True):
            with self.subTest(absent=absent):
                self.configure(2437, 5805)
                self.tick()
                if absent:
                    (self.roles / 'mesh_5_if').write_text('')
                else:
                    self.runner.radio_state.write_text(json.dumps({
                        'desired': {'wlan1': 'down'}}))
                self.tick()
                self.tick(600)
                self.assertEqual(self.repairs(), [])
                self.runner.radio_state.unlink(missing_ok=True)

    def test_busy_and_recent_plan_repair_reset_grace(self):
        """Existing busy, settling and application repair take precedence."""
        for guard in ('busy', 'repair_until', 'settle_until'):
            with self.subTest(guard=guard):
                self.runner.drift_since.clear()
                self.tick()
                if guard == 'busy':
                    self.runner.write_busy(self.elapsed + 100)
                else:
                    self.runner.state[guard] = self.elapsed + 100
                self.tick(60)
                self.assertEqual(self.repairs(), [])
                self.tick(41)
                self.tick(59)
                self.assertEqual(self.repairs(), [])

    def test_prepared_and_committed_agreement_never_repair(self):
        """An active agreement blocks even without a current busy marker."""
        self.tick()
        for phase in ('prepared', 'committed'):
            with self.subTest(phase=phase):
                self.runner.state['protocol'] = {'phase': phase}
                with self.runner.channel_lock():
                    self.elapsed += 600
                    self.assertFalse(self.runner.repair_drift({
                        'discovery': False,
                        'current': {'2.4': 2437, '5': 5785}}))
        self.assertEqual(self.repairs(), [])

    def test_tourguide_channel_lock_blocks_and_restarts_grace(self):
        """A lobby visit cannot be repaired, even at the grace deadline."""
        self.tick()
        with self.runner.channel_lock():
            self.actual['wlan1'] = 5180
            for seconds in (60, 600):
                with self.assertRaises(BlockingIOError):
                    self.tick(seconds)
            self.assertEqual(self.repairs(), [])
        self.actual['wlan1'] = 5785
        self.tick()
        self.tick(59)
        self.assertEqual(self.repairs(), [])
        self.tick(1)
        self.assertEqual(len(self.repairs()), 1)

    def test_discovery_rotation_never_repairs_configured_data_channel(self):
        """Persistent search mode excludes every scheduled lobby slot."""
        self.tick()
        self.runner.discovery.set_mode('search')
        with patch.object(self.runner, 'apply') as apply:
            for seconds in (60, 240, 240, 240):
                self.tick(seconds)
                self.assertEqual(self.repairs(), [])
            self.assertTrue(apply.called)
            self.assertTrue(all(call.kwargs.get('discovering')
                                for call in apply.call_args_list))
        self.runner.discovery.set_mode('data')
        self.tick()
        self.tick(59)
        self.assertEqual(self.repairs(), [])
        self.tick(1)
        self.assertEqual(len(self.repairs()), 1)

    def test_unreadable_radio_breaks_grace(self):
        """A failed read cannot count as continued idle mismatch."""
        self.tick()
        self.read_failure = True
        with self.assertRaises(subprocess.TimeoutExpired):
            self.tick(60)
        self.read_failure = False
        self.tick()
        self.tick(59)
        self.assertEqual(self.repairs(), [])
        self.tick(1)
        self.assertEqual(len(self.repairs()), 1)

    def test_final_readback_can_cancel_repair(self):
        """A radio landing during slow peer work needs no reconfigure."""
        self.tick()

        def land_after_snapshot(args, **kwargs):
            """Land after status samples the wrong frequency."""
            output = self.output(args, **kwargs)
            if args == ['iw', 'dev', 'wlan1', 'info']:
                self.actual['wlan1'] = 5805
            return output

        with patch.object(runtime, 'command', side_effect=land_after_snapshot):
            self.tick(60)
        self.assertEqual(self.repairs(), [])
        self.assertNotIn('wlan1', self.runner.drift_since)

    def test_failed_backoff_save_prevents_reconfigure(self):
        """A failed durable cooldown write cannot authorize a mutation."""
        self.tick()
        write = runtime.private_json_write

        def fail_cooldown(path, value):
            """Fail only the write that records a repair attempt."""
            if path == self.runner.path and value.get('drift_retry_at'):
                raise OSError('storage unavailable')
            write(path, value)

        with patch.object(runtime, 'private_json_write',
                          side_effect=fail_cooldown):
            with self.assertRaises(OSError):
                self.tick(60)
        self.assertEqual(self.repairs(), [])
        self.assertNotIn('drift_retry_at', runtime.read_json(self.runner.path))

    def test_static_and_clockless_nodes_repair_only_their_local_config(self):
        """Local repairs need neither an ACS vote nor synchronized UTC."""
        for static in (False, True):
            with self.subTest(static=static):
                self.runner.state.clear()
                self.runner.reset_timers()
                self.runner.drift_since.clear()
                self.runner.busy_path.unlink(missing_ok=True)
                self.calls.clear()
                if static:
                    self.runner.conf.write_text(
                        'acs=n\nadmin_password=test-secret\n')
                (self.root / 'initial_time_synced').unlink(missing_ok=True)
                with patch.object(self.runner, 'bootstrap_select',
                                  return_value=None):
                    self.tick()
                    self.tick(60)
                self.assertEqual(len(self.repairs()), 1)

    def test_reboot_discards_backoff_and_restarts_grace(self):
        """Persisted cooldowns never carry deadlines into another boot."""
        self.tick()
        self.tick(60)
        (self.root / 'boot').write_text('b' * 32)
        self.elapsed = 10
        self.runner = runtime.Runtime()
        self.tick()
        self.tick(59)
        self.assertEqual(len(self.repairs()), 1)
        self.tick(1)
        self.assertEqual(len(self.repairs()), 2)
