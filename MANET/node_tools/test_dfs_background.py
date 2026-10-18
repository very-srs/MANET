"""Production-supplicant CAC lifecycle, busy read and election regressions."""
from copy import deepcopy
from contextlib import nullcontext
import errno
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import json
import struct

import manet_dfs as dfs
from test_acs import AcsHarness, runtime
from test_acs_dfs import NOW, dfs_view
import manet_acs_agreement as protocol


def preparation(view, now=NOW):
    return dict(leader=min(view), participants={m: s['boot'] for m, s in view.items()},
                frequency=5260, fallback={'5': 5200}, created=now,
                expires=now + 1200, nonce='f' * 32, limp=False)


class BackgroundRegressions(unittest.TestCase):
    def test_unplanned_manual_cac_is_stopped_by_agreement_cleanup(self):
        import manet_bgobs
        runner = object.__new__(runtime.Runtime)
        runner.state = {}
        with tempfile.TemporaryDirectory() as tmp:
            hold = Path(tmp) / 'background_hold'
            hold.write_text(json.dumps(dict(owner=True, freq_mhz=5300)))
            with patch.object(runtime, 'background_hold_path', return_value=hold), \
                    patch.object(manet_bgobs, 'write_command') as write:
                runner.prepare_dfs(dfs_view(), {}, NOW)
            write.assert_called_once_with(hold, 'stop')

    def test_runtime_finishes_preparation_even_when_rescore_would_hold_incumbent(self):
        import manet_bgobs
        view = dfs_view()
        for status in view.values():
            status.update(dfs_candidates=[5260], discovery=False)
        runner = object.__new__(runtime.Runtime)
        runner.own = min(view)
        runner.boot = view[runner.own]['boot']
        runner.state = dict(clock_boot=runner.boot, dfs_prepare=preparation(view))
        runner.ui_status = {}
        runner.last_status = runner.last_publish = None
        runner.status = Mock(return_value=view[runner.own])
        runner.receive = Mock(return_value={m: {'status': s} for m, s in view.items()})
        runner.members = Mock(return_value=list(view))
        runner.interfaces = Mock(return_value={})
        runner.channel_lock = nullcontext
        runner.process_dfs_aborts = Mock()
        runner.connected_destinations = Mock(return_value={})
        runner.save = Mock()
        runner.write_busy = Mock()
        runner.busy = Mock(return_value=True)
        runner.repair_drift = Mock(return_value=False)
        runner.aliases = Mock(return_value=[])
        runner.transport = Mock()
        runner.transport.seal.return_value = {}
        runner.score = Mock(return_value=({'5': 5200}, False))
        with patch.object(manet_bgobs, 'observer_path', return_value=None), \
                patch.object(runtime.time, 'time', return_value=NOW), \
                patch.object(runtime, 'clock_ready', return_value=True), \
                patch.object(runtime, 'command', return_value=''):
            runner._tick(NOW)
        runner.score.assert_not_called()
        self.assertEqual(runner.state['protocol']['phase'], 'prepared')
        self.assertEqual(runner.state['protocol']['plan']['channels'], {'5': 5260})
        self.assertNotIn('dfs_prepare', runner.state)

    def test_completed_intention_becomes_unanimous_timed_plan_without_rescore(self):
        # Actual host plan builder and protocol on two or three participants.
        for count in (2, 3):
            view = dict(list(dfs_view().items())[:count])
            runner = object.__new__(runtime.Runtime)
            runner.state = {'dfs_prepare': preparation(view)}
            runner.score = Mock(side_effect=AssertionError('CAC must not rescore'))
            plan = runner.prepared_dfs_proposal(view, NOW)
            states, outgoing = {}, {}
            for mac in view:
                states[mac], outgoing[mac], due = protocol.advance(
                    {}, mac, NOW, view, {}, plan)
                self.assertIsNone(due)
            for now in (plan['deadline'] - 1, plan['deadline'],
                        plan['deadline'] + 1, plan['activate_at']):
                fresh = {mac: s for mac, s in dfs_view(now).items() if mac in view}
                for mac in view:
                    states[mac], outgoing[mac], due = protocol.advance(
                        states[mac], mac, now, fresh, outgoing)
                    if now == plan['activate_at']:
                        self.assertEqual(due, plan)
                        protocol.require_dfs_activation(due, fresh, now)
            self.assertEqual(plan['dfs']['fallback'], {'5': 5200})

    def test_busy_capability_read_defers_instead_of_withdrawing_support(self):
        path = Mock()
        path.read_text.side_effect = OSError(errno.EBUSY, 'wiphy busy')
        with patch.object(dfs, 'cm4_enabled', return_value=True):
            with self.assertRaises(BlockingIOError):
                dfs.capable(path, '* 5260 MHz (radar detection)')

    def test_busy_clearance_read_defers_without_reporting_clearance_loss(self):
        with patch.object(Path, 'open', side_effect=OSError(errno.EBUSY, 'wiphy busy')):
            with self.assertRaises(BlockingIOError):
                runtime.read_background_hold(Path('/unused'), 'a' * 32, None, NOW)

    def test_missing_peer_observation_does_not_cancel_unexpired_intention(self):
        view = dfs_view()
        runner = object.__new__(runtime.Runtime)
        prep = preparation(view)
        runner.state = {'dfs_prepare': deepcopy(prep)}
        runner.release_dfs_candidate = Mock()
        runner.prepare_dfs(None, {}, NOW + 10)
        self.assertEqual(runner.state.get('dfs_prepare'), prep)
        runner.release_dfs_candidate.assert_not_called()

    def test_fallback_scoring_cannot_reset_election_preference_streak(self):
        runner = object.__new__(runtime.Runtime)
        streak = {'5': dict(incumbent=5180, round=NOW // 180, count=2)}
        runner.state = {'challenge': deepcopy(streak)}
        with tempfile.TemporaryDirectory() as tmp:
            runner.run_dir = Path(tmp)
            def score(args, **kwargs):
                Path(kwargs['env']['OUTPUT_FILE']).write_text(
                    'WINNER_5_0=5200\nLIMP_MODE=false\nURGENT_5_0=false\n')
            with patch.object(runtime.subprocess, 'run', side_effect=score), \
                    patch.object(runtime.time, 'time', return_value=NOW):
                self.assertEqual(runner.score(dfs_view(), fallback=True), ({'5': 5200}, False))
            self.assertEqual(runner.state['challenge'], streak)


class RadarEventTests(unittest.TestCase):
    def test_multipart_kernel_events_preserve_phy_frequency_and_background(self):
        events = object.__new__(dfs.RadarEvents)
        events.family = 42
        def message(phy, frequency, event, background=False):
            body = struct.pack('BBH', 94, 1, 0) + b''.join(
                dfs.attr(k, struct.pack('I', v)) for k, v in
                ((1, phy), (38, frequency), (168, event)))
            if background:
                body += dfs.attr(308, b'')
            return struct.pack('IHHII', 16 + len(body), 42, 0, 0, 0) + body
        rows = events.wireless(message(2, 5300, 5, True) + message(3, 5260, 0))
        self.assertEqual(rows, [dict(kind='radar', phy=2, frequency=5300,
                                    event=5, background=True),
                                dict(kind='radar', phy=3, frequency=5260,
                                     event=0, background=False)])

    def test_wait_has_no_timeout_and_ignores_unrelated_file_churn(self):
        events = object.__new__(dfs.RadarEvents)
        events.files = {Path('/run/agreement.json')}
        events.watches = {4: Path('/run')}
        events.fd = 99
        key = Mock(data='files')
        events.selector = Mock()
        events.selector.select.return_value = [(key, 1)]
        def change(name):
            name = name.encode() + b'\0'
            return struct.pack('iIII', 4, 0x80, 0, len(name)) + name
        with patch.object(dfs.os, 'read', side_effect=[change('unrelated'), change('agreement.json')]):
            self.assertEqual(events.wait(), [dict(kind='file', name='agreement.json')])
        self.assertEqual(events.selector.select.call_args_list, [unittest.mock.call(), unittest.mock.call()])

    def test_busy_latch_does_not_block_new_radar_fence_or_reply_delivery(self):
        from test_acs_dfs import dfs_plan
        class End(BaseException):
            pass
        plan = dfs_plan()
        raw = dict(schema=2, radar_cause='main-radar', radar_epoch='d' * 32,
                   radar_generation=7, radar_sequence=1, radar_freq_mhz=5260,
                   radar_boottime_us=1)
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            path = run / 'phy2/background_hold'
            path.parent.mkdir()
            (run / 'manet-dfs-kernel.json').write_text(json.dumps(dict(path=str(path), raw=raw)))
            events = Mock()
            events.__enter__ = Mock(return_value=events)
            events.__exit__ = Mock(return_value=False)
            events.wait.side_effect = [
                [dict(kind='radar', phy=2, frequency=5260, event=0)],
                [dict(kind='file', name='manet-dfs-kernel.json')], End()]
            runner = Mock()
            runner.superseded_radar_latch.return_value = False
            runner.dfs_plans.return_value = {'plan': plan}
            calls = []
            runner.vacate_dfs.side_effect = lambda p: calls.append('fence')
            with patch.dict(runtime.os.environ, MANET_ACS_RUN_DIR=tmp), \
                    patch.object(dfs, 'RadarEvents', return_value=events), \
                    patch.object(dfs, 'hold_snapshot', side_effect=BlockingIOError(errno.EBUSY, 'busy')), \
                    patch.object(runtime, 'Runtime', return_value=runner), \
                    patch.object(runtime, 'wake_daemon'), \
                    patch.object(runtime.dfs_safety, 'stop_dfs'), \
                    patch.object(runtime, 'deliver_background_radar', side_effect=lambda *a: calls.append('deliver')), \
                    patch.object(runtime.time, 'sleep', side_effect=AssertionError('polling')):
                with self.assertRaises(End):
                    runtime.watch_background_radar(path)
            self.assertEqual(calls, ['fence', 'deliver'])
            self.assertTrue((run / 'manet-dfs-recheck').exists())


class SnapshotResponseTests(AcsHarness):
    def test_single_fresh_snapshot_answers_event_without_erasing_next_request(self):
        from test_acs_dfs import kernel_hold
        with patch.dict(runtime.os.environ, self.env):
            runner = runtime.Runtime()
            marker = self.root / 'manet-dfs-recheck'
            marker.touch()
            write = runtime.private_json_write
            def reply(path, data):
                if path.name == 'manet-dfs-kernel.json':
                    self.assertFalse(marker.exists())
                    # Simulate the watcher receiving the reply rename and
                    # immediately requesting a later read before write returns.
                    marker.touch()
                write(path, data)
            with patch.object(dfs, 'hold_snapshot', return_value=kernel_hold()) as snapshot, \
                    patch.object(dfs, 'capable', return_value=True), \
                    patch.object(runtime, 'private_json_write', side_effect=reply):
                status = runner.status(NOW)
            snapshot.assert_called_once()
            self.assertTrue(status['dfs_hold']['live'])
            self.assertTrue(marker.exists())
            response = json.loads((self.root / 'manet-dfs-kernel.json').read_text())
            self.assertEqual(response['raw']['generation'], 7)
            self.assertTrue(response['path'].endswith('/phy0/background_hold'))




class PeerSwitchEvidenceTests(unittest.TestCase):
    def message(self, *, blocked=True, frequency=5200, ifindex=7, width=1):
        body = struct.pack('BBH', 110, 1, 0) + b''.join(
            dfs.attr(k, struct.pack('I', v)) for k, v in
            ((3, ifindex), (38, frequency), (159, width), (160, frequency)))
        if blocked:
            body += dfs.attr(184, b'')
        return struct.pack('IHHII', 16 + len(body), 42, 0, 0, 0) + body

    def test_kernel_switch_evidence_binds_contract_and_never_renews_deadline(self):
        from test_acs_dfs import dfs_plan
        plan = dfs_plan()
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            boot = run / 'boot'; boot.write_text('a'*32)
            (run / 'manet-dfs-contract.json').write_text(json.dumps(dict(
                boot='a'*32, own=min(plan['participants']), iface='mesh5', operating=plan)))
            events = object.__new__(dfs.RadarEvents); events.family = 42
            events.on_switch = lambda change: runtime.record_dfs_peer_switch(change, run)
            record = run / 'manet-dfs-peer-csa.json'
            with patch.dict(runtime.os.environ, MANET_BOOT_ID_FILE=str(boot)), \
                    patch.object(dfs.socket, 'if_nametoindex', return_value=7), \
                    patch.object(dfs.time, 'clock_gettime', return_value=100) as clock:
                self.assertEqual(events.wireless(self.message()), [])
                evidence = json.loads(record.read_text())
                self.assertEqual(evidence['plan'], protocol.digest(plan))
                self.assertEqual(evidence['started_boottime'], 100)
                self.assertTrue(evidence['block_tx'])
                self.assertEqual(evidence['ifindex'], 7)
                clock.return_value = 105
                events.wireless(self.message())
                self.assertEqual(json.loads(record.read_text())['started_boottime'], 100)
                # An unrelated interface cannot replace this mesh's evidence.
                events.wireless(self.message(ifindex=8, blocked=False))
                self.assertTrue(record.exists())
                for invalid in (dict(blocked=False), dict(frequency=5220), dict(width=3)):
                    events.wireless(self.message(**invalid))
                    self.assertFalse(record.exists())
                    events.wireless(self.message())
                    self.assertTrue(record.exists())
                boot.write_text('b'*32)
                events.wireless(self.message())
                self.assertFalse(record.exists())

    def test_drain_after_busy_collects_switch_without_discarding_radar(self):
        events = object.__new__(dfs.RadarEvents); events.family = 42
        events.deferred = []; events.net = Mock(); received = []
        events.on_switch = received.append
        radar = struct.pack('BBH', 94, 1, 0) + b''.join(
            dfs.attr(k, struct.pack('I', v)) for k, v in ((1, 2), (38, 5260), (168, 0)))
        radar = struct.pack('IHHII', 16 + len(radar), 42, 0, 0, 0) + radar
        events.net.recvfrom.side_effect = [(self.message(), (99, 0)),
                                          (radar + self.message(), (0, 0)), BlockingIOError()]
        events.drain_wireless()
        self.assertEqual(len(received), 1)  # Only the kernel sender is trusted.
        pending = events.wait()
        self.assertEqual([r['kind'] for r in pending], ['radar'])
        self.assertFalse(events.deferred)


class SupersededRadarLatchTests(unittest.TestCase):
    def test_watcher_does_not_fence_new_main_token_for_an_older_unowned_latch(self):
        from test_acs_dfs import dfs_plan, kernel_hold
        class End(BaseException):
            pass
        plan = dfs_plan()
        runner = object.__new__(runtime.Runtime)
        runner.own = min(plan['participants'])
        runner.boot = plan['participants'][runner.own]
        runner.dfs_plans = Mock(return_value={protocol.digest(plan): plan})
        runner.fence_unowned_radar = Mock()
        raw = dict(kernel_hold(), observed_boottime_us=10000000,
                   heartbeat_boottime_us=10000000, owner=False, live=False,
                   receiver_live=False, revoked=True, main_active=True,
                   dfs_mesh_v1=True, main_epoch='d'*32, main_generation=7,
                   main_freq_mhz=5260, radar_cause='main-radar', radar_epoch='9'*32,
                   radar_generation=6, radar_sequence=4, radar_freq_mhz=5260,
                   radar_boottime_us=1)
        events = Mock()
        events.__enter__ = Mock(return_value=events)
        events.__exit__ = Mock(return_value=False)
        events.wait.side_effect = End()
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(runtime.os.environ, MANET_ACS_RUN_DIR=tmp), \
                patch.object(dfs, 'RadarEvents', return_value=events), \
                patch.object(dfs, 'hold_snapshot', return_value=raw), \
                patch.object(runtime, 'Runtime', return_value=runner), \
                patch.object(runtime, 'boot_time', return_value=10), \
                patch.object(runtime.dfs_safety, 'notify_ready'), \
                patch.object(runtime.dfs_safety, 'stop_dfs') as stop:
            with self.assertRaises(End):
                runtime.watch_background_radar(Path(tmp) / 'phy2/background_hold')
        runner.fence_unowned_radar.assert_not_called()
        stop.assert_not_called()

    def test_only_newer_live_contract_bound_receiver_supersedes_old_latch(self):
        from test_acs_dfs import dfs_plan, kernel_hold
        runner = object.__new__(runtime.Runtime)
        plan = dfs_plan()
        runner.own = min(plan['participants'])
        runner.boot = plan['participants'][runner.own]
        runner.dfs_plans = Mock(return_value={protocol.digest(plan): plan})
        raw = dict(kernel_hold(), observed_boottime_us=10000000, heartbeat_boottime_us=10000000)
        event = ('9'*32, 6, 4, 5260)
        with patch.object(runtime, 'boot_time', return_value=10):
            self.assertTrue(runner.superseded_radar_latch(event, raw))
            main = dict(raw, owner=False, live=False, receiver_live=False,
                        revoked=True, reason='main-transfer', main_active=True,
                        dfs_mesh_v1=True, main_epoch='d'*32, main_generation=7,
                        main_freq_mhz=5260)
            self.assertTrue(runner.superseded_radar_latch(event, main))
            for changed in (dict(raw, live=False), dict(raw, heartbeat_boottime_us=1),
                            dict(raw, epoch='8'*32), dict(raw, generation=6)):
                self.assertFalse(runner.superseded_radar_latch(event, changed))
            self.assertFalse(runner.superseded_radar_latch(('d'*32,7,5,5260), raw))
            self.assertFalse(runner.superseded_radar_latch(('9'*32,8,5,5260), raw))
            self.assertFalse(runner.superseded_radar_latch(('9'*32,6,5,5300), raw))
            runner.dfs_plans.return_value = {}
            self.assertFalse(runner.superseded_radar_latch(event, raw))
            self.assertFalse(runner.superseded_radar_latch(event, main))


if __name__ == '__main__':
    unittest.main()
