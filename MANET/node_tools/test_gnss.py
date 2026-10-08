"""GNSS samples, anomaly hints and the gps-reader status file, against fake gpsd."""
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import manet_gnss as mg
from manet_gnss import GnssReader, parse_gnss_time, sky_summary

TOOLS = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('gps_reader', TOOLS / 'gps-reader.py')
reader_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reader_mod)

LAT, LON = 39.7392, -104.9903
T0 = 1791288000.0  # 2026-10-06T12:00:00Z
DEV = '/dev/ttyACM0'


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace('+00:00', 'Z')


def tpv(t, lat=LAT, lon=LON, mode=3, device=DEV, **extra):
    msg = {'class': 'TPV', 'device': device, 'mode': mode}
    if t is not None:
        msg['time'] = iso(t)
    if mode >= 2:
        msg.update(lat=lat, lon=lon, altHAE=1600.0, altMSL=1621.5, eph=4.0, speed=0.0)
    msg.update(extra)
    return msg


def sky(ss, hdop=0.9, device=DEV):
    return {'class': 'SKY', 'device': device, 'hdop': hdop, 'uSat': len(ss),
            'satellites': [{'PRN': i + 1, 'ss': v, 'used': True} for i, v in enumerate(ss)]}


SKY_OK = [44, 38, 31, 41, 27, 35, 46, 30]


def kinds(r):
    return [e['kind'] for e in r.events]


def steady(r, start, seconds, mono0=100.0, offset=0.0, sky_every=True):
    """One SKY and one TPV per second; returns the next raw time."""
    mono = mono0
    for i in range(seconds):
        if sky_every:
            r.feed(sky(SKY_OK), mono)
        r.feed(tpv(start + i + offset), mono)
        mono += 1.0
    return mono


def settled_reader():
    r = GnssReader()
    mono = steady(r, T0, int(mg.SETTLE_S) + 5)
    return r, mono, T0 + int(mg.SETTLE_S) + 5


class ParseTests(unittest.TestCase):
    def test_time_parses_and_rejects_garbage(self):
        self.assertAlmostEqual(parse_gnss_time('2026-10-06T12:00:00.500Z'), T0 + 0.5)
        self.assertIsNone(parse_gnss_time(''))
        self.assertIsNone(parse_gnss_time('not a time'))
        self.assertIsNone(parse_gnss_time(None))

    def test_leap_second_reads_as_next_second(self):
        self.assertEqual(parse_gnss_time('2016-12-31T23:59:60Z'),
                         parse_gnss_time('2017-01-01T00:00:00Z'))

    def test_sky_summary_uses_only_used_signals(self):
        msg = sky([40, 42])
        msg['satellites'].append({'PRN': 9, 'ss': 10, 'used': False})
        s = sky_summary(msg, 1.0)
        self.assertEqual((s['sats_used'], s['sats_seen'], s['ss_mean']), (2, 3, 41.0))

    def test_no_fix_tpv_has_no_position(self):
        r = GnssReader()
        self.assertIsNone(r.feed({'class': 'TPV', 'device': DEV, 'mode': 1}, 1.0)['lat'])

    def test_unknown_altitude_stays_unknown(self):
        r = GnssReader()
        self.assertIsNone(r.feed(tpv(T0, mode=2), 1.0)['alt_hae'])

    def test_out_of_range_position_is_an_incomplete_report(self):
        r = GnssReader()
        self.assertIsNone(r.feed(tpv(T0, lat=91.0), 1.0))

    def test_jam_minus_one_is_unknown(self):
        r = GnssReader()
        self.assertIsNone(r.feed(tpv(T0, jam=-1), 1.0)['jam'])

    def test_stale_sky_is_not_attached(self):
        r = GnssReader()
        r.feed(sky(SKY_OK), 1.0)
        self.assertIsNone(r.feed(tpv(T0), 1.0 + mg.SKY_STALE_S + 1)['sats_used'])


