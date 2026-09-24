"""HaLow-connected nodes share one channel decision and recover without tours."""
from copy import deepcopy
import json
import os
import time
from unittest.mock import patch

from manet_admin import AdminTransport
import manet_acs_agreement as p
from test_acs import AcsHarness, OWN, PEER, THIRD, RADIO, node, runtime
from test_acs_agreement import status
from test_acs_bootstrap import frame


class ConnectedRecoveryTests(AcsHarness):
    def setUp(self):
        super().setUp()
        self.configure(2437, None)
        self.now = int(time.time()) // 180 * 180 + 45
        self.registry.write_text(node(OWN) + node(PEER, aliases=RADIO))
        # Conventional Wi-Fi may have no peers: HaLow alone supplies the view.
        (self.root / 'peers').write_text(json.dumps([
            {'orig_address': RADIO, 'hard_ifname': 'wlan2', 'best': True}]))
        self.command('alfred', '''
path = Path(os.environ['TEST_ROOT'], ('wire-' if sys.argv[1] == '-r' else 'sent-') + sys.argv[-1])
if sys.argv[1] == '-r':
    print(path.read_text() if path.exists() else '')
else:
    path.write_text(sys.stdin.read())
''')
        env = patch.dict(os.environ, self.env)
        env.start(); self.addCleanup(env.stop)
        self.runner = runtime.Runtime()
        self.remote = AdminTransport(self.root / 'mesh.conf', self.root / 'remote-admin')

    def destination(self, freq, created=None, members=None):
        members = members or {OWN: status('a'), PEER: status('b')}
        plan = p.make_plan(members, {'2.4': freq}, False,
                           self.now if created is None else created, 'e' * 32)
        commit = {'plan': p.digest(plan), 'approvals': {m: s['boot'] for m, s in members.items()},
                  'issued_at': plan['deadline']}
        return {'plan': plan, 'commit': commit}

    def keep(self, destination):
        self.runner.state = {'clock_boot': self.runner.boot, 'destination': destination}
        self.runner.save()

    def record(self, freq, destination):
        return {'status': status('b', ready=False, current={'2.4': freq}), 'destination': destination}

    def tick(self, when, records=None):
        self.when = when
        with patch.object(runtime.time, 'time', return_value=when), patch.object(runtime.time, 'time_ns', return_value=when * 10**9):
            if records is None:
                self.runner.tick(when)
            else:
                with patch.object(self.runner, 'receive', return_value=records):
                    self.runner.tick(when)

    def published(self):
        envelope = json.loads((self.root / 'sent-74').read_text())
        with patch.object(runtime.time, 'time_ns', return_value=self.when * 10**9):
            return self.runner.transport.open(74, envelope).payload

    def assert_frequency(self, freq):
        self.assertIn(f'frequency={freq}', (self.wpa / 'wpa_supplicant-wlan0.conf').read_text())

    def test_day_old_plan_recovers_over_halow_with_fresh_authenticated_advertisement(self):
        destination = self.destination(2462)
        when = self.now + 86400 + 121
        payload = dict(self.record(2462, destination), kind='acs_state', node=PEER, aliases=[RADIO])
        with patch.object(runtime.time, 'time_ns', return_value=when * 10**9):
            envelope = self.remote.seal(74, payload)
        (self.root / 'wire-74').write_text(frame(PEER, envelope))
        self.tick(when)
        self.assert_frequency(2462)
        self.assertEqual(self.runner.state['destination'], destination)
        commands = (self.root / 'commands').read_text()
        self.assertNotIn('wpa_cli -i wlan2', commands)
        self.assertNotIn('tourguide', commands)

    def test_old_plan_is_not_authority_without_fresh_message(self):
        destination = self.destination(2462)
        sent = self.now + 121
        payload = dict(self.record(2462, destination), kind='acs_state', node=PEER)
        with patch.object(runtime.time, 'time_ns', return_value=sent * 10**9):
            envelope = self.remote.seal(74, payload)
        (self.root / 'wire-74').write_text(frame(PEER, envelope))
        self.tick(sent + 46)
        self.assert_frequency(2437)

    def test_cached_unreachable_peer_and_wrong_actual_frequency_do_not_recover(self):
        destination = self.destination(2462)
        for records in [{THIRD: self.record(2462, destination)},
                        {PEER: self.record(2437, destination)},
                        {PEER: dict(self.record(2462, destination), _fresh_until=self.now)}]:
            with self.subTest(records=records):
                self.tick(self.now + 121, records)
                self.assert_frequency(2437)

    def test_operating_plan_is_advertised_after_a_day_and_after_reboot(self):
        destination = self.destination(2437)
        self.keep(destination)
        self.runner.state['clock_boot'] = 'f' * 32
        self.runner.state['protocol'] = {'round': 99999999999, 'phase': 'expired'}
        self.runner.save()
        self.runner = runtime.Runtime()
        self.tick(self.now + 86400 + 121, {})
        self.assertEqual(self.runner.state['destination'], destination)
        self.assertEqual(self.published()['destination'], destination)
        self.assertLess(self.runner.state['protocol']['round'], 99999999999)

    def test_changed_radio_cannot_advertise_old_destination(self):
        self.keep(self.destination(2462))  # Radio actually remains on 2437.
        self.tick(self.now + 121, {})
        self.assertNotIn('destination', self.published())

    def test_one_full_certificate_publisher_with_failover(self):
        destination = self.destination(2437)
        self.keep(destination)
        lower = '02:00:00:00:00:00'
        self.registry.write_text(node(OWN) + node(lower, aliases=RADIO))
        records = {lower: dict(self.record(2437, destination), destination_id=p.digest(destination['plan']))}
        self.tick(self.now + 121, records)
        self.assertEqual(self.published()['destination_id'], p.digest(destination['plan']))
        self.assertNotIn('destination', self.published())
        (self.root / 'peers').write_text('[]')
        self.tick(self.now + 127, records)  # Cached record no longer suppresses us.
        self.assertEqual(self.published()['destination'], destination)

    def test_largest_old_certificate_and_next_proposal_fit_message_limit(self):
        members = {f'02:00:00:00:00:{i:02x}': status() for i in range(p.MAX_MEMBERS)}
        old = self.destination(2437, created=self.now - 180, members=members)
        new = self.destination(2462, members=members)
        for outgoing, extra in [({'proposal': new['plan']}, {'destination': old}),
                                ({'proposal': new['plan'], 'commit': new['commit']}, {})]:
            payload = dict(kind='acs_state', node=OWN, status=status(), protocol=outgoing,
                           destination_id=p.digest(old['plan']), **extra)
            envelope = self.runner.transport.seal(74, payload)
            self.assertTrue(self.runner.transport.open(74, envelope).payload)

    def test_matching_radio_learns_certificate_without_hopping_for_missing_ax_peers(self):
        destination = self.destination(2437)
        self.tick(self.now + 86400 + 121, {PEER: self.record(2437, destination)})
        self.assertEqual(self.runner.state['destination'], destination)
        self.assertNotIn('wpa_cli', (self.root / 'commands').read_text())

    def test_new_network_commit_supersedes_stragglers_old_plan(self):
        self.keep(self.destination(2437, created=self.now - 180))
        latest = self.destination(2462)
        self.tick(self.now + 121, {PEER: self.record(2462, latest)})
        self.assert_frequency(2462)
        self.assertEqual(self.runner.state['destination'], latest)

    def test_reunited_islands_use_all_reachable_registry_observations_and_one_election(self):
        left = self.destination(2437, created=self.now - 360, members={OWN: status('a')})
        right = self.destination(2462, created=self.now - 180, members={PEER: status('b')})
        self.keep(left)
        self.runner.state['hold_until'] = self.now + 480
        records = {PEER: self.record(2462, right)}
        # No unilateral adoption just because the other island's plan is newer.
        self.tick(self.now - 1, records)
        self.assert_frequency(2437)
        self.assertNotIn('hold_until', self.runner.state)
        self.reports([
            [{'channel': 2437, 'noise_floor': -95, 'busy_pct': 0},
             {'channel': 2462, 'noise_floor': -95, 'busy_pct': 50}],
            [{'channel': 2437, 'noise_floor': -95, 'busy_pct': 80},
             {'channel': 2462, 'noise_floor': -95, 'busy_pct': 0}],
            # Still cached but unreachable; including this would change winner.
            [{'channel': 2437, 'noise_floor': -95, 'busy_pct': 0},
             {'channel': 2462, 'noise_floor': -95, 'busy_pct': 100}],
        ], [OWN, PEER, THIRD])
        with self.registry.open('a') as out:
            out.write(node(OWN) + node(PEER, aliases=RADIO) + node(THIRD))
        self.runner.request_path.write_text(json.dumps({'round': self.now // 180}))
        records[PEER]['status']['ready'] = True
        self.tick(self.now, records)
        plan = self.runner.state['protocol']['plan']
        self.assertEqual(set(plan['participants']), {OWN, PEER})
        self.assertEqual(plan['channels'], {'2.4': 2462})
        peer_state, peer_out, _ = p.advance({}, PEER, self.now + 1,
            {OWN: self.runner.status(self.now), PEER: records[PEER]['status']}, {OWN: {'proposal': plan}})
        records[PEER]['protocol'] = peer_out
        self.tick(self.now + 5, records)
        self.tick(plan['deadline'], records)
        commit = self.runner.state['protocol']['commit']
        peer_state, _, _ = p.advance(peer_state, PEER, plan['deadline'] + 1, None,
                                    {OWN: {'commit': commit}}, local=records[PEER]['status'])
        peer_applied = p.advance(peer_state, PEER, plan['activate_at'], None, {}, local=records[PEER]['status'])[2]
        self.tick(plan['activate_at'], records)
        self.assert_frequency(2462)
        self.assertEqual(peer_applied['channels'], plan['channels'])

    def test_failed_discovery_never_adopts_a_cached_peer_plan(self):
        with patch.dict(os.environ, TEST_BATCTL_RC='1'):
            self.tick(self.now + 121, {PEER: self.record(2462, self.destination(2462))})
        self.assert_frequency(2437)

    def test_static_member_acknowledges_shared_plan_without_running_channel_apply(self):
        (self.root / 'mesh.conf').write_text('acs=n\nadmin_password=test-secret\n')
        destination = self.destination(2437, members={OWN: status('a', acs=False), PEER: status('b')})
        plan = destination['plan']
        records = {PEER: {'status': status('b'), 'protocol': {'proposal': plan}}}
        self.tick(self.now, records)
        self.assertEqual(self.runner.state['protocol']['phase'], 'prepared')
        records[PEER]['protocol']['commit'] = destination['commit']
        self.tick(plan['deadline'], records)
        with patch.object(self.runner, 'apply') as apply:
            self.tick(plan['activate_at'], records)
            apply.assert_not_called()
        self.assert_frequency(2437)

    def test_failed_recovery_has_bounded_retry_rate(self):
        records = {PEER: self.record(2462, self.destination(2462))}
        when = self.now + 121
        with patch.object(self.runner, 'apply', side_effect=OSError('radio unavailable')) as apply:
            with self.assertRaises(OSError):
                self.tick(when, records)
            self.tick(when + 1, records)
            self.tick(when + 29, records)
            self.assertEqual(apply.call_count, 1)
            with self.assertRaises(OSError):
                self.tick(when + 30, records)
            self.assertEqual(apply.call_count, 2)

    def test_clockless_probe_is_answered_without_a_tourguide_visit(self):
        destination = self.destination(2437)
        self.keep(destination)
        nonce = 'd' * 32
        probe = self.remote.seal_challenge(76, {'kind': 'acs_probe', 'node': PEER,
            'boot': 'b' * 32, 'nonce': nonce, 'aliases': [RADIO], 'current': {'2.4': 2412}})
        (self.root / 'wire-76').write_text(frame(PEER, probe))
        self.tick(self.now + 86400 + 121, {})
        envelope = json.loads((self.root / 'sent-77').read_text())
        reply = self.remote.open_challenge(77, envelope).payload
        self.assertEqual(reply['channels'], {'2.4': 2437})
        self.assertEqual(reply['answers'], {PEER: {'boot': 'b' * 32, 'nonce': nonce}})
        self.assertNotIn('wpa_cli', (self.root / 'commands').read_text())
        (self.root / 'sent-77').unlink()
        self.tick(self.now + 86400 + 127, {})
        self.assertFalse((self.root / 'sent-77').exists())

    def test_only_one_reachable_source_answers_and_no_reply_if_plan_already_matches(self):
        destination = self.destination(2437)
        self.keep(destination)
        probe = self.remote.seal_challenge(76, {'kind': 'acs_probe', 'node': PEER,
            'boot': 'b' * 32, 'nonce': 'd' * 32, 'aliases': [RADIO], 'current': {'2.4': 2412}})
        (self.root / 'wire-76').write_text(frame(PEER, probe))
        lower = '02:00:00:00:00:00'
        self.runner.answer_probes({'2.4': 2437}, 3, {lower: destination, OWN: destination})
        self.assertFalse((self.root / 'sent-77').exists())
        self.runner.answer_probes({'2.4': 2412}, 3,
            {OWN: dict(destination, plan=dict(destination['plan'], channels={'2.4': 2412}))})
        self.assertFalse((self.root / 'sent-77').exists())

    def test_matching_clockless_searcher_gets_confirmation_from_operating_plan_holder(self):
        destination = self.destination(2437)
        self.keep(destination)
        probe = self.remote.seal_challenge(76, {'kind': 'acs_probe', 'node': PEER,
            'boot': 'b' * 32, 'nonce': 'd' * 32, 'aliases': [RADIO],
            'current': {'2.4': 2437}, 'discovery': True})
        (self.root / 'wire-76').write_text(frame(PEER, probe))
        self.tick(self.now + 121, {})
        envelope = json.loads((self.root / 'sent-77').read_text())
        reply = self.remote.open_challenge(77, envelope).payload
        self.assertEqual(reply['channels'], {'2.4': 2437})
        self.assertEqual(reply['answers'], {PEER: {'boot': 'b' * 32, 'nonce': 'd' * 32}})
        self.assertNotIn('wpa_cli', (self.root / 'commands').read_text())

    def test_equal_round_conflicts_do_not_choose_by_certificate_hash(self):
        a, b = self.destination(2437), self.destination(2462)
        destinations = {OWN: a, PEER: b}
        self.assertEqual(p.network_destinations(destinations, [OWN, PEER]), destinations)
        self.assertTrue(p.conflicting_destinations(destinations.values()))
        self.assertFalse(p.conflicting_destinations([a, deepcopy(a)]))
