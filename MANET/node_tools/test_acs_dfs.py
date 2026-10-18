"""DFS lease, authentication, radar ordering and capability-gated activation tests."""
from copy import deepcopy
import base64
import json
import os
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import NodeInfo_pb2
import encoder
import manet_acs_agreement as p
from test_acs import AcsHarness, OWN, PEER, THIRD, RADIO, TOOLS, node, runtime
from test_acs_agreement import status

NOW = 945


def kernel_hold():
    checked = int(runtime.boot_time() * 1000000)
    return dict(schema=2, enabled=True, generation=7, epoch='d' * 32,
                owner=True, fault=False, live=True, freq_mhz=5260,
                center_mhz=5260, width_mhz=20, receiver_live=True,
                revoked=False, observed_boottime_us=checked,
                heartbeat_boottime_us=checked, heartbeat_max_age_us=1500000)


def dfs_status(boot='a', now=NOW):
    """Create authenticated status for a node holding one DFS candidate."""
    result = status(boot, current={'5': 5200})
    result.update(dfs_versions=[1], dfs_capable=True, dfs_allowed=[5260], dfs_hold=dict(
        version=1, frequency=5260, width=20, center=5260, state='held',
        boot=boot * 32, epoch='d' * 32, generation=7, live=True,
        observed_at=now, lease_until=now + p.DFS_LEASE_SECONDS))
    return result


def dfs_view(now=NOW):
    """Use three nodes, including the member reachable only through HaLow."""
    return {m: dfs_status(b, now) for m, b in
            ((OWN, 'a'), (PEER, 'b'), (THIRD, 'c'))}


def dfs_plan(view=None):
    """Freeze a unanimous proposal and its common non-DFS fallback."""
    return p.make_plan(view or dfs_view(), {'5': 5260}, False, NOW,
                       'e' * 32, fallback={'5': 5200})


def committed(plan):
    """Make the authenticated coordinator's unanimous certificate."""
    return dict(plan=p.digest(plan), approvals=plan['participants'],
                issued_at=plan['deadline'])


