"""ATAK service integration with fake sockets/time and synthetic phone packets."""
import copy
import configparser
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import Mock, patch

import manet_cot as cot
import manet_phone as phone
import manet_tak as tak
from test_tak_protocol import sa_fixture

ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


atak = module('manet_atak_service', Path(__file__).with_name('manet-atak.py'))
FIXTURES = Path(__file__).with_name('testdata') / 'atak'
sim = module('atak_phone_packets', FIXTURES / 'packets.py')
scenario = module('atak_scenario', FIXTURES / 'scenario.py')
BASE = datetime(2026, 10, 7, tzinfo=timezone.utc)
IP, LOCAL, MAC = '192.0.2.2', '192.0.2.1', '02:00:00:00:00:02'


class FakeClock:
    def __init__(self):
        self.seconds = 0
        self.wall_offset = 0

    def now(self):
        return atak.Now(100 + self.seconds, 200 + self.seconds, 1000 + self.seconds,
                        BASE + timedelta(seconds=self.seconds + self.wall_offset))

    def advance(self, seconds):
        self.seconds += seconds
        return self.now()


class MemoryStore:
    def __init__(self):
        self.data = None
        self.fail = False
        self.writes = 0

    def load(self):
        return copy.deepcopy(self.data)

    def save(self, data):
        if self.fail:
            raise OSError('disk full')
        # Force the same JSON-only round trip as a durable Store.
        self.data = json.loads(json.dumps(data, allow_nan=False))
        self.writes += 1


class Sender:
    def __init__(self):
        self.sent = []
        self.beacons = []
        self.fail_ports = set()
        self.before_send = None

    def send(self, data, destination):
        if self.before_send:
            self.before_send(data, destination)
        if destination[1] in self.fail_ports:
            raise OSError('simulated transport failure')
        self.sent.append((ET.fromstring(data), destination))
        return True

    def send_beacon(self, data):
        if 6969 in self.fail_ports:
            raise OSError('simulated mesh failure')
        self.beacons.append(ET.fromstring(data))
        return True


class Harness(unittest.TestCase):
    def setUp(self):
        self.clock, self.store, self.tx = FakeClock(), MemoryStore(), Sender()
        self.config = atak.Config('radio-1', enabled=True)
        self.proof = atak.Proof(LOCAL, {IP: MAC}, 'end0 wired-eud 1')
        self.service = atak.Service(self.config, 'boot-1', self.store, self.tx, self.clock.now())
        self.packets = sim.Packets()
        self.packets.radio_uid = 'radio-1'
        self.offset = 0

    def phone_time(self):
        return BASE + timedelta(seconds=self.clock.seconds + self.offset)

    def sa(self, mode='none', point=None, proof=True, address=IP):
        return self.service.ingest(self.packets.sa(mode, self.phone_time(), point), address,
                                   self.proof if proof else None, self.clock.now())

    def send_point(self, point=(40, -105), uid=None):
        return self.service.ingest(self.packets.point(self.phone_time(), point, uid), IP,
                                   self.proof, self.clock.now())

    def tick(self, fix=None, state='UNCHECKED', jammed=False, epoch=0, agrees=False, proof=True):
        self.service.input_memory['fault_epoch'] = max(epoch, self.service.input_memory['fault_epoch'])
        gps = phone.RadioGPS(fix, self.clock.now().mono, state, jammed,
                             self.service.input_memory['fault_epoch'], agrees)
        return self.service.tick(self.clock.now(), self.proof if proof else None, gps, {})

    def of_type(self, kind):
        return [root for root, dest in self.tx.sent if root.get('type') == kind]

    def restart(self, boot='boot-1', now=None):
        self.service = atak.Service(self.config, boot, self.store, self.tx, now or self.clock.now())
        return self.service


class DefaultConfigTests(unittest.TestCase):
    def test_only_explicit_false_values_disable_the_daemon(self):
        for value in (None, '', 'y', 'yes', 'true', '1', 'unknown', 'N', 'no', '0', 'FaLsE', ' NO '):
            conf = {} if value is None else {'atak': value}
            expected = value is None or value.strip().lower() not in ('n', 'no', '0', 'false')
            with self.subTest(value=value), patch.object(atak, 'read_kv', return_value=conf), \
                    patch.object(atak.Path, 'read_text', return_value='a' * 32):
                self.assertEqual(atak.Config.load().enabled, expected)
        self.assertTrue(atak.Config('radio').enabled)

    def test_beacon_defaults_to_hostname_and_honors_callsign_override(self):
        with patch.object(atak.Path, 'read_text', return_value='a' * 32), \
                patch.object(atak.socket, 'gethostname', return_value='mesh-5cc2'):
            for conf, name in (({}, 'mesh-5cc2'), ({'atak_callsign': 'RELAY'}, 'RELAY')):
                with patch.object(atak, 'read_kv', return_value=conf):
                    config = atak.Config.load()
                    self.assertEqual(config.beacon_name, name)
                    self.assertEqual(config.uid, 'MANET-' + 'a' * 32)


class RadioBeaconTests(Harness):
    def announcements(self):
        return [root for root in self.tx.beacons if root.get('type') == cot.BEACON_TYPE]

    def test_idle_trusted_fix_has_distinct_stable_radio_identity_and_no_endpoint(self):
        fix = cot.Fix(40, -105, 'gnss', hae=1600)
        self.tick(fix)
        first = self.announcements()[0]
        self.assertEqual(first.get('uid'), 'radio-1-beacon')
        self.assertEqual(first.get('type'), 'a-f-G-U-U-S-R')
        self.assertEqual(first.find('detail/contact').attrib, {'callsign': self.config.callsign})
        self.assertIsNone(first.find('detail/__group'))
        self.assertIsNone(first.find('detail/link'))
        self.assertEqual(float(first.find('point').get('lat')), 40)
        self.assertEqual(float(first.find('point').get('hae')), 1600)
        self.assertEqual((datetime.fromisoformat(first.get('stale').replace('Z', '+00:00')) -
                          datetime.fromisoformat(first.get('time').replace('Z', '+00:00'))).total_seconds(), 90)
        packet = ET.tostring(first)
        self.assertTrue(packet.startswith(b'<event '))
        self.assertLess(len(packet), 1200)
        self.assertNotIsInstance(tak.decode_mesh_sa(packet), cot.PhoneSA)
        self.clock.advance(31)
        self.restart()
        self.tick(fix)
        self.assertEqual(self.announcements()[-1].get('uid'), first.get('uid'))
        self.assertEqual(self.tx.sent, [])

    def test_no_fix_zero_fix_and_disabled_send_nothing(self):
        self.tick()
        self.tick(cot.Fix(0, 0, 'gnss'))
        self.service.config = atak.replace(self.config, enabled=False)
        self.tick(cot.Fix(40, -105, 'gnss'))
        self.assertEqual(self.tx.beacons, [])
        self.assertEqual(self.tx.sent, [])

    def test_arrival_retires_only_radio_marker_and_lease_expiry_resumes(self):
        fix = cot.Fix(40, -105, 'gnss')
        self.tick(fix)
        self.sa()
        self.tick(fix)
        retirement = self.tx.beacons[-1]
        self.assertEqual(retirement.get('type'), 't-x-d-d')
        self.assertEqual(retirement.find('detail/link').get('uid'), 'radio-1-beacon')
        self.assertIsNotNone(retirement.find('detail/__forcedelete'))
        for _ in range(37):
            self.clock.advance(2)
            self.tick(fix)
        self.assertEqual(len(self.announcements()), 1)
        self.assertEqual(len([r for r in self.tx.beacons if r.get('type') == 't-x-d-d']), 3)
        self.clock.advance(1)
        self.tick(fix)
        self.assertEqual(len(self.announcements()), 2)
        self.assertFalse(self.service.has_live_lease(self.clock.now()))

    def test_live_lease_suppresses_beacon_during_proof_churn(self):
        self.sa()
        self.tick(cot.Fix(40, -105, 'gnss'), proof=False)
        self.clock.advance(30)
        self.tick(cot.Fix(40, -105, 'gnss'), proof=False)
        self.assertEqual(self.announcements(), [])

    def test_suspect_gps_retires_last_fix_and_cannot_be_advertised(self):
        fix = cot.Fix(40, -105, 'gnss')
        self.tick(fix)
        self.clock.advance(1)
        self.tick(fix, state='GNSS_SUSPECTED', epoch=1)
        self.assertEqual(self.tx.beacons[-1].get('type'), 't-x-d-d')
        self.clock.advance(100)
        self.tick(fix, state='GNSS_SUSPECTED', epoch=1)
        self.assertEqual(len(self.announcements()), 1)

    def test_suspect_gps_uses_held_manual_without_disclosing_phone_uid(self):
        self.sa()
        self.send_point((41, -106), 'landmark')
        self.tick(cot.Fix(40, -105, 'gnss'), state='GNSS_SUSPECTED', epoch=1)
        self.clock.advance(76)
        self.tick(cot.Fix(40, -105, 'gnss'), state='GNSS_SUSPECTED', epoch=1)
        root = self.announcements()[-1]
        self.assertEqual(float(root.find('point').get('lat')), 41)
        self.assertEqual(float(root.find('point').get('lon')), -106)
        self.assertNotIn(self.packets.uid, ET.tostring(root).decode())
        self.assertNotEqual(root.get('how'), 'm-g')

    def test_cadence_movement_and_backoff_under_rapid_ticks(self):
        first, moved = cot.Fix(40, -105, 'gnss'), cot.Fix(40.0001, -105, 'gnss')
        self.tick(first)
        for _ in range(24):
            self.clock.advance(.2)
            self.tick(moved)
        self.assertEqual(len(self.announcements()), 1)
        self.clock.advance(.3)
        self.tick(moved)
        self.assertEqual(len(self.announcements()), 2)
        self.clock.advance(29.9)
        self.tick(moved)
        self.assertEqual(len(self.announcements()), 2)
        self.clock.advance(.2)
        self.tick(moved)
        self.assertEqual(len(self.announcements()), 3)
        self.clock.advance(5)
        self.tick(cot.Fix(40.000101, -105, 'gnss'))  # sub-metre jitter
        self.assertEqual(len(self.announcements()), 3)

    def test_failed_sends_have_five_second_backoff_and_bounded_retirement(self):
        self.tx.fail_ports.add(6969)
        with patch.object(self.tx, 'send_beacon', wraps=self.tx.send_beacon) as send:
            for _ in range(50):
                self.tick(cot.Fix(40, -105, 'gnss'))
                self.clock.advance(.1)
            self.assertEqual(send.call_count, 1)
        self.tx.fail_ports.clear()
        self.clock.advance(.1)
        self.tick(cot.Fix(40, -105, 'gnss'))
        self.assertEqual(len(self.announcements()), 1)
        self.tx.fail_ports.add(6969)
        self.sa()
        with patch.object(self.tx, 'send_beacon', wraps=self.tx.send_beacon) as send:
            for _ in range(15):
                self.tick()
                self.clock.advance(2)
            self.assertEqual(send.call_count, 5)
            self.assertIsNone(self.service.beacon.next_due)

    def test_short_send_is_not_success_and_jitter_cancels_early_deadline(self):
        with patch.object(self.tx, 'send_beacon', return_value=False):
            self.tick(cot.Fix(40, -105, 'gnss'))
            self.assertFalse(self.service.beacon.active)
        self.clock.advance(5)
        self.tick(cot.Fix(40, -105, 'gnss'))
        self.clock.advance(1)
        self.tick(cot.Fix(40.001, -105, 'gnss'))
        self.clock.advance(4)
        self.tick(cot.Fix(40, -105, 'gnss'))
        self.assertEqual(self.service.beacon.next_due, self.service.beacon.last_sent + 30)


class IdleApplicationTests(Harness):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.runtime = Path(temp.name)
        self.inputs = atak.Inputs('boot-1', self.runtime)
        self.app = atak.Application(self.service, self.inputs, self.runtime)

    def update(self, proof=True):
        return self.app.update(self.clock.now(), self.proof if proof else None, 'verified' if proof else 'down')

    def write_gps(self):
        now = self.clock.now()
        atak.atomic_json(self.runtime / 'gps_status.json', {
            'schema': 2, 'boot_id': 'boot-1', 'written_boot': now.boot,
            'clock': 'monotonic_raw', 'has_fix': True, 'devices': ['synthetic'],
            'sample': {'mono': now.raw, 'mode': 3, 'lat': 40, 'lon': -105}})

    def test_receiver_is_used_before_first_phone_and_no_fix_expires_beacon(self):
        self.write_gps()
        self.update()
        self.assertEqual(self.tx.beacons[-1].get('type'), cot.BEACON_TYPE)
        self.assertFalse(self.service.phones)
        saves = self.store.writes
        for _ in range(30):
            self.clock.advance(1)
            self.write_gps()
            self.update()
        self.assertEqual(len(self.tx.beacons), 2)
        self.assertEqual(self.store.writes, saves)
        self.clock.advance(20)
        self.update()
        self.assertEqual(self.tx.beacons[-1].get('type'), 't-x-d-d')

    def test_last_lease_expiry_triggers_beacon_even_without_proof_or_changed_input(self):
        self.sa()
        self.send_point((41, -106), 'held')
        self.update(proof=False)
        for seconds in (2, 2, 26, 30, 14):
            self.clock.advance(seconds)
            self.update(proof=False)
        self.assertFalse(any(r.get('type') == cot.BEACON_TYPE for r in self.tx.beacons))
        self.clock.advance(1)
        self.assertTrue(self.update(proof=False))
        self.assertEqual(self.tx.beacons[-1].get('type'), cot.BEACON_TYPE)
        # Beacon transport independently establishes the mesh proof on demand.
        self.assertFalse(self.service.has_live_lease(self.clock.now()))

    def test_continuous_receiver_during_readback_cannot_raise_transient_warning(self):
        self.sa()
        self.write_gps()
        self.update()
        # Admission readback can take time after main captured its cycle time.
        # The producer publishes a valid newer observation during that work.
        old_now = self.clock.now()
        self.clock.advance(.4)
        self.write_gps()
        self.app.update(old_now, None, 'network changed during verification')
        self.assertNotIn('gps_untrusted', self.service.position._warnings)
        self.update()
        self.assertFalse(any('Radio GPS is unavailable' in ET.tostring(root).decode()
                             for root in self.of_type('b-t-f')))

    def test_real_gps_loss_waits_three_seconds_then_logs_raise_and_clear(self):
        self.sa()
        self.write_gps()
        self.update()
        (self.runtime / 'gps_status.json').unlink()
        with patch.object(atak, 'log') as journal:
            self.update()
            self.clock.advance(2.9)
            self.update()
            self.assertNotIn('gps_untrusted', self.service.position._warnings)
            self.clock.advance(.1)
            self.update()
            self.assertIn('gps_untrusted', self.service.position._warnings)
            self.clock.advance(1)
            self.write_gps()
            self.update()
            self.assertNotIn('gps_untrusted', self.service.position._warnings)
        lines = [call.args[0] for call in journal.call_args_list]
        self.assertEqual(len(lines), 2)
        self.assertIn('warning raised code=gps_untrusted', lines[0])
        self.assertIn('warning cleared code=gps_untrusted', lines[1])
        for line in lines:
            for field in ('gps_reason=', 'state=', 'fresh=', 'age_s=', 'distrusted='):
                self.assertIn(field, line)
            self.assertNotIn(self.packets.uid, line)

    def test_fresh_receiver_stays_sampled_across_repeated_proof_loss(self):
        self.sa()
        for i in range(60):
            self.clock.advance(.2)
            self.write_gps()
            self.update(proof=i % 2 == 0)
            self.assertNotIn('gps_untrusted', self.service.position._warnings)
            self.assertIsNotNone(self.inputs.cached[0].fix)

    def test_distrust_and_jamming_warn_immediately_during_availability_delay(self):
        for state, jammed in (('GNSS_SUSPECTED', False), ('UNCHECKED', True)):
            with self.subTest(state=state, jammed=jammed):
                self.setUp()
                self.sa()
                self.tick()
                with patch.object(atak, 'log') as journal:
                    self.tick(state=state, jammed=jammed, epoch=1)
                self.assertIn('gps_untrusted', self.service.position._warnings)
                self.assertTrue(self.service.position._distrusted)
                self.assertEqual(len(journal.call_args_list), 1)

    def test_never_attached_phone_has_no_repeated_app_io_or_serialization(self):
        self.assertTrue(self.update())
        with patch.object(self.inputs, 'read', side_effect=AssertionError('idle input parsing')), \
                patch.object(self.service, 'tick', side_effect=AssertionError('idle selector')), \
                patch.object(atak, 'atomic_json', wraps=atak.atomic_json) as writes:
            count = self.store.writes
            for _ in range(300):
                self.clock.advance(.2)
                self.assertFalse(self.update())
            writes.assert_not_called()
            self.assertEqual(self.store.writes, count)
        self.assertEqual(self.tx.sent, [])
        self.assertEqual(self.service.audio, {})
        self.assertEqual(self.service.position._warnings, {})
        self.assertTrue(json.loads((self.runtime / 'status.json').read_text())['idle'])

    def test_first_phone_and_path_changes_are_processed_in_the_same_cycle(self):
        self.update()
        self.assertTrue(self.update(proof=False))
        self.assertFalse(json.loads((self.runtime / 'status.json').read_text())['path_ready'])
        self.sa()
        self.assertTrue(self.update())
        self.assertTrue(self.tx.sent)
        self.assertTrue(json.loads((self.runtime / 'status.json').read_text())['phone_present'])
        self.clock.advance(1)
        self.assertTrue(self.update())

    def test_absent_pinned_phone_skips_unchanged_cycles_without_sending(self):
        self.sa()
        self.send_point((40, -105), 'held')
        self.update()
        self.clock.advance(100)
        self.update()
        sent, saves = len(self.tx.sent), self.store.writes
        with patch.object(self.service, 'tick', wraps=self.service.tick) as tick, \
                patch.object(self.inputs, 'read', wraps=self.inputs.read) as reads:
            for _ in range(29):
                self.clock.advance(1)
                self.assertFalse(self.update())
            tick.assert_not_called()
            reads.assert_not_called()
        self.assertEqual(len(self.tx.sent), sent)
        self.assertEqual(self.store.writes, saves)
        self.clock.advance(1)
        self.assertTrue(self.update())
        self.assertGreater(self.store.writes, saves)
        self.assertEqual(len(self.tx.sent), sent)
        self.sa()
        self.assertTrue(self.update())
        self.assertGreater(len(self.tx.sent), sent)

    def test_absent_phone_still_observes_faults_and_input_expiration(self):
        self.sa()
        self.update()
        self.clock.advance(100)
        self.update()
        sent = len(self.tx.sent)
        doc = {'schema': 1, 'boot_id': 'boot-1', 'written_boot': self.clock.now().boot,
               'producer_id': 'monitor', 'fault_epoch': 0, 'state': 'UNCHECKED', 'ranges_agree': False}
        atak.atomic_json(self.runtime / 'manet-spoof.json', doc)
        self.assertTrue(self.update())
        self.clock.advance(atak.INPUT_TTL)
        self.assertTrue(self.update())
        self.assertTrue(self.service.position._distrusted)
        self.assertGreater(self.service.input_memory['fault_epoch'], 0)
        self.assertEqual(len(self.tx.sent), sent)

    def test_absent_phone_keeps_alert_deadlines_and_audio_acknowledgements(self):
        self.sa()
        self.update()
        self.clock.advance(3)
        self.update()
        self.service.position._presence = None
        warning = self.service.position._warnings['gps_untrusted']
        warning.deadline = self.clock.now().mono + 2
        self.update()
        sent = len(self.tx.sent)
        self.clock.advance(2)
        self.assertTrue(self.update())
        self.assertGreater(warning.deadline, self.clock.now().mono)
        atak.atomic_json(self.runtime / 'audio-ack.json', {
            'schema': 1, 'session': self.service.session,
            'ids': [item['id'] for item in self.service.audio.values()]})
        self.assertTrue(self.update())
        self.assertEqual(self.service.audio, {})
        self.assertEqual(len(self.tx.sent), sent)

    def test_producer_loss_before_first_phone_survives_restart(self):
        for name, fields in (
                ('manet-spoof.json', {'fault_epoch': 0, 'state': 'UNCHECKED', 'ranges_agree': False}),
                ('manet-lc76g-jam.json', {'asserted': False})):
            atak.atomic_json(self.runtime / name, dict(
                schema=1, boot_id='boot-1', written_boot=self.clock.now().boot,
                producer_id='producer', **fields))
        self.update()
        (self.runtime / 'manet-spoof.json').unlink()
        (self.runtime / 'manet-lc76g-jam.json').unlink()
        self.assertTrue(self.update())
        self.assertEqual(self.tx.sent, [])
        self.assertEqual(self.service.audio, {})
        self.restart()
        self.app = atak.Application(self.service, atak.Inputs('boot-1', self.runtime), self.runtime)
        self.assertTrue(self.service.input_memory['monitor_seen'])
        self.assertTrue(self.service.input_memory['jam_seen'])
        self.assertGreater(self.service.input_memory['fault_epoch'], 0)
        self.sa()
        self.update()
        status = json.loads((self.runtime / 'status.json').read_text())
        self.assertFalse(status['position_trusted'])
        self.assertEqual(status['inputs']['monitor'], 'unavailable after activation')
        self.assertEqual(status['inputs']['jamming_input'], 'GPIO input unavailable after activation')


