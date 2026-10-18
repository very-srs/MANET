"""Run the capture controller with a simulated event stream and no SSH."""
import importlib.util
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'MANET/node_tools'))
path = Path(os.environ.get('MANET_BENCH_SOURCE', Path(__file__).with_name('bench-dfs.py')))
spec = importlib.util.spec_from_file_location('dfs_bench', path)
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


class CaptureTests(unittest.TestCase):
    def run_capture(self, *, disabled=False, recover=False, silence=False, partial=False):
        plan = {'channels': {'5': 5260}, 'dfs': {'fallback': {'5': 5240},
                'holds': {'cm4': ['a', 1], 'r3a': ['b', 2]}}}
        now = [100.0]
        step = [0]
        def sample(host, freq, peers):
            epoch, generation = plan['dfs']['holds'][host]
            return dict(kind='sample', frequency=freq, established=peers, own=host,
                        destination={'plan': plan, 'commit': {}},
                        kernel=dict(main_active=True, main_epoch=epoch,
                                    main_generation=generation, radar_cause='main-radar',
                                    radar_boottime_us=102000000),
                        csa=dict(requested_boottime=102.01, acknowledged_boottime=102.02))
        def get(timeout):
            step[0] += 1
            now[0] += 1
            host = 'cm4' if step[0] % 2 else 'r3a'
            if step[0] <= 2:
                return host, now[0], sample(host, 5260, 2 if partial else 1)
            if disabled and step[0] == 3:
                return host, now[0], dict(kind='journal', event='wlan1: AP-DISABLED')
            if silence:
                raise queue.Empty
            peers = 1 if recover or partial or host == 'cm4' else 0
            return host, now[0], sample(host, 5240, peers)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'capture'
            error = None
            with patch.object(bench.queue, 'Queue', return_value=SimpleNamespace(get=get)), \
                    patch.object(bench.threading, 'Thread'), \
                    patch.object(bench.time, 'monotonic', side_effect=lambda: now[0]), \
                    patch.object(bench.protocol, 'valid_commit', return_value=True), \
                    patch.object(bench.subprocess, 'run', return_value=SimpleNamespace(
                        stdout=json.dumps({'trigger_before': 102, 'trigger_after': 102}))), \
                    patch.object(sys, 'argv', ['bench-dfs.py', 'cm4', 'r3a', '--output', str(output)]):
                try:
                    bench.main()
                except (RuntimeError, TimeoutError) as exc:
                    error = exc
            summary = json.loads((output/'summary.json').read_text()) if (output/'summary.json').exists() else {}
        return error, now[0] - 102, summary

    def test_disabled_bss_fails_immediately_after_radar(self):
        error, elapsed, summary = self.run_capture(disabled=True, recover=True)
        self.assertIsInstance(error, RuntimeError)
        self.assertIn('AP-DISABLED', str(error))
        self.assertLess(elapsed, 5)
        self.assertEqual(summary['result'], 'failed')

    def test_no_peers_fails_with_node_and_bound(self):
        error, elapsed, summary = self.run_capture()
        self.assertIsInstance(error, TimeoutError)
        self.assertLessEqual(elapsed, 61)
        self.assertIn('r3a: peering=0/1', str(error))
        self.assertEqual(summary['result'], 'failed')

    def test_silent_monitor_cannot_extend_deadline(self):
        error, elapsed, _ = self.run_capture(silence=True)
        self.assertIsInstance(error, TimeoutError)
        self.assertLessEqual(elapsed, 61)
        self.assertIn('no fresh post-radar sample', str(error))

    def test_partial_peer_recovery_is_not_success(self):
        error, elapsed, _ = self.run_capture(partial=True)
        self.assertIsInstance(error, TimeoutError)
        self.assertLessEqual(elapsed, 61)
        self.assertIn('peering=1/2', str(error))

    def test_complete_recovery_requires_stability_window(self):
        error, elapsed, summary = self.run_capture(recover=True)
        self.assertIsNone(error)
        self.assertGreaterEqual(elapsed, 12)
        self.assertEqual(summary['result'], 'passed')


if __name__ == '__main__':
    unittest.main()