class DfsProtocolTests(unittest.TestCase):
    """Exercise DFS state changes without RF or authenticated I/O mocks."""

    def test_every_member_needs_version_radio_and_live_hold(self):
        mutations = [None, status('c'),
                     dict(dfs_status('c'), dfs_versions=[2]),
                     dict(dfs_status('c'), dfs_versions=[True]),
                     dict(dfs_status('c'), current={}, allowed={}),
                     dict(dfs_status('c'), dfs_hold=None)]
        for missing in mutations:
            with self.subTest(missing=missing):
                view = dfs_view()
                view[THIRD] = missing
                with self.assertRaises(p.AgreementError):
                    dfs_plan(view)
        # Legacy non-DFS majority behavior retains its original wire shape.
        view = {OWN: status('a'), PEER: status('b'), THIRD: None}
        plan = p.make_plan(view, {'2.4': 2462}, False, NOW, 'e' * 32)
        self.assertNotIn('dfs', plan)
        self.assertEqual(p.majority(plan), 2)

    def test_fallback_is_mandatory_non_dfs_and_supported_by_every_node(self):
        for fallback in (None, {}, {'5': 5260}, {'5': 5180}):
            with self.assertRaises(p.AgreementError):
                p.make_plan(dfs_view(), {'5': 5260}, False, NOW, 'e' * 32,
                            fallback=fallback)
        view = dfs_view()
        view[THIRD]['allowed']['5'] = [5220]
        with self.assertRaises(p.AgreementError):
            dfs_plan(view)
        self.assertEqual(p.dfs_fallback(view), {'5': 5220})

    def test_one_missing_vote_prevents_commit_even_with_fresh_clearances(self):
        plan = dfs_plan()
        saved, _, _ = p.advance({}, OWN, NOW, dfs_view(), {}, plan)
        vote = {'vote': {'plan': p.digest(plan), 'boot': 'b' * 32}}
        saved, _, _ = p.advance(saved, OWN, NOW + 5, dfs_view(NOW + 5),
                                {PEER: vote})
        saved, outgoing, due = p.advance(
            saved, OWN, plan['deadline'], dfs_view(plan['deadline']), {})
        self.assertEqual(saved['phase'], 'expired')
        self.assertFalse(outgoing)
        self.assertIsNone(due)
        bad = committed(plan)
        bad['approvals'] = {OWN: 'a' * 32, PEER: 'b' * 32}
        self.assertFalse(p.valid_commit(bad, plan))

    def test_unanimous_vote_renews_leases_until_due_and_activates(self):
        plan = dfs_plan()
        states, wire = {}, {}
        for mac in dfs_view():
            states[mac], wire[mac], _ = p.advance(
                {}, mac, NOW, dfs_view(), {OWN: {'proposal': plan}}, plan)
        states[OWN], _, _ = p.advance(
            states[OWN], OWN, NOW + 5, dfs_view(NOW + 5), wire)
        states[OWN], outgoing, _ = p.advance(
            states[OWN], OWN, plan['deadline'], dfs_view(plan['deadline']), {})
        self.assertTrue(p.valid_commit(outgoing['commit'], plan))
        for mac in (PEER, THIRD):
            states[mac], _, _ = p.advance(
                states[mac], mac, plan['deadline'] + 1,
                dfs_view(plan['deadline'] + 1), {OWN: outgoing})
        for mac in states:
            _, _, due = p.advance(states[mac], mac, plan['activate_at'],
                                   dfs_view(plan['activate_at']), {})
            self.assertEqual(due, plan)
        p.require_dfs_activation(plan, dfs_view(plan['activate_at']),
                                 plan['activate_at'])

    def test_lost_commit_cannot_be_recovered_from_cached_dfs_destination(self):
        plan = dfs_plan()
        saved, _, _ = p.advance({}, PEER, NOW, dfs_view(),
                                {OWN: {'proposal': plan}})
        _, _, due = p.advance(saved, PEER, plan['activate_at'],
                              dfs_view(plan['activate_at']), {})
        self.assertIsNone(due)
        local = dfs_status(now=plan['activate_at'])
        local['current']['5'] = 5260
        self.assertIsNone(p.live_destination(
            {'plan': plan, 'commit': committed(plan)}, local,
            plan['activate_at']))

    def test_membership_stale_lease_and_receiver_reset_abort_commit(self):
        plan = dfs_plan()
        saved, _, _ = p.advance({}, PEER, NOW, dfs_view(),
                                {OWN: {'proposal': plan}})
        saved, _, _ = p.advance(saved, PEER, plan['deadline'],
                                dfs_view(plan['deadline']),
                                {OWN: {'commit': committed(plan)}})
        at = plan['activate_at']
        views = [None, dfs_view(), dfs_view(at), dfs_view(at),
                 dfs_view(at), dfs_view(at), dfs_view(at)]
        views[2]['02:00:00:00:00:04'] = dfs_status('f', at)
        del views[3][THIRD]
        views[4][THIRD]['dfs_hold']['generation'] += 1
        views[5][THIRD]['dfs_hold']['epoch'] = 'f' * 32
        views[6][THIRD]['boot'] = 'f' * 32
        for view in views:
            state, _, due = p.advance(saved, PEER, at, view, {},
                                      local=dfs_status('b', at))
            self.assertEqual(state['phase'], 'aborted')
            self.assertIsNone(due)

    def test_lease_is_bounded_and_duplicate_status_does_not_renew_it(self):
        local = dfs_status()
        self.assertIsNotNone(p.live_hold(local, NOW + 14))
        self.assertIsNone(p.live_hold(local, NOW + 15))
        for field, value in [('lease_until', NOW + 16),
                             ('observed_at', NOW + 1), ('live', False),
                             ('state', 'cac'), ('generation', 0),
                             ('width', 40), ('version', 2)]:
            changed = deepcopy(local)
            changed['dfs_hold'][field] = value
            self.assertIsNone(p.live_hold(changed, NOW))

    def test_readiness_withdrawal_and_local_channel_change_cancel_promise(self):
        plan = dfs_plan()
        for flag, value in (('ready', False), ('stable', False),
                            ('discovery', True)):
            view = dfs_view()
            view[THIRD][flag] = value
            self.assertFalse(p.dfs_ready(plan, view, NOW))
        saved, _, _ = p.advance({}, PEER, NOW, dfs_view(),
                                {OWN: {'proposal': plan}})
        view = dfs_view(NOW + 1)
        view[PEER]['current']['5'] = 5220
        updated, outgoing, due = p.advance(
            saved, PEER, NOW + 1, view, {})
        self.assertEqual(updated['phase'], 'aborted')
        self.assertIsNone(due)
        self.assertTrue(p.valid_dfs_abort(outgoing['abort'], plan, PEER))

    def test_radar_wins_during_vote_and_activation_despite_late_finished(self):
        plan = dfs_plan()
        initial, _, _ = p.advance({}, PEER, NOW, dfs_view(),
                                  {OWN: {'proposal': plan}})
        for when in (NOW + 5, plan['activate_at']):
            saved = deepcopy(initial)
            if when == plan['activate_at']:
                saved.update(commit=committed(plan), phase='committed')
            abort = p.dfs_abort(plan, THIRD, 'radar')
            state, _, due = p.advance(saved, PEER, when, dfs_view(when),
                                      {THIRD: {'abort': abort}})
            self.assertEqual(state['phase'], 'aborted')
            self.assertIsNone(due)
            # A delayed FINISHED/status refresh cannot reopen this round.
            self.assertIsNone(p.advance(
                state, PEER, when + 1, dfs_view(when + 1),
                {OWN: {'proposal': plan, 'commit': committed(plan)}})[2])

    def test_abort_is_bound_to_sender_boot_epoch_generation_and_plan(self):
        plan = dfs_plan()
        abort = p.dfs_abort(plan, THIRD, 'radar')
        self.assertTrue(p.valid_dfs_abort(abort, plan, THIRD))
        self.assertFalse(p.valid_dfs_abort(abort, plan, PEER))
        for field, value in [('boot', 'f' * 32), ('epoch', 'f' * 32),
                             ('generation', 8), ('plan', 'f' * 64),
                             ('version', 2)]:
            self.assertFalse(p.valid_dfs_abort(dict(abort, **{field: value}),
                                               plan, THIRD))
        changed = deepcopy(plan)
        changed['dfs']['fallback']['5'] = 5220
        self.assertFalse(p.valid_dfs_abort(abort, changed, THIRD))


