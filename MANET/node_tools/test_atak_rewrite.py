"""Phone GPS fallback and fail-open mesh position replacement regression tests."""
import copy
from datetime import timedelta
import socket
import struct
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import Mock, patch

import manet_cot as cot
import manet_phone as phone
import manet_tak as tak
import test_atak_service as base
from test_tak_protocol import field, sa_fixture

atak = base.atak
FIX = cot.Fix(41.25, -106.5, 'gnss', hae=1800,
              horizontal_error=cot.ErrorEstimate(4, .9), vertical_error=cot.ErrorEstimate(6, .9))


class ReceiverAndFallbackTests(base.Harness):
    def observe(self, present=False, state='UNCHECKED', proof=True):
        now = self.clock.now()
        return self.service.tick(now, self.proof if proof else None,
            phone.RadioGPS(None, now.mono, state, receiver_present=present), {'gps': 'no GPS receiver' if not present else 'no fix'})

    def test_no_receiver_silent_even_with_old_distrust_and_hotplug_warns(self):
        self.sa()
        self.tick(FIX, state='GNSS_SUSPECTED')
        self.assertIn('gps_untrusted', self.service.position._warnings)
        self.tx.sent.clear()
        self.observe()
        self.assertNotIn('gps_untrusted', self.service.position._warnings)
        self.assertNotIn('gps_untrusted', self.service.audio)
        for second in range(1, 125):
            self.clock.advance(1)
            if second % 18 == 0:
                self.sa()
            self.observe()
        self.assertEqual(self.of_type('b-t-f'), [])
        self.assertFalse(self.service.audio)
        self.observe(True, 'GNSS_SUSPECTED')
        self.assertIn('gps_untrusted', self.service.position._warnings)
        self.assertIn('gps_untrusted', self.service.audio)

    def test_present_no_fix_debounce_and_sixty_second_audio_reminders(self):
        self.sa()
        self.observe(True)
        self.clock.advance(2.99)
        self.observe(True)
        self.assertFalse(self.service.audio)
        self.clock.advance(.01)
        self.observe(True)
        first = self.service.audio['gps_untrusted']['id']
        for elapsed in (29, 30):
            self.clock.advance(elapsed)
            self.sa()
            self.observe(True)
            self.assertEqual(self.service.audio['gps_untrusted']['id'], first)
        self.clock.advance(1)
        self.observe(True)
        self.assertNotEqual(self.service.audio['gps_untrusted']['id'], first)
        self.assertEqual(phone.WARNING_READ_TIMEOUT_S, 60)

    def test_receiver_hotplug_from_silent_absence_starts_three_second_debounce(self):
        self.sa()
        self.observe()
        self.clock.advance(60)
        self.sa()
        self.observe(True)
        self.clock.advance(2.99)
        self.observe(True)
        self.assertFalse(self.service.audio)
        self.clock.advance(.01)
        self.observe(True)
        self.assertIn('gps_untrusted', self.service.audio)

    def test_spoken_reminder_floor_applies_even_to_short_configured_timeout(self):
        self.service.position.warning_read_timeout_s = 1
        self.sa()
        self.tick(FIX, state='GNSS_SUSPECTED')
        first = self.service.audio['gps_untrusted']['id']
        self.clock.advance(29.99)
        self.tick(FIX, state='GNSS_SUSPECTED')
        self.assertEqual(self.service.audio['gps_untrusted']['id'], first)
        self.clock.advance(.01)
        self.tick(FIX, state='GNSS_SUSPECTED')
        self.assertNotEqual(self.service.audio['gps_untrusted']['id'], first)
        self.assertEqual(phone.MIN_SPOKEN_REMINDER_S, 30)

    def test_gps_reader_distinguishes_explicit_absence_from_failure(self):
        fixture = base.InputTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for devices, expected in (([], False), (['/dev/gps'], True)):
            fixture.gps(devices=devices, sample=None, has_fix=False)
            gps, _ = fixture.inputs.read(fixture.clock.now(), fixture.memory)
            self.assertIs(gps.receiver_present, expected)
        (fixture.run / 'gps_status.json').unlink()
        self.assertIsNone(fixture.inputs.read(fixture.clock.now(), fixture.memory)[0].receiver_present)

    def test_mesh_and_xml_gps_fallback_expires_and_never_feeds_4349(self):
        for wire in (0, 1):
            self.setUp()
            packet = sa_fixture(source='GPS', how='m-g', stamp=self.phone_time())[wire]
            parsed = tak.decode_mesh_sa(packet)
            self.service.ingest(parsed, base.IP, self.proof, self.clock.now())
            status = self.observe()
            self.assertEqual(status['selected']['lat'], 40)
            self.assertEqual(status['selected_source'], 'phone GPS (SYNTHETIC)')
            self.assertFalse(status['position_trusted'])
            self.assertIsNone(self.service.rewrite_fix)
            self.assertFalse(any(dest[1] == 4349 for _, dest in self.tx.sent))
            self.clock.advance(74.9)
            self.assertIsNotNone(self.observe()['selected'])
            self.clock.advance(.1)
            self.assertIsNone(self.observe()['selected'])
            self.assertFalse(self.service.phone_gps)

    def test_disconnect_beacon_uses_only_remaining_sa_freshness(self):
        self.sa('gps')
        self.observe()
        self.clock.advance(10)
        status = self.observe(proof=False)
        self.assertTrue(status['selected_source'].startswith('phone GPS ('))
        beacon = self.tx.beacons[-1]
        self.assertEqual(beacon.get('type'), cot.BEACON_TYPE)
        self.assertEqual(beacon.find('detail/precisionlocation').get('geopointsrc'), 'MANET:phone-gps')
        self.assertLessEqual(cot._parse_time(beacon.get('stale'), 'stale'), base.BASE + timedelta(seconds=75))
        self.clock.advance(65)
        self.assertIsNone(self.observe(proof=False)['selected'])
        self.assertEqual(self.tx.beacons[-1].get('type'), 't-x-d-d')

    def test_sa_stale_shorter_than_presence_cap_withdraws_fallback(self):
        packet, _ = sa_fixture(source='GPS', stamp=self.phone_time())
        parsed = atak.replace(tak.decode_mesh_sa(packet), stale=self.phone_time() + timedelta(seconds=5))
        self.service.ingest(parsed, base.IP, self.proof, self.clock.now())
        self.assertIsNotNone(self.observe()['selected'])
        self.clock.advance(5)
        self.assertIsNone(self.observe()['selected'])

    def test_restart_clears_obsolete_phone_configuration_warning(self):
        self.sa()
        self.service.position._warn('phone_gps', 'obsolete', 'obsolete', web=True)
        self.service.local_actions(self.clock.now())
        self.restart()
        self.assertNotIn('phone_gps', self.service.position._warnings)
        self.assertNotIn('phone_gps', self.service.banners)
        self.assertNotIn('phone_gps', self.service.audio)

    def test_phone_gps_cannot_override_manual_or_radio_and_does_not_survive_restart(self):
        self.sa('gps', (40, -105))
        self.assertEqual(self.tick(FIX)['selected']['lat'], FIX.lat)
        self.clock.advance(1)
        self.send_point((40.0001, -105))
        self.assertEqual(self.observe()['selected']['source'], 'manual')
        self.restart()
        self.assertEqual(self.service.phone_gps, {})

    def test_stale_replay_external_echo_and_guest_cannot_supply_controller_gps(self):
        self.sa('gps')
        deadline = self.observe()['selected_deadline']
        self.clock.advance(20)
        replay = self.packets.sa('gps', base.BASE)
        self.assertNotEqual(self.service.ingest(replay, base.IP, self.proof, self.clock.now()), 'accepted_presence')
        self.assertEqual(self.observe()['selected_deadline'], deadline)
        self.sa('echo')
        self.assertIsNone(self.observe()['selected'])
        other_ip, other_mac = '192.0.2.3', '02:00:00:00:00:03'
        self.proof = atak.Proof(base.LOCAL, {base.IP: base.MAC, other_ip: other_mac}, 'eud',
                                ports={base.IP: 'wired', other_ip: 'wifi'})
        other, _ = sa_fixture(address=other_ip, uid='guest', source='GPS', stamp=self.phone_time())
        self.service.ingest(tak.decode_mesh_sa(other), other_ip, self.proof, self.clock.now())
        self.assertIsNone(self.observe()['selected'])
        self.service.choose_controller(self.clock.now(), self.proof, manual_uid='guest')
        self.assertEqual(self.observe()['selected']['phone_uid'], 'guest')

    def test_fallback_does_not_suppress_present_receiver_no_fix_warning(self):
        self.sa('gps')
        self.observe(True)
        self.clock.advance(3)
        status = self.observe(True)
        self.assertIsNotNone(status['selected'])
        self.assertIn('gps_untrusted', self.service.audio)


