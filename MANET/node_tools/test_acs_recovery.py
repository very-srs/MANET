"""Peer CSA adoption, proposal admission and ordinary ACS audit regressions."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import errno
import json
import os
import time
import unittest
from unittest.mock import patch

import manet_acs_agreement as p
import test_acs_agreement as fixtures
from test_acs import AcsHarness, OWN, PEER, THIRD, RADIO, node, runtime


class RecoveryTests(AcsHarness):
    def setUp(self):
        super().setUp()
        self.configure(2437, None)
        self.now = int(time.time()) // 180 * 180 + 45
        self.env['TEST_NOW'] = str(self.now)
        (self.root / 'manet-acs-request.json').write_text(json.dumps({'round': self.now // 180}))
        self.reports([[{'channel': 2437, 'noise_floor': -95,
                       'busy_pct': 80, 'foreign_pct': 80},
                       {'channel': 2462, 'noise_floor': -95, 'busy_pct': 10}]], [OWN])
        with self.registry.open('a') as out:
            out.write(node(OWN) + node(PEER, aliases=RADIO) + node(THIRD))
        (self.root / 'peers').write_text(json.dumps([{'orig_address': RADIO}, {'orig_address': THIRD}]))
        self.command('alfred', "Path(os.environ['TEST_ROOT'], 'sent-' + sys.argv[-1]).write_text(sys.stdin.read())")
        self.environment = patch.dict(os.environ, self.env)
        self.environment.start(); self.addCleanup(self.environment.stop)
        self.elapsed = 10000.5
        for name in ('monotonic', 'clock_gettime'):
            clock = patch.object(runtime.time, name, side_effect=lambda *args: self.elapsed)
            clock.start(); self.addCleanup(clock.stop)
        self.runner = runtime.Runtime()
        self.records = {PEER: {'status': fixtures.status('b')}, THIRD: {'status': fixtures.status('c')}}

    tick = fixtures.RuntimeTests.tick

    def destination(self, moving=True):
        members = fixtures.view()
        if not moving:
            for s in members.values():
                s['current']['2.4'] = 2462
        plan = p.make_plan(members, {'2.4': 2462}, False, self.now, 'e' * 32)
        return dict(plan=plan, commit=dict(plan=p.digest(plan),
                    approvals={OWN: 'a' * 32, PEER: 'b' * 32},
                    issued_at=plan['deadline']))

    def landed_readback(self, args, **kwargs):
        if args[:2] == ['iw', 'dev'] and args[-1] == 'info':
            return 'wiphy 0\nchannel 11 (2462 MHz)'
        return self.command_before_patch(args, **kwargs)

    def test_landed_follower_persists_config_and_hold_once_without_csa(self):
        for searching in (False, True):
            with self.subTest(searching=searching):
                self.configure(2437, None)
                self.runner.state = dict(clock_boot=self.runner.boot, timer_boot=self.runner.boot)
                self.runner.discovery.set_mode('search' if searching else 'data')
                destination = self.destination()
                records = {PEER: dict(status=fixtures.status('b', ready=False,
                    current={'2.4': 2462}), destination=destination)}
                self.command_before_patch = runtime.command
                journal = StringIO()
                with patch.object(runtime, 'command', side_effect=self.landed_readback), redirect_stdout(journal):
                    when = destination['plan']['activate_at'] + 20
                    self.tick(when, records)
                    self.assertEqual(self.runner.interfaces()['2.4'][2], 2462)
                    self.assertEqual(self.runner.state['recovered_round'], destination['plan']['round'])
                    held = self.runner.state['hold_until']
                    self.assertEqual(held, self.elapsed + p.RECOVERY_SECONDS)
                    self.assertFalse(self.runner.status(when)['ready'])
                    self.runner = runtime.Runtime()
                    for delay in (31, 121, p.RECOVERY_SECONDS + 1):
                        self.tick(when + delay, records)
                        self.assertEqual(self.runner.state['hold_until'], held)
                    self.assertEqual(self.runner.interfaces()['2.4'][2], 2462)
                commands = (self.root / 'commands').read_text()
                self.assertNotIn('switch freq', commands)
                self.assertNotIn('reconfigure', commands)
                self.assertEqual(journal.getvalue().count('ACS adoption '), 1)
                self.assertIn(p.digest(destination['plan']), journal.getvalue())

    def test_unchanged_destination_adoption_has_no_hold(self):
        self.configure(2462, None)
        destination = self.destination(moving=False)
        self.tick(destination['plan']['activate_at'] + 20,
                  {PEER: dict(status=fixtures.status('b', current={'2.4': 2462}),
                              destination=destination)})
        self.assertEqual(self.runner.state['destination'], destination)
        self.assertNotIn('hold_until', self.runner.state)

    def test_readiness_waits_inside_window_without_scoring_or_shrinking_membership(self):
        for r in self.records.values():
            r['status']['ready'] = False
        with patch.object(self.runner, 'score', wraps=self.runner.score) as score:
            self.tick(self.now)
            score.assert_not_called()
            self.assertNotIn('plan', self.runner.state['protocol'])
            self.records[PEER]['status']['ready'] = True
            self.tick(self.now + 2)
            self.assertEqual(set(self.runner.state['protocol']['plan']['participants']), {OWN, PEER, THIRD})
            self.assertEqual(p.majority(self.runner.state['protocol']['plan']), 2)

    def test_incompatible_majority_does_not_propose_or_advance_streak(self):
        for r in self.records.values():
            r['status']['allowed']['2.4'] = [2437]
        original = {'2.4': dict(incumbent=2437, round=self.now // 180 - 1, count=2)}
        self.runner.state.update(clock_boot=self.runner.boot, challenge=deepcopy(original))
        self.tick(self.now)
        self.assertNotIn('plan', self.runner.state['protocol'])
        self.assertEqual(self.runner.state['challenge'], original)

    def test_incompatible_coordinator_cannot_use_other_nodes_majority(self):
        # The plan requires its coordinator's vote even if two others support it.
        own_status = self.runner.status(self.now)
        own_status['allowed']['2.4'] = [2437]
        self.runner.state.update(clock_boot=self.runner.boot)
        with patch.object(self.runner, 'status', return_value=own_status):
            self.tick(self.now)
        self.assertNotIn('plan', self.runner.state['protocol'])
        self.assertEqual(self.runner.state.get('challenge', {}), {})

    def test_busy_dfs_read_still_publishes_fresh_ordinary_status(self):
        self.configure(2437, 5200)
        (self.root / 'manet-dfs-recheck').touch()
        with patch.object(runtime.dfs, 'hold_snapshot', side_effect=BlockingIOError(errno.EBUSY, 'busy')):
            self.tick(self.now)
        self.assertTrue((self.root / 'sent-74').exists())
        local = self.runner.last_status
        self.assertTrue(local['ready'])
        self.assertEqual(local['current'], {'2.4': 2437, '5': 5200})
        self.assertEqual(local['dfs_reason'], 'hold-unavailable')
        self.assertFalse(local['dfs_capable'])
        self.assertEqual(local['dfs_allowed'], [])
        self.assertNotIn('dfs_hold', local)
        self.assertTrue((self.root / 'manet-dfs-recheck').exists())

    def test_ordinary_transitions_log_once_with_quorum_and_reason(self):
        journal = StringIO()
        with redirect_stdout(journal):
            self.tick(self.now)
            plan = self.runner.state['protocol']['plan']
            self.tick(self.now + 1)
            self.tick(plan['deadline'])
            self.tick(plan['deadline'] + 1)
        events = [json.loads(line.removeprefix('ACS transition '))
                  for line in journal.getvalue().splitlines() if line.startswith('ACS transition ')]
        self.assertEqual([e['phase'] for e in events], ['prepared', 'expired'])
        self.assertEqual(events[-1]['reason'], 'no-majority')
        self.assertEqual((events[-1]['votes'], events[-1]['required'], events[-1]['participants']), (1, 2, 3))
        self.assertEqual(events[-1]['proposer'], OWN)
        self.assertIn('readiness', events[-1])
        self.assertIn('hold_seconds', events[-1])

    def test_busy_dfs_commit_publishes_without_activation_or_lease_reuse(self):
        from test_acs_dfs import dfs_view
        self.configure(None, 5200)
        view = dfs_view(self.now)
        plan = p.make_plan(view, {'5': 5260}, False, self.now, 'f' * 32,
                           fallback={'5': 5200})
        saved = dict(round=plan['round'], phase='committed', plan=plan,
                     local=p.snapshot(view[OWN]), votes=plan['participants'],
                     commit=dict(plan=p.digest(plan), approvals=plan['participants'],
                                 issued_at=plan['deadline']))
        self.runner.state.update(clock_boot=self.runner.boot, protocol=saved)
        records = {m: {'status': s} for m, s in view.items() if m != OWN}
        with patch.object(runtime.dfs, 'hold_snapshot', side_effect=BlockingIOError(errno.EBUSY, 'busy')), \
                patch.object(self.runner, 'apply') as apply:
            self.tick(plan['activate_at'], records)
            self.assertEqual(self.runner.state['protocol'], saved)
            self.assertNotIn('dfs_hold', self.runner.last_status)
            self.assertEqual(self.runner.last_status['dfs_allowed'], [])
            self.assertIsNone(json.loads((self.root / 'manet-dfs-hold.json').read_text()))
            self.assertEqual(self.runner.state['last_outgoing'], {})
            self.tick(plan['activate_at'] + p.APPLY_GRACE + 1, records)
            self.assertEqual(self.runner.state['protocol']['phase'], 'aborted')
            apply.assert_not_called()


class LandedCommitTests(unittest.TestCase):
    def setUp(self):
        self.view = fixtures.view()
        self.plan = fixtures.proposal(self.view)
        self.saved = dict(round=self.plan['round'], phase='committed', plan=self.plan,
            local=p.snapshot(self.view[PEER]), votes={PEER: 'b' * 32},
            commit=dict(plan=p.digest(self.plan), approvals={OWN: 'a' * 32, PEER: 'b' * 32},
                        issued_at=self.plan['deadline']))

    def test_committed_peer_csa_landing_applies_in_window(self):
        self.view[PEER] = fixtures.status('b', current={'2.4': 2462})
        state, _, apply = p.advance(self.saved, PEER, self.plan['activate_at'] + 1, self.view, {})
        self.assertEqual(state['phase'], 'attempted')
        self.assertEqual(apply, self.plan)

    def test_landing_does_not_excuse_reboot_bad_commit_wrong_radio_or_time(self):
        for cause in ('reboot', 'bad-commit', 'wrong-radio', 'early', 'late', 'unplanned-band'):
            with self.subTest(cause=cause):
                saved, view = deepcopy(self.saved), deepcopy(self.view)
                local = view[PEER] = fixtures.status('b', current={'2.4': 2462})
                now = self.plan['activate_at'] + 1
                if cause == 'reboot':
                    local['boot'] = 'f' * 32
                elif cause == 'bad-commit':
                    saved['commit']['approvals'] = {OWN: 'a' * 32}
                elif cause == 'wrong-radio':
                    local['current']['2.4'] = 2412
                elif cause == 'early':
                    now = self.plan['activate_at'] - 1
                elif cause == 'late':
                    now = self.plan['activate_at'] + p.APPLY_GRACE + 1
                else:
                    saved['local']['current']['5'] = 5200
                    local['current']['5'] = 5220
                    local['allowed']['5'] = sorted(p.CHANNELS['5'])
                state, _, apply = p.advance(saved, PEER, now, view, {})
                self.assertEqual(state['phase'], 'expired')
                self.assertIsNone(apply)
                self.assertTrue(state.get('expiry_reason'))

    def test_expiry_reasons_cover_membership_snapshot_votes_and_commit_deadline(self):
        for cause, reason in [('members', 'membership-expanded'), ('snapshot', 'local-snapshot-changed'),
                              ('votes', 'no-majority'), ('commit', 'missing-commit'), ('late', 'activation-missed')]:
            with self.subTest(cause=cause):
                saved, view = deepcopy(self.saved), deepcopy(self.view)
                now, own = self.plan['activate_at'], PEER
                if cause != 'late':
                    saved.pop('commit')
                    saved['phase'] = 'prepared'
                if cause == 'members':
                    view['02:00:00:00:00:04'] = fixtures.status('d')
                elif cause == 'snapshot':
                    view[PEER] = fixtures.status('c')
                elif cause == 'votes':
                    own, now = OWN, self.plan['deadline']
                    saved['local'] = p.snapshot(view[OWN])
                    saved['votes'] = {OWN: 'a' * 32}
                elif cause == 'late':
                    now += p.APPLY_GRACE + 1
                state, _, apply = p.advance(saved, own, now, view, {})
                self.assertIsNone(apply)
                self.assertEqual(state.get('expiry_reason'), reason)
