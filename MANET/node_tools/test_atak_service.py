"""ATAK service integration with fake sockets/time and the captured Pixel packets."""
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

ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


atak = module('manet_atak_service', Path(__file__).with_name('manet-atak.py'))
sim = module('atak_phone_sim', ROOT / 'review-collab/atak-20261006/sim/phone.py')
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
        self.fail_ports = set()
        self.before_send = None

    def send(self, data, destination):
        if self.before_send:
            self.before_send(data, destination)
        if destination[1] in self.fail_ports:
            raise OSError('simulated transport failure')
        self.sent.append((ET.fromstring(data), destination))
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


class PixelFlowTests(Harness):
    def test_run1_resend_step_and_user_sa_keep_original_marker_age(self):
        self.clock.wall_offset = -464354.5  # radio clock about 5.4 days slow
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

    def test_internal_phone_gps_warning_and_echo_clear(self):
        self.sa()
        self.tick(cot.Fix(40, -105, 'gnss'))
        self.clock.advance(1)
        self.sa('gps')
        self.tick(cot.Fix(40, -105, 'gnss'))
        self.assertIn('phone_gps', self.service.banners)
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
        for change in ({'schema': 2}, {'uid': 'other'}, {'state': {'class': '__import__', 'fields': {}}}):
            self.store.data = dict(baseline, **change)
            with self.assertRaises((ValueError, KeyError, TypeError)):
                self.restart()

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
    """Real `nft -j list table` output from cm4 (nftables 1.1, kernel 6.18)."""
    READBACK = (Path(__file__).resolve().parents[2]
                / 'review-collab/atak-20261006/samples/nft-readback-cm4.json')

    def test_cm4_readback_omits_implied_matches_and_still_validates(self):
        if not self.READBACK.exists():
            self.skipTest('cm4 readback capture not in this checkout')
        data = json.loads(self.READBACK.read_text())
        self.assertTrue(atak.valid_firewall(data))

    def test_dropping_a_real_match_still_fails(self):
        if not self.READBACK.exists():
            self.skipTest('cm4 readback capture not in this checkout')
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
        unit.read(ROOT / 'MANET/systemd/manet-atak.service')
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
                same = value(m['left']) == value(m['right'])
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
    def __init__(self, family=socket.AF_INET, kind=socket.SOCK_STREAM):
        self.family, self.kind = family, kind
        self.closed = False
        self.options, self.accepted, self.incoming, self.sent = [], [], [], []
        self.bound = None

    def setsockopt(self, *args):
        self.options.append(args)

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
        self.assertEqual(self.network.listener.bound, (LOCAL, 4242))
        self.assertEqual(self.network.udp.bound, (LOCAL, 4242))
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


class LaptopPlanTests(unittest.TestCase):
    def make_plan(self):
        source = (ROOT / 'review-collab/atak-20261006/sim/cm4-plan.sh').read_text()
        source = source.split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        namespace = {'__name__': 'atak_laptop_plan_test'}
        exec(compile(source, 'cm4-plan.sh (embedded Python)', 'exec'), namespace)
        h = Harness()
        h.setUp()

        class RadioClock(FakeClock):
            mono_shift = boot_shift = 0

            def now(self):
                now = super().now()
                return atak.Now(now.mono + self.mono_shift, now.raw,
                                now.boot + self.boot_shift, now.utc)

        h.clock = RadioClock()
        h.clock.wall_offset = -464354.5

        class FakeLaptop(namespace['Plan']):
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

        return FakeLaptop(), namespace['CheckFailure']

    def test_laptop_plan_against_real_service_with_fake_time_io_and_reboot(self):
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
        def crash_after_send(data, destination):
            if ET.fromstring(data).get('type') == 'b-t-f':
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
        self.assertEqual(len(self.of_type('b-t-f')), 1)

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
        self.tx.before_send = lambda data, destination: attempted.append(destination)
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


if __name__ == '__main__':
    unittest.main()