class PhoneFlowTests(Harness):
    def test_resend_step_and_user_sa_keep_original_marker_age(self):
        self.clock.wall_offset = -432000  # radio clock five days slow
        self.sa()
        self.tick()
        original = None
        for second in range(1, 71):
            self.clock.advance(1)
            if second in (9, 13):
                uid = 'point-A' if second == 9 else 'point-B'
                point = (40.001 if second == 9 else 40.002, -105)
                self.assertEqual(self.send_point(point, uid), 'accepted:newest_location')
                if second == 13:
                    original = self.service.position._manual
            if second == 21:
                self.assertEqual(self.service.ingest(self.packets.resend('point-B', self.phone_time()),
                                                     IP, self.proof, self.clock.now()), 'duplicate:same_location')
            if second == 29:
                self.offset += 600
                self.assertEqual(self.sa(), 'accepted_presence')
            if second == 31:
                self.assertEqual(self.sa('user', (41, -106)), 'accepted_presence')
            status = self.tick()
            if original is not None:
                self.assertEqual(self.service.position._manual, original)
                self.assertEqual(status['manual_age_s'], second - 13)
                self.assertEqual(status['manual_observation_mono'], 113)
                self.assertEqual(status['manual_received_mono'], 113)
                self.assertEqual(status['manual_marker_uid'], 'point-B')
                self.assertEqual(status['manual_observation_time'], original.observation_time.isoformat())
                self.assertEqual(status['written_mono'] - status['manual_observation_mono'], status['manual_age_s'])
        self.assertEqual(status['manual_age_s'], 57)
        self.assertEqual(status['clock_diagnostic']['reason'], 'forward_step')

    def test_cold_start_user_selection_feed_echo_and_sent_replacement(self):
        self.assertEqual(self.sa(), 'accepted_presence')
        status = self.tick()
        self.assertTrue(status['phone_present'])
        self.assertIsNone(status['selected'])
        self.assertFalse(self.of_type('b-t-f'))
        self.clock.advance(3)
        self.tick()
        self.assertTrue(self.of_type('b-t-f'))
        contact = next(root for root, dest in self.tx.sent if root.find('detail/contact') is not None)
        self.assertEqual(contact.find('point').get('lat'), '0')
        self.assertEqual(contact.find('detail/contact').get('endpoint'), LOCAL + ':4242:tcp')
        self.clock.advance(1)
        self.assertEqual(self.sa('user', (40, -105)), 'accepted_manual')
        status = self.tick()
        self.assertEqual(status['selected']['source'], 'manual')
        self.assertFalse(any(i['code'] == 'gps_untrusted' for i in self.service.audio.values()))
        feed = next(root for root, dest in reversed(self.tx.sent) if dest[1] == 4349)
        self.assertEqual(feed.find('detail/precisionlocation').get('geopointsrc'), 'MANET:manual')
        self.packets.last_fix = dict(feed.find('point').attrib)
        self.clock.advance(1)
        self.assertEqual(self.sa('echo'), 'accepted_presence')
        self.assertEqual(self.tick()['manual_age_s'], 1)
        self.clock.advance(1)
        self.assertTrue(self.send_point((40.1, -105), 'point-A').startswith('accepted:'))
        self.tick()
        self.clock.advance(1)
        self.assertTrue(self.send_point((40.2, -105), 'point-B').startswith('accepted:'))
        self.tick()
        self.assertEqual(self.of_type('t-x-d-d')[-1].find('detail/link').get('uid'), 'point-A')
        self.assertEqual(self.service.position._manual.marker_uid, 'point-B')
        self.assertIn('point-A', self.service.retire)
        self.assertEqual(self.send_point((41, -105), 'point-A'), 'retirement in flight')

    def test_gps_loss_drag_then_warning_read_and_position_only_recovery(self):
        self.sa()
        gps = cot.Fix(40, -105, 'gnss', 1600)
        self.tick(gps)
        sent = self.service.position._last_sent_mono
        self.clock.advance(1)
        lost = self.tick(gps, 'GNSS_SUSPECTED', epoch=1)
        self.assertIsNone(lost['selected'])
        count = len([d for r, d in self.tx.sent if d[1] == 4349])
        self.clock.advance(5)
        self.assertEqual(self.sa('drag', (40.1, -105)), 'accepted_presence')
        self.tick(gps, 'GNSS_SUSPECTED', epoch=1)
        self.assertEqual(len([d for r, d in self.tx.sent if d[1] == 4349]), count)
        self.clock.advance(5)
        self.assertEqual(self.sa('drag', (40.2, -105)), 'accepted_manual')
        status = self.tick(gps, 'GNSS_SUSPECTED', epoch=1)
        self.assertFalse(status['position_trusted'])
        self.assertTrue(status['gps_overridden'])
        self.assertGreater(self.service.position._last_sent_mono, sent)
        self.clock.advance(1)
        self.send_point((40, -105), 'confirm')
        status = self.tick(gps, 'GNSS_SUSPECTED', epoch=1, agrees=True)
        self.assertTrue(status['position_trusted'])
        self.assertEqual(status['gps_state'], 'GNSS_SUSPECTED')
        self.assertEqual(status['selected']['source'], 'gnss')
        self.assertIn('confirm', self.service.retire)
        self.assertFalse(any('time_ok' in status for _ in [0]))
        self.clock.advance(1)
        later_fault = self.tick(gps, 'GNSS_SUSPECTED', epoch=2)
        self.assertFalse(later_fault['position_trusted'])

    def test_receipts_plain_chat_audio_cancellation_and_timeout(self):
        self.sa()
        self.tick()
        self.clock.advance(3)
        self.tick()
        message = self.of_type('b-t-f')[-1].find('detail/__chat').get('messageId')
        audio = self.service.audio['gps_untrusted']['id']
        self.clock.advance(1)
        delivered = self.packets.receipt(self.phone_time(), message)
        self.assertEqual(self.service.ingest(delivered, IP, self.proof, self.clock.now()), 'delivered')
        chat = self.packets.chat(self.phone_time(), 'opened; restore GPS; shutdown')
        self.assertEqual(self.service.ingest(chat, IP, self.proof, self.clock.now()), 'chat')
        self.assertEqual(len(self.service.chats), 1)
        self.assertIn('gps_untrusted', self.service.audio)
        self.service.audio_ack({'schema': 1, 'session': self.service.session, 'ids': [audio]}, self.clock.now())
        self.assertEqual(self.service.audio, {})
        self.clock.advance(59)
        self.tick()
        self.assertIn('gps_untrusted', self.service.audio)
        self.assertNotEqual(self.service.audio['gps_untrusted']['id'], audio)
        read = self.packets.receipt(self.phone_time(), message, True)
        self.assertEqual(self.service.ingest(read, IP, self.proof, self.clock.now()), 'read')
        self.assertEqual(self.service.audio, {})
        self.clock.advance(60)
        self.tick()
        self.assertEqual(self.service.audio, {})

    def test_internal_phone_gps_is_supported_without_configuration_warning(self):
        self.sa()
        self.tick(cot.Fix(40, -105, 'gnss'))
        self.clock.advance(1)
        self.sa('gps')
        self.tick(cot.Fix(40, -105, 'gnss'))
        self.assertNotIn('phone_gps', self.service.banners)
        self.clock.advance(1)
        self.sa('echo')
        self.tick()
        self.assertNotIn('phone_gps', self.service.banners)

    def test_offsets_radio_steps_phone_steps_presence_and_outbound_stamps(self):
        for skew in (0, 30, -30, 600, -600, 86400, -86400):
            with self.subTest(skew=skew):
                self.setUp()
                self.clock.wall_offset = skew
                self.assertEqual(self.sa(), 'accepted_presence')
                self.tick()
                for root, destination in self.tx.sent:
                    self.assertEqual(root.get('time'), sim.stamp(BASE))
                    self.assertEqual(destination[0], IP)
                self.clock.advance(1)
                self.clock.wall_offset -= 86400
                self.assertEqual(self.sa('user', (40, -105)), 'accepted_manual')
                self.tick()
                self.offset += 600
                self.clock.advance(1)
                self.assertEqual(self.sa('echo'), 'accepted_presence')
                status = self.tick()
                self.assertEqual(status['clock_diagnostic']['reason'], 'forward_step')
                self.offset -= 1200
                self.clock.advance(1)
                self.assertEqual(self.sa('none'), 'replayed_or_out_of_order')
                self.clock.advance(75)
                self.assertEqual(self.sa(), 'accepted_presence')
                self.assertEqual(self.tick()['clock_diagnostic']['reason'], 'backward_step')

    def test_contact_cadence_presence_expiry_and_reappearance(self):
        self.sa()
        self.tick()
        contacts = lambda: len([r for r, d in self.tx.sent if r.find('detail/contact') is not None])
        self.assertEqual(contacts(), 1)
        self.clock.advance(29)
        self.tick()
        self.assertEqual(contacts(), 1)
        self.clock.advance(1)
        self.tick()
        self.assertEqual(contacts(), 2)
        self.clock.advance(45)
        status = self.tick()
        self.assertFalse(status['phone_present'])
        before = len(self.tx.sent)
        self.clock.advance(1)
        self.tick()
        self.assertEqual(len(self.tx.sent), before)
        self.sa()
        self.tick()
        self.assertEqual(contacts(), 3)

    def test_path_loss_and_identity_moves_fail_closed(self):
        self.assertEqual(self.sa(proof=False), 'ingress/path unproven')
        self.assertIsNone(self.service.position.pinned_uid)
        self.sa()
        self.tick()
        before = len(self.tx.sent)
        self.clock.advance(1)
        self.tick(proof=False)
        self.assertEqual(len(self.tx.sent), before)
        self.proof = replace_proof(self.proof, {IP: '02:00:00:00:00:03'})
        self.assertIn('pinned MAC mismatch', self.sa())
        self.tick()
        self.assertEqual(len(self.tx.sent), before)
        self.proof = replace_proof(self.proof, {'192.0.2.3': MAC})
        self.assertEqual(self.sa(address='192.0.2.3'), 'accepted_presence')
        self.tick()
        self.assertEqual(self.tx.sent[-1][1][0], '192.0.2.3')

    def test_retirement_transport_failures_and_uid_quarantine(self):
        self.sa()
        self.send_point(uid='old')
        self.clock.advance(1)
        self.send_point(uid='new')
        self.tx.fail_ports.add(4242)
        self.tick()
        self.assertIn('old', self.service.retire)
        self.assertNotIn('old', self.service.position._retirements)
        job = copy.deepcopy(self.service.retire['old'])
        self.restart()
        self.assertEqual(self.service.retire['old']['id'], job['id'])
        self.assertEqual(self.send_point(uid='old'), 'retirement in flight')
        self.tx.fail_ports.clear()
        for delay in (2, 4, 8):
            self.clock.advance(delay)
            self.tick()
        self.assertIsNotNone(self.service.retire['old']['done_until'])
        self.assertEqual(len(self.of_type('t-x-d-d')), 3)

    def test_failed_sends_never_ack_feed_or_chat_and_keep_stable_message_id(self):
        self.sa()
        self.tx.fail_ports = {4242, 4349}
        self.tick()
        self.clock.advance(3)
        self.tick()
        key = next(iter(self.service.messages))
        message_id = self.service.messages[key]['id']
        self.assertIsNone(self.service.position._warnings['gps_untrusted'].sent_mono)
        self.assertIsNone(self.service.position._last_sent_mono)
        self.clock.advance(1)
        self.tick(cot.Fix(40, -105, 'gnss'))
        self.assertIsNone(self.service.position._last_sent_mono)
        # A separate unavailable-GPS run exercises crash-stable pending chat.
        self.setUp()
        self.sa()
        self.tx.fail_ports = {4242}
        self.tick()
        self.clock.advance(3)
        self.tick()
        key = next(iter(self.service.messages))
        message_id = self.service.messages[key]['id']
        self.restart()
        self.tx.fail_ports.clear()
        self.clock.advance(2)
        self.tick()
        self.assertEqual(self.of_type('b-t-f')[-1].find('detail/__chat').get('messageId'), message_id)
        self.assertIsNotNone(self.service.position._warnings['gps_untrusted'].sent_mono)


def replace_proof(proof, peers):
    return atak.Proof(proof.local_ip, peers, proof.generation)


class PersistenceTests(Harness):
    def test_same_boot_restart_keeps_age_pin_warning_read_ids_and_guard(self):
        self.sa()
        self.tick()
        self.clock.advance(3)
        self.tick()
        msg = self.of_type('b-t-f')[-1].find('detail/__chat').get('messageId')
        self.clock.advance(1)
        self.send_point(uid='held')
        self.tick()
        self.clock.advance(20)
        self.restart()
        self.assertEqual(self.service.position.pinned_uid, self.packets.uid)
        self.assertEqual(self.tick()['manual_age_s'], 20)
        self.assertGreater(self.service.position._feed_until, self.clock.now().mono)
        packet = self.packets.receipt(self.phone_time(), msg, True)
        # The GPS-untrusted warning may be resolved by the held choice on tick;
        # its old message must never attach to a different warning.
        self.assertIn(self.service.ingest(packet, IP, self.proof, self.clock.now()), ('read', 'unknown_message'))

    def test_reboot_rebases_lower_bound_age_and_requires_new_sa(self):
        self.sa()
        self.send_point(uid='held')
        self.tick()
        self.clock.advance(40)
        self.service.commit(self.clock.now())
        now = atak.Now(5, 7, 5, BASE - timedelta(days=10))
        self.restart('boot-2', now)
        self.assertIsNone(self.service.position.phone_now(5))
        self.assertEqual(self.service.position.pinned_uid, self.packets.uid)
        self.assertAlmostEqual(5 - self.service.position._manual.observation_mono, 45)
        self.assertTrue(self.service.age_lower_bound)
        self.assertIn('admitted SA required', self.service.ingest(self.packets.point(BASE), IP, self.proof, now))
        self.assertEqual(self.service.ingest(self.packets.sa('none', BASE + timedelta(seconds=50)), IP, self.proof, now), 'accepted_presence')
        self.assertEqual(self.service.position.phone_now(5), BASE + timedelta(seconds=50))
        self.assertEqual(self.service.position._manual.marker_uid, 'held')

    def test_distrust_confirmation_baselines_and_pending_delete_survive_restart(self):
        self.sa()
        self.send_point((41, -105), 'old')
        self.tick(cot.Fix(40, -105, 'gnss'))
        self.clock.advance(1)
        self.send_point((41.1, -105), 'new')
        self.tick(cot.Fix(40, -105, 'gnss'))
        before = self.service.position
        self.restart()
        p = self.service.position
        self.assertEqual(p._distrusted, before._distrusted)
        self.assertEqual(p._overridden, before._overridden)
        self.assertEqual(p._last_user_sa_point, before._last_user_sa_point)
        self.assertEqual(p._checked_manual_epoch, before._checked_manual_epoch)
        self.assertIn('old', self.service.retire)
        self.assertFalse(self.tick(cot.Fix(40, -105, 'gnss'))['position_trusted'])

    def test_disk_failure_is_fatal_before_any_derived_send(self):
        self.store.fail = True
        with self.assertRaises(atak.PersistenceError):
            self.sa()
        self.assertEqual(self.tx.sent, [])

    def test_corrupt_unknown_schema_and_wrong_identity_refuse_startup(self):
        baseline = copy.deepcopy(self.store.data)
        for change in ({'schema': 99}, {'uid': 'other'}, {'state': {'class': '__import__', 'fields': {}}}):
            self.store.data = dict(baseline, **change)
            with self.assertRaises((ValueError, KeyError, TypeError)):
                self.restart()

    def test_null_checkpoint_refuses_startup_without_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_text('null\n')
            with self.assertRaises(ValueError):
                atak.Service(self.config, 'boot-1', atak.Store(path), self.tx,
                             self.clock.now())
            self.assertEqual(path.read_text(), 'null\n')
            self.assertEqual(self.tx.sent, [])

    def test_absent_checkpoint_initializes_unpinned_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = atak.Store(Path(directory) / 'state.json')
            service = atak.Service(self.config, 'boot-1', store, self.tx,
                                   self.clock.now())
            self.assertIsNone(service.position.pinned_uid)
            self.assertEqual(store.load()['sha256'],
                             atak.state_digest({k: v for k, v in store.load().items()
                                                if k != 'sha256'}))

    def test_real_store_atomic_roundtrip_and_size_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            store = atak.Store(Path(directory) / 'state.json')
            self.assertIsNone(store.load())
            store.save(self.store.data)
            self.assertEqual(store.load(), self.store.data)
            self.assertFalse(Path(directory, 'state.json.tmp').exists())
            with self.assertRaises(ValueError):
                store.save({'data': 'x' * atak.MAX_STATE})
            self.assertEqual(store.load(), self.store.data)


class FirewallReadbackTests(unittest.TestCase):
    """nftables 1.1 JSON with redundant protocol matches omitted."""
    READBACK = FIXTURES / 'nft-readback.json'

    def test_readback_omits_implied_matches_and_still_validates(self):
        data = json.loads(self.READBACK.read_text())
        self.assertFalse(atak.valid_firewall(data))  # obsolete 4242-only policy
        for item in data['nftables']:
            for expression in item.get('rule', {}).get('expr', []):
                match = expression.get('match', {})
                if match.get('left', {}).get('payload', {}).get('field') == 'dport' and match.get('right') == 4242:
                    match['right'] = {'set': [4242, 4349]}
        # The historical capture predates relay SA. Add its new rule in the
        # same normalized form, without relabelling synthetic JSON as a capture.
        for item in copy.deepcopy(atak.firewall_objects()):
            if ('set' in item or item.get('chain', {}).get('hook') == 'forward' or
                    item.get('rule', {}).get('chain') == 'forward' or item == atak.firewall_objects()[-2]):
                if 'rule' in item:
                    item['rule']['expr'] = atak._canonical_expr(item['rule']['expr'])
                data['nftables'].append(item)
        dynamic = [copy.deepcopy(item) for item in atak.firewall_objects()
                   if any('set' in expr for expr in item.get('rule', {}).get('expr', []))]
        for item in dynamic:
            item['rule']['expr'] = atak._canonical_expr(item['rule']['expr'])
        data['nftables'] = dynamic + data['nftables']
        self.assertTrue(atak.valid_firewall(data))

    def test_dropping_a_real_match_still_fails(self):
        data = json.loads(self.READBACK.read_text())
        rule = next(i['rule'] for i in data['nftables']
                    if 'rule' in i and i['rule']['chain'] == 'input'
                    and i['rule']['family'] == 'bridge')
        rule['expr'] = [e for e in rule['expr']
                        if e.get('match', {}).get('left', {}).get('meta', {}).get('key') != 'iifname']
        self.assertFalse(atak.valid_firewall(data))

    def test_each_captured_match_and_verdict_is_required(self):
        original = json.loads(self.READBACK.read_text())
        for index, item in enumerate(original['nftables']):
            if 'rule' not in item:
                continue
            for expression in range(len(item['rule']['expr'])):
                data = copy.deepcopy(original)
                del data['nftables'][index]['rule']['expr'][expression]
                with self.subTest(rule=index, expression=expression):
                    self.assertFalse(atak.valid_firewall(data))

    def test_canonicalization_only_removes_exact_implied_matches(self):
        tail = [atak.match(atak.payload('ip', 'saddr'), '192.0.2.1'), {'drop': None}]
        for protocol in ('vlan', 'arp'):
            expr = [atak.match(atak.payload(protocol, 'type'), 'ip')] + tail
            self.assertEqual(atak._canonical_expr(expr), expr)


class FirewallLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / 'mesh.conf'
        self.config.write_text('atak=y\n')
        self.tables = {('ip', 'nat'): [{'unrelated': 'policy'}],
                       ('bridge', 'manet_dhcp'): [{'unrelated': 'DHCP'}]}
        self.original = copy.deepcopy(self.tables)
        self.batches = []
        self.nft = patch.object(atak.subprocess, 'run', side_effect=self.run_nft).start()
        self.addCleanup(patch.stopall)

    def run_nft(self, argv, **kwargs):
        self.assertEqual(argv, ['nft', '-j', '-f', '-'])
        self.assertTrue(kwargs['check'])
        self.assertEqual(kwargs['timeout'], 5)
        self.assertNotIn('shell', kwargs)
        batch = json.loads(kwargs['input'])
        self.batches.append(batch)
        # Model the table lifecycle, committing only at the end of each batch.
        updated = copy.deepcopy(self.tables)
        for command in batch['nftables']:
            verb, entry = next(iter(command.items()))
            kind, obj = next(iter(entry.items()))
            key = (obj['family'], obj['name'] if kind == 'table' else obj['table'])
            if verb == 'delete':
                del updated[key]
            elif kind == 'table':
                updated.setdefault(key, [entry])
            else:
                updated[key].append(entry)
        self.tables = updated
        return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')

    def invoke(self, action):
        atak.main(['--config', str(self.config), action])

    def test_install_uses_printed_policy_in_one_batch_and_replaces_stale_tables(self):
        self.tables[('bridge', 'manet_atak')] = [{'stale': 'policy'}]
        output = io.StringIO()
        with patch('sys.stdout', output):
            self.invoke('--print-firewall')
        self.assertEqual(self.batches, [])
        for _ in range(2):
            self.invoke('--install-firewall')
            self.assertEqual(self.batches[-1], json.loads(output.getvalue()))
            snapshot = {'nftables': [obj for entries in self.tables.values() for obj in entries]}
            self.assertTrue(atak.valid_firewall(snapshot))
        self.assertEqual(len(self.batches), 2)
        for key, entries in self.original.items():
            self.assertEqual(self.tables[key], entries)

    def test_stop_is_idempotent_and_independent_of_config(self):
        self.invoke('--install-firewall')
        self.config.unlink()
        for _ in range(2):
            self.invoke('--remove-firewall')
            self.assertEqual(self.tables, self.original)
        # A partly missing pair can be cleaned too.
        self.tables[('inet', 'manet_atak')] = [{'stale': 'policy'}]
        self.invoke('--remove-firewall')
        self.assertEqual(self.tables, self.original)

    def test_disabled_start_has_no_active_tables_or_policy_rules(self):
        for config in ('atak=n\n', 'atak=NO\n', 'atak=0\n', 'atak=FaLsE\n'):
            self.invoke('--install-firewall')
            self.config.write_text(config)
            self.invoke('--install-firewall')
            self.assertEqual(self.tables, self.original)
            self.assertFalse(any('rule' in entry for cmd in self.batches[-1]['nftables']
                                 for entry in cmd.values()))
            self.config.write_text('atak=y\n')

    def test_default_and_other_values_install_the_same_firewall(self):
        for config in ('# no ATAK override\n', 'atak=\n', 'atak=yes\n', 'atak=1\n', 'atak=anything\n'):
            with self.subTest(config=config):
                self.config.write_text(config)
                self.invoke('--install-firewall')
                snapshot = {'nftables': [obj for entries in self.tables.values() for obj in entries]}
                self.assertTrue(atak.valid_firewall(snapshot))

    def test_config_failure_installs_nothing_and_nft_failure_is_fatal(self):
        self.config.unlink()
        with self.assertRaises(OSError):
            self.invoke('--install-firewall')
        self.nft.assert_not_called()
        self.config.write_text('atak=y\n')
        for failure in (subprocess.CalledProcessError(1, 'nft', stderr='rejected'),
                        subprocess.TimeoutExpired('nft', 5), OSError('missing nft')):
            self.nft.side_effect = failure
            with self.assertRaises(OSError):
                self.invoke('--install-firewall')
            with self.assertRaises(OSError):
                self.invoke('--remove-firewall')
        self.assertEqual(self.tables, self.original)

    def test_unit_orders_root_hooks_and_runs_them_on_each_start_stop(self):
        unit = configparser.ConfigParser(interpolation=None)
        unit_path = ROOT / 'MANET/systemd/manet-atak.service'
        if not unit_path.is_file():
            unit_path = ROOT.parent / 'etc/systemd/system/manet-atak.service'
        self.assertEqual(unit.read(unit_path), [str(unit_path)])
        self.assertIn('nftables.service', unit['Unit']['After'].split())
        self.assertIn('nftables.service', unit['Unit']['PartOf'].split())
        service = unit['Service']
        self.assertNotIn('ExecCondition', service)
        self.assertNotEqual(service.get('RemainAfterExit'), 'yes')
        for key, action in (('ExecStartPre', '--install-firewall'), ('ExecStopPost', '--remove-firewall')):
            self.assertEqual(service[key], '+/usr/bin/python3 /usr/local/bin/manet-atak.py ' + action)
        # Replay the commands for start, restart (stop/start), then stop.
        for key in ('ExecStartPre', 'ExecStopPost', 'ExecStartPre', 'ExecStopPost'):
            self.invoke(service[key].split()[-1])
            self.assertEqual(('bridge', 'manet_atak') in self.tables, key == 'ExecStartPre')
        self.assertEqual(self.tables, self.original)


class FragmentPolicyTests(unittest.TestCase):
    def drops(self, protocol, port, frag_off, *, bridge='br0', ingress='end0'):
        # A packet-oriented interpreter for the emitted bridge ingress rules.
        # Noninitial fragment bytes deliberately look like port 4242: they
        # must never be interpreted as a transport header.
        packet = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 28, 1, frag_off, 64,
                             protocol, 0, socket.inet_aton(IP), socket.inet_aton(LOCAL))
        packet += struct.pack('!HHHH', 9999, port, 8, 0)
        def value(expr):
            if not isinstance(expr, dict):
                return expr
            if 'set' in expr:
                return set(expr['set'])
            if '&' in expr:
                return value(expr['&'][0]) & value(expr['&'][1])
            if 'meta' in expr:
                return {'ibrname': bridge, 'iifname': ingress,
                        'l4proto': {6: 'tcp', 17: 'udp', 1: 'icmp'}[packet[9]]}[expr['meta']['key']]
            field = expr['payload']
            if field['protocol'] == 'ether':
                return 'ip'
            if field['protocol'] == 'ip':
                return struct.unpack_from('!H', packet, 6)[0]
            if frag_off & 0x1fff:
                return None
            return struct.unpack_from('!H', packet, (packet[0] & 15) * 4 + 2)[0]
        for obj in atak.firewall_objects():
            rule = obj.get('rule', {})
            if rule.get('family') != 'bridge' or rule.get('chain') not in ('prerouting', 'input'):
                continue
            matches = []
            for expr in rule['expr'][:-1]:
                m = expr['match']
                left, right = value(m['left']), value(m['right'])
                same = left in right if isinstance(right, set) else left == right
                matches.append(same if m['op'] == '==' else not same)
            if all(matches):
                self.assertEqual(rule['expr'][-1], {'drop': None})
                return True
        return False

    def test_non_atak_fragments_keep_passing_from_eud_and_mesh(self):
        ports = (22, 53, 67, 68, 80, 123, 1935, 5201, 5353, 8000, 8001,
                 8189, 8384, 8554, 8888, 8889, 8890, 21027, 22000, 38801, 64738)
        for protocol in (6, 17, 1):
            for port in ports:
                for ingress in ('end0', 'bat0', 'wlan0'):
                    for fragment in (0, 0x2000, 0x2001, 1):
                        with self.subTest(protocol=protocol, port=port, ingress=ingress, fragment=fragment):
                            self.assertFalse(self.drops(protocol, port, fragment, ingress=ingress))
        self.assertFalse(self.drops(1, 4242, 0x2000))

    def test_atak_cannot_reassemble_even_with_fragments_from_different_ports(self):
        for protocol in (6, 17):
            self.assertFalse(self.drops(protocol, 4242, 0))
            self.assertTrue(self.drops(protocol, 4242, 0, ingress='bat0'))
            for ingress in ('end0', 'bat0', 'wlan0'):
                self.assertTrue(self.drops(protocol, 4242, 0x2000, ingress=ingress))
                self.assertTrue(self.drops(protocol, 4242, 0x6000, ingress=ingress))
                for offset in (1, 8, 8191, 0x2001):
                    self.assertFalse(self.drops(protocol, 4242, offset, ingress=ingress))
            # Bridge-wide by design: node kernels lack CONFIG_NFT_BRIDGE_META.
            self.assertTrue(self.drops(protocol, 4242, 0x2000, bridge='br-other'))

    def test_fragment_filter_precedes_bridge_defragmentation(self):
        chains = [obj['chain'] for obj in atak.firewall_objects() if 'chain' in obj]
        chain = next(c for c in chains if c['family'] == 'bridge' and c['hook'] == 'prerouting')
        self.assertLess(chain['prio'], -400)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.run, self.net = base / 'run', base / 'net'
        self.run.mkdir()
        (self.net / 'end0').mkdir(parents=True)
        (self.net / 'br0/brif/end0').mkdir(parents=True)
        (self.net / 'end0/master').symlink_to('../br0')
        (self.net / 'end0/carrier').write_text('1')
        (self.net / 'br0/brif/end0/state').write_text('3')
        (self.run / 'ethernet_detection_state').write_text('ETH_MODE=WIRED_EUD\nETH_BRIDGE=br0\n')
        (self.run / 'eth-carrier-generation').write_text('end0 wired-eud 1\n')
        self.rules = {'nftables': atak.firewall_objects()}
        self.fdb = [{'mac': MAC, 'ifname': 'end0', 'flags': ['master']}]
        self.neigh = [{'dst': IP, 'lladdr': MAC, 'state': ['REACHABLE']}]
        self.addresses = [{'addr_info': [{'family': 'inet', 'scope': 'global', 'local': LOCAL}]}]
        self.guard = atak.LinuxGuard(atak.Config('radio-1', enabled=True), self.runner, self.net, self.run)

    def runner(self, argv, **kwargs):
        self.assertTrue(kwargs['check'])
        self.assertLessEqual(kwargs['timeout'], 1)
        value = self.rules if argv[0] == 'nft' else self.fdb if argv[0] == 'bridge' else self.neigh if 'neigh' in argv else self.addresses
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(value))

    def cached_guard(self):
        self.guard.events = Mock(sockets=[], changed=Mock(return_value=False))
        self.guard.runner = Mock(side_effect=self.runner)
        return self.guard

    def test_idle_minute_has_four_startup_commands_and_zero_per_tick(self):
        guard = self.cached_guard()
        first = guard.current()
        self.assertEqual(first.peer(IP), MAC)
        self.assertEqual(guard.runner.call_count, 4)
        # Same 1 Hz output / 5 Hz busy-loop cadence as before, no new sleeps.
        for _ in range(300):
            self.assertIs(guard.current(), first)
        self.assertEqual(guard.runner.call_count, 4)
        self.assertEqual(guard.current(force=True), first)
        self.assertEqual(guard.runner.call_count, 8)

    def test_ruleset_event_revokes_cache_and_restoration_recovers(self):
        guard = self.cached_guard()
        self.assertIsNotNone(guard.current())
        self.rules = {'nftables': []}
        guard.events.changed.side_effect = [True, False]
        self.assertIsNone(guard.current())
        self.assertIn('firewall', guard.reason)
        guard.events.changed.side_effect = None
        count = guard.runner.call_count
        for _ in range(60):
            self.assertIsNone(guard.current())
        self.assertEqual(guard.runner.call_count, count)
        self.rules = {'nftables': atak.firewall_objects()}
        guard.events.changed.side_effect = [True, False]
        self.assertIsNotNone(guard.current())

    def test_link_and_fdb_events_revoke_learned_peer(self):
        guard = self.cached_guard()
        self.assertEqual(guard.current().peer(IP), MAC)
        self.fdb.append({'mac': MAC, 'ifname': 'bat0'})
        guard.events.changed.side_effect = [True, False]
        self.assertIsNone(guard.current().peer(IP))
        (self.net / 'end0/carrier').write_text('0')
        guard.events.changed.side_effect = [True, False]
        self.assertIsNone(guard.current())

    def test_role_and_generation_file_changes_need_no_kernel_event(self):
        guard = self.cached_guard()
        self.assertIsNotNone(guard.current())
        (self.run / 'eth-carrier-generation').write_text('end0 wired-eud 2\n')
        self.assertEqual(guard.current().generation, 'end0 wired-eud 2')
        (self.run / 'ethernet_detection_state').write_text('ETH_MODE=GATEWAY\n')
        self.assertIsNone(guard.current())

    def test_changes_during_snapshot_are_never_admitted(self):
        guard = self.cached_guard()
        guard.events.changed.side_effect = [False, True]
        self.assertIsNone(guard.current())
        self.assertTrue(guard.dirty)
        guard.events.changed.side_effect = None
        self.assertIsNotNone(guard.current())
        original = guard.inspect
        def changing():
            proof = original()
            (self.run / 'eth-carrier-generation').unlink()
            return proof
        guard.inspect = changing
        self.assertIsNone(guard.current(force=True))

    def test_notification_loss_cannot_reuse_proof(self):
        guard = self.cached_guard()
        self.assertIsNotNone(guard.current())
        guard.events.changed.side_effect = OSError('ENOBUFS')
        with self.assertRaises(atak.GuardFailure):
            guard.current()
        self.assertIsNone(guard.cached)

    def test_transient_snapshot_failure_recovers_without_an_unrelated_event(self):
        guard = self.cached_guard()
        guard.runner.side_effect = subprocess.TimeoutExpired('nft', 1)
        self.assertIsNone(guard.current())
        self.assertTrue(guard.dirty)
        guard.runner.side_effect = self.runner
        self.assertEqual(guard.current().peer(IP), MAC)
        self.assertFalse(guard.dirty)

    def test_policy_missing_rules_wrong_hooks_dormant_tables_fail_closed(self):
        self.assertEqual(self.guard.inspect().peer(IP), MAC)
        original = copy.deepcopy(self.rules)
        for index in range(len(original['nftables'])):
            self.rules = copy.deepcopy(original)
            del self.rules['nftables'][index]
            self.assertIsNone(self.guard.inspect())
        self.rules = copy.deepcopy(original)
        self.rules['nftables'][0]['table']['flags'] = ['dormant']
        self.assertIsNone(self.guard.inspect())
        self.rules = copy.deepcopy(original)
        self.rules['nftables'][1]['chain']['hook'] = 'forward'
        self.assertIsNone(self.guard.inspect())
        self.rules = copy.deepcopy(original)
        self.rules['nftables'].append({'rule': {'family': 'bridge', 'table': atak.TABLE, 'chain': 'input', 'expr': [{'accept': None}]}})
        self.assertIsNone(self.guard.inspect())

    def test_role_link_generation_address_and_readback_required(self):
        for path, bad in ((self.run / 'ethernet_detection_state', 'ETH_MODE=GATEWAY\n'),
                          (self.run / 'eth-carrier-generation', 'wlan1 wired-eud 1'),
                          (self.net / 'end0/carrier', '0'),
                          (self.net / 'br0/brif/end0/state', '0')):
            old = path.read_text()
            path.write_text(bad)
            self.assertIsNone(self.guard.inspect())
            path.write_text(old)
        (self.net / 'end0/master').unlink()
        self.assertIsNone(self.guard.inspect())
        (self.net / 'end0/master').symlink_to('../br0')
        self.addresses = []
        self.assertIsNone(self.guard.inspect())
        self.guard.runner = lambda *a, **k: (_ for _ in ()).throw(OSError('no permission'))
        self.assertIsNone(self.guard.inspect())

    def test_neighbours_and_fdb_require_unambiguous_local_port(self):
        for states in (['FAILED'], ['INCOMPLETE'], ['PERMANENT'], []):
            self.neigh[0]['state'] = states
            self.assertIsNone(self.guard.inspect().peer(IP))
        self.neigh[0]['state'] = ['REACHABLE']
        for entries in ([], [{'mac': MAC, 'ifname': 'bat0'}],
                        [{'mac': MAC, 'ifname': 'end0'}, {'mac': MAC, 'ifname': 'bat0'}],
                        [{'mac': MAC, 'ifname': 'end0', 'state': 'permanent'}]):
            self.fdb = entries
            self.assertIsNone(self.guard.inspect().peer(IP))


class InputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name)
        self.clock = FakeClock()
        self.inputs = atak.Inputs('boot-1', self.run)
        self.memory = {'fault_epoch': 0}

    def put(self, name, data):
        atak.atomic_json(self.run / name, data)

    def gps(self, **changes):
        data = {'schema': 2, 'boot_id': 'boot-1', 'written_boot': self.clock.now().boot,
                'clock': 'monotonic_raw', 'has_fix': True,
                'sample': {'mono': self.clock.now().raw, 'mode': 3, 'lat': 40, 'lon': -105,
                           'alt_hae': 1600, 'alt_msl': 1580, 'eph': 3, 'epv': 4}}
        data.update(changes)
        self.put('gps_status.json', data)
        return data

    def monitor(self, **changes):
        data = {'schema': 1, 'boot_id': 'boot-1', 'written_boot': self.clock.now().boot,
                'producer_id': 'monitor-1', 'fault_epoch': 0, 'state': 'UNCHECKED', 'ranges_agree': False}
        data.update(changes)
        self.put('manet-spoof.json', data)

    def test_cache_expires_raw_fix_without_waiting_for_document_expiry(self):
        self.gps()
        now = self.clock.now()
        self.assertIsNotNone(self.inputs.cached_read(now, self.memory)[0].fix)
        with patch.object(self.inputs, 'read', wraps=self.inputs.read) as read:
            self.inputs.cached_read(atak.replace(now, raw=now.raw + phone.FIX_TTL_S - .01), self.memory)
            read.assert_not_called()
            gps, _ = self.inputs.cached_read(atak.replace(now, raw=now.raw + phone.FIX_TTL_S), self.memory)
            self.assertIsNone(gps.fix)
            read.assert_called_once()

    def test_cache_expires_boot_document_even_when_raw_sample_is_fresh(self):
        self.gps()
        self.monitor()
        now = self.clock.now()
        self.inputs.cached_read(now, self.memory)
        gps, status = self.inputs.cached_read(atak.replace(now, boot=now.boot + atak.INPUT_TTL), self.memory)
        self.assertIsNone(gps.fix)
        self.assertEqual(gps.state, 'GNSS_SUSPECTED')
        self.assertEqual(status['monitor'], 'invalid or stale monitor input')
        with patch.object(self.inputs, 'read', side_effect=AssertionError('already expired')):
            self.inputs.cached_read(atak.replace(now, boot=now.boot + atak.INPUT_TTL + 1), self.memory)

    def test_cache_recovers_future_document_and_sample_at_their_own_deadlines(self):
        now = self.clock.now()
        data = self.gps(written_boot=now.boot + 1)
        data['sample']['mono'] = now.raw + 2
        self.put('gps_status.json', data)
        self.assertIsNone(self.inputs.cached_read(now, self.memory)[0].fix)
        self.assertIsNone(self.inputs.cached_read(self.clock.advance(1), self.memory)[0].fix)
        self.assertIsNotNone(self.inputs.cached_read(self.clock.advance(1), self.memory)[0].fix)

    def test_no_receiver_read_before_phone_but_admission_reads_it_immediately(self):
        self.gps()
        with patch.object(atak, 'read_json', wraps=atak.read_json) as read:
            gps, status = self.inputs.cached_read(self.clock.now(), self.memory, receiver=False)
            self.assertIsNone(gps.fix)
            self.assertEqual(status['gps'], 'waiting for phone')
            self.assertNotIn(self.run / 'gps_status.json', [call.args[0] for call in read.call_args_list])
        self.assertIsNotNone(self.inputs.cached_read(self.clock.now(), self.memory, receiver=True)[0].fix)

    def test_cached_producer_deletion_and_same_size_replacement_invalidate(self):
        self.monitor()
        self.inputs.cached_read(self.clock.now(), self.memory)
        self.monitor(fault_epoch=1)
        first = self.inputs.cached_read(self.clock.now(), self.memory)[0].fault_epoch
        self.assertGreater(first, 0)
        (self.run / 'manet-spoof.json').unlink()
        gps, status = self.inputs.cached_read(self.clock.now(), self.memory)
        self.assertGreater(gps.fault_epoch, first)
        self.assertEqual(gps.state, 'GNSS_SUSPECTED')
        self.assertEqual(status['monitor'], 'unavailable after activation')

    def test_receiver_uses_raw_age_true_hae_and_unknown_accuracy_and_gpio(self):
        self.gps()
        self.clock.wall_offset = -86400
        gps, status = self.inputs.read(self.clock.now(), self.memory)
        self.assertEqual(gps.fix.hae, 1600)
        self.assertIsNone(gps.fix.horizontal_error)
        self.assertIsNone(gps.fix.vertical_error)
        self.assertIsNone(gps.fix.phone_uid)
        self.assertEqual(gps.state, 'UNCHECKED')
        self.assertEqual(status['monitor'], 'unwired')
        self.assertIsNone(status['jamming_pin'])
        data = self.gps()
        data['sample']['alt_hae'] = None
        self.put('gps_status.json', data)
        self.assertIsNone(self.inputs.read(self.clock.now(), self.memory)[0].fix.hae)

    def test_missing_stale_wrong_boot_future_and_legacy_fix_rejected(self):
        self.assertIsNone(self.inputs.read(self.clock.now(), self.memory)[0].fix)

    def test_null_receiver_sample_has_a_useful_reason(self):
        for devices, reason in (([], 'no GPS receiver'), (['/dev/ttyACM0'], 'no receiver sample')):
            self.gps(sample=None, has_fix=False, devices=devices)
            gps, status = self.inputs.read(self.clock.now(), self.memory)
            self.assertIsNone(gps.fix)
            self.assertEqual(status['gps'], reason)
        for changes in ({'schema': 1}, {'boot_id': 'other'}, {'written_boot': 0},
                        {'written_boot': 1001}, {'has_fix': False}, {'clock': 'utc'}):
            self.gps(**changes)
            self.assertIsNone(self.inputs.read(self.clock.now(), self.memory)[0].fix)
        data = self.gps()
        for age in (-1, 5, 100):
            data['sample']['mono'] = self.clock.now().raw - age
            self.put('gps_status.json', data)
            self.assertIsNone(self.inputs.read(self.clock.now(), self.memory)[0].fix)

    def test_monitor_generations_loss_and_gpio_unknown_do_not_clear_faults(self):
        self.gps()
        self.monitor()
        self.inputs.read(self.clock.now(), self.memory)
        self.monitor(state='GNSS_SUSPECTED', fault_epoch=1)
        first = self.inputs.read(self.clock.now(), self.memory)[0].fault_epoch
        self.assertGreater(first, 0)
        self.monitor(state='GNSS_SUSPECTED', fault_epoch=2, ranges_agree=True)
        gps, _ = self.inputs.read(self.clock.now(), self.memory)
        self.assertGreater(gps.fault_epoch, first)
        self.assertTrue(gps.ranges_agree)
        self.monitor(fault_epoch=1)
        gps, status = self.inputs.read(self.clock.now(), self.memory)
        self.assertEqual(gps.state, 'GNSS_SUSPECTED')
        self.assertIn('invalid', status['monitor'])
        self.put('manet-lc76g-jam.json', {'schema': 1, 'boot_id': 'boot-1', 'written_boot': 1000,
                                        'producer_id': 'gpio-1', 'asserted': True})
        self.assertTrue(self.inputs.read(self.clock.now(), self.memory)[0].jammed)
        (self.run / 'manet-lc76g-jam.json').unlink()
        gps, status = self.inputs.read(self.clock.now(), self.memory)
        self.assertTrue(gps.jammed)
        self.assertIsNone(status['jamming_pin'])

    def test_invalid_producer_on_first_read_is_not_treated_as_unwired(self):
        self.gps()
        self.monitor(written_boot=0)
        self.put('manet-lc76g-jam.json', {'schema': 1, 'asserted': False})
        gps, status = self.inputs.read(self.clock.now(), self.memory)
        self.assertEqual(gps.state, 'GNSS_SUSPECTED')
        self.assertTrue(gps.jammed)
        self.assertIsNone(status['jamming_pin'])
        (self.run / 'manet-spoof.json').unlink()
        (self.run / 'manet-lc76g-jam.json').unlink()
        gps, _ = self.inputs.read(self.clock.now(), self.memory)
        self.assertEqual(gps.state, 'GNSS_SUSPECTED')
        self.assertTrue(gps.jammed)


class FakeSocket:
    def __init__(self, family=socket.AF_INET, kind=socket.SOCK_STREAM, protocol=0):
        self.family, self.kind = family, kind
        self.protocol = protocol
        self.closed = False
        self.options, self.accepted, self.incoming, self.sent = [], [], [], []
        self.bound = None

    def setsockopt(self, *args):
        self.options.append(args)

    def getsockopt(self, level, option, size):
        if (level, option, size) != (263, 6, 8):
            raise AssertionError('unexpected fake socket option')
        dropped = getattr(self, 'dropped', 0)
        self.dropped = 0
        return struct.pack('=II', 1, dropped)

    def setblocking(self, value):
        self.blocking = value

    def bind(self, address):
        self.bound = address

    def listen(self, count):
        self.backlog = count

    def accept(self):
        if not self.accepted:
            raise BlockingIOError()
        return self.accepted.pop(0)

    def recv(self, count):
        if not self.incoming:
            raise BlockingIOError()
        return self.incoming.pop(0)

    recvfrom = recv

    def recvmsg(self, count, ancillary=0):
        incoming = self.recv(count)
        if len(incoming) == 4:
            return incoming
        data, endpoint = incoming
        destination = getattr(self, 'destination', LOCAL)
        info = struct.pack('=I', 1) + socket.inet_aton(LOCAL) + socket.inet_aton(destination)
        return data, [(socket.IPPROTO_IP, 8, info)], 0, endpoint

    def getsockname(self):
        if self.family == socket.AF_NETLINK:
            return 32000 + self.protocol, self.bound[1]
        return getattr(self, 'destination', LOCAL), 4242

    def sendto(self, packet, destination):
        self.sent.append((packet, destination))
        return len(packet)

    def close(self):
        self.closed = True


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.socks = [Mock(), Mock()]
        for sock in self.socks:
            sock.recvmsg.side_effect = BlockingIOError
        self.events = atak.NetworkEvents(Mock(side_effect=self.socks))
        self.addCleanup(self.events.close)

    @staticmethod
    def packet(kind, payload=b''):
        size = 16 + len(payload)
        return struct.pack('=IHHII', size, kind, 0, 0, 0) + payload + b'\0' * (-size % 4)

    @staticmethod
    def attr(kind, value):
        size = 4 + len(value)
        return struct.pack('=HH', size, kind) + value + b'\0' * (-size % 4)

    @classmethod
    def neighbor(cls, index, *, mac=MAC, address=None, family=socket.AF_BRIDGE,
                 state=2, flags=0, kind=28, attrs=b'', ndm_type=None):
        if ndm_type is None:
            ndm_type = 0 if family == socket.AF_BRIDGE else 1
        body = struct.pack('=BBHiHBB', family, 0, 0, index, state, flags, ndm_type)
        if mac is not None:
            body += cls.attr(2, bytes.fromhex(mac.replace(':', '')))
        if address is not None:
            body += cls.attr(1, socket.inet_pton(family if family != socket.AF_BRIDGE else socket.AF_INET, address))
        return cls.packet(kind, body + attrs)

    @classmethod
    def link(cls, index, name):
        return cls.packet(16, struct.pack('=BBHiII', 0, 0, 1, index, 0, 0)
                          + cls.attr(3, name.encode() + b'\0'))

    def test_scope_preserves_local_links_address_changes_and_unknown_events(self):
        mac = bytes.fromhex(MAC.replace(':', ''))
        self.events.scope = ({2, 3}, 2, {socket.inet_aton(IP): mac},
                             {mac: (3, 0)}, {'br0', 'end0'})
        for packet, expected in (
                (self.link(99, 'unrelated'), False), (self.link(3, 'end0'), True),
                (self.link(77, 'end0'), True),
                (self.packet(20, struct.pack('=BBBBI', 2, 24, 0, 0, 2)), True),
                (self.packet(20, struct.pack('=BBBBI', 2, 24, 0, 0, 99)), False),
                (self.neighbor(99), True),
                (self.neighbor(99, mac='02:00:00:00:00:99'), False),
                (self.neighbor(99, mac=None, address=IP, family=2), True),
                (self.neighbor(99, mac='02:00:00:00:00:99', address='fe80::99', family=10), False),
                (self.neighbor(99, mac=None), True),
                (self.packet(28, b'bad'), True), (self.packet(99), True)):
            self.socks[0].recvmsg.side_effect = [(packet, [], 0, (0, 1)), BlockingIOError()]
            self.assertEqual(self.events.changed(), expected)
        # Even a route-shaped payload on the nft subscription is never ignored.
        self.socks[0].recvmsg.side_effect = BlockingIOError
        self.socks[1].recvmsg.side_effect = [(self.link(99, 'unrelated'), [], 0, (0, 1)), BlockingIOError()]
        self.assertTrue(self.events.changed())

    def test_subscriptions_precede_snapshot_and_idle_needs_no_child(self):
        self.socks[0].bind.assert_called_once_with((0, 1 | 4 | 16))
        self.socks[1].bind.assert_called_once_with((0, 1 << 6))
        self.assertFalse(self.events.changed())
        for sock in self.socks:
            sock.setblocking.assert_called_once_with(False)

    def test_link_address_neighbor_and_nft_messages_invalidate(self):
        for kind in (16, 17, 20, 21, 28, 29, (10 << 8) | 16):
            with self.subTest(kind=kind):
                self.socks[0].recvmsg.side_effect = [(self.packet(kind), [], 0, (0, 1)), BlockingIOError()]
                self.assertTrue(self.events.changed())

    def test_truncated_malformed_overrun_or_non_kernel_notifications_fail(self):
        for data, flags, sender in ((b'x', 0, (0, 1)),
                                    (self.packet(4), 0, (0, 1)),
                                    (self.packet(2), 0, (0, 1)),
                                    (self.packet(16), socket.MSG_TRUNC, (0, 1)),
                                    (self.packet(16), 0, (20, 0))):
            with self.subTest(data=data, flags=flags, sender=sender):
                self.socks[0].recvmsg.side_effect = [(data, [], flags, sender)]
                with self.assertRaises(OSError):
                    self.events.changed()

    def test_backlog_and_receive_errors_fail_closed(self):
        self.socks[0].recvmsg.side_effect = None
        self.socks[0].recvmsg.return_value = (self.packet(16), [], 0, (0, 1))
        with self.assertRaisesRegex(OSError, 'backlog'):
            self.events.changed()
        self.socks[0].recvmsg.side_effect = OSError('ENOBUFS')
        with self.assertRaisesRegex(OSError, 'ENOBUFS'):
            self.events.changed()

    def test_partial_subscription_failure_closes_both_sockets(self):
        self.socks[1].bind.side_effect = PermissionError('denied')
        with self.assertRaises(PermissionError):
            atak.NetworkEvents(Mock(side_effect=self.socks))
        for sock in self.socks:
            sock.close.assert_called_once()


class ProtectedNotificationTests(Harness):
    def setUp(self):
        super().setUp()
        self.fixture = APAdmissionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        # Native bridge readback separates NDA_MASTER from ndm_flags. The
        # ordinary dynamic entry has empty flags/state and master="br0".
        self.fixture.fdb = [{'mac': MAC, 'ifname': 'wlan7', 'master': 'br0', 'flags': [], 'state': ''}]
        for name, index in (('br0', 2), ('end0', 3), ('bat0', 5), ('wlan7', 7)):
            (self.fixture.net / name).mkdir(exist_ok=True)
            (self.fixture.net / name / 'ifindex').write_text(str(index))
        self.guard = self.fixture.cached_guard()
        self.socks = [Mock(), Mock()]
        for sock in self.socks:
            sock.recvmsg.side_effect = BlockingIOError
        self.events = atak.NetworkEvents(Mock(side_effect=self.socks))
        self.addCleanup(self.events.close)
        self.guard.events = self.events
        self.proof = self.guard.current()
        self.assertEqual(self.sa(), 'accepted_presence')
        self.assertEqual(self.proof.port(IP), 'wifi')
        self.network = atak.Network(FakeSocket, self.guard.current)
        self.addCleanup(self.network.close)
        self.network.configure(self.proof)
        self.guard.runner.reset_mock()

    def notify(self, packet):
        # Exhaust only this notification. Later pre/post readback drains are
        # empty, so a changed proof can settle during this same call.
        def receive(*_):
            try:
                return next(messages)
            except StopIteration:
                raise BlockingIOError
        messages = iter([(packet, [], 0, (0, 1))])
        self.socks[0].recvmsg.side_effect = receive
        return self.guard.current(refresh=self.service.has_live_lease(self.clock.now()))

    def test_phone_reachability_cycle_preserves_proof_without_commands(self):
        attrs = NotificationTests.attr(3, b'\0' * 16) + NotificationTests.attr(4, struct.pack('=I', 3))
        for cycle in range(300):
            for state in (2, 4, 8, 16, 2):  # REACHABLE, STALE, DELAY, PROBE
                proof = self.notify(NotificationTests.neighbor(2, address=IP, family=socket.AF_INET, state=state, attrs=attrs))
                self.assertIs(proof, self.proof)
                self.assertTrue(self.service.phone_ready(self.clock.now(), proof))
            if cycle % 5 == 0:
                self.assertTrue(self.network.send(b'feed', (IP, 4349)))
            self.clock.advance(.2)
        self.assertEqual(len(self.network.sender.sent), 60)
        self.guard.runner.assert_not_called()

    def test_phone_fdb_refresh_preserves_proof_without_commands(self):
        attrs = (NotificationTests.attr(9, struct.pack('=I', 2))
                 + NotificationTests.attr(15, struct.pack('=I', 0))
                 + NotificationTests.attr(3, b'\0' * 16))
        for _ in range(300):
            for state in (2, 4):
                proof = self.notify(NotificationTests.neighbor(7, state=state, attrs=attrs))
                self.assertIs(proof, self.proof)
                self.assertTrue(self.service.phone_ready(self.clock.now(), proof))
            self.clock.advance(.2)
        self.guard.runner.assert_not_called()

    def test_matching_ipv6_refresh_is_benign_but_failure_delete_and_move_are_not(self):
        for _ in range(300):
            for state in (2, 4, 8, 16):
                packet = NotificationTests.neighbor(2, address='fe80::2', family=socket.AF_INET6, state=state)
                self.assertIs(self.notify(packet), self.proof)
        self.guard.runner.assert_not_called()
        for changes in ({'state': 32}, {'kind': 29}, {'index': 5}):
            args = dict(index=2, address='fe80::2', family=socket.AF_INET6)
            args.update(changes)
            self.notify(NotificationTests.neighbor(**args))
        self.assertEqual(self.guard.runner.call_count, 12)

    def assert_revoked(self, packet):
        sender = self.network.sender
        proof = self.notify(packet)
        self.assertIsNone(proof.peer(IP))
        self.assertFalse(self.service.phone_ready(self.clock.now(), proof))
        self.assertEqual(self.guard.runner.call_count, 4)
        with self.assertRaises(OSError):
            self.network.send(b'blocked', (IP, 4349))
        self.assertEqual(sender.sent, [])

    def test_ipv4_lladdr_change_revokes_before_output(self):
        changed = '02:00:00:00:00:99'
        self.fixture.neigh[0]['lladdr'] = changed
        self.assert_revoked(NotificationTests.neighbor(2, address=IP, mac=changed, family=socket.AF_INET))

    def test_ipv4_failed_state_revokes_before_output(self):
        self.fixture.neigh[0]['state'] = ['FAILED']
        self.assert_revoked(NotificationTests.neighbor(2, address=IP, family=socket.AF_INET, state=32))

    def test_ipv4_delete_without_lladdr_revokes_before_output(self):
        self.fixture.neigh.clear()
        self.assert_revoked(NotificationTests.neighbor(2, address=IP, mac=None, family=socket.AF_INET, kind=29))

    def test_protected_mac_learned_on_bat0_revokes_before_output(self):
        self.fixture.fdb.append(dict(self.fixture.fdb[0], ifname='bat0'))
        self.assert_revoked(NotificationTests.neighbor(5, attrs=NotificationTests.attr(9, struct.pack('=I', 2))))

    def test_protected_mac_fdb_delete_revokes_before_output(self):
        self.fixture.fdb.clear()
        self.assert_revoked(NotificationTests.neighbor(7, kind=29))

    def test_fdb_static_change_revokes_before_output(self):
        self.fixture.fdb[0]['state'] = 'static'
        self.assert_revoked(NotificationTests.neighbor(7, state=64, attrs=NotificationTests.attr(9, struct.pack('=I', 2))))

    def test_known_identity_membership_cannot_replace_ip_mac_association(self):
        other = '02:00:00:00:00:03'
        self.fixture.fdb.append(dict(self.fixture.fdb[0], mac=other))
        self.fixture.neigh.append(dict(self.fixture.neigh[0], dst='192.0.2.3', lladdr=other))
        self.proof = self.guard.current(force=True)
        self.guard.runner.reset_mock()
        self.fixture.neigh[0]['lladdr'] = other
        proof = self.notify(NotificationTests.neighbor(2, address=IP, mac=other, family=socket.AF_INET))
        self.assertEqual(self.guard.runner.call_count, 4)
        self.assertFalse(self.service.phone_ready(self.clock.now(), proof))

    def test_fdb_flags_must_match_readback_and_unknown_readback_is_conservative(self):
        attrs = NotificationTests.attr(9, struct.pack('=I', 2))
        for flag, bit in (('master', 4), ('extern_learn', 16), ('offload', 32), ('sticky', 64), ('router', 128)):
            with self.subTest(flag=flag):
                self.fixture.fdb[0]['flags'] = [flag]
                proof = self.notify(NotificationTests.neighbor(7, flags=bit, attrs=attrs))
                self.assertEqual(self.guard.runner.call_count, 4)
                self.assertTrue(self.service.phone_ready(self.clock.now(), proof))
                self.guard.runner.reset_mock()
                self.assertIs(self.notify(NotificationTests.neighbor(7, flags=bit, attrs=attrs)), proof)
                self.guard.runner.assert_not_called()
        for extra in ({'flags': ['unknown']}, {'vlan': 1}, {'master': 'other'}, {'flags': ['master', 'master']}):
            with self.subTest(extra=extra):
                self.fixture.fdb[0] = dict(mac=MAC, ifname='wlan7', state='', **extra)
                self.guard.current(force=True)
                self.guard.runner.reset_mock()
                self.notify(NotificationTests.neighbor(7, attrs=attrs))
                self.assertEqual(self.guard.runner.call_count, 4)

    def test_dangerous_or_unclassifiable_peer_events_invalidate(self):
        neighbor = NotificationTests.neighbor
        attr = NotificationTests.attr
        ipargs = dict(index=2, address=IP, family=socket.AF_INET)
        master = attr(9, struct.pack('=I', 2))
        packets = [neighbor(**ipargs, state=state) for state in (0, 1, 32, 64, 128, 2 | 32, 0x100)]
        packets += [neighbor(7, state=state, attrs=master) for state in (0, 1, 8, 16, 32, 64, 128)]
        packets += [neighbor(7, flags=flag, attrs=master) for flag in (1, 2, 8)]
        packets += [neighbor(index, attrs=master) for index in (3, 5, 99, 0, -1)]
        packets += [neighbor(5, address=IP, family=socket.AF_INET),
                    neighbor(2, mac=None, address=IP, family=socket.AF_INET),
                    neighbor(2, family=socket.AF_INET),
                    neighbor(7),  # no bridge master evidence
                    neighbor(7, attrs=attr(9, struct.pack('=I', 99))),
                    neighbor(**ipargs, ndm_type=2), neighbor(7, attrs=master, ndm_type=1)]
        for attrs in (attr(2, b'x' * 6), attr(3, b'x'), attr(4, b'x'),
                      attr(9, b'x'), attr(15, b'x'), attr(15, struct.pack('=I', 1)),
                      attr(5, b'\0' * 2), attr(99, b'unknown'),
                      struct.pack('=HH', 3, 3), struct.pack('=HH', 50, 3),
                      struct.pack('=HH', 5, 12) + b'x', b'\0'):
            packets.append(neighbor(**ipargs, attrs=attrs))
        for packet in packets:
            with self.subTest(packet=packet):
                kind = struct.unpack_from('=H', packet, 4)[0]
                length = struct.unpack_from('=I', packet)[0]
                self.assertTrue(self.events.relevant(kind, packet[16:length]))

    def test_delete_cannot_be_cancelled_by_later_refresh_in_same_batch(self):
        args = dict(index=2, address=IP, family=socket.AF_INET)
        packet = NotificationTests.neighbor(**args, kind=29) + NotificationTests.neighbor(**args)
        self.notify(packet)
        self.assertEqual(self.guard.runner.call_count, 4)

    def test_healthy_refresh_cannot_hide_firewall_change_before_send(self):
        sender = self.network.sender
        self.fixture.rules = {'nftables': []}
        healthy = NotificationTests.neighbor(2, address=IP, family=socket.AF_INET)
        self.socks[0].recvmsg.side_effect = [(healthy, [], 0, (0, 1)), BlockingIOError(), BlockingIOError()]
        self.socks[1].recvmsg.side_effect = [
            (NotificationTests.packet((10 << 8) | 16), [], 0, (0, 1)), BlockingIOError(), BlockingIOError()]
        with self.assertRaises(OSError):
            self.network.send(b'blocked', (IP, 4349))
        self.assertEqual(sender.sent, [])
        self.assertTrue(sender.closed)
        self.assertEqual(self.guard.runner.call_count, 1)


class BeaconTransportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = GuardTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        net = self.fixture.net
        (net / 'end0/carrier').write_text('0')
        (net / 'bat0').mkdir()
        (net / 'bat0/master').symlink_to('../br0')
        (net / 'bat0/carrier').write_text('1')
        (net / 'br0/brif/bat0').mkdir()
        (net / 'br0/brif/bat0/state').write_text('3')
        for name, index in (('br0', 2), ('end0', 3), ('bat0', 4)):
            (net / name / 'ifindex').write_text(str(index))
        self.guard = self.fixture.cached_guard()
        self.network = atak.Network(FakeSocket, self.guard.current)
        self.addCleanup(self.network.close)
        self.packet = cot.radio_beacon('radio-1', 'mesh-5cc2', cot.Fix(40, -105, 'gnss'), BASE)

    def test_no_eud_path_needed_and_socket_cannot_join_or_loop_back(self):
        self.assertTrue(self.network.send_beacon(self.packet))
        proof = self.guard.cached
        self.assertEqual(proof.peers, {})
        self.assertEqual(proof.local_ports, {})
        self.assertEqual(proof.mesh_path, ('2', '4'))
        sock = self.network.beacon_sender
        self.assertEqual(sock.sent, [(self.packet, ('239.2.3.1', 6969))])
        self.assertEqual(sock.bound, (LOCAL, 0))
        for option in ((socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b'br0\0'),
                       (socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(LOCAL)),
                       (socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1),
                       (socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0),
                       (socket.IPPROTO_IP, 10, 2)):
            self.assertIn(option, sock.options)
        self.assertFalse(any(o[:2] == (socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP) for o in sock.options))
        self.assertEqual(self.guard.runner.call_count, 4)
        self.network.send_beacon(self.packet)
        self.assertEqual(self.guard.runner.call_count, 4)
        self.assertIs(self.network.beacon_sender, sock)
        self.network.close()
        self.assertTrue(sock.closed)

    def test_firewall_event_is_checked_before_next_send(self):
        self.network.send_beacon(self.packet)
        sock = self.network.beacon_sender
        self.fixture.rules['nftables'].pop()  # missing EUD egress confinement
        self.guard.events.changed.side_effect = [True, False]
        with self.assertRaisesRegex(OSError, 'path unavailable'):
            self.network.send_beacon(self.packet)
        self.assertEqual(len(sock.sent), 1)
        self.assertTrue(sock.closed)
        self.assertEqual(self.guard.runner.call_count, 5)

    def test_bat0_link_loss_invalidates_proof_and_blocks_beacon(self):
        self.network.send_beacon(self.packet)
        scope = self.guard.events.scope
        events = atak.NetworkEvents.__new__(atak.NetworkEvents)
        events.scope = scope
        self.assertTrue(events.relevant(16, NotificationTests.link(4, 'bat0')[16:]))
        self.assertTrue(events.relevant(16, NotificationTests.link(77, 'bat0')[16:]))
        self.assertFalse(events.relevant(16, NotificationTests.link(99, 'wlan-unrelated')[16:]))
        (self.fixture.net / 'bat0/carrier').write_text('0')
        self.guard.events.changed.side_effect = [True, False]
        with self.assertRaisesRegex(OSError, 'path unavailable'):
            self.network.send_beacon(self.packet)

    def test_bridge_recreation_rebinds_and_oversized_datagrams_are_rejected(self):
        self.network.send_beacon(self.packet)
        sock = self.network.beacon_sender
        (self.fixture.net / 'br0/ifindex').write_text('22')
        self.guard.events.changed.side_effect = [True, False]
        self.network.send_beacon(self.packet)
        self.assertTrue(sock.closed)
        self.assertIsNot(sock, self.network.beacon_sender)
        self.guard.events.changed.side_effect = None
        self.guard.events.changed.return_value = False
        with self.assertRaisesRegex(OSError, 'budget'):
            self.network.send_beacon(b'x' * 1201)

    def test_local_beacon_egress_is_only_bat0(self):
        objects = atak.firewall_objects(('end0', 'wlan7'))
        rule = objects[-2]['rule']
        self.assertEqual((rule['family'], rule['chain']), ('bridge', 'output'))
        for port in ('bat0', 'end0', 'wlan7', 'unexpected-port'):
            for destination in ('239.2.3.1', '239.2.3.2'):
                for dport in (6969, 4242):
                    values = {'meta': {'oifname': port, 'l4proto': 'udp'},
                              'ether': {'type': 'ip'}, 'ip': {'daddr': destination},
                              'udp': {'dport': dport}}
                    matches = []
                    for expr in rule['expr'][:-1]:
                        m = expr['match']
                        left = m['left']
                        if 'meta' in left:
                            actual = values['meta'][left['meta']['key']]
                        else:
                            p = left['payload']
                            actual = values[p['protocol']][p['field']]
                        matches.append((actual == m['right']) if m['op'] == '==' else (actual != m['right']))
                    self.assertEqual(all(matches), port != 'bat0' and destination == '239.2.3.1' and dport == 6969)
        self.assertEqual(rule['expr'][-1], {'drop': None})


class NetworkTests(Harness):
    def setUp(self):
        super().setUp()
        self.network = atak.Network(FakeSocket)
        self.network.configure(self.proof)
        self.addCleanup(self.network.close)

    def connect(self, address=IP):
        sock = FakeSocket()
        self.network.listener.accepted.append((sock, (address, 12000)))
        self.network.handle([self.network.listener], self.clock.now(), self.service)
        return sock

    def test_every_new_tcp_stream_requires_fresh_verification(self):
        self.network.verify = Mock(return_value=self.proof)
        first = self.connect()
        second = self.connect()
        self.assertIn(first, self.network.streams)
        self.assertIn(second, self.network.streams)
        self.assertEqual(self.network.verify.call_count, 2)
        self.network.verify.assert_called_with(force=True)
        self.network.verify.return_value = None
        denied = self.connect()
        self.assertTrue(denied.closed)
        self.assertTrue(first.closed)
        self.assertEqual(self.network.readers(), [])

    def test_new_udp_peer_checked_once_then_again_after_mac_change(self):
        self.network.verify = Mock(return_value=self.proof)
        packet = self.packets.sa('none', self.phone_time())
        for _ in range(3):
            self.network.udp.incoming.append((packet, (IP, 12000)))
            self.network.handle([self.network.udp], self.clock.now(), self.service)
        self.network.verify.assert_called_once_with(force=True)
        updated = atak.Proof(LOCAL, {IP: '02:00:00:00:00:03'}, self.proof.generation)
        self.network.configure(updated)
        self.network.verify.return_value = updated
        self.network.udp.incoming.append((packet, (IP, 12000)))
        self.network.handle([self.network.udp], self.clock.now(), self.service)
        self.assertEqual(self.network.verify.call_count, 2)

    def test_generation_change_and_monitor_loss_cannot_admit_a_stream(self):
        self.network.verify = Mock(return_value=atak.Proof(LOCAL, {IP: MAC}, 'new-generation'))
        denied = self.connect()
        self.assertTrue(denied.closed)
        self.network.verify.side_effect = atak.GuardFailure('lost monitor')
        with self.assertRaises(atak.GuardFailure):
            self.connect()

    def test_send_checks_pending_invalidation_without_forcing_a_readback(self):
        self.network.verify = Mock(return_value=None)
        with self.assertRaises(OSError):
            self.network.send(b'packet', (IP, 4349))
        self.network.verify.assert_called_once_with()
        self.assertIsNone(self.network.sender)

    def test_only_ipv4_br0_binding_and_nonfragmenting_output(self):
        self.assertEqual(self.network.listener.bound, ('0.0.0.0', 4242))
        self.assertEqual(self.network.udp.bound, ('0.0.0.0', 4242))
        for sock in self.network.readers():
            self.assertEqual(sock.family, socket.AF_INET)
            self.assertIn((socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b'br0\0'), sock.options)
        self.assertIn((socket.IPPROTO_IP, 10, 2), self.network.sender.options)
        self.network.configure(None)
        self.assertEqual(self.network.readers(), [])
        with self.assertRaises(OSError):
            self.network.send(b'packet', (IP, 4349))

    def test_fragmented_coalesced_tcp_sa_marker_chat_receipt_and_eof(self):
        sock = self.connect()
        packet = self.packets.sa('none', self.phone_time())
        sock.incoming.append(packet[:100])
        self.network.handle([sock], self.clock.now(), self.service)
        self.assertIsNone(self.service.position.pinned_uid)
        sock.incoming.append(packet[100:])
        self.network.handle([sock], self.clock.now(), self.service)
        self.tick()
        self.clock.advance(3)
        self.tick()
        msg = self.of_type('b-t-f')[-1].find('detail/__chat').get('messageId')
        self.clock.advance(1)
        sock.incoming.append(self.packets.point(self.phone_time(), (40, -105), 'one') +
                             self.packets.chat(self.phone_time(), 'hello') + self.packets.receipt(self.phone_time(), msg, True))
        self.network.handle([sock], self.clock.now(), self.service)
        self.assertEqual(self.service.position._manual.marker_uid, 'one')
        self.assertEqual(self.service.chats[-1]['text'], 'hello')
        self.assertEqual(self.service.audio, {})
        sock.incoming.append(b'')
        self.network.handle([sock], self.clock.now(), self.service)
        self.assertTrue(sock.closed)

    def test_udp_sa_and_unknown_path(self):
        packet = self.packets.sa('none', self.phone_time())
        for address, expected in (('192.0.2.99', None), (IP, self.packets.uid)):
            self.network.udp.incoming.append((packet, (address, 1234)))
            self.network.handle([self.network.udp], self.clock.now(), self.service)
            self.assertEqual(self.service.position.pinned_uid, expected)

    def test_connection_limits_unknown_peers_and_policy_loss(self):
        self.assertTrue(self.connect('192.0.2.99').closed)
        socks = [self.connect() for _ in range(atak.MAX_PER_PEER + 1)]
        self.assertTrue(socks[-1].closed)
        self.network.configure(atak.Proof(LOCAL, {IP: MAC, '192.0.2.3': '02:00:00:00:00:03',
                                                 '192.0.2.4': '02:00:00:00:00:04'}, self.proof.generation))
        socks += [self.connect('192.0.2.3') for _ in range(atak.MAX_PER_PEER)]
        self.assertEqual(len(self.network.streams), atak.MAX_CONNECTIONS)
        self.assertTrue(self.connect('192.0.2.4').closed)
        self.network.configure(None)
        self.assertTrue(all(s.closed for s in socks))

    def test_time_byte_and_event_bounds(self):
        for prefix, timeout in ((b'<event', atak.PARTIAL_S), (None, atak.IDLE_S)):
            sock = self.connect()
            if prefix:
                sock.incoming.append(prefix)
                self.network.handle([sock], self.clock.now(), self.service)
            self.clock.advance(timeout)
            self.network.handle([], self.clock.now(), self.service)
            self.assertTrue(sock.closed)
        stream = atak.Stream(FakeSocket(), IP, MAC, 0)
        self.assertTrue(stream.expired(atak.LIFETIME_S))
        budget = atak.Budget(0)
        for second in range(4):
            self.assertEqual(stream.receive(b' ' * 65536, second, budget), [])
        with self.assertRaises(ValueError):
            stream.receive(b' ', 4, budget)
        stream = atak.Stream(FakeSocket(), IP, MAC, 0)
        with self.assertRaises(ValueError):
            stream.receive(self.packets.sa('none', BASE) * 65, 0, atak.Budget(0))

    def test_malformed_batch_eof_oversize_and_dtd(self):
        packet = self.packets.sa('none', BASE)
        for data in (packet + b'<wrong/>', b'<!DOCTYPE event [<!ENTITY x "y">]><event/>',
                     b'<event value="' + b'x' * atak.MAX_XML):
            sock = self.connect()
            sock.incoming.append(data)
            self.network.handle([sock], self.clock.now(), self.service)
            self.assertTrue(sock.closed)
            self.assertIsNone(self.service.position.pinned_uid)
        sock = self.connect()
        sock.incoming += [b'<event', b'']
        self.network.handle([sock], self.clock.now(), self.service)
        self.network.handle([sock], self.clock.now(), self.service)
        self.assertTrue(sock.closed)

    def test_persistence_error_is_fatal_not_a_connection_error(self):
        sock = self.connect()
        sock.incoming.append(self.packets.sa('none', BASE))
        self.store.fail = True
        with self.assertRaises(atak.PersistenceError):
            self.network.handle([sock], self.clock.now(), self.service)


