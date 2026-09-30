"""Measurement labels and display names cannot escape the session directory."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import manet_manage as manage


class MeasurementPathTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(); self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.sessions = self.root / 'sessions'; self.sessions.mkdir()
        self.sentinel = self.root / 'keep.txt'; self.sentinel.write_text('keep')
        p = patch.object(manage, 'SESSIONS_DIR', str(self.sessions)); p.start(); self.addCleanup(p.stop)

    def test_dot_parent_absolute_and_symlink_sessions_are_rejected(self):
        (self.sessions / 'linked').symlink_to(self.root, target_is_directory=True)
        for label in ('.', '..', '../escape', str(self.root), 'linked', '', 'a/b', 'x' * 65, None):
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    manage.get_session_results(label)
                self.assertFalse(manage.delete_session(label)[0])
        self.assertEqual(self.sentinel.read_text(), 'keep')

    def test_untrusted_node_names_stay_inside_json_and_session_can_be_deleted(self):
        topo = {'active_interfaces': [], 'halow_channel': '', 'halow_bw': '', 'ch_2g': ''}
        name = '../../untrusted/name'
        pairs = [{'src_ip': '10.0.0.1', 'dst_ip': '10.0.0.2', 'src_name': name, 'dst_name': name}]
        with patch.object(manage, 'snapshot_topology', return_value=topo), \
                patch.object(manage, 'get_session_hop_count', return_value=(1, 'test')), \
                patch.object(manage, 'run_local_ping', return_value={'loss_pct': 0}), \
                patch.object(manage.time, 'sleep'):
            manage.run_measurement_session('valid-session', pairs, ['icmp_ping'], 5, '4M')
        self.assertEqual(manage._measure_status['error'], '')
        results = manage.get_session_results('valid-session')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['source_node'], name)
        self.assertTrue(manage.delete_session('valid-session')[0])
        self.assertTrue(self.sentinel.exists())