class EpochTests(unittest.TestCase):
    def test_partial_report_for_same_epoch_merges(self):
        r = GnssReader()
        r.feed({'class': 'TPV', 'device': DEV, 'mode': 3, 'time': iso(T0),
                'lat': LAT, 'lon': LON}, 1.0)
        r.feed({'class': 'TPV', 'device': DEV, 'mode': 3, 'time': iso(T0), 'eph': 3.5}, 1.1)
        r.feed({'class': 'TPV', 'device': DEV, 'mode': 3, 'time': iso(T0),
                'lat': LAT, 'lon': LON, 'speed': 1.5}, 1.2)
        rx = r.receivers[DEV]
        self.assertEqual(len(rx.history), 1)
        self.assertEqual(rx.last_fix['speed'], 1.5)
        self.assertTrue(rx.fix_live)
        self.assertEqual(kinds(r), [])

    def test_partial_report_upgrades_mode_and_keeps_fields(self):
        r = GnssReader()
        r.feed(tpv(T0, mode=2), 1.0)
        r.feed({'class': 'TPV', 'device': DEV, 'mode': 3, 'time': iso(T0),
                'eph': 3.5, 'altHAE': 1600.0}, 1.1)
        fix = r.receivers[DEV].last_fix
        self.assertEqual((fix['mode'], fix['eph'], fix['alt_hae']), (3, 3.5, 1600.0))
        self.assertEqual(fix['mono'], 1.0)

    def test_alternating_replay_does_not_renew_freshness(self):
        r = GnssReader()
        r.feed(tpv(T0), 100.0)
        r.feed(tpv(T0 + 1), 101.0)
        for i in range(40):
            r.feed(tpv(T0 + i % 2), 102.0 + i)
        self.assertEqual(len(r.receivers[DEV].history), 2)
        self.assertFalse(reader_mod.build_status(r, 141.0, 'b')['has_fix'])

    def test_replayed_epoch_does_not_renew_freshness(self):
        r = GnssReader()
        for i in range(20):
            r.feed(tpv(T0), 100.0 + i)
        self.assertFalse(reader_mod.build_status(r, 119.0, 'b')['has_fix'])
        self.assertEqual(len(r.receivers[DEV].history), 1)

    def test_receivers_are_kept_apart(self):
        r = GnssReader()
        r.feed(sky(SKY_OK, device='A'), 1.0)
        r.feed(tpv(T0, device='B'), 1.0)
        r.feed(tpv(T0, device='A', mode=1), 1.1)
        s = reader_mod.build_status(r, 1.2, 'b')
        self.assertTrue(s['has_fix'])
        self.assertEqual(s['device'], 'B')
        self.assertIsNone(s['sample']['sats_used'])

    def test_primary_moves_only_when_it_loses_its_fix(self):
        r = GnssReader()
        r.feed(tpv(T0, device='A'), 1.0)
        reader_mod.build_status(r, 1.0, 'b')
        r.feed(tpv(T0 + 1, device='B'), 2.0)
        self.assertEqual(reader_mod.build_status(r, 2.0, 'b')['device'], 'A')
        r.feed(tpv(T0 + 2, device='A', mode=1), 3.0)
        self.assertEqual(reader_mod.build_status(r, 3.0, 'b')['device'], 'B')

    def test_devices_report_presence(self):
        # Native gpsd 3.25 forms: DEVICE names its receiver in path only.
        r = GnssReader()
        self.assertIsNone(reader_mod.build_status(r, 1.0, 'b')['devices'])
        r.feed({'class': 'DEVICES', 'devices': []}, 1.0)
        self.assertEqual(reader_mod.build_status(r, 1.0, 'b')['devices'], [])
        r.feed({'class': 'DEVICE', 'path': DEV, 'activated': 1.5}, 2.0)
        r.feed(tpv(T0), 2.0)
        self.assertEqual(reader_mod.build_status(r, 2.0, 'b')['devices'], [DEV])
        r.feed({'class': 'DEVICE', 'path': DEV, 'activated': 0}, 3.0)
        s = reader_mod.build_status(r, 3.0, 'b')
        self.assertEqual((s['devices'], s['has_fix']), ([], False))

    def test_device_missing_from_snapshot_loses_its_fix(self):
        r = GnssReader()
        r.feed(tpv(T0), 1.0)
        r.feed({'class': 'DEVICES', 'devices': [{'path': '/dev/other'}]}, 1.5)
        self.assertFalse(reader_mod.build_status(r, 1.5, 'b')['has_fix'])

    def test_omitted_identity_resolves_to_the_only_receiver(self):
        r = GnssReader()
        r.feed({'class': 'DEVICES', 'devices': [{'path': DEV}]}, 1.0)
        r.feed({'class': 'TPV', 'mode': 3, 'time': iso(T0), 'lat': LAT, 'lon': LON}, 2.0)
        self.assertEqual(list(r.receivers), [DEV])
        r.feed({'class': 'DEVICE', 'activated': 0}, 3.0)
        self.assertFalse(reader_mod.build_status(r, 3.0, 'b')['has_fix'])

    def test_anonymous_receiver_is_removed_with_the_only_device(self):
        r = GnssReader()
        r.feed({'class': 'TPV', 'mode': 3, 'time': iso(T0), 'lat': LAT, 'lon': LON}, 100.0)
        reader_mod.build_status(r, 100.0, 'b')
        r.feed({'class': 'DEVICES', 'devices': [{'path': DEV}]}, 101.0)
        r.feed({'class': 'DEVICE', 'path': DEV, 'activated': 0}, 102.0)
        s = reader_mod.build_status(r, 102.0, 'b')
        self.assertFalse(s['has_fix'])
        self.assertEqual(s['device'], DEV)

    def test_device_presence_is_scoped_to_the_session(self):
        r = GnssReader()
        r.feed({'class': 'DEVICES', 'devices': [{'path': DEV}]}, 1.0)
        r.new_session()
        self.assertIsNone(reader_mod.build_status(r, 2.0, 'b')['devices'])