class LazyNetworkTests(Harness):
    def setUp(self):
        super().setUp()
        self.fixture = GuardTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.guard = self.fixture.cached_guard()
        self.network = atak.Network(FakeSocket, self.guard.current)
        self.addCleanup(self.network.close)

    def idle(self):
        proof = self.guard.current(refresh=self.service.has_live_lease(self.clock.now()))
        self.assertIsNone(proof)
        self.network.idle(LOCAL)

    def test_active_unrelated_events_need_no_readback_but_duplicate_mac_and_policy_revoke(self):
        (self.fixture.net / 'br0/ifindex').write_text('2')
        (self.fixture.net / 'end0/ifindex').write_text('3')
        self.idle()
        self.arrive()
        sockets = [Mock(), Mock()]
        events = atak.NetworkEvents(Mock(side_effect=sockets))
        self.addCleanup(events.close)
        for sock in sockets:
            sock.recvmsg.side_effect = BlockingIOError
        self.guard.events = events
        events.scope = self.guard.event_scope(self.guard.cached)
        self.guard.runner.reset_mock()
        unrelated = (NotificationTests.neighbor(5, mac='02:00:00:00:00:99')
                     + NotificationTests.neighbor(5, mac='02:00:00:00:00:99', address='192.0.2.99', family=2)
                     + NotificationTests.link(99, 'unrelated'))
        for _ in range(300):
            sockets[0].recvmsg.side_effect = [(unrelated, [], 0, (0, 1)), BlockingIOError()]
            proof = self.guard.current(refresh=self.service.has_live_lease(self.clock.now()))
            self.assertTrue(self.service.phone_ready(self.clock.now(), proof))
        self.guard.runner.assert_not_called()
        self.fixture.fdb.append({'mac': MAC, 'ifname': 'bat0'})
        sockets[0].recvmsg.side_effect = [
            (NotificationTests.neighbor(5), [], 0, (0, 1)), BlockingIOError(), BlockingIOError()]
        proof = self.guard.current()
        self.assertIsNone(proof.peer(IP))
        self.assertFalse(self.service.phone_ready(self.clock.now(), proof))
        self.assertEqual(self.guard.runner.call_count, 4)
        self.fixture.fdb.pop()
        sockets[0].recvmsg.side_effect = BlockingIOError
        self.network.configure(self.guard.current(force=True))
        sender = self.network.sender
        self.fixture.rules = {'nftables': []}
        sockets[1].recvmsg.side_effect = [
            (NotificationTests.packet((10 << 8) | 16), [], 0, (0, 1)), BlockingIOError(), BlockingIOError()]
        with self.assertRaises(OSError):
            self.network.send(b'blocked', (IP, 4349))
        self.assertEqual(sender.sent, [])
        self.assertTrue(sender.closed)

    def arrive(self, tcp=False):
        packet = self.packets.sa('none', self.phone_time())
        if tcp:
            sock = FakeSocket()
            self.network.listener.accepted.append((sock, (IP, 12000)))
            self.network.handle([self.network.listener], self.clock.now(), self.service)
            if not sock.closed:
                sock.incoming.append(packet)
                self.network.handle([sock], self.clock.now(), self.service)
            return sock
        self.network.udp.incoming.append((packet, (IP, 12000)))
        self.network.handle([self.network.udp], self.clock.now(), self.service)

    def test_idle_neighbor_churn_discards_proof_without_commands(self):
        for previously_verified in (False, True):
            if previously_verified:
                self.assertIsNotNone(self.guard.current())
            self.guard.runner.reset_mock()
            sock = Mock()
            events = atak.NetworkEvents(Mock(return_value=sock))
            self.guard.events = events
            for _ in range(300):
                # RTM_DELNEIGH is the continuous bat0 churn observed on CM4.
                sock.recvmsg.side_effect = [
                    (NotificationTests.packet(29), [], 0, (0, 1)),
                    BlockingIOError(), BlockingIOError()]
                self.idle()
            self.guard.runner.assert_not_called()
            self.assertIsNone(self.guard.cached)
            self.assertTrue(self.guard.dirty)
            status = self.service.status(self.clock.now(), self.network.proof)
            self.assertFalse(status['path_ready'])
            self.assertIsNone(self.network.sender)
            self.assertEqual(self.network.listener.bound, ('0.0.0.0', 4242))
            self.guard.events = Mock(sockets=[], changed=Mock(return_value=False))
            events.close()

    def test_tcp_and_udp_arrival_from_idle_force_full_proof(self):
        for tcp in (False, True):
            with self.subTest(tcp=tcp):
                self.guard.invalidate()
                self.idle()
                self.guard.runner.reset_mock()
                self.arrive(tcp)
                self.assertEqual(self.guard.runner.call_count, 4)
                self.assertTrue(self.service.phone_ready(self.clock.now(), self.network.proof))
                self.clock.advance(100)  # allow the previous SA lease to expire

    def test_new_phone_rejected_if_idle_firewall_was_changed(self):
        self.assertIsNotNone(self.guard.current())
        self.guard.invalidate()
        self.fixture.rules = {'nftables': []}
        self.idle()
        self.guard.runner.reset_mock()
        sock = self.arrive(tcp=True)
        self.assertTrue(sock.closed)
        self.assertEqual(self.guard.runner.call_count, 1)
        self.assertIn('firewall', self.guard.reason)
        self.assertFalse(self.service.phones)
        self.assertIsNone(self.network.sender)

    def test_firewall_change_between_active_tick_and_send_fails_closed(self):
        self.idle()
        self.arrive()
        sender = self.network.sender
        self.fixture.rules = {'nftables': []}
        self.guard.events.changed.side_effect = [True, False]
        with self.assertRaisesRegex(OSError, 'path unavailable'):
            self.network.send(b'blocked', (IP, 4349))
        self.assertEqual(sender.sent, [])
        self.assertTrue(sender.closed)
        self.assertIsNone(self.guard.cached)

    def test_present_phone_keeps_event_readback_and_transient_retry(self):
        self.idle()
        self.arrive()
        self.guard.events.changed.side_effect = [True, False]
        self.fixture.fdb.append({'mac': MAC, 'ifname': 'bat0'})
        proof = self.guard.current(refresh=self.service.has_live_lease(self.clock.now()))
        self.assertIsNone(proof.peer(IP))
        self.assertFalse(self.service.phone_ready(self.clock.now(), proof))
        self.assertTrue(self.service.has_live_lease(self.clock.now()))
        self.guard.events.changed.side_effect = None
        self.fixture.fdb.pop()
        self.guard.invalidate()
        self.guard.runner.side_effect = subprocess.TimeoutExpired('nft', 1)
        self.assertIsNone(self.guard.current(refresh=self.service.has_live_lease(self.clock.now())))
        self.guard.runner.side_effect = self.fixture.runner
        self.assertEqual(self.guard.current(refresh=self.service.has_live_lease(self.clock.now())).peer(IP), MAC)

    def test_idle_forgets_udp_cache_and_unassociated_streams(self):
        self.idle()
        self.arrive()
        self.assertIn(IP, self.network.datagram_peers)
        client = self.arrive(tcp=True)
        self.clock.advance(100)
        self.guard.invalidate()
        self.idle()
        self.assertTrue(client.closed)
        self.assertFalse(self.network.datagram_peers)
        self.guard.runner.reset_mock()
        self.arrive()
        self.assertEqual(self.guard.runner.call_count, 4)

    def test_address_discovery_is_cheap_and_late_address_can_receive(self):
        self.network.idle(None)
        self.assertFalse(self.network.readers())
        probe = Mock()
        reply = b'\0' * 20 + socket.inet_aton(LOCAL) + b'\0' * 232
        with patch.object(atak.socket, 'socket') as factory, patch.object(atak.fcntl, 'ioctl') as ioctl:
            factory.return_value.__enter__.return_value = probe
            ioctl.side_effect = [OSError('no address'), reply]
            self.assertIsNone(self.guard.listen_address())
            self.network.idle(self.guard.listen_address())
        self.guard.runner.assert_not_called()
        self.arrive()
        self.assertTrue(self.service.phone_ready(self.clock.now(), self.network.proof))
        previous = self.network.listener
        self.fixture.addresses[0]['addr_info'][0]['local'] = '192.0.2.10'
        self.guard.invalidate()
        self.network.idle('192.0.2.10')
        self.assertTrue(previous.closed)
        self.assertEqual(self.network.listener.bound, ('0.0.0.0', 4242))
        self.arrive()
        self.assertEqual(self.network.proof.local_ip, '192.0.2.10')

    def test_idle_notification_loss_still_fails_closed(self):
        self.idle()
        self.guard.events.changed.side_effect = OSError('ENOBUFS')
        with self.assertRaises(atak.GuardFailure):
            self.guard.current(refresh=False)
        self.guard.runner.assert_not_called()

    def test_main_loop_defers_snapshots_for_idle_events(self):
        handlers = {}
        event_socket = object()
        self.guard.events.sockets = [event_socket]
        self.guard.events.changed.return_value = True
        cycles = 0

        def ready(*args):
            nonlocal cycles
            cycles += 1
            self.clock.advance(1)
            if cycles == 300:
                handlers[atak.signal.SIGTERM]()
            return [event_socket], [], []

        def local_path(name):
            if name in ('/run/manet-atak', '/var/lib/manet-atak'):
                return Path(self.fixture.temp.name) / name.rsplit('/', 1)[-1]
            return Path(name)

        with patch.object(atak, 'Path', side_effect=local_path), \
                patch.object(atak.Config, 'load', return_value=self.config), \
                patch.object(atak, 'Clock', return_value=self.clock), \
                patch.object(atak, 'Network', return_value=self.network), \
                patch.object(atak, 'Service', return_value=self.service), \
                patch.object(atak, 'LinuxGuard', return_value=self.guard), \
                patch.object(atak, 'Application') as application, \
                patch.object(self.guard, 'start'), \
                patch.object(self.guard, 'listen_address', return_value=LOCAL), \
                patch.object(self.network, 'handle', side_effect=lambda *args: self.clock.advance(.4)), \
                patch.object(atak.select, 'select', side_effect=ready), \
                patch.object(atak.signal, 'signal', side_effect=lambda kind, fn: handlers.update({kind: fn})), \
                patch.object(atak, 'log'):
            application.return_value.update.side_effect = lambda now, *_: self.assertEqual(now, self.clock.now())
            atak.main([])
        self.assertEqual(cycles, 300)
        self.guard.runner.assert_not_called()
        self.assertTrue(all(call.args[1] is None for call in application.return_value.update.call_args_list))


