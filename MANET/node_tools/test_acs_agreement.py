"""Packet-loss, timeout, activation and recovery regressions for ACS."""
from copy import deepcopy
import json
import os
import time
import unittest
from unittest.mock import patch

import manet_acs_agreement as p
from test_acs import AcsHarness, OWN, PEER, THIRD, RADIO, node, runtime

NOW = 945  # :45 in a 180-second round


def status(boot='a', ready=True, current=None, acs=True):
    current = {'2.4': 2437} if current is None else current
    return {'boot': boot * 32, 'acs': acs, 'ready': ready, 'current': current,
            'allowed': {b: sorted(p.CHANNELS[b]) for b in current}}


def view():
    return {OWN: status('a'), PEER: status('b'), THIRD: status('c')}


def proposal(v=None):
    return p.make_plan(v or view(), {'2.4': 2462}, False, NOW, 'e' * 32)


def vote(plan, boot='b'):
    return {'vote': {'plan': p.digest(plan), 'boot': boot * 32}}


class ProtocolTests(unittest.TestCase):
    def test_three_nodes_switch_together_despite_one_missing_ack(self):
        v, plan = view(), proposal()
        leader, outgoing, apply = p.advance({}, OWN, NOW, v, {}, plan)
        follower, ack, _ = p.advance({}, PEER, NOW + 5, v, {OWN: outgoing})
        leader, outgoing, _ = p.advance(leader, OWN, NOW + 10, v, {PEER: ack})
        self.assertNotIn('commit', outgoing)  # The full fixed ACK window is honored.
        leader, outgoing, _ = p.advance(leader, OWN, plan['deadline'], v, {})
        self.assertEqual(set(outgoing['commit']['approvals']), {OWN, PEER})
        follower, _, _ = p.advance(follower, PEER, plan['deadline'] + 5, v, {OWN: outgoing})
        for own, state in [(OWN, leader), (PEER, follower)]:
            durable, _, applied = p.advance(state, own, plan['activate_at'], v, {})
            self.assertEqual(applied, plan)
            self.assertEqual(durable['phase'], 'attempted')
            self.assertIsNone(p.advance(durable, own, plan['activate_at'] + 1, v, {})[2])

    def test_no_majority_expires_and_late_votes_cannot_revive_it(self):
        plan = proposal()
        state, _, _ = p.advance({}, OWN, NOW, view(), {}, plan)
        state, out, action = p.advance(state, OWN, plan['deadline'], view(), {PEER: vote(plan)})
        self.assertEqual(state['phase'], 'expired')
        self.assertEqual(out, {})
        self.assertIsNone(action)
        self.assertIsNone(p.advance(state, OWN, plan['activate_at'], view(), {PEER: vote(plan)})[2])

    def test_two_nodes_require_both_until_next_round_rediscovers_departure(self):
        v = view(); del v[THIRD]
        plan = proposal(v)
        state, _, _ = p.advance({}, OWN, NOW, v, {}, plan)
        state, _, _ = p.advance(state, OWN, plan['deadline'], {OWN: v[OWN]}, {})
        self.assertEqual(state['phase'], 'expired')
        next_now = NOW + p.ROUND_SECONDS
        next_plan = p.make_plan({OWN: v[OWN]}, {'2.4': 2462}, False, next_now, 'f' * 32)
        state, _, _ = p.advance(state, OWN, next_now, {OWN: v[OWN]}, {}, next_plan)
        state, out, _ = p.advance(state, OWN, next_plan['deadline'], {OWN: v[OWN]}, {})
        self.assertTrue(p.valid_commit(out['commit'], next_plan))

    def test_absent_status_stays_in_denominator_without_blocking_a_majority(self):
        v = view(); v[THIRD] = None
        plan = proposal(v)
        state, _, _ = p.advance({}, OWN, NOW, v, {}, plan)
        state, _, _ = p.advance(state, OWN, NOW + 5, v, {PEER: vote(plan)})
        state, out, _ = p.advance(state, OWN, plan['deadline'], v, {})
        self.assertTrue(p.valid_commit(out['commit'], plan))
        self.assertIn(THIRD, plan['participants'])

    def test_membership_churn_and_failed_queries_cannot_extend_deadline(self):
        plan = proposal()
        state, _, _ = p.advance({}, OWN, NOW, view(), {}, plan)
        for when, v in [(NOW + 10, {OWN: status()}), (NOW + 20, None), (NOW + 30, view())]:
            state, _, _ = p.advance(state, OWN, when, v, {}, local=status())
            self.assertEqual(state['plan'], plan)
        state, _, _ = p.advance(state, OWN, plan['deadline'], None, {}, local=status())
        self.assertEqual(state['phase'], 'expired')

    def test_halow_reconnection_stops_an_uncommitted_island_election(self):
        island = {OWN: status('a'), PEER: status('b')}
        plan = proposal(island)
        state, _, _ = p.advance({}, OWN, NOW, island, {}, plan)
        state, _, _ = p.advance(state, OWN, NOW + 5, island, {PEER: vote(plan)})
        # Both old island votes exist, but THIRD has now rejoined via any radio.
        state, out, applied = p.advance(state, OWN, plan['deadline'], view(), {})
        self.assertEqual(state['phase'], 'expired')
        self.assertEqual(out, {})
        self.assertIsNone(applied)

    def test_incompatible_radio_abstains_and_single_band_can_vote(self):
        v = view(); plan = proposal(v)
        v[PEER]['allowed']['2.4'] = [2437]
        self.assertFalse(p.can_vote(plan, PEER, v, NOW))
        v[PEER] = status('b', current={'5': 5200})
        self.assertTrue(p.can_vote(plan, PEER, v, NOW))
        v[PEER] = status('b', acs=False)
        self.assertFalse(p.can_vote(plan, PEER, v, NOW))

    def test_halow_only_participant_can_ack_without_becoming_coordinator(self):
        v = view(); v[OWN] = status(current={})
        self.assertEqual(p.coordinator(v), PEER)
        plan = proposal(v)
        self.assertTrue(p.can_vote(plan, OWN, v, NOW))

    def test_restart_preserves_one_vote_per_round(self):
        plan = proposal()
        saved, _, _ = p.advance({}, PEER, NOW, view(), {OWN: {'proposal': plan}})
        competitor = deepcopy(plan); competitor['nonce'] = 'f' * 32
        restarted = json.loads(json.dumps(saved))
        updated, out, _ = p.advance(restarted, PEER, NOW + 5, view(), {OWN: {'proposal': competitor}})
        self.assertEqual(out['vote']['plan'], p.digest(plan))
        self.assertEqual(updated['plan'], plan)

    def test_reboot_or_local_radio_change_invalidates_a_prepared_vote(self):
        plan = proposal()
        saved, _, _ = p.advance({}, PEER, NOW, view(), {OWN: {'proposal': plan}})
        for local in [status('d'), status('b', current={'2.4': 2462})]:
            state, out, applied = p.advance(saved, PEER, NOW + 5, None, {}, local=local)
            self.assertEqual(state['phase'], 'expired')
            self.assertEqual(out, {})
            self.assertIsNone(applied)

    def test_dropped_late_and_partial_commits_never_activate(self):
        plan = proposal()
        prepared, _, _ = p.advance({}, PEER, NOW, view(), {OWN: {'proposal': plan}})
        commit = {'plan': p.digest(plan), 'approvals': {OWN: 'a' * 32, PEER: 'b' * 32}, 'issued_at': plan['deadline']}
        for delay, approvals in [(p.COMMIT_CUTOFF, commit['approvals']), (20, {OWN: 'a' * 32})]:
            bad = dict(commit, approvals=approvals)
            state, _, _ = p.advance(prepared, PEER, plan['activate_at'] - delay, view(), {OWN: {'commit': bad}})
            self.assertIsNone(p.advance(state, PEER, plan['activate_at'], view(), {})[2])
        self.assertIsNone(p.advance(prepared, PEER, plan['activate_at'], view(), {})[2])

    def test_stale_or_wrong_plan_ack_is_not_reusable(self):
        plan = proposal()
        prepared, _, _ = p.advance({}, OWN, NOW, view(), {}, plan)
        for bad in [vote(dict(plan, nonce='f' * 32)), vote(plan, 'd')]:
            state, _, _ = p.advance(prepared, OWN, NOW + 5, view(), {PEER: bad})
            state, _, _ = p.advance(state, OWN, plan['deadline'], view(), {})
            self.assertEqual(state['phase'], 'expired')

    def test_extended_deadline_and_clock_reversal_are_rejected(self):
        plan = proposal()
        changed = dict(plan, deadline=plan['deadline'] + 1)
        with self.assertRaises(p.AgreementError):
            p.validate_plan(changed)
        saved, _, _ = p.advance({}, OWN, NOW, view(), {}, plan)
        state, out, apply = p.advance(saved, OWN, NOW - 180, view(), {}, plan)
        self.assertEqual(state, saved)
        self.assertEqual(out, {})
        self.assertIsNone(apply)


