"""Radar fallback must allow the beacon countdown without tearing down SAE."""
import json
import socket
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_acs import runtime
from test_acs_dfs import dfs_plan
import manet_acs_agreement as protocol


class RadarFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plan = dfs_plan()
        self.runner = object.__new__(runtime.Runtime)
        r = self.runner
        r.run_dir = Path(self.temp.name)
        r.boot = 'a' * 32
        r.state = {}
        self.config = r.run_dir / 'wpa'
        self.config.write_text('frequency=5260\n')
        r.interfaces = lambda: {'5': ('mesh5', self.config, 5260)}
        r.sync_parked = Mock()
        r.write_busy = Mock()
        self.now = 100.0
        self.calls = []
        self.index = patch.object(socket, 'if_nametoindex', return_value=7)
        self.index.start()
        self.addCleanup(self.index.stop)
        self.clock = patch.object(runtime, 'boot_time', side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.sleep = patch.object(runtime.time, 'sleep', side_effect=self.advance)
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def advance(self, seconds):
        self.now += seconds

    def pending(self):
        (self.runner.run_dir / 'manet-dfs-csa.json').write_text(json.dumps(dict(
            boot=self.runner.boot, plan=protocol.digest(self.plan), frequency=5200,
            requested_boottime=100.0, acknowledged_boottime=100.01)))

    def command(self, args, **kwargs):
        self.calls.append(args)
        if args[-1] == 'info':
            freq = 5200 if self.now >= self.lands_at else 5260
            return f'channel 52 ({freq} MHz)'
        return 'OK'

    def test_three_second_csa_keeps_mesh_bss_and_sae(self):
        self.pending()
        self.lands_at = 103.2
        with patch.object(runtime, 'command', side_effect=self.command):
            self.runner.radar_fallback(self.plan)
        self.assertFalse(any(c[0] in ('ip', 'wpa_cli', 'systemctl') for c in self.calls), self.calls)
        self.runner.sync_parked.assert_called_once_with({'5': 5200})

    def test_pending_csa_deduplicates_after_two_seconds(self):
        self.pending()
        self.now = 102.5
        with patch.object(runtime, 'command') as cmd:
            self.assertTrue(self.runner.vacate_dfs(self.plan))
        cmd.assert_not_called()

    def test_remote_abort_after_peer_landing_does_not_issue_same_channel_csa(self):
        self.lands_at = 99
        with patch.object(runtime, 'command', side_effect=self.command):
            self.assertFalse(self.runner.vacate_dfs(self.plan, check_current=True))
        self.assertEqual(self.calls, [['iw', 'dev', 'mesh5', 'info']])
        self.assertIn('frequency=5200', self.config.read_text())

    def test_landed_peer_without_local_csa_record_is_not_reconfigured(self):
        self.lands_at = 99
        with patch.object(runtime, 'command', side_effect=self.command):
            self.runner.radar_fallback(self.plan)
        self.assertEqual(self.calls, [['iw', 'dev', 'mesh5', 'info']])

    def peer_switch(self, **changes):
        record = dict(boot=self.runner.boot, plan=protocol.digest(self.plan),
                      iface='mesh5', ifindex=7, frequency=5200,
                      started_boottime=100.0, block_tx=True)
        record.update(changes)
        (self.runner.run_dir / 'manet-dfs-peer-csa.json').write_text(json.dumps(record))

    def test_peer_countdown_without_local_ack_preserves_bss(self):
        self.peer_switch()
        self.lands_at = 103.2
        with patch.object(runtime, 'command', side_effect=self.command):
            self.assertTrue(self.runner.vacate_dfs(self.plan, check_current=True))
            self.runner.radar_fallback(self.plan)
        self.assertFalse(any(c[0] in ('ip', 'systemctl') or 'switch' in c for c in self.calls), self.calls)
        self.assertGreaterEqual(self.now, self.lands_at)
        self.assertIn('frequency=5200', self.config.read_text())

    def test_peer_starts_during_local_request_and_is_read_after_busy(self):
        self.lands_at = 103.2
        def command(args, **kw):
            if 'switch' in args:
                self.calls.append(args)
                self.peer_switch()
                raise subprocess.CalledProcessError(1, args, stderr='Device or resource busy (-16)')
            return self.command(args, **kw)
        with patch.object(runtime, 'command', side_effect=command):
            self.assertTrue(self.runner.vacate_dfs(self.plan))
            self.runner.radar_fallback(self.plan)
        self.assertFalse(any(c[0] in ('ip', 'systemctl') for c in self.calls), self.calls)
        self.assertEqual(len([c for c in self.calls if 'switch' in c]), 1)
        self.assertFalse((self.runner.run_dir / 'manet-dfs-csa.json').exists())

    def test_peer_deadline_survives_runtime_restart_and_does_not_slide(self):
        self.peer_switch()
        self.now = 109.5
        self.lands_at = float('inf')
        def command(args, **kw):
            result = self.command(args, **kw)
            if args[:2] == ['systemctl', 'restart']:
                self.assertGreaterEqual(self.now, 110)
                self.assertLess(self.now, 110.2)
                self.lands_at = self.now
            return result
        with patch.object(runtime, 'command', side_effect=command):
            self.runner.radar_fallback(self.plan)
        self.assertTrue(any(c[:2] == ['systemctl', 'restart'] for c in self.calls))

    def test_unproven_switch_does_not_delay_failed_local_fence(self):
        for changes in (dict(block_tx=False), dict(boot='b'*32),
                        dict(plan='f'*64), dict(frequency=5220),
                        dict(iface='other'), dict(ifindex=8),
                        dict(started_boottime=89), dict(started_boottime=101)):
            with self.subTest(changes=changes):
                self.peer_switch(**changes)
                self.calls.clear()
                self.lands_at = float('inf')
                def command(args, **kw):
                    if 'switch' in args:
                        self.calls.append(args)
                        raise subprocess.CalledProcessError(1, args)
                    return self.command(args, **kw)
                with patch.object(runtime, 'command', side_effect=command):
                    self.assertTrue(self.runner.vacate_dfs(self.plan))
                self.assertIn(['ip', 'link', 'set', 'dev', 'mesh5', 'down'], self.calls)
                self.assertEqual(self.now, 100)

    def test_expired_csa_fences_then_restarts_on_safe_configuration(self):
        self.pending()
        self.now = 111
        self.lands_at = float('inf')
        def command(args, **kw):
            result = self.command(args, **kw)
            if args[0] == 'systemctl':
                self.assertIn('frequency=5200', self.config.read_text())
                self.assertIn(['ip', 'link', 'set', 'dev', 'mesh5', 'down'], self.calls)
                self.lands_at = self.now
            return result
        with patch.object(runtime, 'command', side_effect=command):
            self.runner.radar_fallback(self.plan)
        self.assertTrue(any(c[:2] == ['systemctl', 'restart'] for c in self.calls))
        self.assertFalse(any(c[0] == 'wpa_cli' for c in self.calls))
        self.assertLess(self.now, 112)

    def test_failed_restart_leaves_interface_down(self):
        self.lands_at = float('inf')
        def command(args, **kw):
            result = self.command(args, **kw)
            if args[0] == 'systemctl':
                raise subprocess.CalledProcessError(1, args)
            return result
        with patch.object(runtime, 'command', side_effect=command):
            with self.assertRaises(subprocess.CalledProcessError):
                self.runner.radar_fallback(self.plan)
        self.assertEqual(self.calls[-1], ['ip', 'link', 'set', 'dev', 'mesh5', 'down'])


if __name__ == '__main__':
    unittest.main()