class ColdStartTests(unittest.TestCase):
    def test_acquisition_raises_nothing(self):
        r = GnssReader()
        for i in range(40):
            r.feed(sky([40.0] * 8 if i % 2 else SKY_OK[:3]), 100.0 + i)
            r.feed({'class': 'TPV', 'device': DEV, 'mode': 1}, 100.0 + i)
        r.feed(tpv(T0 + 40), 140.0)
        r.feed(tpv(T0 + 41 + 18), 141.0)  # receiver corrects UTC after first fix
        steady(r, T0 + 60, 10, mono0=142.0)
        self.assertEqual(kinds(r), [])
        self.assertEqual(r.boot_first_fix, 140.0)

    def test_interrupted_first_fix_does_not_settle(self):
        r = GnssReader()
        r.feed(tpv(T0), 100.0)
        r.feed(tpv(T0 + 1, mode=1), 101.0)
        r.feed(sky(SKY_OK), 130.0)
        r.feed(sky([40.0] * 8), 131.0)
        s = reader_mod.build_status(r, 131.0, 'b')
        self.assertFalse(s['settled'])
        self.assertEqual(kinds(r), ['fix_lost'])

    def test_settled_flag(self):
        r = GnssReader()
        steady(r, T0, 5)
        self.assertFalse(reader_mod.build_status(r, 104.0, 'b')['settled'])
        steady(r, T0 + 5, int(mg.SETTLE_S), mono0=105.0)
        self.assertTrue(reader_mod.build_status(r, 105.0 + mg.SETTLE_S - 1, 'b')['settled'])


