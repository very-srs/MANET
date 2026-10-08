"""Storage exhaustion cannot discard measurements or start unrecordable work."""

import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import manet_manage as manage
import manet_measurement_storage as storage


class MeasurementStorageTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.session = self.root / 'saved'
        self.session.mkdir()
        self.old = self.session / 'old.json'
        self.old.write_text('{"kept": true}')
        self.result = self.session / 'new.json'
        policy = dict(storage.DEFAULTS)
        override = patch.object(storage, 'limits', return_value=policy)
        override.start()
        self.addCleanup(override.stop)
        self.policy = policy

    def test_byte_file_and_session_limits_preserve_existing_results(self):
        cases = [('MAX_BYTES', 1, self.result),
                 ('MAX_FILES', 1, self.result),
                 ('MAX_SESSIONS', 1, self.root / 'another' / 'new.json')]
        for key, value, destination in cases:
            with self.subTest(key=key), patch.dict(self.policy, {key: value}):
                with self.assertRaises(storage.StorageFull):
                    storage.save_result(self.root, destination, {'new': True})
                self.assertEqual(self.old.read_text(), '{"kept": true}')
                self.assertFalse(destination.exists())

    def test_free_space_reserve_and_oversized_result(self):
        usage = shutil.disk_usage(self.root)
        with patch.object(storage.shutil, 'disk_usage', return_value=
                          usage._replace(free=self.policy['MIN_FREE_BYTES'])):
            with self.assertRaisesRegex(storage.StorageFull, 'free storage'):
                storage.save_result(self.root, self.result, {'new': True})
        with patch.dict(self.policy, MAX_RESULT_BYTES=16):
            with self.assertRaisesRegex(storage.StorageFull, 'size limit'):
                storage.save_result(self.root, self.result, {'large': 'x' * 32})
        self.assertFalse(self.result.exists())

    def test_symlink_and_existing_result_are_never_overwritten(self):
        for linked in (False, True):
            with self.subTest(linked=linked):
                if linked:
                    self.result.symlink_to(self.old)
                else:
                    self.result.write_text('existing')
                with self.assertRaises(ValueError):
                    storage.save_result(self.root, self.result, {'new': True})
                self.result.unlink()
                self.assertEqual(self.old.read_text(), '{"kept": true}')

    def test_failed_atomic_write_leaves_no_partial_result(self):
        with patch('manet_config_io.os.replace',
                   side_effect=OSError('write failed')):
            with self.assertRaises(OSError):
                storage.save_result(self.root, self.result, {'new': True})
        self.assertEqual(list(self.session.iterdir()), [self.old])

    def test_full_store_stops_worker_before_sending_test_traffic(self):
        pairs = [{'src_ip': '10.0.0.1', 'dst_ip': '10.0.0.2',
                  'src_name': 'one', 'dst_name': 'two'}]
        with patch.object(manage, 'SESSIONS_DIR', str(self.root)), \
                patch.dict(self.policy, MAX_FILES=1), \
                patch.object(manage, 'snapshot_topology') as topology, \
                patch.object(manage, 'run_local_ping') as ping:
            manage.run_measurement_session('saved', pairs, ['icmp_ping'],
                                           5, '4M')
        topology.assert_not_called()
        ping.assert_not_called()
        self.assertFalse(manage._measure_status['running'])
        self.assertIn('storage limit', manage._measure_status['error'])
        self.assertEqual(manage._measure_status['done'], 0)

    def test_last_free_result_slot_is_written_and_next_is_refused(self):
        with patch.dict(self.policy, MAX_FILES=2):
            storage.save_result(self.root, self.result, {'new': True})
            with self.assertRaises(storage.StorageFull):
                storage.save_result(self.root, self.session / 'extra.json', {})
        self.assertIn('true', self.result.read_text())
        self.assertEqual(len(list(self.session.iterdir())), 2)


class PolicyTests(unittest.TestCase):
    def test_operator_override_must_be_positive(self):
        with patch.dict(os.environ, MANET_MEASUREMENTS_MAX_FILES='100'):
            self.assertEqual(storage.limits()['MAX_FILES'], 100)
        for value in ('0', '-1', 'not-a-number'):
            with patch.dict(os.environ, MANET_MEASUREMENTS_MAX_FILES=value):
                with self.assertRaises(ValueError):
                    storage.limits()


if __name__ == '__main__':
    unittest.main()