class HeldPointReleaseTests(base.Harness):
    observe = ReceiverAndFallbackTests.observe

    def hold(self, gps=True):
        self.sa('gps' if gps else 'none', (40, -105) if gps else None)
        self.clock.advance(1)
        self.assertEqual(self.send_point((40.0001, -105), 'held'), 'accepted:newest_location')
        self.observe()

    def gps(self, north=0, seconds=1):
        self.clock.advance(seconds)
        self.assertEqual(self.sa('gps', (40 + north / 111195, -105)), 'accepted_presence')
        return self.observe()

    def test_stationary_offset_holds_indefinitely_and_anchor_is_acceptance_fix(self):
        self.hold()
        self.assertEqual(self.service.manual_anchor['point'], (40, -105))
        for _ in range(100):
            self.assertEqual(self.gps(29, 60)['selected_source'], 'manual')
        self.assertEqual(self.service.manual_anchor['point'], (40, -105))
        self.assertIsNone(self.service.manual_motion)

    def test_delayed_first_gps_anchors_and_no_gps_holds(self):
        self.hold(False)
        self.assertIsNone(self.service.manual_anchor['point'])
        self.clock.advance(3600)
        self.assertEqual(self.observe()['selected_source'], 'manual')
        self.gps(80)
        self.assertAlmostEqual(self.service.manual_anchor['point'][0], 40 + 80 / 111195)
        self.assertEqual(self.gps(90, 20)['selected_source'], 'manual')

    def test_hours_old_unanchored_point_releases_on_first_far_fix_without_changing_distrust(self):
        self.hold(False)
        self.service.position._distrusted = True
        self.clock.advance(4.7 * 3600)
        with patch.object(atak, 'log') as journal:
            status = self.gps(300)
        self.assertTrue(any('reason=offset' in str(call) for call in journal.call_args_list))
        self.assertIsNone(self.service.position._manual)
        self.assertTrue(status['selected_source'].startswith('phone GPS'))
        self.assertTrue(self.service.position._distrusted)
        self.assertFalse(status['position_trusted'])
        self.assertFalse(self.service.audio)

    def test_continuous_movement_distinct_samples_retires_and_notifies_once_without_audio(self):
        self.hold()
        self.tx.sent.clear()
        self.gps(50)
        self.gps(50, 14.9)
        self.assertIsNotNone(self.service.position._manual)
        with patch.object(atak, 'log') as journal:
            result = self.gps(50, .1)
        self.assertTrue(result['selected_source'].startswith('phone GPS'))
        self.assertIsNone(self.service.rewrite_fix)
        self.assertIsNone(self.service.position._manual)
        self.assertIsNone(self.service.manual_anchor)
        self.assertIn('held', self.service.retire)
        self.assertEqual(len([c for c in journal.call_args_list if 'held point released' in c.args[0]]), 1)
        self.assertEqual(len(self.of_type('b-t-f')), 1)
        self.assertIn('You moved', self.of_type('b-t-f')[0].find('detail/remarks').text)
        self.assertFalse(self.service.audio)
        self.gps(90, 20)
        self.assertEqual(len(self.of_type('b-t-f')), 1)

    def test_single_jump_ticks_return_inside_and_stale_gap_reset_motion(self):
        self.hold()
        self.gps(50)
        self.clock.advance(16)
        self.observe()  # One sample cannot establish movement.
        self.assertIsNotNone(self.service.position._manual)
        self.gps(0)
        self.gps(50)
        self.gps(50, 14)
        self.assertIsNotNone(self.service.position._manual)
        self.gps(50, 76)  # A gap beyond SA freshness starts another interval.
        self.assertIsNotNone(self.service.position._manual)
        self.gps(50, 15)
        self.assertIsNone(self.service.position._manual)

    def test_upper_limit_immediately_clears_rewrite_before_trigger_frame_is_forwarded(self):
        self.hold()
        self.gps(210)  # About 199 m from the held point.
        self.assertIsNotNone(self.service.position._manual)
        self.clock.advance(1)
        self.sa('gps', (40 + 212 / 111195, -105))
        self.assertIsNone(self.service.position._manual)
        self.assertIsNone(self.service.rewrite_fix)

    def test_anchor_survives_restart_but_movement_interval_and_gps_do_not(self):
        self.hold()
        self.gps(50)
        anchor = dict(self.service.manual_anchor)
        self.clock.advance(14)
        self.restart()
        self.assertEqual(self.service.manual_anchor, anchor)
        self.assertIsNone(self.service.manual_motion)
        self.gps(50)
        self.assertIsNotNone(self.service.position._manual)
        self.gps(50, 15)
        self.assertIsNone(self.service.position._manual)

    def test_path_loss_and_trusted_own_fix_reset_motion(self):
        self.hold()
        self.gps(50)
        self.clock.advance(10)
        self.observe(proof=False)
        self.assertIsNone(self.service.manual_motion)
        # A nearby trusted own fix keeps the existing confirmation behavior.
        self.tick(cot.Fix(40.0001, -105, 'gnss'))
        self.clock.advance(1)
        self.sa('gps', (41, -105))
        self.assertIsNotNone(self.service.position._manual)
        self.assertIsNone(self.service.manual_motion)

    def test_retirement_backpressure_holds_geometry_until_space_available(self):
        self.hold()
        selector = self.service.position
        selector._retirements = {str(i): self.packets.uid for i in range(selector.max_pending_retirements)}
        self.clock.advance(1)
        self.sa('gps', (41, -105))
        self.assertIsNotNone(selector._manual)
        selector._retirements.clear()
        self.gps(300)
        self.assertIsNone(selector._manual)

    def test_boot_rebases_age_but_preserves_geographic_anchor_and_requires_new_motion(self):
        self.hold()
        self.gps(50)
        identity, anchor = self.service.manual_id(), dict(self.service.manual_anchor)
        now = atak.Now(5, 7, 5, base.BASE - timedelta(days=10))
        self.restart('boot-2', now)
        self.assertEqual(self.service.manual_id(), identity)
        self.assertEqual(self.service.manual_anchor, anchor)
        self.assertIsNone(self.service.manual_motion)
        self.assertFalse(self.service.phone_gps)
        for elapsed, held in ((0, True), (14, True), (15, False)):
            current = atak.replace(now, mono=5 + elapsed, boot=7 + elapsed)
            stamp = base.BASE + timedelta(seconds=50 + elapsed)
            packet = self.packets.sa('gps', stamp, (40 + 50 / 111195, -105))
            self.service.ingest(packet, base.IP, self.proof, current)
            self.service.tick(current, self.proof,
                              phone.RadioGPS(None, current.mono, receiver_present=False), {})
            self.assertEqual(self.service.position._manual is not None, held)

    def test_unproven_delete_receive_path_cannot_release(self):
        self.hold()
        self.clock.advance(1)
        packet = cot.retire_marker('held', self.packets.uid, self.phone_time())
        self.service.ingest(packet, base.IP, self.proof, self.clock.now())
        self.assertIsNotNone(self.service.position._manual)

    def test_guest_gps_does_not_release_and_controller_change_reanchors(self):
        self.hold()
        self.gps(50)
        address, mac = '192.0.2.3', '02:00:00:00:00:03'
        self.proof = atak.Proof(base.LOCAL, {base.IP: base.MAC, address: mac}, 'eud',
                               ports={base.IP: 'wired', address: 'wifi'})
        packet = sa_fixture(address=address, uid='guest', source='GPS', stamp=self.phone_time())[0]
        self.service.ingest(tak.decode_mesh_sa(packet), address, self.proof, self.clock.now())
        self.assertIsNotNone(self.service.position._manual)
        self.service.choose_controller(self.clock.now(), self.proof, manual_uid='guest')
        self.observe()
        self.assertEqual(self.service.manual_anchor['uid'], 'guest')
        self.assertIsNone(self.service.manual_motion)