class AnomalyTests(unittest.TestCase):
    def test_steady_fixes_raise_nothing(self):
        r, _, _ = settled_reader()
        self.assertEqual(kinds(r), [])

    def test_persistent_clock_step(self):
        # Clock step rearmed: one step, counted once, no drift.
        r, mono, t = settled_reader()
        steady(r, t, int(mg.STEP_HOLD_S) + 60, mono0=mono, offset=4.0)
        self.assertEqual(kinds(r), ['clock_step'])
        self.assertAlmostEqual(r.events[0]['value'], 4.0, places=1)
        self.assertFalse(reader_mod.build_status(r, mono + 40, 'b')['time_ok'])

    def test_time_ok_when_steady(self):
        r, mono, t = settled_reader()
        self.assertTrue(reader_mod.build_status(r, mono - 1, 'b')['time_ok'])

    def test_delivery_backlog_is_not_a_clock_step_or_jump(self):
        r, mono, t = settled_reader()
        # Three epochs delivered late, nearly together, then normal again.
        for i, late in enumerate((3.0, 3.01, 3.02)):
            r.feed(tpv(t + i, lat=LAT + i * 0.0003), mono + late)
        steady(r, t + 4, 20, mono0=mono + 4.0)
        self.assertEqual(kinds(r), [])

    def test_step_that_comes_back_is_not_a_step(self):
        # The receiver clock jumps 4 s ahead and back: a discontinuity, not a step.
        r, mono, t = settled_reader()
        mono = steady(r, t, 3, mono0=mono, offset=4.0)
        steady(r, t + 3, 40, mono0=mono)
        self.assertEqual(kinds(r), ['time_discontinuity'])

    def test_backward_utc_correction_keeps_the_fix(self):
        # UTC starts 18 s ahead, then is corrected backwards.
        r = GnssReader()
        for i in range(40):
            r.feed(tpv(T0 + 18 + i), 100.0 + i)
        for i in range(40, 60):
            r.feed(tpv(T0 + i), 100.0 + i)
            s = reader_mod.build_status(r, 100.0 + i, 'b')
            self.assertTrue(s['has_fix'], i)
        self.assertIn('time_discontinuity', kinds(r))

    def test_no_fix_with_a_repeated_time_is_still_a_loss(self):
        r = GnssReader()
        r.feed(tpv(T0), 1.0)
        r.feed({'class': 'TPV', 'device': DEV, 'mode': 1, 'time': iso(T0)}, 1.5)
        self.assertFalse(reader_mod.build_status(r, 1.5, 'b')['has_fix'])
        self.assertEqual(kinds(r), ['fix_lost'])

    def test_new_session_resets_settling(self):
        r, mono, t = settled_reader()
        self.assertTrue(r.receivers[DEV].settled(mono))
        r.new_session()
        self.assertFalse(reader_mod.build_status(r, mono, 'b')['settled'])

    def test_gap_before_a_report_ends_the_previous_fix(self):
        r = GnssReader()
        r.feed(tpv(T0), 100.0)
        r.feed(tpv(T0 + 40), 140.0)
        self.assertEqual(kinds(r), ['fix_lost', 'fix_regained'])
        self.assertFalse(r.receivers[DEV].settled(140.0))

    def test_slow_clock_drag_beyond_crystal_tolerance(self):
        r = GnssReader()
        for i in range(600):
            r.feed(tpv(T0 + i * 1.005), 100.0 + i)
        self.assertEqual(kinds(r).count('clock_drift'), 1)
        self.assertNotIn('clock_step', kinds(r))

    def test_crystal_rate_drift_is_tolerated(self):
        r = GnssReader()
        for i in range(3600):
            r.feed(tpv(T0 + i * (1 + 50e-6)), 100.0 + i)
        self.assertEqual(kinds(r), [])

    def test_position_jump_uses_gnss_time(self):
        r, mono, t = settled_reader()
        r.feed(tpv(t, lat=LAT + 0.01), mono)  # about 1.1 km in 1 s
        self.assertEqual(kinds(r), ['jump'])

    def test_vehicle_speed_is_not_a_jump(self):
        r = GnssReader()
        for i in range(5):
            r.feed(tpv(T0 + i, lat=LAT + i * 0.0003), 100.0 + i)  # ~33 m/s
        self.assertEqual(kinds(r), [])

    def test_receiver_fix_lost_and_regained(self):
        r, mono, t = settled_reader()
        r.feed(tpv(t, mode=1), mono)
        r.feed(tpv(t + 1), mono + 1)
        self.assertEqual(kinds(r), ['fix_lost', 'fix_regained'])

    def test_silence_ends_the_fix(self):
        r, mono, t = settled_reader()
        s = reader_mod.build_status(r, mono + mg.FIX_STALE_S + 1, 'b')
        self.assertFalse(s['has_fix'])
        r.feed(tpv(t + 10), mono + 10)
        self.assertEqual(kinds(r), ['fix_lost', 'fix_regained'])
        self.assertEqual(r.events[0]['detail'], 'silence')

    def test_jam_reported_once_while_high(self):
        r = GnssReader()
        for i, j in enumerate((20, 200, 210)):
            r.feed(tpv(T0 + i, jam=j), 100.0 + i)
        self.assertEqual(kinds(r), ['jam'])

    def test_signal_strength_step(self):
        r, mono, t = settled_reader()
        r.feed(sky([v + 10 for v in SKY_OK]), mono)
        self.assertEqual(kinds(r), ['ss_step'])

    def test_uniform_signal_strength_reported_on_onset(self):
        r, mono, t = settled_reader()
        r.feed(sky([40.0, 40.5, 39.8, 40.2, 40.1, 39.9, 40.3]), mono)
        r.feed(sky([40.0, 40.4, 39.8, 40.2, 40.1, 39.9, 40.3]), mono + 1)
        self.assertEqual(kinds(r), ['ss_uniform'])

    def test_satellite_drop(self):
        r, mono, t = settled_reader()
        r.feed(sky(SKY_OK[:3]), mono)
        self.assertEqual(kinds(r), ['sat_drop'])

    def test_history_is_bounded_by_age(self):
        r = GnssReader()
        for i in range(int(mg.HISTORY_S) + 50):
            r.feed(tpv(T0 + i), 100.0 + i)
        h = r.receivers[DEV].history
        self.assertLessEqual(h[-1]['mono'] - h[0]['mono'], mg.HISTORY_S)