class DfsRuntimeTests(AcsHarness):
    """Exercise persistence, encrypted Alfred records and radio commands."""

    def setUp(self):
        """Keep all radio and transport operations inside temporary fixtures."""
        super().setUp()
        self.configure(2437, 5200)
        environment = patch.dict(os.environ, self.env)
        environment.start()
        self.addCleanup(environment.stop)
        self.command('ip', """
with open(Path(os.environ['TEST_ROOT'], 'commands'), 'a') as log:
    log.write('ip ' + ' '.join(sys.argv[1:]) + '\\n')
""")
        self.command('alfred', """
if sys.argv[1] == '-r':
    print('')
else:
    Path(os.environ['TEST_ROOT'], 'sent-' + sys.argv[-1]).write_text(
        sys.stdin.read())
""")
        self.runner = runtime.Runtime()
        self.plan = dfs_plan()
        saved, _, _ = p.advance({}, OWN, NOW, dfs_view(), {}, self.plan)
        self.runner.state['protocol'] = saved
        self.runner.state['clock_boot'] = self.runner.boot
        self.runner.save()

    def radar(self):
        """Deliver the generation actually bound into this plan."""
        self.runner.radar_detected(p.digest(self.plan), 'd' * 32, 7)

    def test_kernel_boundary_missing_malformed_fault_cac_and_live(self):
        path = self.root / 'background_hold'
        self.assertIsNone(runtime.read_background_hold(
            path, 'a' * 32, 'd' * 32, NOW))
        raw = kernel_hold()
        for changes, expected in [({}, 'held'), ({'live': False}, 'cac'),
                                  ({'owner': False}, 'revoked'),
                                  ({'fault': True}, 'fault')]:
            path.write_text(json.dumps(dict(raw, **changes)))
            hold = runtime.read_background_hold(path, 'a' * 32, 'd' * 32, NOW)
            self.assertEqual(hold['state'], expected)
            self.assertEqual(hold['live'], expected == 'held')
        for changes in ({'schema': 1}, {'live': 'true'},
                        {'generation': True}, {'freq_mhz': 5200}):
            path.write_text(json.dumps(dict(raw, **changes)))
            self.assertIsNone(runtime.read_background_hold(
                path, 'a' * 32, 'd' * 32, NOW))

    def test_radar_stops_before_lock_or_network_and_survives_restart(self):
        self.configure(2437, 5260)
        with self.runner.channel_lock():
            self.radar()  # Event entry must not acquire the held ACS lock.
        commands = (self.root / 'commands').read_text().splitlines()
        self.assertEqual(commands[:2], [
            'wpa_cli -i wlan1 raw MANET_DFS_STATUS',
            'iw dev wlan1 switch freq 5200 HT20 beacons 3 block-tx'])
        for type_id in (74, 75):
            envelope = json.loads((self.root / f'sent-{type_id}').read_text())
            payload = self.transport.open(type_id, envelope).payload
            self.assertTrue(p.valid_dfs_abort(payload['dfs_abort'],
                                              self.plan, OWN))
        self.runner = runtime.Runtime()
        with self.runner.channel_lock():
            self.runner.process_dfs_aborts({}, int(time.time()))
        config = (self.wpa / 'wpa_supplicant-wlan1.conf').read_text()
        self.assertIn('frequency=5200', config)
        self.assertIn('frequency=2437',
                      (self.wpa / 'wpa_supplicant-wlan0.conf').read_text())
        self.assertFalse(self.runner.state['dfs_radar']['pending'])
        self.assertEqual(self.runner.state['protocol']['phase'], 'aborted')

    def test_peer_radar_over_type75_moves_to_fallback_once(self):
        self.configure(2437, 5260)
        abort = p.dfs_abort(self.plan, THIRD, 'radar')
        helpers = {THIRD: {'dfs_abort': abort}}
        with patch.object(self.runner, 'receive', return_value=helpers):
            self.runner.process_dfs_aborts({}, NOW)
            until = self.runner.state['dfs_cooldown_until']
            commands = (self.root / 'commands').read_text()
            self.runner.process_dfs_aborts({}, NOW + 1)
        self.assertEqual(self.runner.state['dfs_cooldown_until'], until)
        self.assertEqual((self.root / 'commands').read_text(), commands)
        self.assertIn('iw dev wlan1 switch freq 5200 HT20 beacons 3 block-tx', commands)
        self.radar()  # Local replay must also stop before radio commands.
        self.assertEqual((self.root / 'commands').read_text(), commands)

    def test_radar_during_vote_keeps_non_dfs_home_but_cancels_and_cools(self):
        self.radar()
        self.runner.process_dfs_aborts({}, NOW)
        self.assertEqual(self.runner.state['protocol']['phase'], 'aborted')
        self.assertGreater(self.runner.state['dfs_cooldown_until'],
                           runtime.boot_time())
        self.assertNotIn('ip link', (self.root / 'commands').read_text())

    def test_event_arriving_during_poll_survives_cleanup(self):
        def receive(kind, now):
            """Publish after the consumer has claimed its incoming file."""
            self.radar()
            return {}

        with patch.object(self.runner, 'receive', side_effect=receive):
            self.runner.process_dfs_aborts({}, NOW)
        self.assertTrue((self.root / 'manet-dfs-radar.json').exists())
        self.runner.process_dfs_aborts({}, NOW + 1)
        self.assertEqual(self.runner.state['protocol']['phase'], 'aborted')
        self.assertFalse((self.root / 'manet-dfs-radar.json').exists())

    def test_failed_transport_still_exits_and_failed_landing_stays_down(self):
        self.configure(2437, 5260)
        self.command('alfred', 'sys.exit(1)')
        self.radar()
        self.runner.process_dfs_aborts({}, NOW)
        self.assertFalse(self.runner.state['dfs_radar']['pending'])
        self.assertIn('frequency=5200',
                      (self.wpa / 'wpa_supplicant-wlan1.conf').read_text())
        self.configure(2437, 5260)
        self.command('iw', "print('wiphy 0\\nchannel 52 (5260 MHz)')")
        with patch.object(runtime.time, 'sleep'):
            with self.assertRaisesRegex(ValueError, 'did not land'):
                self.runner.radar_fallback(self.plan)
        self.assertEqual((self.root / 'commands').read_text().splitlines()[-1],
                         'ip link set dev wlan1 down')

    def test_wrong_generation_and_peer_loss_cannot_trigger_fallback(self):
        self.configure(2437, 5260)
        with self.assertRaisesRegex(ValueError, 'armed DFS plan'):
            self.runner.radar_detected(p.digest(self.plan), 'd' * 32, 8)
        self.runner.process_dfs_aborts({}, NOW)
        self.assertFalse((self.root / 'commands').exists())
        self.assertIn('frequency=5260',
                      (self.wpa / 'wpa_supplicant-wlan1.conf').read_text())

    def test_peer_loss_cannot_send_dfs_manager_or_discovery_to_lobby(self):
        self.command('quorum', """
Path(os.environ['TEST_ROOT'], 'quorum-called').touch()
sys.exit(1)
""")
        for frequency, expected in ((5260, 0), (5600, 0), (5200, 1)):
            self.configure(2437, frequency)
            (self.root / 'quorum-called').unlink(missing_ok=True)
            result = self.shell(self.definitions('node-manager-acs.sh') + '''
QUORUM_CHECKER="$TEST_ROOT/bin/quorum"
check_mesh_quorum
''')
            self.assertEqual(result.returncode, expected, result.stderr)
            self.assertEqual((self.root / 'quorum-called').exists(),
                             expected == 1)
        local = dict(dfs_status(), discovery=True)
        local['current']['5'] = 5260
        with patch.object(self.runner, 'apply') as apply:
            self.assertFalse(self.runner.discovery_step(NOW, local, False))
            apply.assert_not_called()

    def test_delayed_finished_cannot_restore_radar_generation(self):
        path = self.root / 'background_hold'
        path.write_text(json.dumps(kernel_hold()))
        with patch.dict(os.environ, MANET_DFS_HOLD_PATH=str(path)):
            hold = self.runner.dfs_hold('phy0', NOW)
            # The out-of-lock event also fences a stale live bit before the
            # daemon can acquire the lock and persist its revocation ledger.
            event = dict(boot=self.runner.boot,
                         abort=dict(reason='radar', epoch=hold['epoch'],
                                    generation=7))
            (self.root / 'manet-dfs-radar.json').write_text(json.dumps(event))
            self.assertFalse(self.runner.dfs_hold('phy0', NOW + 1)['live'])
            (self.root / 'manet-dfs-radar.json').unlink()
            self.runner.state['dfs_revoked'] = [[hold['epoch'], 7]]
            later = self.runner.dfs_hold('phy0', NOW + 20)
        self.assertFalse(later['live'])
        self.assertEqual(later['state'], 'revoked')

    def test_raw_apply_and_helper_recovery_cannot_bypass_activation_gate(self):
        with self.assertRaisesRegex(ValueError, 'DFS cannot'):
            self.runner.apply({'5': 5260})
        self.assertFalse((self.root / 'commands').exists())
        self.assertIsNone(runtime.recovery_destination(
            {PEER: {'channels': {'5': 5260}, 'size': 3, 'dfs_versions': [1]}},
            dfs_status(), OWN))

    def test_activation_rechecks_generation_membership_and_pending_radar(self):
        at = self.plan['activate_at']
        records = {m: {'status': s} for m, s in dfs_view(at).items()}
        with self.runner.channel_lock(), \
                patch.object(runtime.time, 'time', return_value=at), \
                patch.object(self.runner, 'receive', return_value=records), \
                patch.object(self.runner, 'members',
                             return_value=list(records)), \
                patch.object(self.runner, 'status',
                             return_value=dfs_status(now=at)) as local:
            self.runner.check_dfs_activation(self.plan, records)
            local.return_value['dfs_hold']['generation'] += 1
            with self.assertRaisesRegex(ValueError, 'clearance changed'):
                self.runner.check_dfs_activation(self.plan, records)
            local.return_value = dfs_status(now=at)
            records[THIRD]['status']['dfs_hold']['lease_until'] = at
            with self.assertRaisesRegex(ValueError, 'clearance changed'):
                self.runner.check_dfs_activation(self.plan, records)
            self.radar()
            with self.assertRaisesRegex(ValueError, 'radar pending'):
                self.runner.check_dfs_activation(self.plan, records)
            (self.root / 'manet-dfs-radar.json').replace(
                self.root / 'manet-dfs-radar.processing.json')
            with self.assertRaisesRegex(ValueError, 'radar pending'):
                self.runner.check_dfs_activation(self.plan, records)

    def test_dfs_scoring_obeys_same_three_round_preference_hold_down(self):
        self.reports([[dict(channel=5200, foreign_pct=45, busy_pct=45,
                            noise_floor=-95),
                       dict(channel=5260, busy_pct=10, noise_floor=-95)]],
                     [OWN])
        for offset, expected in ((0, 5200), (180, 5200), (360, 5260)):
            at = NOW + offset
            with patch.object(runtime.time, 'time', return_value=at):
                channels, _ = self.runner.score(dfs_view(at))
            self.assertEqual(channels, {'5': expected})
        # Hold metadata without a real candidate measurement is not a score.
        self.reports([[dict(channel=5200, foreign_pct=90, busy_pct=90)]],
                     [OWN])
        with patch.object(runtime.time, 'time', return_value=NOW + 540):
            self.assertEqual(self.runner.score(dfs_view(NOW + 540))[0],
                             {'5': 5200})

    def test_receiver_epoch_belongs_to_kernel_across_reader_restart(self):
        path = self.root / 'background_hold'
        raw = kernel_hold()
        path.write_text(json.dumps(raw))
        with patch.dict(os.environ, MANET_DFS_HOLD_PATH=str(path)):
            first = self.runner.dfs_hold('phy0', NOW)
            raw['generation'] = 1
            raw['epoch'] = 'e' * 32
            path.write_text(json.dumps(raw))
            second = self.runner.dfs_hold('phy0', NOW + 1)
            self.assertNotEqual(first['epoch'], second['epoch'])
            restarted = runtime.Runtime().dfs_hold('phy0', NOW + 2)
            self.assertEqual(second['epoch'], restarted['epoch'])

    def test_local_hold_snapshot_expires_before_telemetry_publication(self):
        path = self.root / 'background_hold'
        path.write_text(json.dumps(kernel_hold()))
        with patch.dict(os.environ, MANET_DFS_HOLD_PATH=str(path)):
            local = self.runner.status(NOW)
        args = SimpleNamespace(
            channel_report_json='{}',
            dfs_hold_file=str(self.root / 'manet-dfs-hold.json'))
        for at, present in ((NOW, True), (NOW + 16, False)):
            message = NodeInfo_pb2.NodeTelemetry()
            with patch.object(encoder.time, 'time', return_value=at):
                encoder.add_dfs_hold(message, args)
            self.assertEqual(message.channel_report.HasField('dfs_hold'),
                             present)
            if present:
                self.assertEqual(message.channel_report.dfs_hold.epoch,
                                 local['dfs_hold']['epoch'])

    def test_halow_alias_membership_keeps_node_without_fresh_status(self):
        members = runtime.canonical_members(
            OWN, [RADIO, THIRD], node(PEER, aliases=RADIO) + node(THIRD), {})
        self.assertEqual(set(members), {OWN, PEER, THIRD})
        view = {OWN: dfs_status(), PEER: dfs_status('b'), THIRD: None}
        with self.assertRaises(p.AgreementError):
            dfs_plan(view)

    def test_64_member_contract_fits_encrypted_wire_budget(self):
        view = {f'02:00:00:00:00:{i:02x}': dfs_status()
                for i in range(p.MAX_MEMBERS)}
        for s in view.values():
            s['dfs_hold']['generation'] = 2**64 - 1
        plan = dfs_plan(view)
        payload = dict(kind='acs_state', node=OWN, status=dfs_status(),
                       aliases=[OWN, PEER, THIRD, RADIO],
                       protocol=dict(proposal=plan, commit=committed(plan),
                                     vote=dict(plan=p.digest(plan),
                                               boot='a' * 32)))
        envelope = self.transport.seal(74, payload)
        self.assertEqual(self.transport.open(74, envelope).payload, payload)

    def test_large_dfs_proposal_defers_old_full_certificate(self):
        view = {f'02:00:00:00:00:{i:02x}': dfs_status()
                for i in range(1, p.MAX_MEMBERS + 1)}
        local = dict(view[OWN], stable=True, discovery=False, halow_ready=True)
        old = p.make_plan(view, {'5': 5200}, False, NOW - 180, 'f' * 32)
        self.runner.state['protocol'] = {}
        self.runner.state['destination'] = dict(plan=old, commit=committed(old))
        records = {m: {'status': s} for m, s in view.items()}
        with patch.object(self.runner, 'receive', return_value=records), \
                patch.object(self.runner, 'members', return_value=list(view)), \
                patch.object(self.runner, 'status', return_value=local), \
                patch.object(self.runner, 'score',
                             side_effect=lambda *a, **k: ({'5': 5200 if k.get('fallback') else 5260}, False)), \
                patch.object(runtime.time, 'time', return_value=NOW):
            self.runner.tick(NOW)
        envelope = json.loads((self.root / 'sent-74').read_text())
        payload = self.transport.open(74, envelope).payload
        self.assertIn('dfs', payload['protocol']['proposal'])
        self.assertIn('destination_id', payload)
        self.assertNotIn('destination', payload)

    def test_first_proposal_abort_prevents_vote_on_both_control_types(self):
        for type_id in (74, 75):
            with self.subTest(type_id=type_id):
                self.runner = runtime.Runtime()
                self.runner.state = dict(clock_boot=self.runner.boot,
                                         timer_boot=self.runner.boot)
                self.runner.busy_path.unlink(missing_ok=True)
                view = dfs_view()
                local = dict(view[OWN], stable=True, discovery=False,
                             halow_ready=True)
                states = {m: {'status': s} for m, s in view.items()}
                helpers = {}
                source = states if type_id == 74 else helpers
                source.setdefault(THIRD, {})['dfs_abort'] = p.dfs_abort(
                    self.plan, THIRD, 'radar')

                def receive(kind, now):
                    """Deliver fresh live status together with the abort."""
                    return states if kind == 74 else helpers

                with patch.object(self.runner, 'receive',
                                  side_effect=receive), \
                        patch.object(self.runner, 'members',
                                     return_value=list(view)), \
                        patch.object(self.runner, 'status',
                                     return_value=local), \
                        patch.object(self.runner, 'score',
                                     side_effect=lambda *a, **k: ({'5': 5200 if k.get('fallback') else 5260}, False)), \
                        patch.object(runtime.secrets, 'token_hex',
                                     return_value='e' * 32), \
                        patch.object(runtime.time, 'time', return_value=NOW):
                    self.runner.tick(NOW)
                envelope = json.loads((self.root / 'sent-74').read_text())
                payload = self.transport.open(74, envelope).payload
                self.assertNotIn('vote', payload['protocol'])
                self.assertNotIn('proposal', payload['protocol'])
                self.assertEqual(self.runner.state['protocol']['phase'],
                                 'aborted')

    def test_abort_rejects_tampering_wrong_sender_and_staleness(self):
        now = int(time.time())
        payload = dict(kind='acs_state', node=THIRD,
                       dfs_abort=p.dfs_abort(self.plan, THIRD, 'radar'))
        envelope = self.transport.seal(74, payload)

        def row(sender, envelope):
            """Wrap the encrypted envelope in Alfred's escaped record syntax."""
            return '{"' + sender + '",' + json.dumps(json.dumps(envelope)) + '}'

        self.assertIn(THIRD, runtime.authenticated_records(
            self.transport, 74, row(THIRD, envelope), now))
        self.assertEqual(runtime.authenticated_records(
            self.transport, 74, row(PEER, envelope), now), {})
        self.assertEqual(runtime.authenticated_records(
            self.transport, 74, row(THIRD, envelope), now + 46), {})
        damaged = dict(envelope, ciphertext='A' * 40)
        self.assertEqual(runtime.authenticated_records(
            self.transport, 74, row(THIRD, damaged), now), {})