class HeldMarkerRefreshIntervalTest(unittest.TestCase):
    def test_shipped_refresh_is_one_minute(self):
        # Resend the held marker every minute.
        self.assertEqual(atak.HELD_MARKER_REFRESH_S, 60)


class HeldMarkerRefreshTests(base.Harness):
    observe = ReceiverAndFallbackTests.observe

    def setUp(self):
        super().setUp()
        # Schedules below are written for a 300 s interval. The behaviour under
        # test does not depend on its length. The shipped value is checked below.
        interval = patch.object(atak, 'HELD_MARKER_REFRESH_S', 300)
        interval.start()
        self.addCleanup(interval.stop)
        self.raw = []
        send = self.tx.send
        def record(data, dest):
            self.raw.append((data, dest))
            return send(data, dest)
        self.tx.send = record

    def hold(self, uid='held'):
        self.sa()
        self.clock.advance(1)
        packet = self.packets.point(self.phone_time(), (40, -105), uid)
        root = ET.fromstring(packet)
        ET.SubElement(root.find('detail'), 'usericon', iconsetpath='custom/icon.png')
        ET.SubElement(root.find('detail'), 'color', argb='-65536')
        ET.SubElement(root.find('detail'), 'remarks').text = 'Keep this label & content'
        # The fixture already has remarks, so retain exactly one known element.
        remarks = root.findall('detail/remarks')
        for old in remarks[:-1]:
            root.find('detail').remove(old)
        packet = ET.tostring(root)
        self.assertTrue(self.service.ingest(packet, base.IP, self.proof, self.clock.now()).startswith('accepted:'))
        self.observe()
        return packet

    def advance_present(self, seconds):
        while seconds > 0:
            step = min(seconds, 60)
            self.clock.advance(step)
            self.sa()
            self.observe()
            seconds -= step

    def notices(self):
        return [r for r in self.of_type('b-t-f') if 'The radio is holding your point.' in r.find('detail/remarks').text]

    def marker_attempts(self, uid='held'):
        return [(packet, dest) for packet, dest in self.raw if ET.fromstring(packet).get('uid') == uid]

    def test_acceptance_explained_once_and_exact_marker_repeated_every_five_minutes_without_chat(self):
        packet = self.hold()
        self.assertEqual(len(self.notices()), 1)
        text = self.notices()[0].find('detail/remarks').text
        for phrase in ('Deleting it in ATAK does not release it', 'open manet.local in your browser',
                       'Use phone GPS again', 'move on'):
            self.assertIn(phrase, text)
        self.assertNotIn('http://', text)
        chats = len(self.of_type('b-t-f'))
        self.advance_present(atak.HELD_MARKER_REFRESH_S - .01)
        self.assertEqual(self.marker_attempts(), [])
        self.advance_present(.01)
        self.assertEqual(self.marker_attempts(), [(packet, (base.IP, 4242))])
        self.advance_present(atak.HELD_MARKER_REFRESH_S)
        self.assertEqual(self.marker_attempts(), [(packet, (base.IP, 4242))] * 2)
        self.assertEqual(len(self.of_type('b-t-f')), chats)
        self.assertFalse(self.service.audio)

    def test_user_and_drag_notify_without_creating_a_marker_or_refreshing_phone_self_sa(self):
        for mode in ('user', 'drag'):
            with self.subTest(mode=mode):
                self.setUp()
                self.sa()
                self.clock.advance(1)
                self.assertEqual(self.sa(mode, (40, -105)), 'accepted_manual')
                self.observe()
                self.assertEqual(len(self.notices()), 1)
                self.assertIsNone(self.service.held_marker)
                self.advance_present(301)
                self.assertEqual(len(self.notices()), 1)
                self.assertFalse(self.marker_attempts(self.packets.uid))

    def test_duplicate_restart_and_reanchor_do_not_repeat_chat_or_reset_refresh_due(self):
        packet = self.hold()
        due = self.service.held_marker['due']
        self.clock.advance(1)
        self.assertTrue(self.service.ingest(packet, base.IP, self.proof, self.clock.now()).startswith('duplicate:'))
        self.observe()
        self.restart()
        self.service.manual_anchor = None
        self.service.anchor_manual(self.clock.now())
        self.observe()
        self.assertEqual(self.service.held_marker['due'], due)
        self.advance_present(299)
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(self.marker_attempts(), [(packet, (base.IP, 4242))])

    def test_reboot_preserves_original_bytes_waits_for_presence_and_does_not_announce(self):
        packet = self.hold()
        now = atak.Now(5, 7, 5, base.BASE - timedelta(days=10))
        self.restart('boot-2', now)
        self.assertEqual(bytes.fromhex(''.join(self.service.held_marker['xml'])), packet)
        for elapsed in (300, 301):
            current = atak.replace(now, mono=5 + elapsed, boot=7 + elapsed)
            if elapsed == 301:
                self.service.ingest(self.packets.sa('none', base.BASE + timedelta(seconds=elapsed)),
                                    base.IP, self.proof, current)
            self.service.tick(current, self.proof, phone.RadioGPS(None, current.mono, receiver_present=False), {})
            self.assertEqual(len(self.marker_attempts()), elapsed - 300)
        self.assertEqual(len(self.notices()), 1)

    def test_release_at_refresh_deadline_and_retirement_replay_hold_expiry_never_resurrect(self):
        self.hold()
        self.advance_present(299)
        self.clock.advance(1)
        self.sa()
        self.assertTrue(self.service.release_manual(self.clock.now(), self.proof, moved=False))
        self.observe()
        self.advance_present(600)
        self.restart()
        self.advance_present(301)
        self.assertFalse(self.marker_attempts())
        self.assertIsNone(self.service.held_marker)
        self.assertTrue(any(r.find('detail/link').get('uid') == 'held' for r in self.of_type('t-x-d-d')))

    def test_gps_confirmation_retires_before_due_refresh_and_no_false_holding_chat(self):
        self.sa()
        self.send_point((40, -105), 'held')
        self.tick(cot.Fix(40, -105, 'gnss'))
        self.assertFalse(self.notices())
        self.assertIsNone(self.service.held_marker)
        self.clock.advance(301)
        self.sa()
        self.observe()
        self.assertFalse(self.marker_attempts())

    def test_replacement_and_newer_edit_of_same_uid_replace_retained_content(self):
        first = self.hold()
        self.advance_present(299)
        self.clock.advance(1)
        packet = self.packets.point(self.phone_time(), (40.0001, -105), 'held')
        self.service.ingest(packet, base.IP, self.proof, self.clock.now())
        self.observe()
        self.assertFalse(self.marker_attempts())
        self.advance_present(300)
        self.assertEqual(self.marker_attempts(), [(packet, (base.IP, 4242))])
        self.assertNotEqual(first, packet)
        self.assertEqual(len(self.notices()), 2)
        self.clock.advance(1)
        self.send_point((40.0002, -105), 'replacement')
        self.observe()
        self.advance_present(300)
        self.assertEqual(len(self.marker_attempts()), 1)
        self.assertEqual(len(self.marker_attempts('replacement')), 1)

    def test_pending_retirement_wins_even_if_delivery_queue_is_full(self):
        self.hold()
        self.advance_present(299)
        self.service.position._retirements['held'] = self.packets.uid
        self.service.retire = {str(i): {'id':str(i), 'attempts':5, 'successes':0, 'due':0,
                                      'done_until':None, 'phone_uid':self.packets.uid}
                               for i in range(atak.MAX_RETIRE)}
        self.advance_present(1)
        self.assertFalse(self.marker_attempts())
        self.assertIsNone(self.service.held_marker)

    def test_failed_notice_and_refresh_have_no_fast_retry_and_schedule_is_durable_before_send(self):
        self.tx.fail_ports.add(4242)
        packet = self.hold()
        self.assertFalse(self.notices())
        self.tx.fail_ports.clear()
        self.advance_present(299)
        self.assertFalse(self.notices())
        self.tx.fail_ports.add(4242)
        self.advance_present(1)
        self.assertEqual(len(self.marker_attempts()), 1)
        self.restart()
        self.tx.fail_ports.clear()
        self.advance_present(299)
        self.assertEqual(len(self.marker_attempts()), 1)
        self.advance_present(1)
        self.assertEqual(self.marker_attempts(), [(packet, (base.IP, 4242))] * 2)

    def test_path_loss_suppresses_refresh_until_controller_returns(self):
        self.hold()
        self.advance_present(299)
        self.clock.advance(1)
        self.observe(proof=False)
        self.assertFalse(self.marker_attempts())
        self.observe()
        self.assertEqual(len(self.marker_attempts()), 1)

    def test_large_accepted_marker_persists_exactly_and_legacy_checkpoint_has_no_invented_payload(self):
        self.sa()
        root = ET.fromstring(self.packets.point(self.phone_time(), (40, -105), 'large'))
        for _ in range(12):
            ET.SubElement(root.find('detail'), 'extension').text = 'x' * 1024
        packet = ET.tostring(root)
        self.assertTrue(self.service.ingest(packet, base.IP, self.proof, self.clock.now()).startswith('accepted:'))
        self.observe()
        self.restart()
        self.advance_present(300)
        self.assertEqual(self.marker_attempts('large'), [(packet, (base.IP, 4242))])
        state = atak.decode(self.store.data['state'])
        del state['held_marker']
        self.store.data['state'] = atak.encode(state)
        self.store.data['sha256'] = atak.state_digest({k:v for k,v in self.store.data.items() if k != 'sha256'})
        self.restart()
        self.assertIsNotNone(self.service.position._manual)
        self.assertIsNone(self.service.held_marker)
        self.advance_present(300)
        self.assertEqual(len(self.marker_attempts('large')), 1)
        self.assertEqual(len(self.notices()), 1)

    def test_new_controller_receives_refresh_without_new_acceptance_chat(self):
        packet = self.hold()
        other_ip, other_mac = '192.0.2.3', '02:00:00:00:00:03'
        self.proof = atak.Proof(base.LOCAL, {base.IP:base.MAC, other_ip:other_mac}, 'eud',
                               ports={base.IP:'wired', other_ip:'wifi'})
        other = base.sim.Packets()
        other.uid = 'guest'
        self.service.ingest(other.sa('none', self.phone_time()), other_ip, self.proof, self.clock.now())
        self.service.choose_controller(self.clock.now(), self.proof, manual_uid=other.uid)
        self.observe()
        for _ in range(5):
            self.clock.advance(60)
            self.service.ingest(other.sa('none', self.phone_time()), other_ip, self.proof, self.clock.now())
            self.observe()
        self.assertEqual(self.marker_attempts(), [(packet, (other_ip, 4242))])
        self.assertEqual(len(self.notices()), 1)


