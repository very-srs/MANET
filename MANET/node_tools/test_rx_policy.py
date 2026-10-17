"""Receiver policy ownership, boot-clock throttling and driver busy errors."""

import errno
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import manet_rx_policy as policy


class RxPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.observer = self.base / 'mt76/background_scan'
        self.observer.parent.mkdir()
        self.path = self.observer.with_name('rx_transition_probe')
        self.status = dict(
            schema=1,
            chainmask=28,
            reserved=False,
            failed=False,
            transitions=0,
            last_boottime_us=0,
        )
        self.path.write_text(json.dumps(self.status))
        gate = patch.object(policy, 'cm4_enabled', return_value=True)
        gate.start()
        self.addCleanup(gate.stop)

    def write(self, path, value):
        self.assertEqual(path, self.path)
        self.status.update(
            reserved=value == '2', chainmask=12 if value == '2' else 28
        )
        self.path.write_text(json.dumps(self.status))

    def test_local_trouble_reserves_and_clear_restores(self):
        with patch.object(
            policy, 'write_command', side_effect=self.write
        ) as write:
            self.assertEqual(
                policy.reconcile_receive_paths(self.observer, False), '3x3'
            )
            write.assert_not_called()
            self.assertEqual(
                policy.reconcile_receive_paths(self.observer, True), 'reserved'
            )
            self.assertEqual(
                policy.reconcile_receive_paths(self.observer, False), '3x3'
            )

    def test_hold_owner_keeps_reservation_after_trouble_clears(self):
        (self.base / 'background_hold').write_text(
            json.dumps(dict(schema=2, owner=True, enabled=True))
        )
        with patch.object(policy, 'write_command', side_effect=self.write):
            self.assertEqual(
                policy.reconcile_receive_paths(self.observer, False),
                'reserved',
            )
            self.assertEqual(
                policy.reconcile_receive_paths(self.observer, False),
                'reserved',
            )

    def test_rate_limit_and_busy_never_stop_an_owner(self):
        self.status.update(transitions=1, last_boottime_us=100000000)
        self.path.write_text(json.dumps(self.status))
        with patch.object(
            policy.time, 'clock_gettime', return_value=159
        ), patch.object(policy, 'write_command') as write:
            self.assertEqual(
                policy.reconcile_receive_paths(self.observer, True), 'deferred'
            )
            write.assert_not_called()
        for error in (errno.EBUSY, errno.EAGAIN):
            with patch.object(
                policy.time, 'clock_gettime', return_value=160
            ), patch.object(
                policy, 'write_command', side_effect=OSError(error, 'retry')
            ) as write:
                self.assertEqual(
                    policy.reconcile_receive_paths(self.observer, True),
                    'deferred',
                )
                self.assertEqual(write.call_args.args[1], '2')

    def test_missing_or_unqualified_kernel_never_writes(self):
        self.path.unlink()
        with patch.object(policy, 'write_command') as write:
            self.assertEqual(
                policy.reconcile_receive_paths(self.observer, True),
                'unsupported-kernel',
            )
            write.assert_not_called()
        with patch.object(policy, 'cm4_enabled', return_value=False):
            self.assertEqual(
                policy.reconcile_receive_paths(self.observer, True),
                'unsupported-board',
            )


if __name__ == '__main__':
    unittest.main()