class DfsTelemetryTests(unittest.TestCase):
    """Verify optional-field presence with the fleet protobuf runtime."""

    def test_cli_roundtrip_and_absence(self):
        for hold in (None, dfs_status()['dfs_hold']):
            report = {'results': []}
            if hold is not None:
                report['dfs_hold'] = hold
            raw = subprocess.check_output([
                sys.executable, str(TOOLS / 'encoder.py'), 'telemetry',
                '--timestamp', str(NOW),
                '--channel-report-json', json.dumps(report)], text=True)
            message = NodeInfo_pb2.NodeTelemetry.FromString(
                base64.b64decode(raw))
            self.assertEqual(message.channel_report.HasField('dfs_hold'),
                             hold is not None)
            decoded = subprocess.check_output([
                sys.executable, str(TOOLS / 'decoder.py'), 'telemetry', raw],
                text=True)
            values = dict(line.split('=', 1) for line in decoded.splitlines())
            value = shlex.split(values['CHANNEL_REPORT_JSON'])[0]
            self.assertEqual(json.loads(value), report)

    def test_unknown_version_is_not_encoded_as_supported(self):
        hold = dict(dfs_status()['dfs_hold'], version=2)
        message = NodeInfo_pb2.NodeTelemetry()
        encoder.add_dfs_hold(message, SimpleNamespace(
            channel_report_json=json.dumps({'dfs_hold': hold})))
        self.assertFalse(message.channel_report.HasField('dfs_hold'))


if __name__ == '__main__':
    unittest.main()
