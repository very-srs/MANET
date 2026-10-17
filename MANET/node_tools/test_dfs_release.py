"""Regression coverage for the schema-2 receiver boundary and capability gate."""

import errno
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch, Mock

from test_acs import runtime
from test_acs_dfs import NOW, dfs_view, dfs_plan, THIRD
import manet_acs_agreement as protocol


def hold_status(**changes):
    raw = dict(
        schema=2,
        enabled=True,
        owner=True,
        fault=False,
        live=True,
        receiver_live=True,
        revoked=False,
        epoch='d' * 32,
        generation=7,
        freq_mhz=5260,
        center_mhz=5260,
        width_mhz=20,
        observed_boottime_us=10000000,
        heartbeat_boottime_us=9750000,
        heartbeat_max_age_us=1500000,
    )
    raw.update(changes)
    return raw


class ReceiverBoundaryTests(unittest.TestCase):
    def test_pending_radar_delivery_cannot_defer_local_shutdown(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = Mock(
                boot='a' * 32, own='02:00:00:00:00:01', run_dir=Path(temp)
            )
            plan = dfs_plan()
            runner.own = plan['leader']
            runner.dfs_plans.return_value = {protocol.digest(plan): plan}
            (runner.run_dir / 'manet-dfs-radar.json').touch()
            with patch.object(runtime, 'wake_daemon') as wake:
                runtime.deliver_background_radar(
                    runner, ('d' * 32, 7, 2, 5260), set()
                )
            runner.vacate_dfs.assert_called_once_with(plan)
            runner.radar_detected.assert_not_called()
            wake.assert_called_once()

    def read(self, raw, checked=10):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'hold'
            path.write_text(json.dumps(raw))
            with patch.object(runtime, 'boot_time', return_value=checked):
                return runtime.read_background_hold(path, 'a' * 32, None, NOW)

    def test_schema2_keeps_kernel_identity(self):
        result = self.read(hold_status())
        self.assertIsNotNone(result)
        self.assertEqual(result['epoch'], 'd' * 32)
        self.assertTrue(result['live'])

    def test_schema1_and_boolean_geometry_are_rejected(self):
        for changes in (
            {'schema': 1},
            {'generation': True},
            {'width_mhz': True},
            {'freq_mhz': True},
        ):
            self.assertIsNone(self.read(hold_status(**changes)))

    def test_stale_future_and_revoked_receiver_cannot_renew(self):
        for changes in (
            {'heartbeat_boottime_us': 8500000},
            {'observed_boottime_us': 10000001},
            {'receiver_live': False},
            {'revoked': True},
        ):
            result = self.read(hold_status(**changes))
            self.assertIsNotNone(result)
            self.assertFalse(result['live'])

    def test_radar_requires_explicit_latch_and_valid_types(self):
        raw = dict(
            schema=2,
            radar_cause='main-radar',
            radar_epoch='d' * 32,
            radar_sequence=2,
            radar_generation=7,
            radar_boottime_us=9,
            radar_freq_mhz=5260,
        )
        self.assertEqual(
            runtime.kernel_radar_event(raw), ('d' * 32, 7, 2, 5260)
        )
        for changes in (
            {'schema': True},
            {'radar_sequence': True},
            {'radar_cause': 'cancelled'},
            {'radar_freq_mhz': 5200},
        ):
            self.assertIsNone(runtime.kernel_radar_event(dict(raw, **changes)))

    def test_hold_without_handoff_capability_never_forms_dfs_plan(self):
        view = dfs_view()
        view[THIRD]['dfs_capable'] = False
        with self.assertRaises(protocol.AgreementError):
            dfs_plan(view)


if __name__ == '__main__':
    unittest.main()
