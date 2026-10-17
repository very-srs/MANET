"""DFS admission and immediate evacuation regression checks."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_acs import runtime
from test_acs_dfs import dfs_plan, dfs_view
import manet_acs_agreement as protocol


class DfsExecutionTests(unittest.TestCase):
    def test_unanimous_live_capable_plan_can_activate(self):
        plan = dfs_plan()
        protocol.require_dfs_activation(plan, dfs_view(plan['activate_at']),
                                        plan['activate_at'])

    def test_operating_main_token_can_agree_on_refreshed_fallback(self):
        from test_acs_dfs import NOW, committed
        view = dfs_view()
        for status in view.values():
            status['current']['5'] = 5260
        plan = protocol.make_plan(view, {'5': 5260}, False, NOW, 'f' * 32,
                                  fallback={'5': 5220})
        self.assertFalse(plan['moving'])
        state, _, _ = protocol.advance({}, plan['leader'], NOW, view, {}, plan)
        state['commit'] = committed(plan)
        live = dfs_view(plan['activate_at'])
        for status in live.values():
            status['current']['5'] = 5260
        _, _, due = protocol.advance(state, plan['leader'], plan['activate_at'], live, {})
        self.assertEqual(due['dfs']['fallback'], {'5': 5220})
        protocol.require_dfs_activation(due, live, plan['activate_at'])

    def test_radar_requests_block_tx_csa_before_persistence_or_publication(self):
        plan = dfs_plan()
        with tempfile.TemporaryDirectory() as temp:
            runner = object.__new__(runtime.Runtime)
            runner.own, runner.boot = plan['leader'], 'a' * 32
            runner.state, runner.ui_status = {}, {}
            runner.run_dir = Path(temp)
            config = Path(temp) / 'mesh.conf'
            config.write_text('network={\nfrequency=5260\n'
                              'max_oper_chwidth=0\ndisable_ht40=1\n}\n')
            runner.interfaces = lambda: {'5': ('mesh5', config, 5260)}
            runner.dfs_plans = lambda: {protocol.digest(plan): plan}
            calls = []
            runner.publish_dfs_abort = lambda abort: calls.append(['publish'])
            def command(args, **kwargs):
                calls.append(args)
                return ''
            with patch.object(runtime, 'command', side_effect=command), \
                    patch.object(runtime, 'wake_daemon'), \
                    patch.object(runtime, 'private_json_write',
                                 side_effect=lambda *a: calls.append(['persist'])):
                runner.radar_detected(protocol.digest(plan), 'd' * 32, 7)
            self.assertEqual(calls[0], ['iw', 'dev', 'mesh5', 'switch', 'freq',
                                       '5200', 'HT20', 'beacons', '3', 'block-tx'])
            self.assertLess(calls.index(['persist']), calls.index(['publish']))
            self.assertNotIn(['ip', 'link', 'set', 'dev', 'mesh5', 'down'], calls)
            self.assertEqual(config.read_text(),
                             'network={\nfrequency=5200\n'
                             'max_oper_chwidth=0\ndisable_ht40=1\n}\n')

class DfsNetlinkTests(unittest.TestCase):
    def test_background_and_mesh_switch_have_required_uapi_flags(self):
        import manet_dfs as dfs
        import struct
        with patch.object(dfs, 'request') as request:
            dfs.start_cac('mesh5', 5260)
            iface, cmd, attrs = request.call_args.args
            self.assertEqual((iface, cmd), ('mesh5', 94))
            parsed = dict(dfs.attributes(b''.join(attrs)))
            self.assertEqual(parsed[308], b'')
            self.assertEqual(struct.unpack('I', parsed[159])[0], 1)
            self.assertEqual(struct.unpack('I', parsed[38])[0], 5260)
            self.assertEqual(struct.unpack('I', parsed[160])[0], 5260)
            dfs.switch_dfs('mesh5', 5260)
            iface, cmd, attrs = request.call_args.args
            self.assertEqual(cmd, 102)
            parsed = dict(dfs.attributes(b''.join(attrs)))
            self.assertEqual(parsed[191], b'')
            self.assertEqual(struct.unpack('I', parsed[183])[0], 5)
            self.assertEqual(struct.unpack('I', parsed[159])[0], 1)
            self.assertEqual(struct.unpack('I', parsed[38])[0], 5260)
            self.assertEqual(struct.unpack('I', parsed[160])[0], 5260)

    def test_actual_radar_flags_and_nop_are_required(self):
        import manet_dfs as dfs
        text = '''* 5260 MHz [52] (20.0 dBm) (radar detection)
                    DFS state: usable
                  * 5280 MHz [56] (20.0 dBm) (radar detection)
                    DFS state: unavailable
                  * 5300 MHz [60] (20.0 dBm)
                  * 5320 MHz [64] (disabled) (radar detection)'''
        self.assertEqual(dfs.dfs_frequencies(text), [5260])

    def test_staggered_csa_does_not_cancel_committed_plan(self):
        from test_acs_dfs import NOW, committed
        from test_acs import OWN
        plan = dfs_plan()
        state, _, _ = protocol.advance({}, OWN, NOW, dfs_view(), {}, plan)
        state['commit'] = committed(plan)
        view = dfs_view(plan['activate_at'])
        view[OWN]['current']['5'] = 5260
        updated, _, due = protocol.advance(state, OWN, plan['activate_at'], view, {})
        self.assertEqual(due, plan)
        self.assertEqual(updated['phase'], 'attempted')

    def test_main_token_survives_retired_background_owner(self):
        import json
        from test_dfs_release import hold_status
        from test_acs_dfs import NOW
        raw = hold_status(dfs_mesh_v1=True, main_active=True,
                          main_epoch='c' * 32, main_generation=8,
                          main_freq_mhz=5280, owner=False, revoked=True,
                          live=False, receiver_live=False,
                          heartbeat_boottime_us=1)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'hold'
            path.write_text(json.dumps(raw))
            with patch.object(runtime, 'boot_time', return_value=10):
                hold = runtime.read_background_hold(path, 'a' * 32, None, NOW)
            self.assertTrue(hold['live'])
            self.assertEqual((hold['epoch'], hold['generation'], hold['frequency']),
                             ('c' * 32, 8, 5280))

class DfsPreparationTests(unittest.TestCase):
    def test_all_nodes_start_same_candidate_and_reuse_kernel_owner(self):
        import json
        import manet_bgobs
        import manet_rx_policy
        from test_acs_dfs import NOW
        view = dfs_view()
        for status in view.values():
            status['dfs_candidates'] = [5260, 5280]
        prep = dict(leader=min(view), participants={m: s['boot'] for m, s in view.items()},
                    frequency=5260, fallback={'5': 5200}, created=NOW,
                    expires=NOW+1200, nonce='f'*32)
        starts=[]
        with tempfile.TemporaryDirectory() as temp:
            for n, (mac, status) in enumerate(view.items()):
                root=Path(temp)/str(n); root.mkdir()
                observer=root/'phy7/mt76/background_scan'
                observer.parent.mkdir(parents=True)
                hold=observer.parent.parent/'background_hold'
                hold.write_text(json.dumps(dict(enabled=False, owner=False)))
                runner=object.__new__(runtime.Runtime)
                runner.own,runner.boot,runner.state=mac,status['boot'],{}
                runner.run_dir=root
                runner.save=Mock()
                runner.interfaces=lambda: {'5': ('mesh5', root/'wpa',5200)}
                def start(iface, freq):
                    starts.append((mac, freq))
                    hold.write_text(json.dumps(dict(enabled=True,owner=True,freq_mhz=freq)))
                with patch.object(manet_bgobs,'observer_path',return_value=observer), \
                        patch.object(manet_bgobs,'write_command'), \
                        patch.object(manet_rx_policy,'reconcile_receive_paths',return_value='reserved'), \
                        patch.object(runtime.dfs,'start_cac',side_effect=start):
                    runner.prepare_dfs(view,{min(view):{'dfs_prepare':prep}},NOW)
                    runner.prepare_dfs(view,{min(view):{'dfs_prepare':prep}},NOW+1)
                self.assertEqual(runner.state['dfs_prepare'],prep)
        self.assertEqual(starts,[(m,5260) for m in view])

    def test_membership_change_invalidates_cac_intention(self):
        from test_acs_dfs import NOW
        view=dfs_view()
        for status in view.values(): status['dfs_candidates']=[5260]
        runner=object.__new__(runtime.Runtime)
        runner.state={'dfs_prepare':dict(leader=min(view),participants={},frequency=5260,
                                        expires=NOW+100,created=NOW,nonce='f'*32,
                                        fallback={'5':5200})}
        with patch.object(runtime,'background_hold_path',return_value=None), \
                patch.object(runtime.dfs,'start_cac') as start:
            runner.prepare_dfs(view,{},NOW)
        self.assertNotIn('dfs_prepare',runner.state)
        start.assert_not_called()

class DfsCsaTests(unittest.TestCase):
    def test_radar_uses_operating_fallback_until_refresh_activates(self):
        from copy import deepcopy
        plan = dfs_plan()
        proposed = deepcopy(plan)
        proposed['dfs']['fallback']['5'] = 5220
        with tempfile.TemporaryDirectory() as temp:
            runner = object.__new__(runtime.Runtime)
            runner.own, runner.boot = plan['leader'], 'a' * 32
            runner.run_dir = Path(temp)
            runner.state = {'destination': {'plan': plan},
                            'protocol': {'plan': proposed, 'phase': 'prepared'}}
            runner.vacate_dfs = Mock()
            runner.radar_detected = Mock()
            runtime.deliver_background_radar(runner, ('d' * 32, 7, 1, 5260), set())
            runner.vacate_dfs.assert_called_once_with(plan)
            runner.radar_detected.assert_called_once_with(protocol.digest(plan), 'd' * 32, 7)

    def test_entry_uses_csa_and_requires_transferred_main_token(self):
        import json
        from test_acs_dfs import committed, dfs_status
        plan=dfs_plan()
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            config=root/'wpa'; config.write_text('frequency=5200\n')
            hold=root/'hold'
            hold.write_text(json.dumps(dict(main_active=True,main_freq_mhz=5260,
                                           main_epoch='d'*32,main_generation=7)))
            runner=object.__new__(runtime.Runtime)
            runner.state={'protocol':dict(plan=plan,commit=committed(plan))}
            runner.own,runner.run_dir=plan['leader'],root
            runner.status=lambda now: dfs_status(now=now)
            runner.interfaces=lambda: {'5':('mesh5',config,5200)}
            runner.sync_parked=Mock();runner.discovery=Mock()
            calls=[]
            def command(args,**kw):
                calls.append(args)
                return 'channel 52 (5260 MHz)'
            with patch.object(runtime,'command',side_effect=command), \
                    patch.object(runtime,'require_clock'), \
                    patch.object(runtime.time,'time',return_value=plan['activate_at']), \
                    patch.object(runtime,'background_hold_path',return_value=hold), \
                    patch.object(runtime.dfs,'switch_dfs') as switch:
                runner.apply({'5':5260})
                switch.assert_called_once_with('mesh5',5260)
            self.assertFalse(any('reconfigure' in c or 'restart' in c for c in calls))
            self.assertIn('frequency=5260',config.read_text())
            hold.write_text(json.dumps(dict(main_active=False)))
            with patch.object(runtime,'command',side_effect=command), \
                    patch.object(runtime,'require_clock'), \
                    patch.object(runtime.time,'time',return_value=plan['activate_at']), \
                    patch.object(runtime.time,'sleep'), \
                    patch.object(runtime,'background_hold_path',return_value=hold), \
                    patch.object(runtime.dfs,'switch_dfs'):
                with self.assertRaisesRegex(ValueError,'main detector token'):
                    runner.apply({'5':5260})
            self.assertEqual(calls[-1],['ip','link','set','dev','mesh5','down'])

class DfsCapabilityTests(unittest.TestCase):
    def test_gate_needs_board_loaded_driver_matching_cfg_and_actual_dfs_flags(self):
        import manet_dfs as dfs
        import json
        raw=Mock()
        phy='* 5260 MHz [52] (20.0 dBm) (radar detection)\n DFS state: usable'
        for board,driver,abi,flags,expected in (
            (True,'Y',True,phy,True), (False,'Y',True,phy,False),
            (True,'N',True,phy,False), (True,'Y',False,phy,False),
            (True,'Y',True,'* 5260 MHz [52] (20.0 dBm)',False),
        ):
            raw.read_text.return_value=json.dumps(dict(dfs_mesh_v1=abi))
            with patch.object(dfs,'cm4_enabled',return_value=board), \
                    patch.object(dfs.Path,'read_text',return_value=driver):
                self.assertEqual(dfs.capable(raw,flags),expected)

    def test_pending_event_cannot_issue_duplicate_csa(self):
        import json
        from test_acs_dfs import dfs_plan
        plan=dfs_plan()
        with tempfile.TemporaryDirectory() as temp:
            runner=object.__new__(runtime.Runtime)
            runner.run_dir=Path(temp);runner.boot='a'*32
            config=Path(temp)/'wpa';config.write_text('frequency=5260\n')
            runner.interfaces=lambda: {'5':('mesh5',config,5260)}
            with patch.object(runtime,'command',return_value='') as command:
                self.assertTrue(runner.vacate_dfs(plan))
                self.assertTrue(runner.vacate_dfs(plan))
            self.assertEqual(command.call_count,1)


if __name__ == '__main__':
    unittest.main()
