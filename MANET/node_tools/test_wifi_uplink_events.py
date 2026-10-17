"""Event-driven follower regressions, plus real Linux IPC without radio access."""

import json
import os
from pathlib import Path
import socket
import struct
import unittest
from unittest import mock

import manet_wifi_events as events
import manet_wifi_uplink as wifi
import test_wifi_uplink as existing


class EndWatch(BaseException):
    pass


class EventFollowerTests(existing.UplinkHarness):
    def setUp(self):
        super().setUp()
        self.clock = 1000.0
        self.waits = []
        self.syncs = []

    def run_watch(self, *steps):
        steps = iter(steps)
        stream = mock.MagicMock()
        stream.__enter__.return_value = stream
        stream.sync.side_effect = lambda *a, **kw: self.syncs.append((a, kw))
        def wait(timeout):
            self.waits.append(timeout)
            step = next(steps, None)
            if step is None:
                raise EndWatch()
            delay, callback, reasons = step
            self.assertLessEqual(delay, timeout)
            self.clock += delay
            callback()
            return set(reasons)
        stream.wait.side_effect = wait
        with mock.patch.object(wifi, 'UplinkEvents', return_value=stream, create=True), \
                mock.patch.object(wifi.time, 'monotonic', side_effect=lambda: self.clock), \
                mock.patch.dict(os.environ, {'MANET_RADIO_SETUP_LOCK': str(self.root / 'setup.lock')}):
            try:
                self.uplink.watch()
            except EndWatch:
                pass
        return stream

    def test_disabled_watcher_exits_without_subprocess_or_event_daemon(self):
        for profile in ({'enabled': False}, {'enabled': False, 'blocked_ssid': 'EUD-5cc2'}):
            self.uplink.store(self.uplink.config, profile)
            self.radio.calls.clear()
            with mock.patch.object(self.uplink, 'watch_once', wraps=self.uplink.watch_once) as reconcile:
                stream = self.run_watch()
            reconcile.assert_not_called()
            self.assertFalse(self.radio.calls)
            stream.__enter__.assert_not_called()
            self.uplink.refresh_policy.assert_not_called()

    def test_healthy_follower_has_no_three_second_or_fifteen_second_tick(self):
        self.choose()
        self.model.associated = 5180
        self.uplink.reconcile()
        self.run_watch()
        self.assertEqual(self.waits, [120])
        self.assertEqual(self.model.ap_freq, 5180)

    def test_connected_event_starts_only_after_live_association(self):
        self.choose()
        def associate():
            self.assertFalse(self.model.ap_state)
            self.model.associated = 5180
        self.run_watch((.1, associate, {'change'}))
        self.assertEqual(self.model.ap_freq, 5180)
        self.assertTrue(self.model.ap_state)
        self.assertNotIn('disconnect', ' '.join(self.radio.calls))

    def test_disconnect_event_stops_ap_before_reconnect(self):
        self.choose()
        self.model.associated = 5180
        self.uplink.reconcile()
        def lose():
            self.radio.calls.clear()
            self.model.associated = None
        self.run_watch((.1, lose, {'change', 'disconnect'}))
        self.assertFalse(self.model.ap_state)
        stop = self.radio.calls.index('systemctl stop hostapd.service')
        reconnect = next(i for i, c in enumerate(self.radio.calls) if c.endswith('reconnect'))
        self.assertLess(stop, reconnect)
        self.assertLess(self.clock, 1003)

    def test_channel_switch_event_and_ack_without_completion_use_bounded_verify(self):
        self.choose()
        self.model.associated = 5180
        self.uplink.reconcile()
        self.model.csa_works = False
        self.run_watch((.1, lambda: setattr(self.model, 'associated', 5200), {'change'}),
                       (3, lambda: None, set()))
        self.assertGreaterEqual(len(self.waits), 2)
        self.assertEqual(self.waits[1], 3)
        self.assertEqual(self.model.ap_freq, 5200)
        self.assertIn('systemctl start hostapd.service', self.radio.calls)

    def test_dfs_channel_move_and_hostapd_crash_recover_from_events(self):
        self.enable_follower()
        self.choose(dict(existing.PROFILE, freq=5260))
        self.model.associated = 5260
        self.uplink.reconcile()
        def move():
            self.model.associated = 5280
            self.model.ap_state = ''
            self.radio.active.discard('hostapd.service')
        self.run_watch((.1, move, {'change'}))
        self.assertEqual(self.model.ap_freq, 5280)
        self.assertTrue(self.model.ap_follow)

    def test_control_loss_reconnect_coalescing_clears_dfs_failure_latch(self):
        self.enable_follower()
        self.choose(dict(existing.PROFILE, freq=5260))
        self.model.associated = 5260
        self.uplink.publish('connected', freq=5260, dfs_failed_freq=5260)
        self.run_watch((.1, lambda: None, {'change', 'disconnect'}))
        self.assertTrue(self.model.ap_state)
        self.assertNotIn('dfs_failed_freq', json.loads(self.uplink.status_path.read_text()))

    def test_busy_lock_retains_event_and_retries_in_three_seconds(self):
        self.choose()
        self.model.associated = 5180
        original = self.uplink.watch_once
        calls = []
        def once(**kwargs):
            calls.append(kwargs)
            return False if len(calls) == 2 else original(**kwargs)
        with mock.patch.object(self.uplink, 'watch_once', side_effect=once):
            self.run_watch((.1, lambda: None, {'disconnect'}),
                           (3, lambda: None, set()))
        self.assertGreaterEqual(len(self.waits), 2)
        self.assertEqual(self.waits[1], 3)
        self.assertTrue(calls[1]['disconnected'])
        self.assertTrue(calls[2]['disconnected'])

    def test_csa_countdown_notifications_do_not_exhaust_follower_starts(self):
        self.enable_follower()
        self.choose(dict(existing.PROFILE, freq=5260))
        self.model.associated = 5260
        self.uplink.reconcile()
        def start():
            self.model.ap_state = ''
            self.model.ap_start_fails = True
            self.radio.calls.clear()
        def finish():
            self.assertNotIn('systemctl start hostapd.service', self.radio.calls)
            self.model.associated = 5280
            self.model.ap_start_fails = False
        self.run_watch((.1, start, {'csa-start'}),
                       (.1, lambda: None, {'change'}),
                       (.1, finish, {'csa-complete'}))
        self.assertEqual(self.model.ap_freq, 5280)
        self.assertTrue(self.model.ap_state)

    def test_profile_event_blocks_own_hotspot_and_finishes_cleanup(self):
        self.choose()
        self.model.associated = 5180
        def block():
            self.uplink.radio.conf.write_text(self.uplink.radio.conf.read_text() + 'lan_ap_ssid=house\n')
            self.uplink.store(self.uplink.config, dict(existing.PROFILE, ssid='house-5cc2'))
        self.run_watch((.1, block, {'change'}))
        self.assertFalse(self.uplink.profile()['enabled'])
        self.assertFalse(self.uplink.saved.exists())
        self.assertFalse(self.uplink.watch_pending())
        self.assertEqual(len(self.waits), 1)

    def test_disabled_scan_repair_retries_then_exits(self):
        self.uplink.store(self.uplink.repair_path, {'kind': 'ap', 'iface': 'wlan1'})
        self.radio.fail_on.add('systemctl start hostapd.service')
        self.run_watch((3, lambda: self.radio.fail_on.clear(), set()))
        self.assertEqual(self.waits, [3])
        self.assertFalse(self.uplink.repair_path.exists())
        self.assertIn('hostapd.service', self.radio.active)

    def test_boot_restore_recreates_client_before_waiting(self):
        self.uplink.store(self.uplink.config, existing.PROFILE)
        self.run_watch()
        self.assertIn('manet-wifi-client@wlan1.service', self.radio.active)
        self.assertFalse(self.model.ap_state)

    def test_dhcp_event_refreshes_policy_and_quiet_safety_interval_is_120(self):
        self.choose()
        self.model.associated = 5180
        self.model.address = ''
        self.uplink.reconcile()
        self.uplink.refresh_policy.reset_mock()
        self.run_watch((1, lambda: setattr(self.model, 'address', '192.0.2.20'), {'address'}),
                       (119, lambda: None, set()))
        self.assertEqual(self.uplink.refresh_policy.call_count, 2)
        self.assertEqual(self.waits, [120, 119, 120])
        self.assertTrue(any(kw.get('probe') for _, kw in self.syncs))

    def test_quiet_safety_pass_checks_live_radio_without_dispatcher(self):
        self.choose()
        self.model.associated = 5180
        self.uplink.reconcile()
        self.radio.calls.clear()
        def settled():
            self.radio.calls.clear()
        with mock.patch.object(self.uplink, 'refresh_policy',
                               side_effect=lambda: wifi.WifiUplink.refresh_policy(self.uplink)):
            self.run_watch((120, settled, set()))
        self.assertIn('iw dev wlan1 link', self.radio.calls)
        self.assertTrue(any(c.startswith('hostapd_cli ') for c in self.radio.calls))
        self.assertNotIn('/usr/local/bin/manet-uplink-dispatch.sh reconcile', self.radio.calls)
        self.assertTrue(any(kw.get('probe') for _, kw in self.syncs))

    def test_safety_pass_still_dispatches_a_missed_address_change(self):
        self.choose()
        self.model.associated = 5180
        self.uplink.reconcile()
        def changed():
            self.uplink.refresh_policy.reset_mock()
            self.model.address = '192.0.2.99'
        self.run_watch((120, changed, set()))
        self.uplink.refresh_policy.assert_called_once_with()