class StatusTests(unittest.TestCase):
    def test_fresh_fix_keeps_legacy_keys(self):
        r = GnssReader()
        r.feed(sky(SKY_OK, hdop=1.2), 99.0)
        r.feed(tpv(T0), 100.0)
        s = reader_mod.build_status(r, 101.0, 'boot', now_wall=T0 + 1)
        self.assertTrue(s['has_fix'])
        self.assertEqual((s['latitude'], s['longitude'], s['altitude'], s['hdop']),
                         (LAT, LON, 1621.5, 1.2))
        self.assertEqual(s['timestamp'], int(T0))

    def test_written_boot_is_the_boot_clock_at_write(self):
        r = GnssReader()
        r.feed(tpv(T0), 100.0)
        s = reader_mod.build_status(r, 100.0, 'b', now_wall=T0 + 3600, now_boot=777.5)
        self.assertEqual(s['written_boot'], 777.5)

    def test_legacy_alt_is_used_without_altmsl(self):
        r = GnssReader()
        r.feed({'class': 'TPV', 'device': DEV, 'mode': 3, 'time': iso(T0),
                'lat': LAT, 'lon': LON, 'alt': 123.45}, 100.0)
        self.assertEqual(reader_mod.build_status(r, 100.0, 'b')['altitude'], 123.45)

    def test_timestamp_follows_raw_age_across_wall_correction(self):
        r = GnssReader()
        r.feed(tpv(T0), 100.0)
        s = reader_mod.build_status(r, 103.0, 'b', now_wall=T0 + 3600 + 3)
        self.assertEqual(s['timestamp'], int(T0 + 3600))

    def test_stale_fix_is_no_fix(self):
        r = GnssReader()
        r.feed(tpv(T0), 100.0)
        s = reader_mod.build_status(r, 100.0 + mg.FIX_STALE_S + 1, 'boot')
        self.assertFalse(s['has_fix'])
        self.assertEqual(s['latitude'], 0.0)

    def test_disconnected_gpsd_is_no_fix(self):
        r = GnssReader()
        r.feed(tpv(T0), 100.0)
        self.assertFalse(reader_mod.build_status(r, 100.0, 'b', connected=False)['has_fix'])

    def test_first_fix_survives_a_reader_restart(self):
        with tempfile.TemporaryDirectory() as d, \
                patch.object(reader_mod, 'GPS_FIRST_FIX_PATH', f'{d}/f.json'), \
                patch.object(reader_mod, 'GPS_STATUS_PATH', f'{d}/s.json'), \
                patch.object(reader_mod, 'GPS_HISTORY_PATH', f'{d}/h.json'):
            r = GnssReader()
            r.feed(tpv(T0), 100.0)
            reader_mod.Publisher(r, 'boot1').maybe_write(100.0)
            self.assertEqual(reader_mod.load_first_fix('boot1'), 100.0)
            self.assertIsNone(reader_mod.load_first_fix('boot2'))

    def test_failed_first_fix_write_is_retried(self):
        with tempfile.TemporaryDirectory() as d, \
                patch.object(reader_mod, 'GPS_FIRST_FIX_PATH', f'{d}/missing/f.json'), \
                patch.object(reader_mod, 'GPS_STATUS_PATH', f'{d}/s.json'), \
                patch.object(reader_mod, 'GPS_HISTORY_PATH', f'{d}/h.json'):
            r = GnssReader()
            r.feed(tpv(T0), 100.0)
            pub = reader_mod.Publisher(r, 'boot1')
            pub.maybe_write(100.0)
            self.assertFalse(pub.first_fix_saved)
            Path(f'{d}/missing').mkdir()
            pub.maybe_write(101.0)
            self.assertTrue(pub.first_fix_saved)

    def test_status_is_json_serialisable(self):
        r = GnssReader()
        steady(r, T0, 3)
        json.dumps(reader_mod.build_status(r, 103.0, 'b'))


