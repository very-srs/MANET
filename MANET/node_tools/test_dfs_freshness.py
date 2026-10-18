"""Clearance delivery deadlines without extending authenticated leases."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import manet_acs_agreement as p
import test_acs_dfs as f
from test_acs import OWN, PEER, THIRD, runtime


class ClearanceTests(unittest.TestCase):
    def setUp(self):
        self.plan = f.dfs_plan()
        self.saved, _, _ = p.advance({}, PEER, f.NOW, f.dfs_view(),
                                     {OWN: {'proposal': self.plan}})

    def stale_view(self, now):
        view = f.dfs_view(now)
        view[OWN] = f.dfs_status('a', now - p.DFS_LEASE_SECONDS)
        return view

    def test_expired_peer_defers_commit_then_accepts_fresh_authenticated_status(self):
        now = self.plan['deadline']
        view = self.stale_view(now)
        records = {OWN: {'commit': f.committed(self.plan)}}
        saved, outgoing, due = p.advance(self.saved, PEER, now, view, records)
        self.assertEqual(saved['phase'], 'prepared')
        self.assertEqual(outgoing, {})
        self.assertIsNone(due)
        self.assertIsNone(p.live_hold(view[OWN], now))
        self.assertNotIn('commit', saved)
        saved, outgoing, due = p.advance(saved, PEER, now + 1,
                                         f.dfs_view(now + 1), records)
        self.assertEqual(saved['phase'], 'committed')
        self.assertIn('commit', outgoing)
        self.assertIsNone(due)

    def test_wait_cannot_slide_with_new_expired_records_or_survive_restart(self):
        now = self.plan['deadline']
        saved, _, _ = p.advance(self.saved, PEER, now, self.stale_view(now), {})
        for delta in range(1, 6):
            # Reconstruct persisted state as a restarted daemon would.
            saved, outgoing, due = p.advance(deepcopy(saved), PEER, now + delta,
                                              self.stale_view(now + delta), {})
            self.assertIsNone(due)
            self.assertEqual(saved['phase'], 'prepared' if delta < 5 else 'aborted')
        self.assertTrue(p.valid_dfs_abort(outgoing['abort'], self.plan, PEER))

    def test_activation_wait_never_uses_expired_clearance_or_extends_apply_window(self):
        saved = dict(self.saved, phase='committed', commit=f.committed(self.plan))
        now = self.plan['activate_at'] + p.APPLY_GRACE - 1
        view = self.stale_view(now)
        with self.assertRaises(p.AgreementError):
            p.require_dfs_activation(self.plan, view, now)
        held, wire, due = p.advance(saved, PEER, now, view, {})
        self.assertEqual(held['phase'], 'committed')
        self.assertEqual(wire, {})
        self.assertIsNone(due)
        at = now + 1
        self.assertEqual(p.advance(held, PEER, at, f.dfs_view(at), {})[2], self.plan)
        late = now + 2
        self.assertIsNone(p.advance(held, PEER, late, f.dfs_view(late), {})[2])
        self.assertEqual(p.advance(held, PEER, late, self.stale_view(late), {})[0]['phase'], 'aborted')

    def test_real_loss_and_radar_never_wait_for_refresh(self):
        now = self.plan['deadline']
        for cause in ('local-expired', 'revoked', 'generation', 'boot', 'future',
                      'capability', 'missing', 'member', 'radar'):
            with self.subTest(cause=cause):
                view = self.stale_view(now)
                records = {}
                if cause == 'local-expired':
                    view[PEER] = f.dfs_status('b', now - p.DFS_LEASE_SECONDS)
                elif cause == 'revoked':
                    view[OWN]['dfs_hold']['live'] = False
                elif cause == 'generation':
                    view[OWN]['dfs_hold']['generation'] += 1
                elif cause == 'boot':
                    view[OWN]['boot'] = 'f' * 32
                elif cause == 'future':
                    view[OWN] = f.dfs_status('a', now + 1)
                elif cause == 'capability':
                    view[OWN]['dfs_capable'] = False
                elif cause == 'missing':
                    view[OWN] = None
                elif cause == 'member':
                    del view[THIRD]
                else:
                    records[THIRD] = {'abort': p.dfs_abort(self.plan, THIRD, 'radar')}
                saved, _, due = p.advance(self.saved, PEER, now, view, records)
                self.assertEqual(saved['phase'], 'aborted')
                self.assertIsNone(due)

    def test_fast_passes_cover_preparation_and_entire_vote_window(self):
        runner = runtime.Runtime.__new__(runtime.Runtime)
        with patch.object(runtime, 'clock_ready', return_value=True):
            runner.state = {'dfs_prepare': {'expires': f.NOW + 1200}}
            self.assertLessEqual(runner.interval(f.NOW + 30), 1)
            for phase in ('prepared', 'committed'):
                runner.state = {'protocol': dict(self.saved, phase=phase)}
                for at in range(f.NOW + 1, self.plan['activate_at'] + p.APPLY_GRACE + 1):
                    self.assertLessEqual(runner.interval(at), 1, (phase, at))

    def test_activation_reads_new_peer_clearance_instead_of_pass_start_cache(self):
        runner = runtime.Runtime.__new__(runtime.Runtime)
        runner.boot, runner.own = 'b' * 32, PEER
        from pathlib import Path
        import tempfile
        now = self.plan['activate_at']
        fresh = {m: {'status': s} for m, s in f.dfs_view(now).items()}
        stale = {m: {'status': s} for m, s in self.stale_view(now).items()}
        with tempfile.TemporaryDirectory() as directory:
            runner.run_dir = Path(directory)
            with patch.object(runner, 'receive', return_value=fresh) as receive, \
                    patch.object(runner, 'status', return_value=fresh[PEER]['status']), \
                    patch.object(runner, 'members', return_value=list(fresh)), \
                    patch.object(runtime.time, 'time', return_value=now):
                runner.check_dfs_activation(self.plan, stale)
                receive.assert_called_with(74, now)
                fresh[THIRD]['dfs_abort'] = p.dfs_abort(self.plan, THIRD, 'radar')
                with self.assertRaisesRegex(p.AgreementError, 'abort received'):
                    runner.check_dfs_activation(self.plan, stale)


if __name__ == '__main__':
    unittest.main()