class LinuxEventTransportTests(existing.UplinkHarness):
    def source(self):
        stream = events.UplinkEvents(self.uplink)
        self.addCleanup(stream.__exit__)
        return stream

    def server(self, name):
        path = self.uplink.run / name
        path.parent.mkdir(exist_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(server.close)
        server.bind(str(path))
        server.settimeout(1)
        return server, path

    def test_unrelated_run_writes_do_not_wake_selector(self):
        # Observe readiness before the userspace filename filter. Returning
        # an empty event set after waking for every /run write is still costly.
        self.uplink.radio.gateway = self.uplink.run / 'manet-uplink/mesh-gateway.state'
        self.uplink.radio.gateway.parent.mkdir()
        stream = self.source()
        for directories_present in (False, True):
            with self.subTest(control_directories_present=directories_present):
                if directories_present:
                    for name in ('hostapd', 'wpa_supplicant'):
                        (self.uplink.run / name).mkdir(exist_ok=True)
                stream.sync('wlan1')
                stream.wait(.01)
                for name in ('mesh-voice.json.tmp', 'mesh-voice.json',
                             'channel-election.lock', 'manet-acs-status.json'):
                    path = self.uplink.run / name
                    path.write_text('unrelated')
                    path.unlink()
                self.assertEqual(stream.selector.select(0), [])

    def test_control_attach_channel_loss_and_ignored_scan_notifications(self):
        server, path = self.server('wpa_supplicant/wlan1')
        stream = self.source()
        stream.sync('wlan1')
        message, peer = server.recvfrom(8192)
        self.assertEqual(message, b'ATTACH')
        server.sendto(b'OK\n', peer)
        self.assertEqual(stream.wait(.1), {'change'})
        for event, extra in (('CONNECTED', set()), ('CHANNEL-SWITCH', {'csa-complete'}),
                             ('STARTED-CHANNEL-SWITCH', {'csa-start'})):
            server.sendto(('<3>CTRL-EVENT-' + event + ' freq=5200').encode(), peer)
            self.assertEqual(stream.wait(.1), {'change'} | extra)
        server.sendto(b'<3>CTRL-EVENT-SCAN-RESULTS', peer)
        self.assertEqual(stream.wait(.01), set())
        server.sendto(b'<3>CTRL-EVENT-DISCONNECTED reason=3', peer)
        self.assertEqual(stream.wait(.1), {'change', 'disconnect'})

    def test_hostapd_enable_csa_and_disable_events(self):
        server, path = self.server('hostapd/manetap')
        stream = self.source()
        stream.sync('wlan1')
        _, peer = server.recvfrom(8192)
        server.sendto(b'OK\n', peer)
        stream.wait(.1)
        for event in ('AP-ENABLED', 'AP-CSA-FINISHED', 'AP-DISABLED', 'DFS-CAC-START'):
            server.sendto(('<3>' + event + ' freq=5260').encode(), peer)
            self.assertEqual(stream.wait(.1), {'change'})

    def test_socket_directory_creation_and_restart_reattach(self):
        stream = self.source()
        stream.sync('wlan1')
        server, path = self.server('wpa_supplicant/wlan1')
        self.assertIn('change', stream.wait(.1))
        stream.sync('wlan1')
        _, peer = server.recvfrom(8192)
        server.sendto(b'OK\n', peer)
        stream.wait(.1)
        server.close()
        path.unlink()
        replacement, _ = self.server('wpa_supplicant/wlan1')
        self.assertIn('change', stream.wait(.1))
        stream.sync('wlan1')
        message, new_peer = replacement.recvfrom(8192)
        self.assertEqual(message, b'ATTACH')
        self.assertNotEqual(peer, new_peer)

    def test_atomic_intent_repair_lifecycle_and_ignored_own_status_writes(self):
        self.uplink.config.parent.mkdir(exist_ok=True)
        stream = self.source()
        stream.sync('wlan1')
        self.uplink.store(self.uplink.config, existing.PROFILE)
        self.assertEqual(stream.wait(.1), {'change'})
        self.uplink.store(self.uplink.config, existing.PROFILE)
        self.uplink.publish('connected')
        (self.uplink.run / 'unrelated').write_text('ignore')
        self.assertEqual(stream.wait(.01), set())
        self.uplink.store(self.uplink.repair_path, {'kind': 'ap'})
        self.assertEqual(stream.wait(.1), {'change'})
        for _ in range(2):
            (self.uplink.events_dir / 'ap-event').touch()
            self.assertEqual(stream.wait(.1), {'change'})

    def test_rtnetlink_filters_other_interfaces_and_link_statistics(self):
        stream = self.source()
        stream.sync('wlan1')
        def packet(name, flags=1, extra=b''):
            raw = name.encode() + b'\0'
            attr = struct.pack('HH', len(raw) + 4, 3) + raw
            attr += b'\0' * (-len(attr) % 4)
            body = struct.pack('BBHiII', 0, 0, 1, 4, flags, 0) + attr + extra
            return struct.pack('IHHII', len(body) + 16, 16, 0, 0, 0) + body
        self.assertEqual(stream.net_events(packet('bat0')), set())
        self.assertEqual(stream.net_events(packet('wlan1')), {'change'})
        self.assertEqual(stream.net_events(packet('wlan1', extra=struct.pack('HHI', 8, 7, 100))), set())
        self.assertEqual(stream.net_events(packet('wlan1', flags=0)), {'change'})
        body = struct.pack('BBBBI', socket.AF_INET, 24, 0, 0, 4)
        address = struct.pack('IHHII', len(body) + 16, 20, 0, 0, 0) + body
        with mock.patch.object(socket, 'if_indextoname', return_value='wlan1'):
            self.assertEqual(stream.net_events(address), {'address'})

    def test_silent_control_timeout_backs_off_until_next_safety_pass(self):
        server, path = self.server('wpa_supplicant/wlan1')
        stream = self.source()
        stream.sync('wlan1')
        server.recvfrom(8192)  # Leave ATTACH unanswered.
        stream.controls[path]['deadline'] = 0
        self.assertEqual(stream.wait(.1), {'change'})
        self.assertNotIn(path, stream.controls)
        stream.sync('wlan1')
        self.assertNotIn(path, stream.controls)
        stream.sync('wlan1', probe=True)
        self.assertIn(path, stream.controls)

    def test_run_and_var_run_aliases_keep_both_gateway_and_service_events(self):
        alias = self.root / 'var-run'
        alias.symlink_to(self.uplink.run, target_is_directory=True)
        self.uplink.radio.gateway = alias / 'manet-uplink/mesh-gateway.state'
        stream = self.source()
        stream.sync('wlan1')
        self.uplink.radio.gateway.touch()
        self.assertEqual(stream.wait(.1), {'change'})
        (self.uplink.events_dir / 'client-event').touch()
        self.assertEqual(stream.wait(.1), {'change'})

    def test_removed_control_directory_is_recreated_without_watching_run(self):
        stream = self.source()
        self.uplink.radio.gateway = self.uplink.run / 'manet-uplink/mesh-gateway.state'
        stream.sync('wlan1')
        directory = self.uplink.run / 'wpa_supplicant'
        directory.rmdir()
        self.assertEqual(stream.wait(.1), {'change'})
        stream.sync('wlan1')
        self.assertTrue(directory.is_dir())
        server, path = self.server('wpa_supplicant/wlan1')
        self.assertEqual(stream.wait(.1), {'change'})
        stream.sync('wlan1')
        message, peer = server.recvfrom(8192)
        self.assertEqual(message, b'ATTACH')
        server.sendto(b'OK\n', peer)
        stream.wait(.1)
        stream.wait(.01)
        (self.uplink.run / 'mesh-voice.json').write_text('unrelated')
        self.assertEqual(stream.selector.select(0), [])

    def test_overflow_and_directory_removal_request_live_resubscription(self):
        stream = self.source()
        stream.sync('wlan1')
        self.assertEqual(stream.file_events(struct.pack('iIII', -1, 0x4000, 0, 0)), {'change'})
        wd = next(iter(stream.watches))
        self.assertEqual(stream.file_events(struct.pack('iIII', wd, 0x8000, 0, 0)), {'change'})
        self.assertNotIn(wd, stream.watches)


class EventUnitTests(unittest.TestCase):
    def test_idle_wakeups_and_crash_notifications_are_packaged(self):
        root = Path(__file__).resolve().parents[1]
        service = (root / 'systemd/manet-wifi-uplink.service').read_text()
        self.assertIn('Wants=manet-wifi-uplink.path', service)
        self.assertIn('Also=manet-wifi-uplink.path', service)
        path = (root / 'systemd/manet-wifi-uplink.path').read_text()
        self.assertIn('PathChanged=/etc/manet/wifi-uplink.json', path)
        self.assertIn('PathChanged=/run/manet-wifi-uplink/scan-repair.json', path)
        for name in ('manet-wifi-client@.service', 'hostapd.service.d/30-wifi-uplink-events.conf'):
            source = (root / 'systemd' / name).read_text()
            self.assertIn('ExecStopPost=/usr/bin/touch', source)
            self.assertIn('ExecStartPost=/usr/bin/touch', source)
            self.assertIn('RuntimeDirectory=manet-wifi-uplink', source)
            self.assertIn('RuntimeDirectoryPreserve=yes', source)
            marker = ('client-event' if name.startswith('manet-wifi-client') else 'ap-event')
            for directive in ('ExecStartPost', 'ExecStopPost'):
                self.assertIn(f'{directive}=/usr/bin/touch /run/manet-wifi-uplink/{marker}', source)


if __name__ == '__main__':
    unittest.main()
