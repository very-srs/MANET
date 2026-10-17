"""Exercise the host's real reservation policy and CAC preparation together."""
import errno
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'MANET/node_tools'))
import manet_bgobs
import manet_rx_policy
from test_acs_dfs import NOW, dfs_view

spec = importlib.util.spec_from_file_location(
    'background_cac_runtime', ROOT / 'MANET/node_tools/mesh-channel-agreement.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class HostSequenceTests(unittest.TestCase):
    def test_reserve_before_cac_and_release_after_both_owners(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            observer = root / 'phy7/mt76/background_scan'
            observer.parent.mkdir(parents=True)
            transition = observer.with_name('rx_transition_probe')
            hold = observer.parent.parent / 'background_hold'
            status = dict(schema=1, reserved=False, failed=False, chainmask=28,
                          transitions=0, last_boottime_us=0)
            ownership = dict(schema=2, owner=False, enabled=False, main_active=False)
            transition.write_text(json.dumps(status))
            hold.write_text(json.dumps(ownership))
            view = dfs_view()
            for member in view.values():
                member['dfs_candidates'] = [5260]
            prep = dict(leader=min(view), participants={m: s['boot'] for m, s in view.items()},
                        frequency=5260, fallback={'5': 5200}, created=NOW,
                        expires=NOW + 1200, nonce='f' * 32)
            runner = object.__new__(runtime.Runtime)
            runner.state, runner.run_dir, runner.save = {}, root, Mock()
            runner.interfaces = lambda: {'5': ('mesh5', root / 'wpa', 5200)}
            events = []
            error = None

            def write(path, value):
                events.append(value)
                if path == transition:
                    if error:
                        raise OSError(error, 'reservation deferred')
                    status.update(reserved=value == '2', chainmask=12 if value == '2' else 28)
                    transition.write_text(json.dumps(status))
                else:
                    self.assertEqual((path, value), (hold, 'enable'))
                    self.assertTrue(status['reserved'])
                    ownership['enabled'] = True
                    hold.write_text(json.dumps(ownership))

            def start(iface, frequency):
                self.assertEqual((iface, frequency), ('mesh5', 5260))
                self.assertTrue(status['reserved'] and ownership['enabled'])
                events.append('cac')
                ownership.update(owner=True, freq_mhz=frequency)
                hold.write_text(json.dumps(ownership))

            with patch.object(manet_rx_policy, 'cm4_enabled', return_value=True), \
                    patch.object(manet_rx_policy, 'write_command', side_effect=write), \
                    patch.object(manet_bgobs, 'write_command', side_effect=write), \
                    patch.object(manet_bgobs, 'observer_path', return_value=observer), \
                    patch.object(runtime.dfs, 'start_cac', side_effect=start):
                for error in (errno.EAGAIN, errno.EBUSY):
                    events.clear()
                    runner.prepare_dfs(view, {min(view): {'dfs_prepare': prep}}, NOW)
                    self.assertEqual(events, ['2'])
                    self.assertFalse(ownership['enabled'] or ownership['owner'])
                error = None
                status['failed'] = True
                transition.write_text(json.dumps(status))
                events.clear()
                with self.assertRaisesRegex(ValueError, 'invalid RX transition'):
                    runner.prepare_dfs(view, {min(view): {'dfs_prepare': prep}}, NOW)
                self.assertEqual(events, [])
                status['failed'] = False
                transition.write_text(json.dumps(status))
                runner.prepare_dfs(view, {min(view): {'dfs_prepare': prep}}, NOW)
                self.assertEqual(events, ['2', 'enable', 'cac'])
                events.clear()
                runner.prepare_dfs(view, {min(view): {'dfs_prepare': prep}}, NOW + 1)
                self.assertEqual(events, [])
                self.assertEqual(manet_rx_policy.reconcile_receive_paths(observer, False), 'reserved')
                ownership.update(owner=False, main_active=True)
                hold.write_text(json.dumps(ownership))
                self.assertEqual(manet_rx_policy.reconcile_receive_paths(observer, False), 'reserved')
                self.assertEqual(events, [])
                ownership.update(owner=True, main_active=False)
                hold.write_text(json.dumps(ownership))
                self.assertEqual(manet_rx_policy.reconcile_receive_paths(observer, False), 'reserved')
                ownership['owner'] = False
                hold.write_text(json.dumps(ownership))
                self.assertEqual(manet_rx_policy.reconcile_receive_paths(observer, False), '3x3')
                self.assertEqual(events, ['3'])


if __name__ == '__main__':
    unittest.main()