class FakeGpsdTests(unittest.TestCase):
    """The watch loop against a real socket, with fragmented sends."""

    def run_session(self, chunks, extra_patches=()):
        srv = socket.socket()
        srv.bind(('127.0.0.1', 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        got = {}

        def serve():
            conn, _ = srv.accept()
            with conn:
                got['watch'] = conn.recv(1024)
                for c in chunks:
                    conn.sendall(c)
                    time.sleep(0.01)
            srv.close()

        threading.Thread(target=serve, daemon=True).start()
        with tempfile.TemporaryDirectory() as d, \
                patch.object(reader_mod, 'GPSD_PORT', port), \
                patch.object(reader_mod, 'GPS_STATUS_PATH', f'{d}/s.json'), \
                patch.object(reader_mod, 'GPS_HISTORY_PATH', f'{d}/h.json'), \
                patch.object(reader_mod, 'GPS_FIRST_FIX_PATH', f'{d}/f.json'):
            for name, value in extra_patches:
                patch.object(reader_mod, name, value).start()
            r = GnssReader()
            with self.assertRaises(ConnectionError):
                reader_mod.watch(r, reader_mod.Publisher(r, 'boot'))
            patch.stopall()
            status = json.loads(Path(f'{d}/s.json').read_text())
            history = json.loads(Path(f'{d}/h.json').read_text())
        return got, r, status, history

    def test_fragmented_stream_is_parsed_and_published_on_disconnect(self):
        data = b''.join(json.dumps(m).encode() + b'\r\n' for m in
                        [{'class': 'VERSION'}, sky(SKY_OK), tpv(T0), tpv(T0 + 1)]) + b'{bad\n'
        chunks = [data[i:i + 37] for i in range(0, len(data), 37)]
        got, r, status, history = self.run_session(chunks)
        self.assertIn(b'?WATCH', got['watch'])
        self.assertFalse(status['has_fix'])  # the session ended
        self.assertEqual(status['events'][-1]['detail'], 'gpsd session ended')
        self.assertEqual(len(history['samples']), 2)
        self.assertEqual(history['samples'][0]['sats_used'], len(SKY_OK))

    def test_oversized_report_is_dropped_and_stream_resyncs(self):
        chunks = [b'x' * 5000, b'x' * 5000, b'\n', json.dumps(tpv(T0)).encode() + b'\n']
        _, r, _, history = self.run_session(chunks, [('MAX_PENDING', 4096)])
        self.assertEqual(len(history['samples']), 1)

    def test_suffix_of_an_oversized_line_is_not_parsed(self):
        # Stream check: no boundary precedes the JSON-looking tail.
        line = json.dumps(tpv(T0)).encode()
        chunks = [b'x' * (len(line) + 100), line + b'\n',
                  json.dumps(tpv(T0 + 1)).encode() + b'\n']
        _, r, _, history = self.run_session(chunks, [('MAX_PENDING', len(line) + 50)])
        self.assertEqual([s['gnss_time'] for s in history['samples']], [T0 + 1])


if __name__ == '__main__':
    unittest.main()
