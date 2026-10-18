"""DFS ownership outlives general agreements and release transactions."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_acs import AcsHarness, runtime, OWN, PEER, THIRD
from test_acs_dfs import NOW, dfs_plan, dfs_view, committed
from test_acs_agreement import status
import manet_acs_agreement as protocol
import manet_dfs as dfs


class OperatingContractTests(AcsHarness):
    def operating(self):
        self.configure(2437, 5260)
        runner = runtime.Runtime()
        plan = dfs_plan()
        runner.state.update(destination=dict(plan=plan, commit=committed(plan)),
                            protocol=dict(plan=plan, commit=committed(plan)))
        # Seed the independent record as a successful earlier DFS entry would.
        runtime.private_json_write(runner.run_dir / 'manet-dfs-contract.json',
            dict(boot=runner.boot, own=runner.own, iface='wlan1', operating=plan))
        return runner, plan

    def ordinary(self, runner, channels):
        view = dfs_view()
        for s in view.values():
            s['current'] = {'2.4': 2437, '5': 5260}
            s['allowed']['2.4'] = sorted(protocol.CHANNELS['2.4'])
        view[THIRD] = status('c', current={'2.4': 2437, '5': 5200})
        if channels == {'2.4': 2437}:
            self.reports([[dict(channel=f, busy_pct=5, foreign_pct=5) for f in (2437, 5260)]] * 2,
                         [OWN, PEER])
            channels, limp = runner.score(view, candidates=True)
            self.assertEqual(channels, {'2.4': 2437})
        plan = protocol.make_plan(view, channels, False, NOW + 180, 'f' * 32)
        state, _, _ = protocol.advance({}, OWN, NOW + 180, view, {}, plan)
        votes = {m: {'vote': {'plan': protocol.digest(plan), 'boot': v['boot']}}
                 for m, v in view.items()}
        state, _, _ = protocol.advance(state, OWN, NOW + 181, view, votes)
        state, _, _ = protocol.advance(state, OWN, plan['deadline'], view, {})
        _, _, due = protocol.advance(state, OWN, plan['activate_at'], view, {})
        self.assertEqual(due, plan)
        runner.state['protocol'] = dict(plan=plan, commit=committed(plan))
        runner.adopt_destination(dict(plan=plan, commit=committed(plan)), view[OWN], 'test')
        return plan, view[OWN]

    def test_mixed_member_24_only_agreement_preserves_operating_fallback(self):
        with patch.dict(os.environ, self.env):
            runner, old = self.operating()
            plan, local = self.ordinary(runner, {'2.4': 2437})
            with patch.object(runner, 'status', return_value=local):
                runner.apply(plan['channels'])
            self.assertIn(protocol.digest(old), runner.dfs_plans())
            with patch.object(runner, 'vacate_dfs') as fence, \
                    patch.object(runner, 'radar_detected') as deliver:
                runtime.deliver_background_radar(runner, ('d'*32, 7, 1, 5260), set())
            fence.assert_called_once_with(old)
            deliver.assert_called_once()

    def test_failed_departure_keeps_contract_even_after_config_changes(self):
        with patch.dict(os.environ, self.env):
            runner, old = self.operating()
            plan, local = self.ordinary(runner, {'5': 5200})
            with patch.object(runner, 'status', return_value=local), \
                    patch.object(runtime, 'command', return_value='channel 52 (5260 MHz)'), \
                    patch.object(runtime.time, 'sleep'):
                with self.assertRaises(ValueError):
                    runner.apply(plan['channels'])
            self.assertIn(protocol.digest(old), runner.dfs_plans())

    def test_radar_without_contract_stops_operating_radio(self):
        with patch.dict(os.environ, self.env):
            runner, _ = self.operating()
            (runner.run_dir / 'manet-dfs-contract.json').unlink()
            runner.state = {}
            with patch.object(runner, 'vacate_dfs') as fence:
                runtime.deliver_background_radar(runner, ('d'*32, 7, 1, 5260), set())
            fence.assert_called_once_with()

    def test_native_bss_keeps_evacuation_with_missing_or_stale_contract(self):
        with patch.dict(os.environ, self.env):
            runner, old = self.operating()
            with patch.object(dfs, 'supplicant_control',
                              return_value='MANET_DFS_V3 owner=0'), \
                    patch.object(runtime, 'command') as command:
                self.assertFalse(runner.vacate_dfs(old))
                (runner.run_dir / 'manet-dfs-contract.json').unlink()
                runner.state = {}
                runner.fence_unowned_radar(5260)
                command.assert_not_called()

    def test_unknown_bss_ownership_cannot_suppress_radar_fence(self):
        with patch.dict(os.environ, self.env):
            runner, _ = self.operating()
            with patch.object(dfs, 'supplicant_control',
                              return_value='UNKNOWN COMMAND'), \
                    patch.object(runtime, 'command') as command:
                self.assertTrue(runner.vacate_dfs())
                command.assert_any_call(['ip', 'link', 'set', 'dev', 'wlan1', 'down'], timeout=2)

    def test_verified_departure_retires_record_and_bss_opt_in(self):
        with patch.dict(os.environ, self.env):
            runner, old = self.operating()
            plan, local = self.ordinary(runner, {'5': 5200})
            with patch.object(runner, 'status', return_value=local), \
                    patch.object(runtime, 'command', return_value='channel 40 (5200 MHz)'), \
                    patch.object(dfs, 'external_owner') as owner:
                runner.apply(plan['channels'])
            owner.assert_called_once_with('wlan1', False)
            self.assertFalse((runner.run_dir / 'manet-dfs-contract.json').exists())
            self.assertNotIn(protocol.digest(old), runner.dfs_plans())

    def test_replacement_contract_requires_verified_main_token(self):
        with patch.dict(os.environ, self.env):
            runner, old = self.operating()
            replacement = deepcopy(old)
            replacement['dfs']['fallback']['5'] = 5220
            runner.state['protocol'] = dict(plan=replacement, commit=committed(replacement))
            local = dfs_view(replacement['activate_at'])[OWN]
            local['current']['5'] = 5260
            raw = self.root / 'hold'
            raw.write_text(json.dumps(dict(main_active=False)))
            with patch.object(runtime, 'background_hold_path', return_value=raw), \
                    patch.object(runtime.time, 'time', return_value=replacement['activate_at']), \
                    patch.object(runtime.time, 'sleep'), \
                    patch.object(runtime, 'command', return_value='channel 52 (5260 MHz)'), \
                    patch.object(runner, 'status', return_value=local), \
                    patch.object(dfs, 'guardian_ready', return_value=True), \
                    patch.object(dfs, 'external_owner'):
                with self.assertRaisesRegex(ValueError, 'main detector token'):
                    runner.apply({'5': 5260})
                path = runner.run_dir / 'manet-dfs-contract.json'
                self.assertEqual(json.loads(path.read_text())['operating'], old)
                raw.write_text(json.dumps(dict(main_active=True, main_freq_mhz=5260,
                    main_epoch='d'*32, main_generation=7)))
                runner.apply({'5': 5260})
                self.assertEqual(json.loads(path.read_text())['operating'], replacement)
                self.assertNotIn('pending', json.loads(path.read_text()))


class CapabilityTests(unittest.TestCase):
    def test_kernel_and_new_disk_binary_do_not_authorize_old_running_process(self):
        with patch.object(dfs, 'cm4_enabled', return_value=True), \
                patch.object(Path, 'read_text', return_value='Y'):
            # No live mesh control endpoint exists in this fixture.
            self.assertFalse(dfs.capable(Path('/hold'), '* 5260 MHz (radar detection)',
                                         raw={'dfs_mesh_v1': True}))

    def test_capability_uses_live_control_identity_and_guardian(self):
        for reply, ready, expected in (
                ('MANET_DFS_V3 owner=0', True, True),
                ('MANET_DFS_V3 owner=1', True, True),
                ('UNKNOWN COMMAND', True, False),
                ('MANET_DFS_V3 owner=0', False, False)):
            with self.subTest(reply=reply, ready=ready), \
                    patch.object(dfs, 'cm4_enabled', return_value=True), \
                    patch.object(Path, 'read_text', return_value='Y'), \
                    patch.object(dfs, 'supplicant_control', return_value=reply), \
                    patch.object(dfs, 'guardian_ready', return_value=ready):
                self.assertEqual(dfs.capable(Path('/hold'), '* 5260 MHz (radar detection)',
                    raw={'dfs_mesh_v1': True}, iface='mesh5'), expected)

    def test_dfs_package_refuses_old_or_missing_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / 'repo/MANET/binaries_arm64/wpa_supplicant_manet'
            binary.parent.mkdir(parents=True)
            for old in (None, b'wpa_supplicant v2.11-manet\0'):
                if old:
                    binary.write_bytes(old)
                result = subprocess.run(['bash', '-c', '. "$1"; stage_mesh_supplicant "$2" "$3"',
                    'test', str(Path(__file__).parents[1] / 'packaging/lib-supplicant.sh'),
                    str(root/'stage'), str(root/'repo')], capture_output=True,
                    env=dict(os.environ, MANET_DFS_ENABLED='1'))
                self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()

class GuardianHandoffTests(unittest.TestCase):
    def setUp(self):
        import manet_dfs_safety
        self.safety = manet_dfs_safety
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.boot = self.root / 'proc/sys/kernel/random/boot_id'
        self.boot.parent.mkdir(parents=True)
        self.boot.write_text('a' * 32)
        (self.root / 'run').mkdir()
        self.config = self.root / 'etc/wpa_supplicant/wpa_supplicant-mesh5.conf'
        self.config.parent.mkdir(parents=True)
        self.config.write_text('frequency=5260\n')
        self.calls = []
        self.running = True
        self.frequency = 5260
        self.patch = patch.object(self.safety, 'safety_command', side_effect=self.command)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def command(self, args):
        self.calls.append(args)
        if args == ['iw', 'dev']:
            return f'phy#2\n Interface mesh5\n  type mesh point\n  channel 52 ({self.frequency} MHz)\n'
        if args[:2] == ['systemctl', 'stop']:
            self.running = False
        return ''

    def contract(self):
        (self.root / 'run/manet-dfs-contract.json').write_text(json.dumps(
            dict(boot='a'*32, own=OWN, iface='mesh5', operating=dfs_plan())))

    def test_handoff_fences_before_service_stop_and_saves_safe_resume(self):
        self.contract()
        self.safety.stop_dfs(self.root)
        self.assertEqual(self.calls[2], ['ip', 'link', 'set', 'dev', 'mesh5', 'down'])
        self.assertEqual(self.calls[3], ['systemctl', 'stop', 'wpa_supplicant@mesh5.service'])
        self.assertFalse(self.running)
        self.assertIn('frequency=5200', self.config.read_text())
        self.safety.resume_dfs(self.root)
        check = ['systemctl', 'is-active', '--quiet', 'manet-dfs-watch.service']
        self.assertLess(self.calls.index(check), self.calls.index(
            ['systemctl', 'start', 'wpa_supplicant@mesh5.service']))

    def test_static_bss_keeps_native_protection_during_guardian_handoff(self):
        self.contract()
        def static(args):
            result = self.command(args)
            return 'MANET_DFS_V3 owner=0' if args[0] == 'wpa_cli' else result
        with patch.object(self.safety, 'safety_command', side_effect=static):
            self.safety.stop_dfs(self.root)
        self.assertTrue(self.running)
        self.assertFalse(any(c[0] == 'ip' for c in self.calls))

    def test_missing_contract_leaves_dfs_supplicant_stopped(self):
        self.safety.stop_dfs(self.root)
        self.assertFalse(self.running)
        self.assertFalse((self.root / 'run/manet-dfs-resume.json').exists())

    def test_idle_halow_does_not_prevent_fencing_the_dfs_mesh(self):
        self.contract()
        def idle_halow(args):
            result = self.command(args)
            if args == ['iw', 'dev']:
                return 'Interface wlan2\n type mesh point\n' + result
            return result
        with patch.object(self.safety, 'safety_command', side_effect=idle_halow):
            self.safety.stop_dfs(self.root)
        self.assertFalse(self.running)
        self.assertFalse(any('wlan2' in ' '.join(c) for c in self.calls))

    def test_halow_on_a_mapped_dfs_number_is_left_running(self):
        # Morse reports S1G channels as 5 GHz numbers, e.g. channel 132 (5660 MHz).
        self.contract()
        for iface, driver in (('wlan2', 'morse_usb'), ('mesh5', 'mt7915e')):
            device = self.root / f'sys/class/net/{iface}/device'
            device.mkdir(parents=True)
            (device / 'driver').symlink_to(f'../../../bus/drivers/{driver}')
        def halow(args):
            result = self.command(args)
            if args == ['iw', 'dev']:
                return 'Interface wlan2\n type mesh point\n channel 132 (5660 MHz)\n' + result
            return result
        with patch.object(self.safety, 'safety_command', side_effect=halow):
            self.safety.stop_dfs(self.root)
        self.assertFalse(self.running)
        self.assertFalse(any('wlan2' in ' '.join(c) for c in self.calls))
        self.assertIn(['ip', 'link', 'set', 'dev', 'mesh5', 'down'], self.calls)

    def test_guardian_start_failure_cannot_resume_radio(self):
        self.contract()
        self.safety.stop_dfs(self.root)
        self.calls.clear()
        with patch.object(self.safety, 'safety_command', side_effect=subprocess.CalledProcessError(1, 'systemctl')):
            with self.assertRaises(subprocess.CalledProcessError):
                self.safety.resume_dfs(self.root)
        self.assertFalse(self.running)
        self.assertTrue((self.root / 'run/manet-dfs-resume.json').exists())

    def test_manager_quiesce_keeps_guardian_and_radar_delivery(self):
        unit = (Path(__file__).parents[1] / 'systemd/manet-dfs-watch.service').read_text()
        self.assertNotIn('PartOf=node-manager.service', unit)
        self.assertIn('Type=notify', unit)
        self.assertIn('ExecStopPost=/usr/local/bin/manet_dfs_safety.py stop', unit)
        from test_node_update import update
        with patch.object(update, 'run_command', return_value=b'inactive') as systemctl:
            update.Updater.quiesce_config(object.__new__(update.Updater))
        self.assertFalse(any('manet-dfs-watch.service' in c.args[0] for c in systemctl.call_args_list))
        # Radar immediately after quiesce still executes the saved contract.
        runner = object.__new__(runtime.Runtime)
        runner.run_dir = self.root / 'run'
        runner.own, runner.boot, runner.state = OWN, 'a'*32, {}
        self.contract()
        runner.vacate_dfs = Mock()
        runner.radar_detected = Mock()
        runtime.deliver_background_radar(runner, ('d'*32, 7, 1, 5260), set())
        runner.vacate_dfs.assert_called_once_with(dfs_plan())
        runner.radar_detected.assert_called_once()