def mesh_frame(payload, *, address=IP, mac=MAC, destination=tak.MESH_GROUP,
               port=tak.MESH_PORT, options=b'', fragment=0, checksum=True):
    udp = struct.pack('!HHHH', 40000, port, 8 + len(payload), 0) + payload
    source, target = socket.inet_aton(address), socket.inet_aton(destination)
    if checksum:
        value = atak.mesh_checksum(source + target + b'\0\x11' + udp[4:6] + udp) or 0xffff
        udp = udp[:6] + struct.pack('!H', value) + udp[8:]
    size = 20 + len(options)
    header = struct.pack('!BBHHHBBH4s4s', 0x40 | (size // 4), 0, size + len(udp), 1,
                         fragment, 64, 17, 0, source, target) + options
    header = header[:10] + struct.pack('!H', atak.mesh_checksum(header)) + header[12:]
    return bytes.fromhex('01005e020301' + mac.replace(':', '') + '0800') + header + udp


def run_mesh_bpf(frame, packet_type=2):
    """Interpret the emitted classic BPF subset, including short-load rejection."""
    program, pc, accumulator, index = atak.mesh_sa_bpf(), 0, 0, 0
    while pc < len(program):
        code, jt, jf, value = program[pc]
        pc += 1
        if code in (0x20, 0x28, 0x30, 0x48, 0xb1):
            if value == 0xfffff004:
                accumulator = packet_type
                continue
            offset = value + (index if code == 0x48 else 0)
            size = {0x20: 4, 0x28: 2, 0x30: 1, 0x48: 2, 0xb1: 1}[code]
            if offset + size > len(frame):
                return 0
            loaded = int.from_bytes(frame[offset:offset + size], 'big')
            if code == 0xb1:
                index = 4 * (loaded & 15)
            else:
                accumulator = loaded
        elif code == 0x80:
            accumulator = len(frame)
        elif code == 0x54:
            accumulator &= value
        elif code == 0x87:
            accumulator = index
        elif code == 0x04:
            accumulator += value
        elif code == 0x16:
            return accumulator
        elif code in (0x15, 0x25, 0x35, 0x45):
            matches = {0x15: accumulator == value, 0x25: accumulator > value,
                       0x35: accumulator >= value, 0x45: bool(accumulator & value)}[code]
            pc += jt if matches else jf
        elif code == 0x06:
            return value
        else:
            raise AssertionError('unexpected BPF opcode')
    raise AssertionError('BPF program fell off end')


class MeshSAIngressTests(Harness):
    def setUp(self):
        super().setUp()
        self.fixture = APAdmissionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.guard = self.fixture.cached_guard()
        self.factory = Mock(side_effect=FakeSocket)
        self.network = atak.Network(self.factory, self.guard.current)
        self.addCleanup(self.network.close)
        self.guard.current(refresh=False)
        self.network.sync_mesh_ports(self.guard.local_ports)
        self.network.idle(LOCAL)

    def receive(self, *, iface='end0', payload=None, packet_type=2, metadata_iface=None,
                flags=0, **options):
        if payload is None:
            payload = sa_fixture(stamp=self.phone_time(), contact_xml=True)[0]
        sock = self.network.mesh_sockets[iface][1]
        data = mesh_frame(payload, **options)
        sock.incoming.append((data, [], flags, (metadata_iface or iface, 0x0800, packet_type,
                                               1, bytes.fromhex(MAC.replace(':', '')))))
        self.network.handle([sock], self.clock.now(), self.service)

    def test_own_beacon_is_never_local_phone_admission(self):
        beacon = cot.radio_beacon(self.config.uid, 'mesh-5cc2', cot.Fix(40, -105, 'gnss'), BASE)
        frame = mesh_frame(beacon, address=LOCAL)
        self.assertEqual(run_mesh_bpf(frame, socket.PACKET_OUTGOING), 0)
        with patch.object(tak, 'decode_mesh_sa', wraps=tak.decode_mesh_sa) as decoder:
            self.receive(payload=beacon, address=LOCAL, packet_type=socket.PACKET_OUTGOING)
            self.receive(payload=beacon, address=LOCAL)  # reflected frame still has no proven local peer
            decoder.assert_not_called()
        self.receive(payload=beacon)  # copied payload from a real EUD is not person SA
        self.assertEqual(self.service.phones, {})
        self.assertEqual(self.tx.sent, [])
        forged = self.packets.sa('none', self.phone_time()).replace(
            self.packets.uid.encode(), (self.config.uid + '-beacon').encode())
        proof = self.guard.current()
        self.assertEqual(self.service.ingest(forged, IP, proof, self.clock.now()), 'reserved radio beacon UID')

    def test_bpf_drops_unrelated_fragments_egress_and_short_frames(self):
        good = mesh_frame(b'example')
        self.assertEqual(run_mesh_bpf(good), atak.MAX_MESH_FRAME)
        self.assertEqual(run_mesh_bpf(mesh_frame(b'example', options=b'\x01' * 4)), atak.MAX_MESH_FRAME)
        rejected = [mesh_frame(b'x', destination='239.2.3.2'), mesh_frame(b'x', port=4242),
                    mesh_frame(b'x', fragment=0x2000), mesh_frame(b'x', fragment=1),
                    mesh_frame(b'x', fragment=0x8000),
                    mesh_frame(b'x' * (atak.MAX_MESH_FRAME + 1)),
                    good[:12] + b'\x86\xdd' + good[14:], good[:23] + b'\x06' + good[24:],
                    good[:14] + b'\x44' + good[15:]]
        for frame in rejected:
            self.assertEqual(run_mesh_bpf(frame), 0)
        for size in range(38):
            self.assertEqual(run_mesh_bpf(good[:size]), 0, size)
        for kind in (4, 5, 6):
            self.assertEqual(run_mesh_bpf(good, kind), 0)
        # A remote phone's frame forwarded out an EUD port is PACKET_OUTGOING.
        self.assertEqual(run_mesh_bpf(good, socket.PACKET_OUTGOING), 0)

    def test_filter_installed_before_bind_without_any_membership_or_promiscuity(self):
        self.assertEqual(set(self.network.mesh_sockets), {'end0', 'wlan7'})
        for iface, (_, sock) in self.network.mesh_sockets.items():
            self.assertEqual((sock.family, sock.kind, sock.protocol), (socket.AF_PACKET, socket.SOCK_RAW, 0))
            self.assertEqual(sock.bound, (iface, 3))
            self.assertEqual(sock.options[0][:2], (socket.SOL_SOCKET, 26))
            self.assertIn((socket.SOL_SOCKET, 44, 1), sock.options)
            self.assertIn((263, 23, 1), sock.options)
        for sock in self.network.readers():
            self.assertFalse(any(level == socket.IPPROTO_IP and option == socket.IP_ADD_MEMBERSHIP
                                 or level == 263 and option == 1
                                 for level, option, _ in sock.options))
        with self.assertRaises(ValueError):
            atak.make_mesh_sa_socket('bat0', self.factory)

    def test_kernel_accepts_native_filter_and_filters_synthetic_local_datagrams(self):
        # AF_UNIX exercises the real kernel verifier/filter and ctypes ABI
        # without CAP_NET_RAW, hardware access, multicast or a network address.
        receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(receiver.close)
        self.addCleanup(sender.close)
        proxy = Mock()

        def option(level, name, value):
            if level == socket.SOL_SOCKET:
                receiver.setsockopt(level, name, value)

        proxy.setsockopt.side_effect = option
        proxy.setblocking.side_effect = receiver.setblocking
        try:
            atak.make_mesh_sa_socket('end0', Mock(return_value=proxy))
        except PermissionError:
            self.skipTest('sandbox disallows attaching a kernel socket filter')
        good = mesh_frame(b'mesh SA')
        sender.send(mesh_frame(b'not SA', port=4242))
        sender.send(good)
        self.assertEqual(receiver.recv(atak.MAX_MESH_FRAME), good)
        with self.assertRaises(BlockingIOError):
            receiver.recv(atak.MAX_MESH_FRAME)

    def test_filter_failure_closes_socket_before_it_can_receive(self):
        sock = Mock()
        sock.setsockopt.side_effect = OSError('filter rejected')
        with self.assertRaises(OSError):
            atak.make_mesh_sa_socket('end0', Mock(return_value=sock))
        sock.bind.assert_not_called()
        sock.close.assert_called_once()

    def test_ap_and_wired_v1_admission_and_unicast_replies(self):
        for iface, port in (('end0', 'wired'), ('wlan7', 'wifi')):
            with self.subTest(iface=iface):
                self.fixture.fdb[0]['ifname'] = iface
                self.guard.invalidate()
                self.clock.advance(100)
                self.receive(iface=iface)
                item = self.service.phones['synthetic-phone']
                self.assertEqual((item['ip'], item['mac'], item['port']), (IP, MAC, port))
                self.assertTrue(self.service.phone_ready(self.clock.now(), self.network.proof))
                self.service.transport = self.network
                self.service.tick(self.clock.now(), self.network.proof,
                                  phone.RadioGPS(cot.Fix(40.5, -105.5, 'gnss'), self.clock.now().mono,
                                                 'UNCHECKED', False, 0, False), {})
                self.assertTrue(self.network.sender.sent)
                self.assertTrue(all(endpoint[0] == IP and endpoint[1] in (4242, 4349)
                                    for _, endpoint in self.network.sender.sent))
                self.assertEqual({endpoint[1] for _, endpoint in self.network.sender.sent}, {4242, 4349})
                self.assertNotIn('+15550100000', json.dumps(self.store.data))
                self.assertNotIn('+15550100000', json.dumps(self.service.status(self.clock.now(), self.network.proof)))

    def test_legacy_xml_mesh_sa_and_endpoint_mismatch(self):
        _, xml = sa_fixture(stamp=self.phone_time())
        self.receive(payload=xml)
        self.assertIn('synthetic-phone', self.service.phones)
        self.clock.advance(1)
        for endpoint in ('192.0.2.99:4242:tcp', 'manet.local:4242:tcp', 'tcpsrcreply:4242:srctcp', ''):
            packet, _ = sa_fixture(uid='another-phone', endpoint=endpoint, stamp=self.phone_time())
            self.receive(payload=packet)
            self.assertNotIn('another-phone', self.service.phones)
            self.assertIn('endpoint', self.service.last_rejection)

    def test_remote_and_unproven_frames_never_reach_decoder(self):
        with patch.object(tak, 'decode_mesh_sa') as decoder:
            self.receive(packet_type=socket.PACKET_OUTGOING)
            self.receive(metadata_iface='bat0')
            self.receive(flags=socket.MSG_TRUNC)
            self.receive(mac='02:00:00:00:00:03')
            self.receive(iface='wlan7')  # MAC is learned on end0, not wlan7.
            self.fixture.fdb.append({'mac': MAC, 'ifname': 'bat0'})
            self.guard.invalidate()
            self.receive()
            self.fixture.fdb.pop()
            self.fixture.neigh = []
            self.guard.invalidate()
            self.receive()
            decoder.assert_not_called()
        self.assertNotIn('bat0', self.network.mesh_sockets)

    def test_firewall_change_blocks_mesh_decoder_and_output(self):
        self.receive()
        self.fixture.rules = {'nftables': []}
        self.guard.events.changed.side_effect = [True, False]
        with patch.object(tak, 'decode_mesh_sa') as decoder:
            self.receive()
            decoder.assert_not_called()
        self.guard.events.changed.side_effect = None
        with self.assertRaises(OSError):
            self.network.send(b'blocked', (IP, 4349))

    def test_link_role_and_hostapd_events_change_sockets_without_polling(self):
        original = dict(self.network.mesh_sockets)
        with patch.object(self.guard, 'local_paths', wraps=self.guard.local_paths) as paths:
            for _ in range(300):
                self.guard.current(refresh=False)
                self.network.sync_mesh_ports(self.guard.local_ports)
            paths.assert_not_called()
        self.assertEqual(self.network.mesh_sockets, original)
        self.guard.runner.assert_not_called()
        (self.fixture.proc / '123/stat').unlink()
        self.guard.current(refresh=False)
        self.network.sync_mesh_ports(self.guard.local_ports)
        self.assertTrue(original['wlan7'][1].closed)
        self.assertIs(self.network.mesh_sockets['end0'][1], original['end0'][1])
        (self.fixture.proc / '123/stat').write_text('123 (hostapd) S ' + '0 ' * 18 + '54321\n')
        self.guard.current(refresh=False)
        self.network.sync_mesh_ports(self.guard.local_ports)
        self.assertIsNot(self.network.mesh_sockets['wlan7'][1], original['wlan7'][1])
        (self.fixture.net / 'end0/carrier').write_text('0')
        self.guard.events.changed.return_value = True
        self.guard.current(refresh=False)
        self.network.sync_mesh_ports(self.guard.local_ports)
        self.assertNotIn('end0', self.network.mesh_sockets)
        previous = self.network.mesh_sockets['wlan7'][1]
        (self.fixture.net / 'wlan7/ifindex').write_text('77\n')
        self.guard.current(refresh=False)
        self.network.sync_mesh_ports(self.guard.local_ports)
        self.assertTrue(previous.closed)
        self.assertIsNot(previous, self.network.mesh_sockets['wlan7'][1])
        self.guard.runner.assert_not_called()

    def test_bad_checksum_lengths_and_shared_budget_prevent_decoding(self):
        packet = mesh_frame(b'example')
        self.assertEqual(atak.mesh_checksum(bytes.fromhex('450000730000400040110000c0a80001c0a800c7')), 0xb861)
        self.assertEqual(atak.mesh_sa_frame(mesh_frame(b'example', checksum=False))[2], b'example')
        self.assertEqual(atak.mesh_sa_frame(mesh_frame(b'example', options=b'\x01' * 4))[2], b'example')
        for bad in (packet[:-1], packet[:24] + b'\x01' + packet[25:], packet[:-1] + b'!',
                    packet[:38] + b'\x00\x01' + packet[40:]):
            with self.assertRaises(ValueError):
                atak.mesh_sa_frame(bad)
        self.network.budget = atak.Budget(self.clock.now().mono)
        self.network.budget.accepts = 0
        with patch.object(tak, 'decode_mesh_sa') as decoder:
            self.receive()
            decoder.assert_not_called()
        self.guard.runner.assert_not_called()

    def test_secondary_unicast_address_allowed_other_destinations_rejected(self):
        secondary = '192.0.2.10'
        self.fixture.addresses[0]['addr_info'].append(
            {'family': 'inet', 'scope': 'global', 'local': secondary, 'secondary': True})
        self.network.configure(self.guard.current(force=True))
        self.assertEqual(self.network.proof.local_ip, LOCAL)
        for destination in (secondary, '192.0.2.99'):
            sock = FakeSocket()
            sock.destination = destination
            self.network.listener.accepted.append((sock, (IP, 12000)))
            self.network.handle([self.network.listener], self.clock.now(), self.service)
            self.assertEqual(sock.closed, destination != secondary)
        _, packet = sa_fixture(stamp=self.phone_time())
        self.network.udp.destination = '192.0.2.99'
        self.network.udp.incoming.append((packet, (IP, 12000)))
        self.network.handle([self.network.udp], self.clock.now(), self.service)
        self.assertFalse(self.service.phones)
        self.network.udp.destination = secondary
        self.network.udp.incoming.append((packet, (IP, 12000)))
        self.network.handle([self.network.udp], self.clock.now(), self.service)
        self.assertTrue(self.service.phone_ready(self.clock.now(), self.network.proof))
        # The existing bind override must not promote a secondary address to
        # the advertised contact/sender address.
        self.guard.config = atak.Config('radio-1', bind_ip=secondary)
        self.assertIsNone(self.guard.current(force=True))


class SimulatorTests(unittest.TestCase):
    def test_all_sample_modes_and_direct_messages_parse(self):
        packets = sim.Packets()
        packets.radio_uid = 'radio-1'
        for mode, source in {'none': 'unknown', 'gps': 'gps', 'user': 'manual', 'drag': 'manual', 'echo': 'external'}.items():
            parsed = cot.parse_self_sa(packets.sa(mode, BASE))
            self.assertIsInstance(parsed, cot.PhoneSA)
            self.assertEqual(parsed.source, source)
        point = cot.parse_marker(packets.point(BASE, (40, -105), 'marker-1'))
        self.assertEqual(point.creator_uid, packets.uid)
        resent = cot.parse_marker(packets.resend('marker-1', BASE + timedelta(seconds=60)))
        self.assertEqual(resent.creator_time, BASE)
        self.assertGreater(resent.time, point.time)
        self.assertIsInstance(cot.parse_phone_chat(packets.chat(BASE, '<plain text>')), cot.PhoneChat)
        for read in (False, True):
            receipt = cot.parse_chat_receipt(packets.receipt(BASE, 'message-1', read))
            self.assertIsInstance(receipt, cot.ChatReceipt)
            self.assertEqual(receipt.destination_uid, 'radio-1')


class ScenarioPlanTests(unittest.TestCase):
    def make_plan(self):
        h = Harness()
        h.setUp()

        class RadioClock(FakeClock):
            mono_shift = boot_shift = 0

            def now(self):
                now = super().now()
                return atak.Now(now.mono + self.mono_shift, now.raw,
                                now.boot + self.boot_shift, now.utc)

        h.clock = RadioClock()
        h.clock.wall_offset = -432000

        class FakeScenario(scenario.Plan):
            def __init__(self):
                super().__init__(h.packets)
                self.token = 'offline'
                self.output_cursor = 0
                self.fault = False
                self.captures = []

            def mono(self):
                return h.clock.seconds

            def utc(self):
                return BASE + timedelta(seconds=h.clock.seconds)

            def record(self, *args, **kwargs):
                pass

            def send(self, data, tcp=True):
                h.service.ingest(data, IP, h.proof, h.clock.now())

            def status(self):
                result = h.tick(state='GNSS_SUSPECTED' if self.fault else 'UNCHECKED', epoch=int(self.fault))
                while self.output_cursor < len(h.tx.sent):
                    root, destination = h.tx.sent[self.output_cursor]
                    self.output_cursor += 1
                    # This legacy GPS scenario acknowledges its first chat;
                    # group notices are separately exercised by multi-phone tests.
                    remarks = root.find('detail/remarks')
                    if remarks is None or 'ATAK phones are connected' not in (remarks.text or ''):
                        self.receive(destination[1], ET.tostring(root))
                return dict(result, _radio_wall=h.clock.now().utc.timestamp())

            def wait(self, seconds):
                end = self.mono() + seconds
                while self.mono() < end:
                    h.clock.advance(min(.25, end - self.mono()))
                    if not self.silent and self.mono() >= self.due:
                        self.sa()
                    self.status()

            def monitor_fault(self):
                self.fault = True

            def restart(self):
                h.restart()
                self.wait(1)

            def reboot(self, boot):
                self.check('old boot identified', boot == h.service.boot_id)
                h.clock.mono_shift = -h.clock.seconds - 90
                h.clock.boot_shift = -h.clock.seconds - 990
                h.restart('boot-2')
                self.wait(1)

            def capture_start(self):
                self.captures.append('start')

            def capture_stop(self, label):
                self.captures.append(label)

        return FakeScenario(), scenario.CheckFailure

    def test_scenario_against_real_service_with_fake_time_io_and_reboot(self):
        plan, _ = self.make_plan()
        with patch('sys.stdout', io.StringIO()):
            plan.run()
        self.assertGreater(plan.passes, 60)
        self.assertEqual(plan.captures, ['before reboot', 'start', 'after reboot'])

    def test_plan_rejects_wrong_received_coordinates(self):
        plan, failure = self.make_plan()
        receive = plan.receive

        def corrupt(port, data):
            root = ET.fromstring(data)
            if port == 4349:
                root.find('point').set('lat', '42')
            receive(port, ET.tostring(root))
        plan.receive = corrupt
        with patch('sys.stdout', io.StringIO()), self.assertRaisesRegex(failure, '4349 manual coordinates'):
            plan.run()

    def test_plan_retries_status_while_runtime_directory_is_recreated(self):
        plan, _ = self.make_plan()
        attempts = []

        def startup():
            attempts.append(None)
            if len(attempts) < 3:
                raise subprocess.CalledProcessError(1, 'ssh', stderr='status not written yet')
            return True
        with patch('sys.stdout', io.StringIO()):
            plan.until('fresh status', startup)
        self.assertEqual(len(attempts), 3)


class CrashAndCadenceTests(Harness):
    def test_receipt_recovers_send_interrupted_before_durable_ack(self):
        self.sa()
        self.tick()
        self.clock.advance(3)
        def crash_after_send(data, destination):
            if b'Radio GPS is unavailable or untrusted' in data:
                self.store.fail = True
        self.tx.before_send = crash_after_send
        with self.assertRaises(atak.PersistenceError):
            self.tick()
        message = self.of_type('b-t-f')[-1].find('detail/__chat').get('messageId')
        self.store.fail = False
        self.tx.before_send = None
        self.clock.advance(1)
        self.restart()
        self.assertIsNone(self.service.position._warnings['gps_untrusted'].message_id)
        receipt = self.packets.receipt(self.phone_time(), message, True)
        self.assertEqual(self.service.ingest(receipt, IP, self.proof, self.clock.now()), 'read')
        self.assertEqual(self.service.messages, {})
        self.assertEqual(self.service.audio, {})
        self.tick()
        warnings = [r for r in self.of_type('b-t-f') if 'Radio GPS is unavailable' in ET.tostring(r).decode()]
        self.assertEqual(len(warnings), 1)

    def test_new_monitor_activation_is_durable_without_a_position_fault(self):
        self.service.input_memory.update(monitor_seen=True, monitor_token=['producer-1', 0])
        self.tick(cot.Fix(40, -105, 'gnss'))
        count = self.store.writes
        self.clock.advance(1)
        self.tick(cot.Fix(40, -105, 'gnss'))
        self.assertEqual(self.store.writes, count)
        self.restart()
        self.assertTrue(self.service.input_memory['monitor_seen'])

    def test_failed_heartbeat_attempts_are_limited_to_one_per_second(self):
        self.sa()
        attempted = []
        self.tx.before_send = lambda data, destination: attempted.append(destination) if ET.fromstring(data).get('type') != 'b-t-f' else None
        self.tx.fail_ports = {4242, 4349}
        self.tick(cot.Fix(40, -105, 'gnss'))
        self.assertEqual(len(attempted), 2)
        self.clock.advance(.2)
        self.tick(cot.Fix(40, -105, 'gnss'))
        self.assertEqual(len(attempted), 2)
        self.clock.advance(.8)
        self.tick(cot.Fix(40, -105, 'gnss'))
        self.assertEqual(len(attempted), 4)
        self.assertIsNone(self.service.position._last_sent_mono)

    def test_endpoint_change_refreshes_contact_before_normal_interval(self):
        self.sa()
        self.tick()
        self.clock.advance(1)
        self.proof = atak.Proof('192.0.2.10', {IP: MAC}, self.proof.generation)
        self.tick()
        contacts = [r for r, d in self.tx.sent if r.find('detail/contact') is not None]
        self.assertEqual(len(contacts), 2)
        self.assertEqual(contacts[-1].find('detail/contact').get('endpoint'), '192.0.2.10:4242:tcp')


class APAdmissionTests(unittest.TestCase):
    runner = GuardTests.runner
    cached_guard = GuardTests.cached_guard

    def setUp(self):
        GuardTests.setUp(self)
        base = Path(self.temp.name)
        self.role = base / 'ap_interface'
        self.conf = base / 'hostapd.conf'
        self.proc = base / 'proc'
        self.role.write_text('wlan7\n')
        self.conf.write_text('interface=wlan7\nbridge=br0\n')
        (self.proc / '123').mkdir(parents=True)
        (self.proc / '123/stat').write_text('123 (hostapd) S ' + '0 ' * 18 + '12345\n')
        (self.proc / '123/cgroup').write_text('0::/system.slice/hostapd.service\n')
        (self.run / 'hostapd.pid').write_text('123\n')
        (self.net / 'wlan7').mkdir()
        (self.net / 'wlan7/master').symlink_to('../br0')
        (self.net / 'wlan7/carrier').write_text('1')
        (self.net / 'br0/brif/wlan7').mkdir()
        (self.net / 'br0/brif/wlan7/state').write_text('3')
        self.guard = atak.LinuxGuard(atak.Config('radio-1'), self.runner, self.net, self.run,
                                     self.role, self.conf, self.proc)
        self.rules = {'nftables': atak.firewall_objects(('end0', 'wlan7'))}

    # The parent methods describe wired-only fixtures; run those in GuardTests.
    def test_ap_only_with_routed_end0_and_missing_ethernet_state(self):
        (self.net / 'end0/master').unlink()
        for name in ('ethernet_detection_state', 'eth-carrier-generation'):
            (self.run / name).unlink()
        self.fdb[0]['ifname'] = 'wlan7'
        proof = self.guard.inspect()
        self.assertEqual(proof.peer(IP), MAC)
        self.assertEqual(proof.port(IP), 'wifi')
        for name in ('carrier',):
            (self.net / 'wlan7' / name).write_text('0')
        self.assertIsNone(self.guard.inspect())

    def test_both_ports_independent_and_mesh_mac_never_admitted(self):
        wifi_ip, wifi_mac = '192.0.2.3', '02:00:00:00:00:03'
        self.fdb.append({'mac': wifi_mac, 'ifname': 'wlan7'})
        self.neigh.append({'dst': wifi_ip, 'lladdr': wifi_mac, 'state': ['REACHABLE']})
        proof = self.guard.inspect()
        self.assertEqual(proof.ports, {IP: 'wired', wifi_ip: 'wifi'})
        self.fdb.append({'mac': wifi_mac, 'ifname': 'bat0'})
        self.assertIsNone(self.guard.inspect().peer(wifi_ip))
        self.fdb.pop()
        (self.net / 'end0/carrier').write_text('0')
        proof = self.guard.inspect()
        self.assertIsNone(proof.peer(IP))
        self.assertEqual(proof.peer(wifi_ip), wifi_mac)

    def test_hostapd_stop_stale_pid_and_wrong_config_revoke_only_ap(self):
        self.fdb[0]['ifname'] = 'wlan7'
        guard = self.cached_guard()
        self.assertEqual(guard.current().peer(IP), MAC)
        commands = guard.runner.call_count
        for _ in range(300):
            self.assertEqual(guard.current().peer(IP), MAC)
        self.assertEqual(guard.runner.call_count, commands)
        (self.proc / '123/stat').unlink()
        self.assertIsNone(guard.current().peer(IP))
        self.fdb[0]['ifname'] = 'end0'
        self.assertEqual(guard.current(force=True).peer(IP), MAC)
        (self.proc / '123/stat').write_text('123 (other) S ' + '0 ' * 18 + '12345\n')
        self.assertIsNone(guard.hostapd_identity())
        (self.proc / '123/stat').write_text('123 (hostapd) S ' + '0 ' * 18 + '12345\n')
        self.conf.write_text('interface=wlan8\nbridge=br0\n')
        self.fdb[0]['ifname'] = 'wlan7'
        self.assertIsNone(guard.current().peer(IP))

    def test_ap_rename_requires_firewall_restart_and_unknown_ports_fail(self):
        self.role.write_text('wlan8\n')
        self.assertIsNone(self.guard.inspect())
        self.assertIn('restart', self.guard.reason)
        self.assertTrue(atak.valid_firewall(self.rules, ('end0', 'wlan7')))
        self.assertFalse(atak.valid_firewall(self.rules, ('end0', 'wlan8')))
        self.assertFalse(atak.valid_firewall(self.rules))
        reordered = copy.deepcopy(self.rules)
        for obj in reordered['nftables']:
            for expr in obj.get('rule', {}).get('expr', []):
                right = expr.get('match', {}).get('right')
                if isinstance(right, dict) and 'set' in right:
                    right['set'].reverse()
        self.assertTrue(atak.valid_firewall(reordered, ('end0', 'wlan7')))
        for value in ('bat0', 'br0', 'wlan*', '../wlan7', 'wlan7 wlan8'):
            self.role.write_text(value)
            with self.assertRaises(ValueError):
                atak.atak_ap_interface(self.role)


class MultiPhoneTests(Harness):
    def setUp(self):
        super().setUp()
        self.other = sim.Packets()
        self.other.uid, self.other.callsign, self.other.radio_uid = 'guest-phone', 'GUEST', 'radio-1'
        self.other_ip, self.other_mac = '192.0.2.3', '02:00:00:00:00:03'
        self.proof = atak.Proof(LOCAL, {IP: MAC, self.other_ip: self.other_mac}, 'both',
                                {IP: 'wifi', self.other_ip: 'wifi'})

    def other_sa(self, mode='none', point=None):
        return self.service.ingest(self.other.sa(mode, self.phone_time(), point), self.other_ip,
                                   self.proof, self.clock.now())

    def chats_to(self, address, contains):
        return [r for r, dest in self.tx.sent if dest[0] == address and r.get('type') == 'b-t-f'
                and contains in ET.tostring(r).decode()]

    def drain(self, fix=None):
        for _ in range(6):
            self.tick(fix)
            self.clock.advance(1)

    def test_feeds_contacts_and_warnings_reach_both_in_their_own_clock(self):
        self.sa()
        other_time = self.phone_time() + timedelta(days=2)
        self.service.ingest(self.other.sa('none', other_time), self.other_ip, self.proof, self.clock.now())
        gps = cot.Fix(40, -105, 'gnss')
        self.tick(gps)
        for address in (IP, self.other_ip):
            self.assertTrue(any(dest == (address, 4349) for _, dest in self.tx.sent))
            self.assertTrue(any(dest[0] == address and root.find('detail/contact') is not None for root, dest in self.tx.sent))
        other_feed = next(root for root, dest in self.tx.sent if dest == (self.other_ip, 4349))
        self.assertEqual(other_feed.get('time'), sim.stamp(other_time))
        self.clock.advance(1)
        self.tick(gps, 'GNSS_SUSPECTED', epoch=1)
        before = sum(dest[1] == 4349 for _, dest in self.tx.sent)
        self.drain()
        self.assertEqual(sum(dest[1] == 4349 for _, dest in self.tx.sent), before)
        self.assertTrue(self.chats_to(IP, 'Radio GPS is unavailable'))
        self.assertTrue(self.chats_to(self.other_ip, 'Radio GPS is unavailable'))

    def test_guest_marker_drag_user_and_receipt_cannot_adopt_or_ack(self):
        self.sa()
        self.other_sa()
        self.drain()
        controller = self.service.position.pinned_uid
        for _ in range(3):
            packet = self.other.point(self.phone_time(), (42, -107))
            self.assertIn('guest marker', self.service.ingest(packet, self.other_ip, self.proof, self.clock.now()))
            self.clock.advance(1)
        self.assertEqual(self.other_sa('user', (42, -107)), 'accepted_presence')
        self.clock.advance(1)
        self.assertEqual(self.other_sa('drag', (43, -108)), 'accepted_presence')
        self.drain()
        self.assertIsNone(self.service.position._manual)
        notices = self.chats_to(self.other_ip, ' follows ')
        self.assertEqual(len(notices), 1)
        self.assertIn('To change it, open manet.local in your browser.', ET.tostring(notices[0]).decode())
        warning = self.service.position._warnings['gps_untrusted']
        receipt = self.other.receipt(self.phone_time(), warning.message_id, True)
        self.assertEqual(self.service.ingest(receipt, self.other_ip, self.proof, self.clock.now()), 'guest_receipt')
        self.assertFalse(warning.acknowledged)
        self.assertEqual(self.service.position.pinned_uid, controller)

    def test_notice_once_per_growth_or_controller_change_and_random_stable(self):
        with patch.object(atak.secrets, 'choice', side_effect=lambda peers: peers[0]) as pick:
            self.sa()
            self.other_sa()
            self.drain(cot.Fix(40, -105, 'gnss'))
            chosen = self.service.position.pinned_uid
            count = len(self.of_type('b-t-f'))
            self.drain(cot.Fix(40, -105, 'gnss'))
            self.restart()
            self.drain(cot.Fix(40, -105, 'gnss'))
            self.assertEqual(pick.call_count, 1)
            self.assertEqual(self.service.position.pinned_uid, chosen)
            self.assertEqual(len(self.of_type('b-t-f')), count)
            for ip in (IP, self.other_ip):
                self.assertEqual(len(self.chats_to(ip, '2 ATAK phones are connected')), 1)
            self.service.choose_controller(self.clock.now(), self.proof, self.other.uid)
            self.drain(cot.Fix(40, -105, 'gnss'))
            for ip in (IP, self.other_ip):
                self.assertEqual(len(self.chats_to(ip, '2 ATAK phones are connected')), 2)

    def test_lone_phone_gets_no_group_notice_until_a_second_arrives(self):
        self.sa()
        self.drain(cot.Fix(40, -105, 'gnss'))
        self.assertEqual(self.service.position.pinned_uid, self.packets.uid)
        self.assertEqual(self.chats_to(IP, 'ATAK phones are connected'), [])
        self.other_sa()
        self.drain(cot.Fix(40, -105, 'gnss'))
        for ip in (IP, self.other_ip):
            self.assertEqual(len(self.chats_to(ip, '2 ATAK phones are connected')), 1)

    def test_only_wired_preferred_then_manual_wins_and_recovers_after_absence(self):
        self.sa()
        self.proof = atak.Proof(LOCAL, self.proof.peers, 'both', {IP: 'wifi', self.other_ip: 'wired'})
        self.other_sa()
        self.assertEqual(self.service.position.pinned_uid, self.other.uid)
        self.service.choose_controller(self.clock.now(), self.proof, self.packets.uid)
        self.assertEqual(self.service.position.pinned_uid, self.packets.uid)
        self.send_point(uid='held')
        held = self.service.position._manual
        self.restart()
        self.assertEqual(self.service.manual_uid, self.packets.uid)
        self.clock.advance(76)
        self.other_sa()
        self.assertEqual(self.service.position.pinned_uid, self.other.uid)
        self.assertEqual(self.service.position._manual, held)
        self.assertEqual(self.service.manual_uid, self.packets.uid)
        self.restart()
        self.clock.advance(1)
        self.sa()
        self.assertEqual(self.service.position.pinned_uid, self.packets.uid)
        self.assertEqual(self.service.position._manual, held)

    def test_replacement_phone_takes_over_after_presence_expiry(self):
        self.sa()
        self.send_point(uid='held')
        self.clock.advance(76)
        self.other_sa()
        self.assertEqual(self.service.position.pinned_uid, self.other.uid)
        self.assertEqual(self.service.position._manual.marker_uid, 'held')
        # Different phone wall clocks cannot prevent a new controller's new mark.
        self.clock.advance(1)
        packet = self.other.point(self.phone_time(), (41, -106), 'replacement')
        self.assertEqual(self.service.ingest(packet, self.other_ip, self.proof, self.clock.now()), 'accepted:newest_location')
        self.tick()
        self.assertEqual(self.service.retire['held']['phone_uid'], self.packets.uid)
        self.assertEqual(self.service.retire['held']['attempts'], 0)

    def test_manual_choice_survives_reboot_and_requires_fresh_presence(self):
        self.sa()
        self.other_sa()
        self.service.choose_controller(self.clock.now(), self.proof, self.other.uid)
        self.send_point(uid='ignored')  # now a guest: cannot become the held location
        self.restart('boot-2', atak.Now(5, 7, 5, BASE))
        self.assertEqual(self.service.manual_uid, self.other.uid)
        self.assertEqual(self.service.present_phones(atak.Now(5, 7, 5, BASE), self.proof), {})
        self.assertEqual(self.service.position.pinned_uid, self.other.uid)
        self.assertIsNone(self.service.position._manual)

    def test_capacity_rejects_live_overflow_but_replaces_expired_and_bad_sa_cannot_evict(self):
        self.service.config = atak.replace(self.config, max_phones=1)
        self.sa()
        self.assertEqual(self.other_sa(), 'phone table full')
        self.assertEqual(list(self.service.phones), [self.packets.uid])
        self.clock.advance(76)
        invalid = self.other.sa('none', self.phone_time()).replace(b'guest-phone', b'radio-1')
        self.service.ingest(invalid, self.other_ip, self.proof, self.clock.now())
        self.assertEqual(list(self.service.phones), [self.packets.uid])
        self.assertEqual(self.other_sa(), 'accepted_presence')
        self.assertEqual(list(self.service.phones), [self.other.uid])
        self.restart()
        self.assertEqual(self.service.position.pinned_uid, self.other.uid)

    def test_disabled_or_wrong_port_has_no_feed_or_controller_mutation(self):
        self.sa()
        self.other_sa()
        self.tx.sent.clear()
        moved = atak.Proof(LOCAL, {IP: MAC}, 'wifi', {IP: 'wired'})
        self.assertEqual(self.service.present_phones(self.clock.now(), moved), {})
        self.service.tick(self.clock.now(), moved, phone.RadioGPS(cot.Fix(40, -105, 'gnss'), self.clock.now().mono), {})
        self.assertEqual(self.tx.sent, [])

    def test_absent_controller_random_choice_between_guests_is_stable(self):
        self.sa()
        self.clock.advance(40)
        self.other_sa()
        third = sim.Packets()
        third.uid, third.callsign, third.radio_uid = 'third-phone', 'THIRD', self.config.uid
        ip, mac = '192.0.2.4', '02:00:00:00:00:04'
        self.proof = atak.Proof(LOCAL, dict(self.proof.peers, **{ip: mac}), 'both',
                                dict(self.proof.ports, **{ip: 'wifi'}))
        self.service.ingest(third.sa('none', self.phone_time()), ip, self.proof, self.clock.now())
        self.clock.advance(36)
        with patch.object(atak.secrets, 'choice', return_value=third.uid) as pick:
            self.tick(cot.Fix(40, -105, 'gnss'))
            self.assertEqual(set(pick.call_args.args[0]), {self.other.uid, third.uid})
            self.assertEqual(self.service.position.pinned_uid, third.uid)
            self.restart()
            self.clock.advance(1)
            self.tick(cot.Fix(40, -105, 'gnss'))
            self.assertEqual(self.service.position.pinned_uid, third.uid)
            self.assertEqual(pick.call_count, 1)

    def test_provisioned_limit_above_eight_admits_ninth_phone(self):
        self.service.config = atak.replace(self.config, max_phones=20)
        self.sa()
        for index in range(8):
            peer = sim.Packets()
            peer.uid, peer.callsign, peer.radio_uid = f'phone-{index}', f'P{index}', self.config.uid
            ip, mac = f'192.0.2.{index + 10}', f'02:00:00:00:01:{index:02x}'
            self.proof = atak.Proof(LOCAL, dict(self.proof.peers, **{ip: mac}), 'wifi',
                                    dict(self.proof.ports, **{ip: 'wifi'}))
            result = self.service.ingest(peer.sa('none', self.phone_time()), ip, self.proof, self.clock.now())
            self.assertEqual(result, 'accepted_presence')
        self.assertEqual(len(self.service.present_phones(self.clock.now(), self.proof)), 9)
        self.restart()
        self.assertEqual(len(self.service.phones), 9)

    def test_changed_controller_allows_one_new_guest_notice_and_keeps_held_mark(self):
        self.sa()
        self.other_sa()
        self.send_point(uid='held')
        packet = self.other.point(self.phone_time(), (42, -107))
        self.service.ingest(packet, self.other_ip, self.proof, self.clock.now())
        self.drain()
        self.service.choose_controller(self.clock.now(), self.proof, self.other.uid)
        self.service.choose_controller(self.clock.now(), self.proof, self.packets.uid)
        self.service.ingest(packet, self.other_ip, self.proof, self.clock.now())
        self.drain()
        self.assertEqual(len(self.chats_to(self.other_ip, ' follows ')), 2)
        self.assertEqual(self.service.position._manual.marker_uid, 'held')


class CapacityConfigTests(unittest.TestCase):
    def test_provisioned_range_zero_wired_and_logged_fallback(self):
        for raw, limit in [('0', 1), ('1', 1), ('20', 20), ('253', 253), ('8', 8),
                           ('254', 8), ('-1', 8), ('1.5', 8), ('', 8), ('٢', 8)]:
            with self.subTest(raw=raw), patch.object(atak, 'read_kv', return_value={'max_euds_per_node': raw}), \
                    patch.object(atak.Path, 'read_text', return_value='a' * 32), patch.object(atak, 'log') as logger:
                self.assertEqual(atak.Config.load().max_phones, limit)
                self.assertEqual(logger.called, raw in {'254', '-1', '1.5', '', '٢'})

    def test_schema_one_is_explicitly_discarded(self):
        h = Harness(); h.setUp()
        h.sa()
        data = h.store.data
        data['schema'] = 1
        data['sha256'] = atak.state_digest({k: v for k, v in data.items() if k != 'sha256'})
        h.restart()
        self.assertEqual(h.service.phones, {})
        self.assertIsNone(h.service.position.pinned_uid)
        self.assertEqual(h.store.data['schema'], 2)


class AtakOverviewTests(Harness):
    @classmethod
    def setUpClass(cls):
        cls.web = module('atak_overview_status', Path(__file__).with_name('mesh-status.py'))

    def test_panel_renders_only_in_the_local_radio_panel(self):
        # The button posts to this radio, so another radio's drawer must not
        # show it, and the local panel must.
        source = Path(__file__).with_name('mesh-status.py').read_text()
        def body(name):
            start = source.index('function ' + name + '(')
            return source[start:source.index('\n}\n', start)]
        self.assertIn('atakPanel(d.atak)', body('renderLocalPanel'))
        self.assertNotIn('atak', body('renderPeerDrawer').lower())

    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.runtime = Path(temp.name)
        self.conf = self.runtime / 'mesh.conf'
        self.conf.write_text('ipv4_network=192.0.2.0/24\natak=y\n')
        self.dhcp = self.runtime / 'dhcp.conf'
        self.dhcp.write_text('dhcp-range=192.0.2.2,192.0.2.8,255.255.255.0,12h\n')
        self.boot = self.runtime / 'boot-id'; self.boot.write_text('boot-1')
        for name, value in [('ATAK_RUNTIME', self.runtime), ('ATAK_DHCP_CONF', self.dhcp),
                            ('ATAK_BOOT_ID', self.boot), ('MESH_CONF_FILE', str(self.conf))]:
            patcher = patch.object(self.web, name, value)
            patcher.start(); self.addCleanup(patcher.stop)
        self.sa()
        self.app = atak.Application(self.service, atak.Inputs('boot-1', self.runtime), self.runtime)
        self.app.update(self.clock.now(), self.proof, 'verified')

    def post(self, origin='http://manet.local', client=IP, content_type='application/json', uid=None,
             release=False, manual_id=None):
        from email.message import Message
        handler = object.__new__(self.web.MeshHandler)
        handler.path = '/api/atak/release' if release else '/api/atak/controller'
        handler.client_address = (client, 12345)
        handler.headers = Message()
        for name, value in [('Host', 'manet.local'), ('Content-Type', content_type), ('Origin', origin)]:
            handler.headers[name] = value
        handler._body = json.dumps({'uid': uid or self.packets.uid,
                                   'manual_id': manual_id or self.service.manual_id()}).encode()
        handler._body_length = len(handler._body)
        handler.send_json = Mock()
        handler.send_403 = Mock()
        with patch.object(self.web.time, 'monotonic', return_value=self.clock.now().mono), \
                patch.object(self.web.time, 'clock_gettime', return_value=self.clock.now().boot):
            handler._do_POST()
        return handler

    def test_local_eud_password_free_post_is_consumed_on_existing_cycle(self):
        handler = self.post()
        self.assertEqual(handler.send_json.call_args.kwargs['status'], 202)
        self.assertIsNone(self.service.manual_uid)
        self.app.update(self.clock.now(), self.proof, 'verified')
        self.assertEqual(self.service.manual_uid, self.packets.uid)
        revision = self.service.revision
        self.app.update(self.clock.now(), self.proof, 'verified')
        self.assertEqual(self.service.revision, revision)
        self.restart()
        self.assertEqual(self.service.manual_uid, self.packets.uid)

    def test_local_panel_exposes_only_public_fields_and_post_keeps_session(self):
        # Exercise /api/local's assembler with unrelated node probes stubbed.
        with patch.multiple(self.web, get_my_hostname=Mock(return_value='radio-1'),
                            get_battery=Mock(return_value={}), get_interfaces=Mock(return_value=[]),
                            get_connected_euds=Mock(return_value=[]), get_running_services=Mock(return_value=[]),
                            get_local_uptime=Mock(return_value='1h'), get_power_status=Mock(return_value={}),
                            parse_registry=Mock(return_value={}), get_my_mac=Mock(return_value=MAC),
                            get_manet_version=Mock(return_value='test'), configured_ssid=Mock(return_value='ap'),
                            recovery_status=Mock(return_value={})), \
                patch.object(self.web.time, 'monotonic', return_value=self.clock.now().mono):
            panel = self.web.assemble_local_data()['atak']
        self.assertEqual(panel, {'enabled': True, 'running': True, 'manual': None, 'position': None, 'phones': [
            {'uid': self.packets.uid, 'callsign': self.service.phones[self.packets.uid]['callsign'],
             'port': 'wired', 'controller': True, 'tak_server': False, 'gps_disagreement_m': None}]})
        self.assertEqual(self.post().send_json.call_args.kwargs['status'], 202)
        request = atak.read_json(self.runtime / 'controller.json')
        self.assertEqual(request['session'], self.service.session)
        self.assertEqual(request['boot_id'], 'boot-1')

    def test_public_panel_still_expires_leases_and_handles_missing_status(self):
        with patch.object(self.web.time, 'monotonic', return_value=self.clock.advance(100).mono):
            self.assertEqual(self.web.atak_overview({}), {'enabled': True, 'running': True, 'manual': None, 'position': None, 'phones': []})
        (self.runtime / 'status.json').unlink()
        self.assertEqual(self.web.atak_overview({}), {'enabled': True, 'running': False, 'manual': None, 'position': None, 'phones': []})

    def hold_for_release(self):
        self.clock.advance(1)
        self.send_point(uid='held')
        self.app.update(self.clock.now(), self.proof, 'verified')

    def test_controller_release_post_is_consumed_and_retires_marker(self):
        self.hold_for_release()
        with patch.object(self.web.time, 'monotonic', return_value=self.clock.now().mono):
            public = self.web.atak_overview({})
        self.assertEqual(public['manual'], {'id': self.service.manual_id(), 'uid': self.packets.uid})
        self.assertEqual(self.post(release=True).send_json.call_args.kwargs['status'], 202)
        self.app.update(self.clock.now(), self.proof, 'verified')
        self.assertIsNone(self.service.position._manual)
        self.assertIn('held', self.service.retire)
        self.assertFalse(self.service.audio)
        messages = [e for e in self.of_type('b-t-f') if 'Held point released' in e.find('detail/remarks').text]
        self.assertEqual(len(messages), 1)
        self.app.update(self.clock.now(), self.proof, 'verified')
        self.restart()
        self.assertIsNone(self.service.position._manual)

    def test_status_demand_ignores_tick_and_receiver_receipt_timestamps(self):
        update = Mock()
        self.app.tak_update = update
        fix = cot.Fix(40, -105, 'gnss')
        with patch.object(self.app.inputs, 'cached_read', side_effect=lambda now, *args, **kwargs:
                          (phone.RadioGPS(fix, now.mono), {})):
            for _ in range(60):
                self.app.update(self.clock.now(), self.proof, 'verified')
                self.clock.advance(1)
        self.assertEqual([call.kwargs['request'] for call in update.call_args_list], [True] + [False] * 59)

    def test_guest_foreign_origin_and_obsolete_point_cannot_post_release(self):
        self.hold_for_release()
        for options, expected in (({'client': '192.0.2.3'}, 403),
                                  ({'origin': 'http://evil.example'}, 403),
                                  ({'uid': 'guest'}, 409), ({'manual_id': 'obsolete'}, 409)):
            with self.subTest(options=options):
                self.assertEqual(self.post(release=True, **options).send_json.call_args.kwargs['status'], expected)
                self.assertFalse((self.runtime / 'release.json').exists())
        self.assertIsNotNone(self.service.position._manual)

    def test_release_service_rechecks_point_controller_session_age_and_proof(self):
        self.hold_for_release()
        self.post(release=True)
        data = atak.read_json(self.runtime / 'release.json')
        for change, proof in (({'manual_id': 'obsolete'}, self.proof), ({'uid': 'guest'}, self.proof),
                              ({'session': 'old'}, self.proof), ({'boot_id': 'old'}, self.proof),
                              ({'written_boot': self.clock.now().boot - 6}, self.proof),
                              ({'ip': '192.0.2.3'}, self.proof), ({'mac': '02:00:00:00:00:03'}, self.proof),
                              ({}, None)):
            with self.subTest(change=change, proof=proof):
                self.assertFalse(self.service.release_request(dict(data, **change), self.clock.now(), proof))
                self.assertIsNotNone(self.service.position._manual)

    def test_trusted_own_fix_keeps_existing_confirmation_even_with_staged_release(self):
        self.hold_for_release()
        self.post(release=True)
        data = atak.read_json(self.runtime / 'release.json')
        now = self.clock.now()
        self.service.tick(now, self.proof, phone.RadioGPS(cot.Fix(40, -105, 'gnss'), now.mono), {},
                          release_request=data)
        self.assertIsNotNone(self.service.position._manual)

    def test_public_server_and_disagreement_evidence_expire_independently(self):
        status = atak.read_json(self.runtime / 'status.json')
        status['phones'][0].update(tak_server=True, tak_server_deadline=110,
                                   gps_disagreement_m=123, gps_disagreement_deadline=102)
        atak.atomic_json(self.runtime / 'status.json', status)
        for mono, expected in ((100, (True, 123)), (103, (True, None)), (111, (False, None))):
            with patch.object(self.web.time, 'monotonic', return_value=mono):
                peer = self.web.atak_overview({})['phones'][0]
                self.assertEqual((peer['tak_server'], peer['gps_disagreement_m']), expected)
                self.assertNotIn('mac', peer)
                self.assertNotIn('tak_server_deadline', peer)
        status['phones'][0].update(tak_server_deadline='invalid', gps_disagreement_deadline=None)
        atak.atomic_json(self.runtime / 'status.json', status)
        with patch.object(self.web.time, 'monotonic', return_value=100):
            peer = self.web.atak_overview({})['phones'][0]
            self.assertFalse(peer['tak_server'])
            self.assertIsNone(peer['gps_disagreement_m'])

    def test_phone_gps_public_position_expires_even_if_daemon_stops_writing(self):
        self.clock.advance(1)
        self.sa('gps')
        self.app.update(self.clock.now(), self.proof, 'verified')
        with patch.object(self.web.time, 'monotonic', return_value=self.clock.now().mono):
            position = self.web.atak_overview({})['position']
        self.assertTrue(position['source'].startswith('phone GPS ('))
        self.assertEqual(set(position), {'lat', 'lon', 'hae', 'source'})
        with patch.object(self.web.time, 'monotonic', return_value=self.clock.advance(75).mono):
            self.assertIsNone(self.web.atak_overview({})['position'])

    def test_foreign_origin_nonlocal_eud_nonjson_and_absent_phone_rejected(self):
        for options, expected in [({'origin': 'http://evil.example'}, 403),
                                  ({'origin': 'null'}, 403), ({'origin': 'http://['}, 403),
                                  ({'origin': 'https://manet.local'}, 403), ({'client': '192.0.2.25'}, 403),
                                  ({'content_type': 'text/plain'}, 415), ({'uid': 'absent'}, 409)]:
            with self.subTest(options=options):
                handler = self.post(**options)
                self.assertEqual(handler.send_json.call_args.kwargs['status'], expected)
                self.assertFalse((self.runtime / 'controller.json').exists())

    def test_disabled_panel_stale_choice_wrong_session_and_path_loss(self):
        self.assertIsNone(self.web.atak_overview({'atak': 'n'}))
        self.post()
        data = atak.read_json(self.runtime / 'controller.json')
        for modified, proof in [(dict(data, session='other'), self.proof),
                                (dict(data, written_boot=data['written_boot'] - 6), self.proof),
                                (data, None)]:
            self.assertFalse(self.service.controller_request(modified, self.clock.now(), proof))
        self.assertIsNone(self.service.manual_uid)


if __name__ == '__main__':
    unittest.main()