class RuntimeTests(AcsHarness):
    def setUp(self):
        super().setUp()
        self.configure(2437, None)
        self.now = int(time.time()) // 180 * 180 + 45
        self.env['TEST_NOW'] = str(self.now)
        (self.root / 'manet-acs-request.json').write_text(json.dumps({'round': self.now // 180}))
        self.reports([[{'channel': 2437, 'noise_floor': -95, 'busy_pct': 80},
                       {'channel': 2462, 'noise_floor': -95, 'busy_pct': 10}]], [OWN])
        with self.registry.open('a') as out:
            out.write(node(OWN) + node(PEER, aliases=RADIO) + node(THIRD))
        (self.root / 'peers').write_text(json.dumps([{'orig_address': RADIO}, {'orig_address': THIRD}]))
        self.command('alfred', "Path(os.environ['TEST_ROOT'], 'sent-' + sys.argv[-1]).write_text(sys.stdin.read())")
        self.environment = patch.dict(os.environ, self.env)
        self.environment.start(); self.addCleanup(self.environment.stop)
        self.runner = runtime.Runtime()
        self.records = {PEER: {'status': status('b')}, THIRD: {'status': status('c')}}

    def tick(self, when, records=None):
        with patch.object(self.runner, 'receive', return_value=self.records if records is None else records), patch.object(runtime.time, 'time', return_value=when):
            self.runner.tick(when)

    def test_real_score_save_majority_activation_and_cooldown(self):
        self.tick(self.now)
        plan = self.runner.state['protocol']['plan']
        self.assertEqual(plan['channels'], {'2.4': 2462})
        self.assertIn('frequency=2437', (self.wpa / 'wpa_supplicant-wlan0.conf').read_text())
        persisted = json.loads(self.runner.path.read_text())
        self.assertEqual(persisted['protocol']['phase'], 'prepared')
        self.records[PEER]['protocol'] = vote(plan)
        self.tick(self.now + 5)
        self.tick(plan['deadline'])
        self.assertEqual(self.runner.state['protocol']['phase'], 'committed')
        self.tick(plan['activate_at'])
        self.assertIn('frequency=2462', (self.wpa / 'wpa_supplicant-wlan0.conf').read_text())
        self.assertEqual(self.runner.state['protocol']['phase'], 'attempted')
        self.assertFalse(self.runner.status(plan['activate_at'] + 1)['ready'])
        self.assertEqual(self.runner.state['hold_until'], plan['activate_at'] + p.RECOVERY_SECONDS)
        self.assertIn('wpa_cli -i wlan0 reconfigure', (self.root / 'commands').read_text())

    def test_failed_durable_write_cannot_publish_vote_on_retry(self):
        with patch.object(runtime, 'private_json_write', side_effect=OSError('disk full')):
            for when in (self.now, self.now + 1):
                with self.assertRaises(OSError):
                    self.tick(when)
                self.assertFalse((self.root / 'sent-74').exists())
                self.assertIn('frequency=2437', (self.wpa / 'wpa_supplicant-wlan0.conf').read_text())

    def test_regular_election_requests_readiness_without_changing_channels(self):
        result = self.run_command(['bash', str(runtime.TOOLS / 'channel-election.sh')])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('frequency=2437', (self.wpa / 'wpa_supplicant-wlan0.conf').read_text())
        self.assertFalse((self.root / 'commands').exists())

    def test_no_sync_means_no_vote_apply_or_future_round_history(self):
        (self.root / 'initial_time_synced').unlink()
        for acs in ('y', 'n'):
            (self.root / 'mesh.conf').write_text(f'acs={acs}\nadmin_password=test-secret\n')
            self.assertFalse(self.runner.status(self.now)['ready'])
        self.tick(self.now + 1000000000)
        self.assertFalse(self.runner.path.exists())
        self.assertFalse((self.root / 'sent-74').exists())
        self.assertFalse((self.root / 'admin/sender.json').exists())
        with self.assertRaisesRegex(ValueError, 'initial GPS/NTP'):
            self.runner.apply({'2.4': 2462})
        result = self.run_command(['bash', str(runtime.TOOLS / 'channel-election.sh')])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.runner.request_path.exists())

    def test_new_boot_after_sync_discards_old_future_round_but_restart_does_not(self):
        future = self.now + 1000000000
        self.runner.state = {'clock_boot': 'f' * 32, 'protocol': {'round': future // 180, 'phase': 'expired'},
                             'hold_until': future, 'recovered_round': future // 180}
        self.runner.save()
        (self.root / 'initial_time_synced').unlink()
        self.tick(future)
        self.assertEqual(self.runner.state['hold_until'], future)
        (self.root / 'initial_time_synced').touch()
        self.tick(self.now)
        plan = self.runner.state['protocol']['plan']
        self.assertEqual(plan['round'], self.now // 180)
        self.assertNotIn('hold_until', self.runner.state)
        self.runner = runtime.Runtime()
        self.tick(self.now + 1)
        self.assertEqual(self.runner.state['protocol']['plan'], plan)

    def test_missed_commit_recovered_from_peer_already_on_destination(self):
        v = view()
        plan = p.make_plan(v, {'2.4': 2462}, False, self.now, 'e' * 32)
        commit = {'plan': p.digest(plan), 'approvals': {OWN: 'a' * 32, PEER: 'b' * 32}, 'issued_at': plan['deadline']}
        records = {PEER: {'status': status('b', current={'2.4': 2462}), 'destination': {'plan': plan, 'commit': commit}}}
        self.tick(plan['activate_at'] + 20, records)
        self.assertIn('frequency=2462', (self.wpa / 'wpa_supplicant-wlan0.conf').read_text())
        self.assertEqual(self.runner.state['recovered_round'], plan['round'])

    def test_incomplete_certificate_cannot_drive_recovery(self):
        plan = p.make_plan(view(), {'2.4': 2462}, False, self.now, 'e' * 32)
        commit = {'plan': p.digest(plan), 'approvals': {OWN: 'a' * 32}, 'issued_at': plan['deadline']}
        records = {PEER: {'status': status('b', current={'2.4': 2462}), 'destination': {'plan': plan, 'commit': commit}}}
        self.tick(plan['activate_at'] + 20, records)
        self.assertIn('frequency=2437', (self.wpa / 'wpa_supplicant-wlan0.conf').read_text())

    def test_authentication_record_binding_and_helper_expiry(self):
        payload = {'kind': 'acs_helper', 'node': PEER, 'channels': {'2.4': 2462}, 'size': 3}
        now = int(time.time())
        envelope = self.transport.seal(75, payload)
        raw = '{ "' + PEER + '", ' + json.dumps(json.dumps(envelope)) + ' },'
        records = runtime.authenticated_records(self.transport, 75, raw, now)
        self.assertIn(PEER, records)
        self.assertEqual(runtime.authenticated_records(self.transport, 75, raw, now + 46), {})
        self.assertEqual(runtime.authenticated_records(self.transport, 75, raw.replace(PEER, THIRD), now), {})
        unsigned = '{ "' + PEER + '", ' + json.dumps(json.dumps(payload)) + ' },'
        self.assertEqual(runtime.authenticated_records(self.transport, 75, unsigned, now), {})
        envelope['ciphertext'] = envelope['ciphertext'][:-4] + 'AAAA'
        damaged = '{ "' + PEER + '", ' + json.dumps(json.dumps(envelope)) + ' },'
        self.assertEqual(runtime.authenticated_records(self.transport, 75, damaged, now), {})

    def test_helper_selection_prefers_largest_partition_and_excludes_pre_hop_peers(self):
        records = {PEER: {'channels': {'2.4': 2462}, 'size': 3},
                   THIRD: {'channels': {'2.4': 2437}, 'size': 5}}
        local = status(current={'2.4': 2412})
        self.assertEqual(runtime.recovery_destination(records, local, 0, OWN), {'2.4': 2437})
        self.assertEqual(runtime.recovery_destination(records, local, 0, OWN, [THIRD]), {'2.4': 2462})
        self.assertIsNone(runtime.recovery_destination(records, local, 0, OWN, [THIRD], 4))

    def test_equal_partition_tie_has_exactly_one_mover(self):
        for own, other, moves in [(OWN, PEER, False), (PEER, OWN, True)]:
            choice = runtime.recovery_destination({other: {'channels': {'2.4': 2462}, 'size': 2}},
                                                 status(), 0, own, (), 2)
            self.assertEqual(choice is not None, moves)

    def test_failed_peer_query_is_not_a_solo_proposal(self):
        with patch.dict(os.environ, TEST_BATCTL_RC='1'):
            self.tick(self.now)
        self.assertNotIn('plan', self.runner.state['protocol'])

    def test_loopback_mac_is_not_advertised_as_a_mesh_identity(self):
        (self.root / 'net/lo').mkdir()
        (self.root / 'net/lo/address').write_text('00:00:00:00:00:00')
        self.assertEqual(self.runner.aliases(), [OWN])

    def test_slow_discovery_cannot_activate_after_the_grace_period(self):
        self.tick(self.now)
        plan = self.runner.state['protocol']['plan']
        self.records[PEER]['protocol'] = vote(plan)
        self.tick(self.now + 5)
        self.tick(plan['deadline'])
        late = plan['activate_at'] + p.APPLY_GRACE + 1
        with patch.object(self.runner, 'receive', return_value=self.records), patch.object(runtime.time, 'time', return_value=late):
            self.runner.tick(plan['activate_at'])
        self.assertEqual(self.runner.state['protocol']['phase'], 'expired')
        self.assertIn('frequency=2437', (self.wpa / 'wpa_supplicant-wlan0.conf').read_text())

    def test_external_settling_marker_survives_daemon_polling(self):
        self.runner.busy_path.write_text(str(self.now + 30))
        self.tick(self.now)
        self.assertTrue(self.runner.busy(self.now))
        self.assertNotIn('plan', self.runner.state['protocol'])

    def test_unchanged_plan_does_not_start_an_eight_minute_hold(self):
        self.reports([[{'channel': 2437, 'noise_floor': -95, 'busy_pct': 10}]], [OWN])
        # Preserve discovery identities while changing only the scan report.
        with self.registry.open('a') as out:
            out.write(node(PEER, aliases=RADIO) + node(THIRD))
        self.tick(self.now)
        plan = self.runner.state['protocol']['plan']
        self.assertFalse(plan['moving'])
        self.records[PEER]['protocol'] = vote(plan)
        self.tick(self.now + 5)
        self.tick(plan['deadline'])
        self.tick(plan['activate_at'])
        self.assertNotIn('hold_until', self.runner.state)

    def test_largest_supported_group_fits_authenticated_message_limit(self):
        members = {f'02:00:00:00:00:{i:02x}': status('a') for i in range(p.MAX_MEMBERS)}
        plan = p.make_plan(members, {'2.4': 2462, '5': 5220}, False, self.now, 'e' * 32)
        commit = {'plan': p.digest(plan), 'approvals': {m: 'a' * 32 for m in members}, 'issued_at': plan['deadline']}
        envelope = self.transport.seal(74, {'kind': 'acs_state', 'node': OWN, 'status': status(),
                                          'protocol': {'proposal': plan, 'commit': commit}})
        self.assertTrue(self.transport.open(74, envelope).payload)

    def test_actual_radio_failure_is_not_reported_as_success(self):
        self.command('iw', "print('wiphy 0\\nchannel 1 (2437 MHz)' if sys.argv[1] == 'dev' else '* 2462.0 MHz [11] (20.0 dBm)')")
        with patch.object(runtime.time, 'sleep'):
            with self.assertRaisesRegex(ValueError, 'did not reach'):
                self.runner.apply({'2.4': 2462})
        self.assertFalse((self.root / 'election').exists())
        self.assertIn('systemctl restart wpa_supplicant@wlan0.service', (self.root / 'commands').read_text())


if __name__ == '__main__':
    unittest.main()