class PositionWireTests(unittest.TestCase):
    def fields(self, data):
        result, offset = {}, 0
        while offset < len(data):
            tag, offset = tak._tak_varint(data, offset)
            if tag & 7 == 0:
                result[tag >> 3], offset = tak._tak_varint(data, offset)
            else:
                if tag & 7 == 2:
                    size, offset = tak._tak_varint(data, offset)
                else:
                    size = 8 if tag & 7 == 1 else 4
                result[tag >> 3] = data[offset:offset + size]
                offset += size
        return result

    def test_v1_keeps_all_nonposition_fields_and_unknown_bytes(self):
        packet, _ = sa_fixture(source='GPS', how='m-g', contact_xml=True,
            xml_detail='<__group name="Blue" role="Team Member"/><status battery="42"/>'
                       '<takv version="x"/><track speed="2.5" course="270"/><unknown a="b"/>')
        message = self.fields(packet[3:])
        event = self.fields(message[2])
        # Typed group/status/takv/track plus unknown detail/event/envelope fields.
        extra = b''.join(field(n, b'\x08\x01') for n in (3, 5, 6, 7, 100))
        event_bytes = message[2].replace(field(15, event[15]), field(15, event[15] + extra)) + field(100, b'opaque-event')
        packet = b'\xbf\x01\xbf' + field(1, b'control') + field(2, event_bytes) + field(100, b'opaque-envelope')
        result = tak.rewrite_mesh_sa(packet, FIX, 'MANET:mesh-test')
        self.assertNotEqual(result, packet)
        before, after = self.fields(packet[3:]), self.fields(result[3:])
        self.assertEqual({k: v for k, v in before.items() if k != 2}, {k: v for k, v in after.items() if k != 2})
        before, after = self.fields(before[2]), self.fields(after[2])
        self.assertEqual({k: v for k, v in before.items() if k not in range(10, 16)},
                         {k: v for k, v in after.items() if k not in range(10, 16)})
        original_detail, updated_detail = self.fields(before[15]), self.fields(after[15])
        self.assertEqual({k: v for k, v in original_detail.items() if k != 4},
                         {k: v for k, v in updated_detail.items() if k != 4})
        decoded = tak.decode_mesh_sa(result)
        self.assertEqual((decoded.lat, decoded.lon, decoded.hae, decoded.ce, decoded.le), (41.25, -106.5, 1800, 4, 6))
        self.assertEqual(decoded.geopointsrc, 'MANET:mesh-test')
        self.assertIn(extra, result)

    def test_unknown_precision_fields_and_xml_precedence_are_preserved(self):
        for xml in ('', '<precisionlocation geopointsrc="GPS" altsrc="GPS" odd="yes"/>'):
            packet, _ = sa_fixture(source='GPS', xml_detail=xml)
            event = self.fields(self.fields(packet[3:])[2])
            detail = self.fields(event[15])
            precision = detail[4] + field(100, b'unknown-precision')
            altered = self.fields(packet[3:])[2].replace(field(15, event[15]), field(15,
                      event[15].replace(field(4, detail[4]), field(4, precision))))
            result = tak.rewrite_mesh_sa(b'\xbf\x01\xbf' + field(2, altered), FIX, 'MANET:node')
            self.assertEqual(tak.decode_mesh_sa(result).geopointsrc, 'MANET:node')
            self.assertIn(field(100, b'unknown-precision'), result)
            if xml:
                self.assertIn(b'odd="yes"', result)

    def test_xml_nonposition_bytes_quotes_comments_unknowns_and_order_unchanged(self):
        _, packet = sa_fixture(source='GPS', how='m-g', xml_detail='<unknown text="x &gt; y"/><!--keep-->')
        packet = packet.replace(b'<point ', b'<point unknown="keep" ').replace(b'lat="40.0"', b"lat = '40.0'")
        packet = packet.replace(b'</detail>', b'<!--keep--></detail>')
        packet = packet.replace(b'<point ', '<point πlat=\'fake lat="99"\' '.encode())
        result = tak.rewrite_mesh_sa(packet, FIX, 'MANET:n<&"\'')
        self.assertNotEqual(result, packet)
        before, after = ET.fromstring(packet), ET.fromstring(result)
        for root in (before, after):
            for key in ('lat', 'lon', 'hae', 'ce', 'le'):
                del root.find('point').attrib[key]
            for key in ('geopointsrc', 'altsrc'):
                del root.find('detail/precisionlocation').attrib[key]
        self.assertEqual(ET.tostring(before), ET.tostring(after))
        self.assertIn(b"lat = '41.25'", result)
        for untouched in (b'<!--keep-->', b'<unknown text="x &gt; y"', b'unknown="keep"', 'πlat=\'fake lat="99"\''.encode()):
            self.assertIn(untouched, result)

    def test_missing_precision_or_zero_scalars_are_added(self):
        for packet in sa_fixture(point=(0, -105, 0)):
            if packet.startswith(b'<'):
                root = ET.fromstring(packet)
                root.find('detail').remove(root.find('detail/precisionlocation'))
                packet = ET.tostring(root)
            result = tak.rewrite_mesh_sa(packet, FIX, 'MANET:node')
            self.assertEqual(tak.decode_mesh_sa(result).lat, FIX.lat)
            self.assertEqual(tak.decode_mesh_sa(result).altsrc, 'MANET:node')

    def test_malformed_unsupported_and_oversize_return_original(self):
        good, xml = sa_fixture()
        for packet in (b'<event', b'bad', good[:-1], good + field(2, b'bad'),
                       b'x' * 8193, xml.replace(b'a-f-G-U-C', b'b-t-f'),
                       good + b'\x13', good.replace(b'\xbf\x01\xbf', b'\xbf\x02\xbf', 1)):
            self.assertEqual(tak.rewrite_mesh_sa(packet, FIX, 'MANET:node'), packet)
        self.assertEqual(tak.rewrite_mesh_sa(good, atak.replace(FIX, phone_uid='phone'), 'MANET:node'), good)
        other_encoding = b'<?xml version="1.0" encoding="iso-8859-1"?>' + xml
        self.assertEqual(tak.rewrite_mesh_sa(other_encoding, FIX, 'MANET:node'), other_encoding)

    def test_frame_preserves_sender_headers_options_and_recomputes_checksums(self):
        for packet in sa_fixture(source='GPS'):
            frame = base.mesh_frame(packet, options=b'\x01\x01\x01\x01')
            replacement = tak.rewrite_mesh_sa(packet, FIX, 'MANET:node')
            result = atak.replace_mesh_payload(frame, replacement)
            self.assertEqual(atak.mesh_sa_frame(result), (base.IP, base.MAC, replacement))
            self.assertEqual(frame[:16], result[:16])
            self.assertEqual(frame[18:24], result[18:24])
            self.assertEqual(frame[26:42], result[26:42])


