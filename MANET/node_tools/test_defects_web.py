"""Regression coverage for measurement and voice review defects."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import decoder
import manet_manage as manage
import test_voice_beacon as beacon


class OriginatorTests(unittest.TestCase):
    def hop_count(self, rows):
        registry = {'peer': {
            'IPV4_ADDRESS': '10.0.0.2',
            'MAC_ADDRESSES': '0c:bf:74:00:2b:f1',
        }}
        with patch.object(manage, 'parse_registry', return_value=registry), \
                patch.object(manage.subprocess, 'run', return_value=
                             SimpleNamespace(stdout=rows, returncode=0)):
            return manage.get_session_hop_count('10.0.0.2')

    def test_padded_batctl_rows_with_and_without_selected_marker(self):
        # Format from mesh-throughput-mean.sh, including separate * and (.
        for marker in ('    * ', '      '):
            for nexthop, hops in (('0c:bf:74:00:2b:f1', 1),
                                  ('00:0a:52:09:60:fe', 2)):
                with self.subTest(marker=marker, nexthop=nexthop):
                    row = (marker + '0c:bf:74:00:2b:f1    0.372s '
                           '(       43.2)  ' + nexthop + ' [     wlan2]')
                    self.assertEqual(self.hop_count(row), (hops, 'batctl'))

    def test_selected_route_wins_over_an_earlier_alternative(self):
        rows = (
            '      Originator        last-seen ( throughput)  Nexthop'
            '           [outgoingIF]\n'
            '      0c:bf:74:00:2b:f1    0.372s (        4.2)  '
            '00:0a:52:09:60:fe [     wlan0]\n'
            '    * 0c:bf:74:00:2b:f1    0.372s (       43.2)  '
            '0c:bf:74:00:2b:f1 [     wlan2]\n')
        self.assertEqual(self.hop_count(rows), (1, 'batctl'))

    def test_headers_and_unknown_originators_stay_unknown(self):
        self.assertEqual(self.hop_count('Originator last-seen Nexthop'),
                         (None, 'unknown'))
        self.assertEqual(self.hop_count(
            ' * 00:0a:52:09:60:fe 0.372s ( 43.2) '
            '0c:bf:74:00:2b:f1 [ wlan2]'), (None, 'unknown'))


class MeasurementBudgetTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.status = {'running': False}
        for name, value in (
                ('SESSIONS_DIR', str(self.root)),
                ('_measure_status', self.status),
                ('_measure_mono', {'started': None, 'current': None})):
            patcher = patch.object(manage, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.pair = {'src_ip': '10.0.0.1', 'dst_ip': '10.0.0.2',
                     'src_name': 'one', 'dst_name': 'two'}

    def start_batch(self, pairs, tests, duration):
        handler = manage.ManageRoutes()
        handler.path = '/api/measure/start'
        handler.read_body = Mock(return_value=json.dumps({
            'label': 'budget', 'pairs': [self.pair] * pairs,
            'tests': tests, 'duration': duration,
        }).encode())
        handler.send_json = Mock()
        with patch.object(manage.threading, 'Thread') as worker:
            handler.manage_do_POST()
        return handler.send_json.call_args, worker

    def test_admission_includes_pause_route_storage_and_setup(self):
        for pairs, tests in ((64, ['tcp_1stream']),
                             (32, ['tcp_1stream', 'reverse']),
                             (53, ['tcp_1stream'])):
            with self.subTest(pairs=pairs, tests=tests):
                self.status['running'] = False
                response, worker = self.start_batch(pairs, tests, 30)
                self.assertEqual(response.args[1:], (400,))
                self.assertFalse(response.args[0]['ok'])
                self.assertIn('one hour', response.args[0]['error'])
                worker.assert_not_called()

    def test_batches_that_fit_still_start(self):
        for pairs, tests in ((52, ['tcp_1stream']), (64, ['icmp_ping'])):
            with self.subTest(pairs=pairs, tests=tests):
                self.status['running'] = False
                response, worker = self.start_batch(pairs, tests, 30)
                self.assertTrue(response.args[0]['ok'])
                worker.return_value.start.assert_called_once()

    def run_worker(self, slow_stage):
        clock = [0.0]
        topology = {'active_interfaces': [], 'halow_channel': '',
                    'halow_bw': '', 'ch_2g': ''}

        def snapshot():
            if slow_stage == 'topology':
                clock[0] = 3601.0
            return topology

        def route(_):
            if slow_stage == 'route':
                clock[0] = 3580.0
            return 1, 'batctl'

        real_save = manage.save_result

        def save(*args):
            real_save(*args)
            if slow_stage == 'storage':
                clock[0] = 3601.0

        with patch.object(manage.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(manage.time, 'sleep') as pause, \
                patch.object(manage, 'snapshot_topology', side_effect=snapshot), \
                patch.object(manage, 'get_session_hop_count', side_effect=route), \
                patch.object(manage, 'save_result', side_effect=save), \
                patch.object(manage, 'run_local_ping',
                             return_value={'loss_pct': 0}) as ping:
            manage.run_measurement_session(
                'deadline', [self.pair] * 2, ['icmp_ping'], 30, '4M')
        return ping, pause

    def test_worker_stops_if_setup_or_route_uses_the_remaining_budget(self):
        for stage in ('topology', 'route'):
            with self.subTest(stage=stage):
                ping, pause = self.run_worker(stage)
                ping.assert_not_called()
                pause.assert_not_called()
                self.assertFalse(self.status['running'])
                self.assertIn('one-hour', self.status['error'])
                self.assertEqual(self.status['done'], 0)

    def test_worker_keeps_saved_results_and_stops_after_storage_overrun(self):
        ping, pause = self.run_worker('storage')
        ping.assert_called_once()
        pause.assert_not_called()
        self.assertFalse(self.status['running'])
        self.assertIn('one-hour', self.status['error'])
        self.assertEqual(self.status['done'], 1)
        self.assertEqual(len(manage.get_session_results('deadline')), 1)
        self.assertIsNone(self.status['current'])


class VoiceRegistryTests(unittest.TestCase):
    def test_registry_hostnames_unescape_writer_quote_breaks(self):
        names = ["Mike's radio", "'quoted'", "two''quotes", r"literal\path"]
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / 'registry'
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                for i, name in enumerate(names, 1):
                    decoder.emit(f'NODE_{i:012x}_IPV4_ADDRESS', f'10.0.0.{i}')
                    decoder.emit(f'NODE_{i:012x}_HOSTNAME', name)
                    decoder.emit(f'NODE_{i:012x}_NODE_STATE', 'ACTIVE')
            registry.write_text(output.getvalue())
            with patch.object(beacon.voice, 'REGISTRY_FILE', str(registry)):
                self.assertEqual(beacon.voice.read_registry({'10.0.0.4'}),
                                 [(f'10.0.0.{i}', name)
                                  for i, name in enumerate(names[:3], 1)])


class VoiceDefectTests(unittest.TestCase):
    def setUp(self):
        # Reuse the existing fake pipelines while exercising the real daemon.
        self.fixture = beacon.VoiceBeaconTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.daemon = self.fixture.daemon
        self.daemon.cfg.codec = 'lyra'
        self.daemon.packing = 1

    def test_sparse_windows_restart_the_clean_hold(self):
        daemon = self.daemon
        for sparse in (0, beacon.voice.PACKING_MIN_SAMPLE - 1):
            with self.subTest(packets=sparse):
                daemon.packing = 1
                daemon._pk_clean_since = 0.0
                daemon._pk_last_pushed = daemon._pk_last_lost = 0
                clock = [0.0]
                pushed = [0]
                with patch.object(beacon.voice.time, 'monotonic',
                                  side_effect=lambda: clock[0]), \
                        patch.object(daemon, '_rx_loss_stats',
                                     side_effect=lambda: (pushed[0], 0)), \
                        patch.object(daemon, '_link_mbps', return_value=10), \
                        patch.object(daemon, '_set_packing') as packing:
                    for tick in range(2, 60, 2):
                        clock[0] = tick
                        pushed[0] += sparse
                        self.assertTrue(daemon._tick_packing())
                    for tick in range(60, 88, 2):
                        clock[0] = tick
                        pushed[0] += 25
                        self.assertTrue(daemon._tick_packing())
                        packing.assert_not_called()
                    clock[0] = 90
                    pushed[0] += 25
                    self.assertTrue(daemon._tick_packing())
                    self.assertEqual(packing.call_args.args[0], 2)

    def test_transmit_only_windows_do_not_count_as_clean_receive_time(self):
        daemon = self.daemon
        daemon._pk_clean_since = 0.0
        daemon.transmitting = True
        with patch.object(beacon.voice.time, 'monotonic', return_value=58), \
                patch.object(daemon, '_rx_loss_stats', return_value=(25, 0)), \
                patch.object(daemon, '_link_mbps', return_value=10), \
                patch.object(daemon, '_set_packing') as packing:
            daemon._tick_packing()
            daemon.transmitting = False
            beacon.voice.time.monotonic.return_value = 60
            daemon._tick_packing()
        packing.assert_not_called()

    def build_codec(self, configured, plugins=True, model=True):
        self.daemon.cfg.codec = configured
        self.daemon.cfg.bitrate = 24000
        self.daemon.cfg.frame_ms = 60
        self.fixture.gst.ElementFactory = SimpleNamespace(
            find=Mock(return_value=object() if plugins else None))
        with patch.object(beacon.voice.os.path, 'isdir', return_value=model):
            self.daemon.build()

    def test_fallback_state_uses_opus_bitrate_and_packet_duration(self):
        for plugins, model in ((False, True), (True, False)):
            with self.subTest(plugins=plugins, model=model):
                self.build_codec('lyra', plugins=plugins, model=model)
                self.daemon.write_state()
                state = self.fixture.state()
                self.assertEqual(state['codec'], 'opus')
                self.assertEqual(state['codec_configured'], 'lyra')
                self.assertTrue(state['codec_fallback'])
                self.assertIn('bitrate=24000 frame-size=60',
                              self.daemon.tx.desc)
                self.assertEqual(state['bitrate'], 24000)
                self.assertEqual(state['frame_ms'], 60)
                self.assertIsNone(state['frames_per_packet'])

    def test_native_codec_state_matches_pipeline(self):
        for codec, bitrate, frame_ms, fpp in (
                ('lyra', 6000, 20, 1), ('opus', 24000, 60, None)):
            with self.subTest(codec=codec):
                self.build_codec(codec)
                self.daemon.write_state()
                state = self.fixture.state()
                self.assertFalse(state['codec_fallback'])
                self.assertEqual(state['codec'], codec)
                self.assertEqual(state['bitrate'], bitrate)
                self.assertEqual(state['frame_ms'], frame_ms)
                self.assertEqual(state['frames_per_packet'], fpp)

    def test_fallback_does_not_adapt_the_opus_payloader(self):
        self.build_codec('lyra', plugins=False)
        self.daemon._pk_clean_since = 0.0
        with patch.object(beacon.voice.time, 'monotonic', return_value=60), \
                patch.object(self.daemon, '_rx_loss_stats', return_value=(25, 0)), \
                patch.object(self.daemon, '_link_mbps', return_value=10):
            self.daemon._tick_packing()
        self.assertNotIn('frames-per-packet', self.daemon.payloader.props)


if __name__ == '__main__':
    unittest.main()