class RewriteNetworkTests(base.Harness):
    def setUp(self):
        super().setUp()
        self.proof = atak.Proof(base.LOCAL, {base.IP: base.MAC}, 'generation',
            interfaces={base.IP: 'end0'}, local_ports={'end0': ('wired', '3')}, mesh_path=('2', '5'))
        self.network = atak.Network(Mock(side_effect=base.FakeSocket))
        self.addCleanup(self.network.close)
        self.network.configure(self.proof)
        self.network.sync_mesh_ports(self.proof.local_ports)
        self.network.events = Mock()
        self.network.events.tak_members.return_value = {}
        self.network.events.tak_pending = None
        self.receive()
        self.tick(FIX)
        self.network.refresh_rewrite(self.service, self.clock.now())

    def receive(self, packet=None, address=base.IP, mac=base.MAC, iface='end0'):
        packet = packet if packet is not None else sa_fixture(source='GPS', stamp=self.phone_time())[0]
        frame = base.mesh_frame(packet, address=address, mac=mac)
        sock = self.network.mesh_sockets[iface][1]
        sock.incoming.append((frame, [], 0, (iface, 0x800, 2, 1, bytes.fromhex(mac.replace(':', '')))))
        self.network.handle([sock], self.clock.now(), self.service)
        return frame

    def test_only_admitted_active_members_rewrite_to_bat0_no_local_copy(self):
        self.clock.advance(18)
        self.tick(FIX)
        self.receive()
        self.assertEqual(len(self.network.raw_sender.sent), 1)
        frame, destination = self.network.raw_sender.sent[0]
        self.assertEqual(destination, ('bat0', 0))
        self.assertEqual(tak.decode_mesh_sa(atak.mesh_sa_frame(frame)[2]).lat, FIX.lat)
        self.assertTrue(all(not sock.sent for _, sock in self.network.mesh_sockets.values()))
        self.receive(address='192.0.2.99', mac='02:00:00:00:00:99')
        self.assertEqual(len(self.network.raw_sender.sent), 1)

    def test_malformed_budget_rejected_identity_and_stale_fix_relay_original(self):
        self.clock.advance(1)
        for packet in (b'<bad', b'\xbf\x02\xbfunknown', sa_fixture(uid='other', stamp=self.phone_time())[0]):
            frame = self.receive(packet)
            self.assertEqual(self.network.raw_sender.sent[-1], (frame, ('bat0', 0)))
        self.network.budget.take(self.clock.now().mono)
        self.network.budget.events = 0
        frame = self.receive()
        self.assertEqual(self.network.raw_sender.sent[-1][0], frame)
        self.clock.advance(phone.FIX_TTL_S + 1)
        frame = self.receive()
        self.assertEqual(self.network.raw_sender.sent[-1][0], frame)

    def test_guest_on_wifi_is_rewritten_without_receiving_a_local_copy(self):
        address, mac = '192.0.2.3', '02:00:00:00:00:03'
        self.proof = atak.replace(self.proof, peers={base.IP: base.MAC, address: mac},
            ports={base.IP: 'wired', address: 'wifi'}, interfaces={base.IP: 'end0', address: 'wlan7'},
            local_ports=dict(self.proof.local_ports, wlan7=('wifi', '7')))
        self.network.configure(self.proof)
        self.network.sync_mesh_ports(self.proof.local_ports)
        self.clock.advance(1)
        packet = sa_fixture(address=address, uid='guest', source='GPS', stamp=self.phone_time())[1]
        self.receive(packet, address, mac, 'wlan7')
        self.tick(FIX)
        self.network.refresh_rewrite(self.service, self.clock.now())
        self.assertEqual(len(self.network.rewrite_members), 2)
        self.clock.advance(1)
        packet = sa_fixture(address=address, uid='guest', source='GPS', stamp=self.phone_time())[1]
        self.receive(packet, address, mac, 'wlan7')
        frame, destination = self.network.raw_sender.sent[-1]
        parsed = tak.decode_mesh_sa(atak.mesh_sa_frame(frame)[2])
        self.assertEqual((parsed.uid, parsed.lat), ('guest', FIX.lat))
        self.assertEqual(self.service.position.pinned_uid, 'synthetic-phone')
        self.assertEqual(destination, ('bat0', 0))
        self.assertTrue(all(not sock.sent for _, sock in self.network.mesh_sockets.values()))

    def test_manual_rewrites_under_untrusted_gps_then_no_fix_opens(self):
        self.clock.advance(1)
        self.service.position._manual = phone.PhoneFix(cot.Fix(40.0001, -105, 'manual'), 'synthetic-phone',
            'marker', self.phone_time(), self.clock.now().mono, self.clock.now().mono, None, None)
        self.tick(FIX, state='GNSS_SUSPECTED')
        self.network.refresh_rewrite(self.service, self.clock.now())
        self.receive()
        parsed = tak.decode_mesh_sa(atak.mesh_sa_frame(self.network.raw_sender.sent[-1][0])[2])
        self.assertEqual((parsed.lat, parsed.lon), (40.0001, -105))
        self.service.position._manual = None
        self.tick()
        self.network.refresh_rewrite(self.service, self.clock.now())
        self.assertEqual(self.network.rewrite_members, set())
        self.network.events.rewrite_members.assert_called_with(set())

    def test_refresh_stops_when_phone_gone_or_proof_lost_and_idle_has_no_work(self):
        self.clock.advance(76)
        self.tick(FIX)
        self.network.refresh_rewrite(self.service, self.clock.now())
        self.assertFalse(self.network.rewrite_members)
        self.network.events.reset_mock()
        for _ in range(60):
            self.clock.advance(1)
            self.network.refresh_rewrite(self.service, self.clock.now())
        self.network.events.rewrite_members.assert_not_called()

    def test_path_loss_flushes_and_never_associated_idle_has_no_rewrite_cost(self):
        self.network.configure(None)
        self.network.refresh_rewrite(self.service, self.clock.now())
        self.network.events.rewrite_members.assert_called_with(set())
        empty = atak.Service(self.config, 'boot-1', base.MemoryStore(), self.tx, self.clock.now())
        network = atak.Network(Mock(side_effect=base.FakeSocket))
        self.addCleanup(network.close)
        network.events = Mock()
        network.configure(self.proof)
        network.sync_mesh_ports(self.proof.local_ports)
        calls = network.factory.call_count
        for _ in range(60):
            now = self.clock.advance(1)
            empty.tick(now, self.proof, phone.RadioGPS(FIX, now.mono), {})
            network.refresh_rewrite(empty, now)
        self.assertEqual(network.factory.call_count, calls)
        self.assertIsNone(network.raw_sender)
        network.events.rewrite_members.assert_not_called()

    def test_rewrite_decline_sends_byte_identical_original_frame(self):
        self.clock.advance(1)
        with patch.object(tak, 'rewrite_mesh_sa', side_effect=lambda data, *args: data):
            frame = self.receive()
        self.assertEqual(self.network.raw_sender.sent[-1][0], frame)

    def test_capture_queue_drops_relay_original_and_open_native_forwarding(self):
        self.clock.advance(1)
        self.network.mesh_sockets['end0'][1].dropped = 1
        frame = self.receive()
        self.assertEqual(self.network.raw_sender.sent[-1][0], frame)
        self.network.refresh_rewrite(self.service, self.clock.now())
        self.assertFalse(self.network.rewrite_members)
        self.network.events.rewrite_members.assert_called_with(set())

    def test_rewrite_send_failure_retries_original_and_total_failure_stops_daemon(self):
        self.clock.advance(1)
        raw = self.network.raw_sender
        raw.sendto = Mock(side_effect=[OSError('MTU'), 1000])
        # Correct original length is required for success.
        packet = sa_fixture(source='GPS', stamp=self.phone_time())[0]
        frame = base.mesh_frame(packet)
        raw.sendto.side_effect = [OSError('MTU'), len(frame)]
        self.receive(packet)
        self.assertEqual(raw.sendto.call_args.args, (frame, ('bat0', 0)))
        raw.sendto.side_effect = OSError('link down')
        self.clock.advance(1)
        with self.assertRaises(atak.GuardFailure):
            self.receive()


class RewriteNetlinkTests(unittest.TestCase):
    def setUp(self):
        self.events = atak.NetworkEvents(Mock(side_effect=base.FakeSocket))
        self.addCleanup(self.events.close)

    def attrs(self, data):
        result = []
        while data:
            length, kind = struct.unpack_from('=HH', data)
            self.assertGreaterEqual(length, 4)
            result.append((kind & 0x3fff, data[4:length]))
            data = data[(length + 3) & ~3:]
        return result

    def transaction(self, members, now, state):
        self.events.rewrite_members(members)
        batch = self.events.sockets[1].sent[-1][0]
        kinds, staged = [], dict(state)
        while batch:
            size, kind, flags, seq, pid = struct.unpack_from('=IHHII', batch)
            kinds.append(kind)
            self.assertFalse(flags & 8)  # No requested echo, but multicast can arrive.
            body = batch[20:size]
            if kind == 0xa0e:
                staged.clear()
            elif kind == 0xa0c:
                attrs = dict(self.attrs(body))
                self.assertEqual(attrs[1], b'manet_atak\0')
                self.assertEqual(attrs[2], b'rewrite_phones\0')
                for _, element in self.attrs(attrs[3]):
                    fields = dict(self.attrs(element))
                    key = dict(self.attrs(fields[1]))[1]
                    self.assertEqual(len(key), 28)
                    identity = (key[:16].rstrip(b'\0').decode(), socket.inet_ntoa(key[24:28]), key[16:22].hex(':'))
                    timeout = struct.unpack('!Q', fields[4])[0] / 1000
                    self.assertEqual(timeout, 3)
                    staged[identity] = now + timeout
            if flags & 4:
                ack = struct.pack('=IHHIIi', 36, 2, 0, seq, 0, 0) + batch[:16]
                self.events.sockets[1].incoming.append((ack, [], 0, (0, 0)))
            batch = batch[(size + 3) & ~3:]
        self.assertEqual((kinds[0], kinds[-1]), (16, 17))
        self.assertFalse(self.events.changed())
        state.clear()
        state.update(staged)

    def test_actual_encoded_timeout_opens_without_daemon_and_refresh_extends(self):
        key, state = ('end0', base.IP, base.MAC), {}
        self.transaction({key}, 0, state)
        self.assertGreater(state[key], 2.9)
        self.assertFalse(3 < state[key])  # Dead/stopped daemon: no calls needed to expire.
        self.transaction({key}, 1, state)
        self.assertTrue(3 < state[key])
        self.assertFalse(4 < state[key])
        self.transaction(set(), 2, state)
        self.assertFalse(state)

    def test_unacknowledged_refresh_and_kernel_errors_fail_closed(self):
        self.events.rewrite_members({('end0', base.IP, base.MAC)})
        with self.assertRaisesRegex(OSError, 'acknowledgement'):
            self.events.rewrite_members(set())
        seq = min(self.events.rewrite_acks)
        ack = struct.pack('=IHHIIi', 36, 2, 0, seq, 0, -1) + b'\0' * 16
        self.events.sockets[1].incoming.append((ack, [], 0, (0, 0)))
        with self.assertRaises(OSError):
            self.events.changed()

    def test_external_policy_events_are_never_elided(self):
        state = {}
        self.transaction({('end0', base.IP, base.MAC)}, 0, state)
        self.events.sockets[1].incoming.append((struct.pack('=IHHII', 20, 0xa0c, 0, 0, 999) + b'\x07\0\0\0', [], 0, (0, 0)))
        self.assertTrue(self.events.changed())

    def test_policy_requires_timeout_and_exact_port_mac_ip_key(self):
        objects = atak.firewall_objects(('end0', 'wlan7'))
        rule = objects[-1]['rule']
        self.assertEqual((rule['family'], rule['chain']), ('bridge', 'forward'))
        self.assertIn(atak.match(atak.meta('oifname'), 'bat0'), rule['expr'])
        self.assertIn(atak.match(atak.meta('iifname'), {'set': ['end0', 'wlan7']}), rule['expr'])
        self.assertEqual(rule['expr'][-2]['match']['left'], {'concat': [atak.meta('iifname'),
            atak.payload('ether', 'saddr'), atak.payload('ip', 'saddr')]})
        self.assertTrue(atak.valid_firewall({'nftables': objects}, ('end0', 'wlan7')))
        for change in ({'timeout': 0}, {'timeout': 60}, {'flags': []}, {'type': 'ipv4_addr'}):
            bad = copy.deepcopy(objects)
            next(o['set'] for o in bad if 'set' in o).update(change)
            self.assertFalse(atak.valid_firewall({'nftables': bad}, ('end0', 'wlan7')))
        for timeout, valid in ((3, True), (0, False), (60, False), ('3', False)):
            dump = copy.deepcopy(objects)
            next(o['set'] for o in dump if 'set' in o)['elem'] = [
                {'elem': {'val': {'concat': ['end0', base.MAC, base.IP]}, 'timeout': timeout, 'expires': 1.5}}]
            self.assertEqual(atak.valid_firewall({'nftables': dump}, ('end0', 'wlan7')), valid)


if __name__ == '__main__':
    unittest.main()
