"""Tethered-phone association, freshness, provenance and manual observation age."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

from manet_cot import (Fix, PhoneSA, parse_marker, parse_self_sa, hidden_contact,
                       external_position, geochat_report, retire_marker, ChatReceipt,
                       PhoneChat, parse_phone_chat, parse_chat_receipt)
from manet_phone import PhonePosition, RadioGPS, PHONE_SA_HISTORY


SAMPLES = Path(__file__).resolve().parents[2] / "review-collab/atak-20261006/samples"
GPS = parse_self_sa((SAMPLES / "self-sa-gps.xml").read_bytes())
MANUAL = parse_self_sa((SAMPLES / "self-sa-manual.xml").read_bytes())
USER_SELECTED = parse_self_sa((SAMPLES / "self-sa-user-selected.xml").read_bytes())
NO_LOCATION = parse_self_sa((SAMPLES / "self-sa-no-location.xml").read_bytes())
PHONE_CHAT = parse_phone_chat((SAMPLES / "chat-from-phone.xml").read_bytes())
SENT = parse_marker((SAMPLES / "sent-point.xml").read_bytes())
BASE = datetime(2026, 10, 7, tzinfo=timezone.utc)


def utc(seconds=0):
    return BASE + timedelta(seconds=seconds)


def sa(source="gps", seconds=0, lifetime=75, **kwargs):
    template = GPS if source == "gps" else MANUAL
    return replace(template, source=source, time=utc(seconds), start=utc(seconds),
                   stale=utc(seconds + lifetime), **kwargs)


def feed(position, value=None, seconds=0, **kwargs):
    return position.feed(sa(seconds=seconds) if value is None else value,
                         100 + seconds, utc(seconds), from_ethernet=True, **kwargs)


def current(position, seconds=0, **kwargs):
    return position.select(100 + seconds, utc(seconds), **kwargs).location


def mark(uid="point-1", created=0, **kwargs):
    fields = dict(uid=uid, creator_time=utc(created), time=utc(created),
                  start=utc(created), stale=utc(created + 365 * 86400))
    fields.update(kwargs)
    return replace(SENT, **fields)


def send(position, value=None, seconds=0, from_ethernet=True):
    return position.feed_marker(mark(created=seconds) if value is None else value,
                                100 + seconds, utc(seconds), from_ethernet=from_ethernet)


class PhonePresenceTests(unittest.TestCase):
    def test_nonmanual_self_sa_sources_pin_but_never_supply_geography(self):
        for source in ("gps", "unknown", "external"):
            with self.subTest(source=source):
                position = PhonePosition()
                result = feed(position, sa(source))
                self.assertEqual(result.status, "accepted_presence")
                self.assertEqual(result.pinned_uid, GPS.uid)
                self.assertTrue(result.reappeared)
                selected = position.select(100, utc())
                self.assertTrue(selected.phone_present)
                self.assertIsNone(selected.fix)
                self.assertIsNone(selected.location)
                self.assertIsNone(position.get_fix(100, utc()))
                self.assertIsNone(position.select(100, utc(), radio_gps=RadioGPS(None, 100)).fix)

    def test_ingress_and_pin_are_not_inferred_from_ip_or_creator(self):
        position = PhonePosition()
        self.assertEqual(position.feed(sa(), 100, utc(), from_ethernet=False).status, "wrong_ingress")
        self.assertIsNone(position.pinned_uid)
        feed(position)
        other = sa(seconds=1, uid="OTHER-PHONE")
        self.assertEqual(feed(position, other, 1).status, "uid_mismatch")
        self.assertFalse(position.select(175, utc(75)).phone_present)
        self.assertEqual(feed(position, replace(other, time=utc(76)), 76).status, "uid_mismatch")
        self.assertEqual(position.pinned_uid, GPS.uid)

    def test_presence_age_stale_reappearance_and_no_redating(self):
        position = PhonePosition()
        self.assertTrue(feed(position).reappeared)
        self.assertFalse(feed(position, sa(seconds=58, lifetime=3600), 60).reappeared)
        self.assertTrue(position.select(232.99, utc(132.99)).phone_present)
        self.assertFalse(position.select(233, utc(133)).phone_present)
        self.assertEqual(position.pinned_uid, GPS.uid)
        self.assertTrue(feed(position, seconds=134).reappeared)
        self.assertFalse(feed(position, seconds=135).reappeared)
        self.assertEqual(feed(position, sa(seconds=135), 136).status, "replayed_or_out_of_order")
        self.assertFalse(position.select(310, utc(210)).phone_present)
        self.assertTrue(feed(position, seconds=211).reappeared)
        position = PhonePosition(receipt_max_age_s=40)
        feed(position, sa(lifetime=10))
        self.assertTrue(position.select(109.9, utc(9.9)).phone_present)
        self.assertFalse(position.select(110, utc(10)).phone_present)

    def test_stale_future_and_replay_cannot_pin_or_refresh(self):
        # Initial offset is unknowable; only the SA's own envelope can reject it.
        for packet, status in ((sa(lifetime=0), "stale"),
                               (replace(sa(), start=utc(6)), "future")):
            position = PhonePosition()
            self.assertEqual(feed(position, packet).status, status)
            self.assertIsNone(position.pinned_uid)
            self.assertIsNone(position.phone_now(100))
        position = PhonePosition(phone_clock_step_tolerance_s=100)
        feed(position)
        self.assertEqual(feed(position, sa(seconds=1, lifetime=1), 3).status, "stale")
        self.assertEqual(feed(position, sa(seconds=1, lifetime=3600), 76).status, "stale")
        self.assertEqual(position.phone_now(176), utc(76))
        position = PhonePosition()
        feed(position)
        self.assertEqual(feed(position, sa(), 74).status, "replayed_or_out_of_order")
        self.assertFalse(position.select(175, utc(75)).phone_present)

    def test_presence_utc_step_cannot_resurrect_or_erase_a_manual_mark(self):
        position = PhonePosition()
        feed(position)
        send(position)
        self.assertTrue(position.select(101, utc(100)).phone_present)
        self.assertTrue(position.select(102, utc(-86400)).phone_present)
        self.assertEqual(position.select(102, utc(2)).location.marker_uid, "point-1")
        self.assertFalse(position.select(175, utc(-86400)).phone_present)
        self.assertFalse(position.select(176, utc(86400)).phone_present)
        self.assertTrue(feed(position, seconds=77).reappeared)

    def test_manual_self_changes_and_external_echo_never_replace_sent_mark(self):
        position = PhonePosition()
        feed(position)
        send(position)
        position.position_sent(current(position).fix, 100, utc())
        for second, source in enumerate(("manual", "external", "gps", "unknown"), 1):
            feed(position, sa(source, second, lat=40, lon=-105), second)
            selected = position.select(100 + second, utc(second))
            self.assertEqual(selected.location.marker_uid, "point-1")
            self.assertEqual(selected.location.observation_time, utc())
            self.assertEqual(selected.retire_marker_uids, ())
            self.assertTrue(selected.phone_present)

    def test_invalid_configuration_and_clock_inputs(self):
        for kwargs in ({"receipt_max_age_s": 0}, {"max_pending_retirements": 0},
                       {"alert_threshold_m": float("nan")}, {"future_tolerance_s": -1},
                       {"gps_max_age_s": 0}, {"restore_margin_m": 0},
                       {"restore_margin_m": 21}, {"warning_read_timeout_s": -1},
                       {"feed_timeout_s": float("inf")},
                       {"phone_clock_step_tolerance_s": 0}):
            with self.assertRaises(ValueError):
                PhonePosition(**kwargs)
        position = PhonePosition()
        feed(position)
        for mono, wall in ((99, utc()), (100, BASE.replace(tzinfo=None)), (float("nan"), utc())):
            with self.assertRaises(ValueError):
                position.select(mono, wall)
        with self.assertRaises(ValueError):
            position.feed("rejection", 100, BASE, from_ethernet=True)
        with self.assertRaises(ValueError):
            position.feed(sa(), 100, BASE, from_ethernet="yes")


class LocationSelectionTests(unittest.TestCase):
    def position(self, **kwargs):
        position = PhonePosition(radio_uid="radio-1", **kwargs)
        feed(position)
        return position

    def selection(self, position, seconds=0, **kwargs):
        return position.select(100 + seconds, utc(seconds), **kwargs)

    def test_real_sent_point_is_accepted_by_pinned_creator_and_held(self):
        position = PhonePosition(radio_uid="radio-1")
        position.feed(GPS, 100, GPS.time, from_ethernet=True)
        # Preserve the elapsed phone time between these separately captured packets.
        sent_mono = 100 + (SENT.time - GPS.time).total_seconds()
        outcome = position.feed_marker(SENT, sent_mono, SENT.time, from_ethernet=True)
        self.assertEqual(outcome.status, "accepted")
        selected = position.select(sent_mono, SENT.time)
        location = selected.location
        self.assertEqual((location.source, location.marker_uid), ("marker", SENT.uid))
        self.assertEqual(location.observation_time, SENT.creator_time)
        self.assertAlmostEqual(location.age_s, (SENT.time - SENT.creator_time).total_seconds())
        self.assertEqual((location.fix.lat, location.fix.lon, location.fix.hae),
                         (SENT.lat, SENT.lon, SENT.hae))
        self.assertEqual(location.fix.source, "manual")
        self.assertIsNone(location.stale)
        self.assertIsNone(location.expires_mono)
        held = position.select(sent_mono + 86400, SENT.time + timedelta(days=1)).location
        self.assertEqual(held.marker_uid, SENT.uid)
        self.assertAlmostEqual(held.age_s, location.age_s + 86400)
        self.assertEqual(selected.retire_marker_uids, ())

    def test_adoption_gates_leave_plain_candidate_and_no_side_effects(self):
        cases = [
            (mark(), False, "wrong_ingress"),
            (mark(creator_uid="OTHER-PHONE"), True, "creator_mismatch"),
            (mark(creator_uid=None), True, "creator_mismatch"),
            (mark(creator_time=None), True, "missing_creator_time"),
            (mark(type="u-rb-a"), True, "not_point"),
            (mark(type="u-d-f"), True, "not_point"),
            (mark(created=6), True, "future_creator_time"),
            (mark(creator_time=BASE.replace(tzinfo=None)), True, "invalid_creator_time"),
            (mark(uid=GPS.uid), True, "reserved_uid"),
            (mark(uid="radio-1"), True, "reserved_uid"),
            (mark(uid="radio-1.external-position"), True, "reserved_uid"),
        ]
        for packet, ingress, reason in cases:
            position = self.position()
            with self.subTest(reason=reason):
                outcome = send(position, packet, from_ethernet=ingress)
                self.assertEqual((outcome.status, outcome.reason), ("candidate", reason))
                self.assertEqual(outcome.candidate, packet)
                self.assertIsNone(current(position))
                self.assertEqual(self.selection(position).retire_marker_uids, ())
        position = PhonePosition()
        self.assertEqual(send(position).reason, "phone_not_pinned")
        self.assertIsNone(position.pinned_uid)  # A Sent point cannot establish its own authority.

    def test_creator_not_send_or_arrival_time_orders_points(self):
        position = self.position()
        self.assertEqual(send(position, mark("A", 10), 12).status, "accepted")
        old = mark("B", 9, time=utc(20), start=utc(20), stale=utc(500))
        self.assertEqual(send(position, old, 20).reason, "older_location")
        # An expired event envelope does not expire the accepted manual location.
        newest = mark("C", 11, time=utc(0), start=utc(0), stale=utc(1))
        self.assertEqual(send(position, newest, 21).status, "accepted")
        selected = self.selection(position, 21)
        self.assertEqual(selected.location.marker_uid, "C")
        self.assertEqual(selected.location.age_s, 10)
        self.assertEqual(selected.retire_marker_uids, ("A",))

    def test_duplicates_ties_and_same_uid_newer_edit(self):
        position = self.position()
        send(position, mark("A", 0), 0)
        self.assertEqual(send(position, mark("A", 0), 1).status, "duplicate")
        self.assertEqual(current(position, 1).age_s, 1)
        self.assertEqual(send(position, mark("B", 0), 2).reason, "tied_creator_time")
        self.assertEqual(send(position, mark("A", 0, lat=40), 3).reason, "tied_creator_time")
        self.assertEqual(send(position, mark("A", 4, lat=40), 4).status, "accepted")
        selected = self.selection(position, 4)
        self.assertEqual(selected.location.fix.lat, 40)
        self.assertEqual(selected.retire_marker_uids, ())  # never delete the active UID

    def test_retirement_list_is_acknowledged_and_bounded(self):
        position = self.position(max_pending_retirements=1)
        send(position, mark("A", 0), 0)
        send(position, mark("B", 1), 1)
        outcome = send(position, mark("C", 2), 2)
        self.assertEqual((outcome.status, outcome.reason), ("candidate", "retirement_queue_full"))
        selected = self.selection(position, 2)
        self.assertEqual(selected.location.marker_uid, "B")
        self.assertEqual(selected.retire_marker_uids, ("A",))
        self.assertEqual(self.selection(position, 3).retire_marker_uids, ("A",))
        self.assertEqual(send(position, mark("A", 4), 4).reason, "retirement_pending")
        position.ack_retired(("A", "unknown"))
        self.assertEqual(send(position, mark("C", 2), 5).status, "accepted")
        self.assertEqual(self.selection(position, 5).retire_marker_uids, ("B",))
        position.ack_retired(("B",))
        self.assertEqual(self.selection(position, 5).retire_marker_uids, ())

    def test_manual_age_advances_monotonically_through_utc_steps(self):
        position = self.position()
        send(position, mark(created=-60), 0)
        first = current(position, 0)
        self.assertEqual(first.age_s, 60)
        later = position.select(110, utc(-100)).location
        self.assertEqual(later.age_s, 70)
        self.assertEqual(later.fix.remarks.count("location age"), 1)
        much_later = position.select(120, utc(100000)).location
        self.assertEqual(much_later.age_s, 80)
        self.assertEqual(much_later.marker_uid, first.marker_uid)

    def test_radio_uid_cannot_pin_and_nonpoint_sample_never_adopts(self):
        position = PhonePosition(radio_uid=GPS.uid)
        self.assertEqual(feed(position).status, "radio_uid")
        self.assertIsNone(position.pinned_uid)
        line = parse_marker((SAMPLES / "sent-rb-line.xml").read_bytes())
        self.assertIsInstance(line, str)
        with self.assertRaises(ValueError):
            send(self.position(), line)


class SelectorTestCase(unittest.TestCase):
    def position(self, **kwargs):
        position = PhonePosition(radio_uid="radio-1", **kwargs)
        feed(position)
        return position

    def gps(self, seconds=0, **kwargs):
        fix = kwargs.pop("fix", Fix(SENT.lat, SENT.lon, "gnss", SENT.hae))
        return RadioGPS(fix, 100 + seconds, **kwargs)

    def selected(self, position, seconds=0, **kwargs):
        return position.select(100 + seconds, utc(seconds), **kwargs)

    def event(self, position, code, kind="geochat"):
        return next((e for e in position.outbound_events if (e.code, e.kind) == (code, kind)), None)

    def ack(self, position, event, seconds=0, message_id=None):
        return position.ack_event(event.event_id, 100 + seconds, utc(seconds), message_id=message_id)


class GPSGateTests(SelectorTestCase):
    def test_usable_gps_wins_and_get_fix_returns_actual_chosen_fix(self):
        for state in ("UNCHECKED", "RANGE_CONSISTENT"):
            position = self.position()
            send(position)
            gps = self.gps(state=state)
            chosen = self.selected(position, radio_gps=gps)
            self.assertTrue(chosen.own_gnss_selected)
            self.assertEqual(chosen.fix, gps.fix)
            self.assertIsNone(chosen.location)
            self.assertEqual(chosen.landmark_distance_m, 0)
            self.assertEqual(chosen.outbound_events, ())
            self.assertEqual(position.get_fix(100, utc(), radio_gps=gps), gps.fix)
            self.assertEqual(self.selected(position, 5, radio_gps=gps).location.marker_uid, "point-1")

    def test_stale_future_spoof_jam_and_no_fix_block_gps_without_mark(self):
        cases = [self.gps(-5), self.gps(1), self.gps(state="NO_FIX"),
                 self.gps(state="GNSS_SUSPECTED"), self.gps(state="INCONSISTENT_UNATTRIBUTED"),
                 self.gps(jammed=True), self.gps(fix=None)]
        for gps in cases:
            with self.subTest(gps=gps):
                position = self.position()
                chosen = self.selected(position, radio_gps=gps)
                self.assertIsNone(chosen.fix)
                self.assertIsNone(external_position("radio-1", chosen.fix, utc()))
                self.assertIn("NO GPS", self.event(position, "gps_untrusted").text)
                self.assertIsNotNone(self.event(position, "gps_untrusted", "audio"))
                # Contact is still available for Send, even with no fix.
                contact = ET.fromstring(hidden_contact("radio-1", "RADIO", ("10.0.0.1", 4242), utc()))
                self.assertEqual(contact.find("point").get("lat"), "0")
        position = self.position()
        self.assertTrue(self.selected(position, 4.999, radio_gps=self.gps()).own_gnss_selected)
        self.assertFalse(self.selected(position, 5, radio_gps=self.gps()).own_gnss_selected)

    def test_persistent_mark_fallback_and_retirements_when_gps_wins(self):
        position = self.position()
        send(position, mark("A"))
        send(position, mark("B", 1), 1)
        chosen = self.selected(position, 1, radio_gps=self.gps(1))
        self.assertTrue(chosen.own_gnss_selected)
        self.assertEqual(chosen.retire_marker_uids, ("A", "B"))
        position.ack_retired(("A", "B"))
        chosen = self.selected(position, 10, radio_gps=self.gps(10, state="GNSS_SUSPECTED"))
        self.assertFalse(chosen.own_gnss_selected)  # old nearby mark cannot restore trust
        self.assertEqual(chosen.location.marker_uid, "B")
        self.assertEqual(chosen.retire_marker_uids, ())

    def test_conflict_latches_with_stable_bounded_feedback(self):
        position = self.position()
        send(position, mark(lat=SENT.lat + .001))
        chosen = self.selected(position, radio_gps=self.gps())
        self.assertTrue(chosen.gps_overridden)
        self.assertFalse(chosen.position_trusted)
        self.assertEqual(chosen.gps_state, "UNCHECKED")  # raw monitor is not rewritten
        self.assertEqual(chosen.fix.source, "manual")
        self.assertAlmostEqual(chosen.landmark_distance_m, 111.19508, places=3)
        first = self.event(position, "gps_override")
        self.assertIn("111.2 m", first.text)
        self.assertEqual(self.event(position, "gps_override", "audio").text, "GPS location overridden")
        for second in range(1, 5):
            self.selected(position, second, radio_gps=self.gps(second))
            self.assertEqual(self.event(position, "gps_override"), first)
        send(position, mark("B", 5, lat=SENT.lat + .002), 5)
        self.selected(position, 5, radio_gps=self.gps(5))
        second = self.event(position, "gps_override")
        self.assertGreater(second.event_id, first.event_id)
        self.assertFalse(self.ack(position, first, 5, "obsolete"))
        self.assertEqual(self.event(position, "gps_override"), second)
        self.assertEqual(len(position.outbound_events), 3)
        self.assertTrue(self.event(position, "gps_override", "web_ui").active)

    def test_clean_ranging_never_restores_and_requests_new_nearby_mark(self):
        position = self.position()
        self.selected(position, radio_gps=self.gps(state="GNSS_SUSPECTED"))
        for second, state in ((1, "RANGE_CONSISTENT"), (2, "UNCHECKED")):
            chosen = self.selected(position, second, radio_gps=self.gps(second, state=state))
            self.assertIsNone(chosen.fix)
            self.assertFalse(chosen.position_trusted)
        request = self.event(position, "confirm_position")
        self.assertIn("Mark your best known", request.text)
        self.selected(position, 3, radio_gps=self.gps(3, state="RANGE_CONSISTENT"))
        self.assertEqual(self.event(position, "confirm_position"), request)
        send(position, mark("near", 4), 4)
        chosen = self.selected(position, 4, radio_gps=self.gps(4))
        self.assertTrue(chosen.own_gnss_selected)
        self.assertTrue(chosen.position_trusted)
        self.assertEqual(chosen.outbound_events, ())
        # The landmark persists for outages without retriggering the user action.
        self.assertEqual(self.selected(position, 20).location.marker_uid, "near")
        self.assertTrue(self.selected(position, 21, radio_gps=self.gps(21)).own_gnss_selected)

    def test_near_mark_clears_override_banner_and_old_unread_prompt(self):
        position = self.position(warning_read_timeout_s=10)
        send(position, mark("far", lat=SENT.lat + .001))
        self.selected(position, radio_gps=self.gps())
        self.ack(position, self.event(position, "gps_override"), 0, "far-warning")
        send(position, mark("near", 1), 1)
        chosen = self.selected(position, 1, radio_gps=self.gps(1))
        self.assertTrue(chosen.own_gnss_selected)
        self.assertFalse(chosen.gps_overridden)
        self.assertEqual(chosen.retire_marker_uids, ("far", "near"))
        clear = self.event(position, "gps_override", "web_ui")
        self.assertFalse(clear.active)
        self.ack(position, clear, 1)
        self.assertEqual(self.selected(position, 20, radio_gps=self.gps(20)).outbound_events, ())

    def test_nearby_mark_confirms_position_but_leaves_time_monitor_quarantined(self):
        position = self.position()
        self.selected(position, radio_gps=self.gps(state="GNSS_SUSPECTED", fault_epoch=1))
        send(position, mark("near", 1), 1)
        gps = self.gps(1, state="GNSS_SUSPECTED", fault_epoch=1, ranges_agree=True)
        chosen = self.selected(position, 1, radio_gps=gps)
        self.assertTrue(chosen.own_gnss_selected)
        self.assertEqual(chosen.gps_state, "GNSS_SUSPECTED")
        self.assertEqual(gps.state, "GNSS_SUSPECTED")  # immutable input, no time grant
        self.assertTrue(self.selected(position, 2, radio_gps=replace(gps, observed_mono=102)).own_gnss_selected)
        # New failed check under unchanged raw status revokes confirmation.
        chosen = self.selected(position, 3, radio_gps=replace(gps, observed_mono=103, fault_epoch=2))
        self.assertFalse(chosen.own_gnss_selected)
        self.assertFalse(chosen.position_trusted)
        self.assertEqual(chosen.location.marker_uid, "near")
        self.assertIsNotNone(self.event(position, "confirm_position"))

    def test_restore_margin_old_marks_and_jamming(self):
        for gap, restored in ((9.9, True), (10.1, False), (15, False)):
            position = self.position()
            self.selected(position, radio_gps=self.gps(state="GNSS_SUSPECTED"))
            send(position, mark(created=1, lat=SENT.lat + gap / 111195.08), 1)
            chosen = self.selected(position, 1, radio_gps=self.gps(1))
            self.assertEqual(chosen.own_gnss_selected, restored)
        position = self.position()
        self.selected(position, radio_gps=self.gps(jammed=True))
        send(position, mark("near", 1), 1)
        self.assertFalse(self.selected(position, 1, radio_gps=self.gps(1, jammed=True)).own_gnss_selected)
        # Releasing jam does not make the held mark a fresh action.
        self.assertFalse(self.selected(position, 2, radio_gps=self.gps(2)).own_gnss_selected)
        send(position, mark("new", 3), 3)
        self.assertTrue(self.selected(position, 3, radio_gps=self.gps(3)).own_gnss_selected)
        self.assertFalse(self.selected(position, 4, radio_gps=self.gps(4, jammed=True)).own_gnss_selected)

    def test_old_point_sent_after_fault_cannot_confirm(self):
        position = self.position()
        self.selected(position, 10, radio_gps=self.gps(10, state="GNSS_SUSPECTED"))
        send(position, mark("old", 1), 11)
        self.assertFalse(self.selected(position, 11, radio_gps=self.gps(11)).own_gnss_selected)

    def test_new_mark_without_gps_compared_when_first_fresh_fix_arrives(self):
        position = self.position()
        send(position, mark(lat=SENT.lat + .001))
        self.selected(position, 20, radio_gps=self.gps())
        self.assertIsNone(self.event(position, "gps_override"))
        self.selected(position, 21, radio_gps=self.gps(21))
        self.assertIsNotNone(self.event(position, "gps_override"))

    def test_threshold_is_strict_horizontal_and_wraps_antimeridian(self):
        distance = 6371008.8 * .0001 * 3.141592653589793 / 180
        for offset, expected in ((0, False), (1e-6, True)):
            position = self.position(alert_threshold_m=distance - offset)
            send(position, mark(lat=0.0001, lon=0, hae=10000))
            chosen = self.selected(position, radio_gps=self.gps(fix=Fix(0, 0, "gnss", 0)))
            self.assertEqual(chosen.gps_overridden, expected)
        position = self.position()
        send(position, mark(lat=0, lon=179.99999))
        chosen = self.selected(position, radio_gps=self.gps(fix=Fix(0, -179.99999, "gnss")))
        self.assertFalse(chosen.gps_overridden)

    def test_confirming_marker_retired_once_without_losing_manual_fallback(self):
        position = self.position()
        self.assertEqual(position.restore_margin_m, 10)
        self.selected(position, radio_gps=self.gps(state="GNSS_SUSPECTED"))
        send(position, mark("confirm", 1), 1)
        chosen = self.selected(position, 1, radio_gps=self.gps(1, state="GNSS_SUSPECTED"))
        self.assertTrue(chosen.own_gnss_selected)
        self.assertEqual(chosen.gps_state, "GNSS_SUSPECTED")
        self.assertEqual(chosen.retire_marker_uids, ("confirm",))
        self.assertEqual(self.selected(position, 2, radio_gps=self.gps(2)).retire_marker_uids, ("confirm",))
        position.ack_retired(("confirm",))
        self.assertEqual(self.selected(position, 3, radio_gps=self.gps(3)).retire_marker_uids, ())
        fallback = self.selected(position, 10)
        self.assertEqual((fallback.fix.lat, fallback.fix.lon), (SENT.lat, SENT.lon))
        self.assertEqual(fallback.location.observation_time, utc(1))
        self.assertEqual(fallback.location.age_s, 9)
        send(position, mark("next", 11, lat=SENT.lat + .001), 11)
        self.assertEqual(self.selected(position, 11).retire_marker_uids, ())  # no second delete

    def test_confirmation_backpressure_retries_without_dropping_deletes(self):
        position = self.position(max_pending_retirements=1)
        self.selected(position, radio_gps=self.gps(state="GNSS_SUSPECTED"))
        send(position, mark("old", 1, lat=SENT.lat + .001), 1)
        send(position, mark("confirm", 2), 2)
        chosen = self.selected(position, 2, radio_gps=self.gps(2))
        self.assertFalse(chosen.own_gnss_selected)
        self.assertEqual(chosen.retire_marker_uids, ("old",))
        self.assertEqual(chosen.location.marker_uid, "confirm")
        position.ack_retired(("old",))
        chosen = self.selected(position, 3, radio_gps=self.gps(3))
        self.assertTrue(chosen.own_gnss_selected)
        self.assertEqual(chosen.retire_marker_uids, ("confirm",))
        position.ack_retired(("confirm",))
        send(position, mark("later", 4), 4)
        self.assertEqual(self.selected(position, 4).retire_marker_uids, ())

    def test_restore_margin_can_be_tuned_and_boundary_is_inclusive(self):
        # Great-circle displacement for a due-north arc at the equator.
        distance = 6371008.8 * .00005 * 3.141592653589793 / 180
        position = self.position(restore_margin_m=distance)
        self.selected(position, radio_gps=self.gps(state="GNSS_SUSPECTED"))
        send(position, mark("edge", 1, lat=.00005, lon=0), 1)
        chosen = self.selected(position, 1, radio_gps=self.gps(1, fix=Fix(0, 0, "gnss")))
        self.assertTrue(chosen.own_gnss_selected)
        self.assertEqual(chosen.retire_marker_uids, ("edge",))
        position = self.position(restore_margin_m=5)
        self.selected(position, radio_gps=self.gps(state="GNSS_SUSPECTED"))
        send(position, mark(created=1, lat=SENT.lat + 8 / 111195.08), 1)
        self.assertFalse(self.selected(position, 1, radio_gps=self.gps(1)).own_gnss_selected)

    def test_invalid_radio_gps_and_fault_epoch(self):
        position = self.position()
        for gps in (self.gps(fix=Fix(1, 2, "gnss", phone_uid=GPS.uid)),
                    self.gps(fix=Fix(1, 2, "manual")), self.gps(jammed=1),
                    self.gps(state="TRUSTED"), self.gps(fix=Fix(float("nan"), 2, "gnss")),
                    self.gps(ranges_agree=1), "bad"):
            with self.assertRaises(ValueError):
                self.selected(position, radio_gps=gps)
        for epoch in (-1, True, 1.5):
            with self.assertRaises(ValueError):
                self.selected(position, radio_gps=self.gps(fault_epoch=epoch))
        self.selected(position, radio_gps=self.gps(fault_epoch=2))
        with self.assertRaises(ValueError):
            self.selected(position, radio_gps=self.gps(fault_epoch=1))


class DragTests(SelectorTestCase):
    def test_real_cold_start_then_user_selection_adopts_and_feeds(self):
        position = PhonePosition(radio_uid="radio-1")
        self.assertEqual(position.feed(NO_LOCATION, 100, NO_LOCATION.time, from_ethernet=True).status,
                         "accepted_presence")
        chosen = position.select(100, NO_LOCATION.time)
        self.assertTrue(chosen.phone_present)
        self.assertIsNone(chosen.fix)
        self.assertIsNotNone(self.event(position, "gps_untrusted", "audio"))
        self.assertIn("tap it to select", self.event(position, "gps_untrusted").text)
        elapsed = (USER_SELECTED.time - NO_LOCATION.time).total_seconds()
        result = position.feed(USER_SELECTED, 100 + elapsed, USER_SELECTED.time, from_ethernet=True)
        self.assertEqual(result.status, "accepted_manual")
        self.assertIsNone(self.event(position, "gps_untrusted", "audio"))
        chosen = position.select(100 + elapsed, USER_SELECTED.time)
        self.assertEqual((chosen.fix.lat, chosen.fix.lon), (USER_SELECTED.lat, USER_SELECTED.lon))
        self.assertEqual(chosen.location.observation_time, USER_SELECTED.time)
        self.assertEqual(chosen.retire_marker_uids, ())  # never delete the user's own dot
        packet = ET.fromstring(external_position("radio-1", chosen.fix, USER_SELECTED.time))
        self.assertEqual(packet.find("detail/precisionlocation").get("geopointsrc"), "MANET:manual")
        position.position_sent(chosen.fix, 100 + elapsed, USER_SELECTED.time)
        repeat = replace(USER_SELECTED, time=USER_SELECTED.time + timedelta(seconds=30),
                         start=USER_SELECTED.start + timedelta(seconds=30),
                         stale=USER_SELECTED.stale + timedelta(seconds=30))
        self.assertEqual(position.feed(repeat, 130 + elapsed, repeat.time, from_ethernet=True).status,
                         "accepted_presence")
        self.assertEqual(position.select(130 + elapsed, repeat.time).location.observation_time,
                         USER_SELECTED.time)

    def test_user_selection_is_gated_by_actual_feed_and_source_not_how(self):
        for how in ("m-g", "h-e", "h-g-i-g-o", "m-f"):
            position = self.position()
            self.assertEqual(feed(position, sa("manual", 1, geopointsrc="USER", how=how), 1).status,
                             "accepted_manual")
        position = self.position()
        position.position_sent(self.gps().fix, 100, utc())
        self.assertEqual(feed(position, sa("manual", 1, geopointsrc="USER", how="m-g"), 1).status,
                         "accepted_presence")
        position.position_sent(self.gps(5).fix, 105, utc(5))
        # The periodic held USER report seen during the feed is not a later gesture.
        self.assertEqual(feed(position, sa("manual", 15, geopointsrc="USER", how="m-g"), 15).status,
                         "accepted_presence")
        self.assertEqual(feed(position, sa("manual", 16, geopointsrc="USER", how="m-g", lat=40), 16).status,
                         "accepted_manual")

    def test_explicit_user_transition_can_confirm_the_last_fed_coordinate(self):
        position = self.position()
        gps = self.gps()
        position.position_sent(gps.fix, 100, utc())
        self.selected(position, 1, radio_gps=self.gps(1, state="GNSS_SUSPECTED"))
        result = feed(position, sa("manual", 10, geopointsrc="USER", how="m-g",
                                   lat=gps.fix.lat, lon=gps.fix.lon), 10)
        self.assertEqual(result.status, "accepted_manual")
        chosen = self.selected(position, 10, radio_gps=self.gps(10, state="GNSS_SUSPECTED"))
        self.assertTrue(chosen.own_gnss_selected)
        self.assertEqual(chosen.retire_marker_uids, ())
        self.assertEqual(chosen.gps_state, "GNSS_SUSPECTED")

    def test_newest_user_selection_and_sent_mark_order(self):
        position = self.position()
        send(position, mark("A", 1), 1)
        result = feed(position, sa("manual", 2, geopointsrc="USER", how="m-g", lat=40), 2)
        self.assertEqual(result.status, "accepted_manual")
        self.assertEqual(self.selected(position, 2).retire_marker_uids, ("A",))
        self.assertEqual(send(position, mark("older", 1), 3).reason, "older_location")
        self.assertEqual(send(position, mark("newer", 4), 4).status, "accepted")
        self.assertEqual(self.selected(position, 4).location.marker_uid, "newer")

    def test_new_feed_baseline_allows_return_to_an_earlier_manual_location(self):
        position = self.position()
        feed(position, sa("manual", 1, lat=40), 1)
        fix = self.selected(position, 1).fix
        position.position_sent(replace(fix, lat=41), 102, utc(2))
        self.assertEqual(feed(position, sa("manual", 12, lat=40), 12).status, "accepted_manual")
        self.assertEqual(self.selected(position, 12).location.observation_time, utc(12))

    def test_hand_set_sa_adopted_without_feed_and_periodic_sa_not_redated(self):
        position = self.position()
        result = feed(position, sa("manual", 1), 1)
        self.assertEqual(result.status, "accepted_manual")
        chosen = self.selected(position, 1)
        self.assertEqual(chosen.location.source, "self_manual")
        self.assertEqual(chosen.fix.phone_uid, GPS.uid)
        self.assertEqual(chosen.location.observation_time, utc(1))
        feed(position, sa("manual", 31), 31)
        chosen = self.selected(position, 31)
        self.assertEqual(chosen.location.observation_time, utc(1))
        self.assertEqual(chosen.location.age_s, 30)
        self.assertEqual(chosen.retire_marker_uids, ())

    def test_drag_then_feed_locks_until_timeout_and_held_echo_never_adopts(self):
        position = self.position()
        feed(position, sa("manual", 1), 1)
        fix = self.selected(position, 1).fix
        position.position_sent(fix, 101, utc(1))
        feed(position, sa("manual", 2, lat=40), 2)
        self.assertEqual(self.selected(position, 2).fix.lat, fix.lat)
        # Waiting for loss never converts a previously observed h-e into an action.
        feed(position, sa("manual", 11, lat=40), 11)
        self.assertEqual(self.selected(position, 11).fix.lat, fix.lat)
        feed(position, sa("manual", 12, lat=fix.lat, lon=fix.lon, hae=9000), 12)
        self.assertEqual(self.selected(position, 12).fix.hae, fix.hae)
        self.assertEqual(feed(position, sa("manual", 13, lat=41), 13).status, "accepted_manual")
        self.assertEqual(self.selected(position, 13).fix.lat, 41)

    def test_selection_without_send_does_not_fake_a_live_feed(self):
        position = self.position()
        self.selected(position, radio_gps=self.gps())
        self.assertEqual(feed(position, sa("manual", 1), 1).status, "accepted_manual")
        for fix in (None, Fix(1, 2, "gnss", phone_uid=GPS.uid), Fix(0, 0, "manual")):
            with self.assertRaises(ValueError):
                position.position_sent(fix, 101, utc(1))

    def test_newest_drag_and_sent_creator_time_win_and_retire_only_sent_uid(self):
        position = self.position()
        send(position, mark("A", 1), 1)
        feed(position, sa("manual", 2, lat=40), 2)
        chosen = self.selected(position, 2)
        self.assertEqual(chosen.location.source, "self_manual")
        self.assertEqual(chosen.retire_marker_uids, ("A",))
        self.assertEqual(send(position, mark("old", 1), 3).reason, "older_location")
        self.assertEqual(send(position, mark("B", 4, type="a-h-G"), 4).status, "accepted")
        chosen = self.selected(position, 4)
        self.assertEqual(chosen.location.marker_uid, "B")
        self.assertEqual(chosen.retire_marker_uids, ("A",))
        self.assertEqual(feed(position, sa("manual", 5, lat=40), 5).status, "accepted_presence")
        self.assertEqual(self.selected(position, 5).location.marker_uid, "B")

    def test_echo_without_parent_and_source_stripping_cannot_adopt_after_feed_loss(self):
        position = self.position()
        gps = self.gps()
        position.position_sent(gps.fix, 100, utc())
        for second, packet in ((11, sa("external", 11, geopointsrc="MANET:manual", parent_uids=(), lat=40)),
                               (12, sa("manual", 12, lat=gps.fix.lat, lon=gps.fix.lon)),
                               (13, sa("manual", 13, geopointsrc="MANET:manual", lat=41)),
                               (14, sa("manual", 14, parent_uids=("radio-1.external-position",), lat=42))):
            feed(position, packet, second)
            self.assertIsNone(self.selected(position, second).location)

    def test_drag_backpressure_does_not_lose_old_marker_retirement(self):
        position = self.position(max_pending_retirements=1)
        send(position, mark("A"))
        send(position, mark("B", 1), 1)
        self.assertEqual(feed(position, sa("manual", 2), 2).status, "retirement_queue_full")
        self.assertEqual(self.selected(position, 2).location.marker_uid, "B")
        position.ack_retired(("A",))
        self.assertEqual(feed(position, sa("manual", 3), 3).status, "accepted_manual")
        self.assertEqual(self.selected(position, 3).retire_marker_uids, ("B",))


class FeedbackTests(SelectorTestCase):
    def receipt(self, position, status="read", seconds=1, message_id="warning-1", **kwargs):
        packet = ChatReceipt(message_id, status, GPS.uid, "radio-1", utc(seconds),
                             utc(seconds), utc(seconds + 86400))
        packet = replace(packet, **kwargs)
        return position.feed_receipt(packet, 100 + seconds, utc(seconds), from_ethernet=True)

    def warning(self, timeout=10):
        position = self.position(warning_read_timeout_s=timeout)
        self.selected(position, radio_gps=self.gps(state="GNSS_SUSPECTED"))
        return position

    def test_real_receipts_acknowledge_only_on_read(self):
        delivered = parse_chat_receipt((SAMPLES / "chat-receipt-delivered.xml").read_bytes())
        read = parse_chat_receipt((SAMPLES / "chat-receipt-read.xml").read_bytes())
        position = PhonePosition(radio_uid=delivered.destination_uid, warning_read_timeout_s=10)
        wall = delivered.time - timedelta(seconds=2)
        pin = replace(GPS, time=wall, start=wall, stale=wall + timedelta(seconds=75))
        position.feed(pin, 100, wall, from_ethernet=True)
        position.select(100, wall)
        position.ack_event(self.event(position, "gps_untrusted").event_id, 101, wall + timedelta(seconds=1),
                           message_id=delivered.message_id)
        self.assertEqual(position.feed_receipt(delivered, 102, delivered.time, from_ethernet=True), "delivered")
        self.assertIsNotNone(self.event(position, "gps_untrusted", "audio"))
        self.assertEqual(position.feed_receipt(read, 119, read.time, from_ethernet=True), "read")
        position.select(200, read.time + timedelta(seconds=81))
        self.assertIsNone(self.event(position, "gps_untrusted", "audio"))

    def test_each_manual_gesture_stops_repeating_without_restoring_distrusted_gps(self):
        for gesture in ("marker", "drag", "select"):
            for ack_chat_first in (True, False):
                with self.subTest(gesture=gesture, ack_chat_first=ack_chat_first):
                    position = self.warning()
                    chat = self.event(position, "gps_untrusted")
                    if ack_chat_first:
                        self.ack(position, chat, 0, "warning-1")
                    # Between restore and conflict thresholds: acknowledge, do not restore.
                    lat = SENT.lat + 15 / 111195.08
                    if gesture == "marker":
                        self.assertEqual(send(position, mark(created=1, lat=lat), 1).status, "accepted")
                    else:
                        fields = dict(lat=lat, lon=SENT.lon)
                        if gesture == "select":
                            fields.update(geopointsrc="USER", how="m-g")
                        self.assertEqual(feed(position, sa("manual", 1, **fields), 1).status, "accepted_manual")
                    if not ack_chat_first:
                        self.ack(position, chat, 1, "warning-1")  # late send must not rearm
                    self.assertIsNone(self.event(position, "gps_untrusted", "audio"))
                    chosen = self.selected(position, 30, radio_gps=self.gps(30, state="GNSS_SUSPECTED"))
                    self.assertFalse(chosen.own_gnss_selected)
                    self.assertFalse(chosen.position_trusted)
                    self.assertIsNone(self.event(position, "gps_untrusted", "audio"))
                    self.assertEqual(self.receipt(position, "delivered", 31), "delivered")
                    self.assertEqual(self.receipt(position, "read", 32), "read")  # gesture didn't fake read
                    self.selected(position, 100)
                    self.assertIsNone(self.event(position, "gps_untrusted", "audio"))

    def test_gesture_cancels_an_already_queued_repeat(self):
        position = self.warning()
        self.ack(position, self.event(position, "gps_untrusted"), 0, "warning-1")
        self.ack(position, self.event(position, "gps_untrusted", "audio"))
        self.selected(position, 10)
        repeat = self.event(position, "gps_untrusted", "audio")
        self.assertIsNotNone(repeat)
        send(position, mark(created=11, lat=SENT.lat + 15 / 111195.08), 11)
        self.assertIsNone(self.event(position, "gps_untrusted", "audio"))
        self.assertFalse(self.ack(position, repeat, 11))
        self.selected(position, 100)
        self.assertIsNone(self.event(position, "gps_untrusted", "audio"))

    def test_fresh_resend_acknowledges_without_replacing_but_old_replay_does_not(self):
        for same_uid in (True, False):
            position = self.position(warning_read_timeout_s=10)
            original = mark(lat=SENT.lat + 15 / 111195.08)
            send(position, original)
            self.selected(position, 1, radio_gps=self.gps(1, state="GNSS_SUSPECTED"))
            self.ack(position, self.event(position, "gps_untrusted"), 1, "warning-1")
            self.ack(position, self.event(position, "gps_untrusted", "audio"), 1)
            self.assertEqual(send(position, original, 2).status, "duplicate")
            self.selected(position, 11)
            self.assertIsNotNone(self.event(position, "gps_untrusted", "audio"))
            resent = replace(original, time=utc(12), start=utc(12))
            if not same_uid:
                resent = replace(resent, uid="older", creator_time=utc(-1))
            result = send(position, resent, 12)
            self.assertEqual(result.status, "duplicate" if same_uid else "candidate")
            chosen = self.selected(position, 30, radio_gps=self.gps(30, state="GNSS_SUSPECTED"))
            self.assertIsNone(self.event(position, "gps_untrusted", "audio"))
            self.assertEqual(chosen.location.marker_uid, original.uid)
            self.assertEqual(chosen.location.observation_time, utc())
            self.assertFalse(chosen.position_trusted)

    def test_retirement_backpressure_does_not_prevent_gesture_ack(self):
        for source in (None, "USER"):
            position = self.position(max_pending_retirements=1, warning_read_timeout_s=10)
            send(position, mark("A"))
            send(position, mark("B", 1, lat=SENT.lat + 15 / 111195.08), 1)
            self.selected(position, 2, radio_gps=self.gps(2, state="GNSS_SUSPECTED"))
            self.ack(position, self.event(position, "gps_untrusted"), 2, "warning-1")
            self.ack(position, self.event(position, "gps_untrusted", "audio"), 2)
            self.selected(position, 12)
            self.assertIsNotNone(self.event(position, "gps_untrusted", "audio"))
            packet = sa("manual", 13, lat=40, geopointsrc=source, how="m-g" if source else "h-e")
            self.assertEqual(feed(position, packet, 13).status, "retirement_queue_full")
            chosen = self.selected(position, 30)
            self.assertEqual(chosen.location.marker_uid, "B")
            self.assertEqual(chosen.retire_marker_uids, ("A",))
            self.assertIsNone(self.event(position, "gps_untrusted", "audio"))

    def test_far_mark_override_is_one_shot_feedback_for_an_acknowledged_gesture(self):
        position = self.warning()
        send(position, mark(created=1, lat=SENT.lat + .001), 1)
        self.selected(position, 1, radio_gps=self.gps(1))
        self.assertIsNone(self.event(position, "gps_untrusted", "audio"))
        self.ack(position, self.event(position, "gps_override"), 1, "override-1")
        self.ack(position, self.event(position, "gps_override", "audio"), 1)
        self.assertTrue(self.selected(position, 30, radio_gps=self.gps(30)).gps_overridden)
        self.assertIsNone(self.event(position, "gps_override", "audio"))

    def test_candidates_echoes_and_unchanged_periodic_manual_sa_do_not_ack(self):
        position = self.warning()
        self.ack(position, self.event(position, "gps_untrusted"), 0, "warning-1")
        self.ack(position, self.event(position, "gps_untrusted", "audio"))
        self.assertEqual(send(position, mark(created=1, creator_uid="other"), 1).status, "candidate")
        self.assertEqual(send(position, mark(created=2), 2, from_ethernet=False).status, "candidate")
        feed(position, sa("external", 3, geopointsrc="MANET:manual"), 3)
        self.selected(position, 10)
        self.assertIsNotNone(self.event(position, "gps_untrusted", "audio"))
        for source in (None, "USER"):
            position = self.position(warning_read_timeout_s=10)
            packet = sa("manual", 1, lat=SENT.lat + 15 / 111195.08, lon=SENT.lon,
                        geopointsrc=source, how="m-g" if source else "h-e")
            feed(position, packet, 1)
            self.selected(position, 2, radio_gps=self.gps(2, state="GNSS_SUSPECTED"))
            self.ack(position, self.event(position, "gps_untrusted"), 2, "warning-1")
            self.ack(position, self.event(position, "gps_untrusted", "audio"), 2)
            repeat = replace(packet, time=utc(31), start=utc(31), stale=utc(106))
            self.assertEqual(feed(position, repeat, 31).status, "accepted_presence")
            self.selected(position, 31)
            self.assertIsNotNone(self.event(position, "gps_untrusted", "audio"))

    def test_actual_feed_plus_internal_gps_warns_once_chat_and_ui(self):
        position = self.position()
        position.position_sent(self.gps().fix, 100, utc())
        feed(position, sa(seconds=1), 1)
        event = self.event(position, "phone_gps")
        self.assertIn("Android location services off", event.text)
        self.assertTrue(self.event(position, "phone_gps", "web_ui").active)
        self.assertIsNone(self.event(position, "phone_gps", "audio"))
        feed(position, sa(seconds=2), 2)
        self.assertEqual(self.event(position, "phone_gps"), event)
        feed(position, sa("external", 3, geopointsrc="MANET:gnss", how="m-g"), 3)
        self.assertIsNone(self.event(position, "phone_gps"))
        self.assertFalse(self.event(position, "phone_gps", "web_ui").active)
        feed(position, sa(seconds=11), 11)  # expired feed: internal GPS never becomes fallback
        self.assertIsNone(self.event(position, "phone_gps"))
        self.assertIsNone(self.selected(position, 11).fix)

    def test_unread_timeout_starts_after_send_delivered_is_not_read(self):
        position = self.warning()
        self.ack(position, self.event(position, "gps_untrusted", "audio"))
        self.selected(position, 20)
        self.assertIsNone(self.event(position, "gps_untrusted", "audio"))  # no send yet
        chat = self.event(position, "gps_untrusted")
        self.ack(position, chat, 20, "warning-1")
        self.assertEqual(self.receipt(position, "delivered", 21), "delivered")
        self.assertFalse(self.ack(position, chat, 25, "warning-1"))
        self.selected(position, 29.999)
        self.assertIsNone(self.event(position, "gps_untrusted", "audio"))
        self.selected(position, 30)
        audio = self.event(position, "gps_untrusted", "audio")
        self.assertIsNotNone(audio)
        self.selected(position, 100)  # one pending prompt, no catch-up burst
        self.assertEqual(self.event(position, "gps_untrusted", "audio"), audio)
        self.ack(position, audio, 100)
        self.selected(position, 110)
        self.assertGreater(self.event(position, "gps_untrusted", "audio").event_id, audio.event_id)
        self.assertEqual(self.receipt(position, "read", 111), "read")
        self.assertIsNone(self.event(position, "gps_untrusted", "audio"))
        self.selected(position, 1000)
        self.assertIsNone(self.event(position, "gps_untrusted", "audio"))

    def test_read_before_delivered_is_terminal_does_not_refresh_presence(self):
        position = self.warning()
        self.ack(position, self.event(position, "gps_untrusted"), 0, "warning-1")
        self.assertEqual(self.receipt(position, "read", 1), "read")
        self.assertEqual(self.receipt(position, "delivered", 2), "duplicate")
        self.assertEqual(self.receipt(position, "read", 3), "duplicate")
        self.assertFalse(self.selected(position, 75).phone_present)
        self.assertIsNone(self.event(position, "gps_untrusted", "audio"))

    def test_receipt_identity_ingress_message_and_time_gates(self):
        position = self.warning()
        self.ack(position, self.event(position, "gps_untrusted"), 10, "warning-1")
        for fields, reason in (({"sender_uid": "other"}, "uid_mismatch"),
                               ({"destination_uid": "other"}, "wrong_destination"),
                               ({"message_id": "other"}, "unknown_message"),
                               ({"time": utc(-80)}, "stale"),
                               ({"time": utc(0)}, "predates_message"),
                               ({"time": utc(20)}, "future"),
                               ({"stale": utc(10)}, "stale")):
            args = dict(seconds=11)
            args.update(fields)
            self.assertEqual(self.receipt(position, **args), reason)
        packet = ChatReceipt("warning-1", "read", GPS.uid, "radio-1", utc(11), utc(11), utc(100))
        self.assertEqual(position.feed_receipt(packet, 111, utc(11), from_ethernet=False), "wrong_ingress")
        self.selected(position, 20)
        self.assertIsNotNone(self.event(position, "gps_untrusted", "audio"))
        with self.assertRaises(ValueError):
            position.feed_receipt("bad", 120, utc(20), from_ethernet=True)

    def test_resolution_cancels_pending_reminders_and_old_receipts(self):
        position = self.warning()
        self.ack(position, self.event(position, "gps_untrusted"), 0, "warning-1")
        send(position, mark(created=1), 1)
        self.assertTrue(self.selected(position, 1, radio_gps=self.gps(1)).own_gnss_selected)
        self.assertEqual(position.outbound_events, ())
        self.assertEqual(self.receipt(position, seconds=2), "unknown_message")
        self.selected(position, 10, radio_gps=self.gps(10))
        self.assertEqual(position.outbound_events, ())

    def test_unpinned_warning_queues_chat_after_sa_and_unique_ids_required(self):
        position = PhonePosition(radio_uid="radio-1")
        self.selected(position)
        self.assertIsNone(self.event(position, "gps_untrusted"))
        self.assertIsNotNone(self.event(position, "gps_untrusted", "audio"))
        feed(position, seconds=1)
        self.selected(position, 1)
        chat = self.event(position, "gps_untrusted")
        self.assertEqual(chat.phone_uid, GPS.uid)
        with self.assertRaises(ValueError):
            self.ack(position, chat, 1)
        self.ack(position, chat, 1, "id-1")
        position.position_sent(self.gps(1).fix, 101, utc(1))
        feed(position, seconds=2)
        with self.assertRaises(ValueError):
            self.ack(position, self.event(position, "phone_gps"), 2, "id-1")


class PhoneChatAdmissionTests(SelectorTestCase):
    def chat(self, seconds=1, **kwargs):
        packet = replace(PHONE_CHAT, destination_uid="radio-1", time=utc(seconds),
                         start=utc(seconds), stale=utc(seconds + 86400), remarks_time=utc(seconds))
        return replace(packet, **kwargs)

    def test_real_phone_reply_admitted_for_display_without_pinning_or_refreshing_sa(self):
        position = PhonePosition(radio_uid=PHONE_CHAT.destination_uid)
        self.assertEqual(position.feed_chat(PHONE_CHAT, 100, PHONE_CHAT.time, from_ethernet=True), "uid_mismatch")
        self.assertIsNone(position.pinned_uid)
        pin = replace(GPS, time=PHONE_CHAT.time, start=PHONE_CHAT.time,
                      stale=PHONE_CHAT.time + timedelta(seconds=75))
        position.feed(pin, 100, PHONE_CHAT.time, from_ethernet=True)
        received = position.feed_chat(PHONE_CHAT, 101, PHONE_CHAT.time, from_ethernet=True)
        self.assertEqual(received, PHONE_CHAT)
        self.assertEqual(received.text, "opened")
        self.assertFalse(position.select(175, PHONE_CHAT.time + timedelta(seconds=75)).phone_present)

    def test_commands_and_opened_text_do_not_acknowledge_or_change_position(self):
        position = self.position(warning_read_timeout_s=10)
        self.selected(position, radio_gps=self.gps(state="GNSS_SUSPECTED"))
        self.ack(position, self.event(position, "gps_untrusted"), 0, "warning-1")
        self.ack(position, self.event(position, "gps_untrusted", "audio"))
        before = position.outbound_events
        for second, text in enumerate(("opened", "ack", "restore GPS", "set location 1,2", "shutdown"), 1):
            packet = self.chat(second, text=text)
            self.assertEqual(position.feed_chat(packet, 100 + second, utc(second), from_ethernet=True), packet)
            self.assertEqual(position.outbound_events, before)
        chosen = self.selected(position, 10, radio_gps=self.gps(10))
        self.assertIsNone(chosen.fix)
        self.assertFalse(chosen.position_trusted)
        self.assertEqual(chosen.retire_marker_uids, ())
        self.assertIsNotNone(self.event(position, "gps_untrusted", "audio"))

    def test_ingress_identity_destination_and_time_gates(self):
        position = self.position()
        for fields, expected in (({"sender_uid": "other"}, "uid_mismatch"),
                                 ({"destination_uid": "other"}, "wrong_destination"),
                                 ({"time": utc(-100)}, "stale"), ({"stale": utc(1)}, "stale"),
                                 ({"time": utc(7)}, "future"), ({"start": utc(7)}, "future"),
                                 ({"remarks_time": utc(7)}, "future")):
            self.assertEqual(position.feed_chat(self.chat(**fields), 101, utc(1), from_ethernet=True), expected)
        self.assertEqual(position.feed_chat(self.chat(), 101, utc(1), from_ethernet=False), "wrong_ingress")
        self.assertEqual(position.outbound_events, ())
        for packet, ingress in (("bad", True), (self.chat(), 1)):
            with self.assertRaises(ValueError):
                position.feed_chat(packet, 101, utc(1), from_ethernet=ingress)


class PhoneClockTests(unittest.TestCase):
    def assert_stamps(self, position, mono, radio_time, expected):
        stamp = position.outbound_time(mono, radio_time)
        self.assertEqual(stamp, expected)
        packets = (
            (hidden_contact("radio-1", "Radio", ("192.0.2.1", 4242), stamp), 3600),
            (external_position("radio-1", Fix(40, -105, "gnss"), stamp), 10),
            (geochat_report("radio-1", "Radio", GPS.uid, "status", "msg-1", stamp), 86400),
            (retire_marker("old-point", "delete-1", stamp), 60),
        )
        for packet, lifetime in packets:
            root = ET.fromstring(packet)
            for attr, seconds in (("time", 0), ("start", 0), ("stale", lifetime)):
                actual = datetime.fromisoformat(root.get(attr).replace("Z", "+00:00"))
                self.assertEqual(actual, expected + timedelta(seconds=seconds))
            remarks = root.find("detail/remarks")
            if remarks is not None and remarks.get("time") is not None:
                self.assertEqual(remarks.get("time"), root.get("time"))

    def full_flow(self, skew):
        position = PhonePosition(radio_uid="radio-1")
        radio_time = lambda second: utc(second + skew(second))
        receive_sa = lambda packet, second: position.feed(
            packet, 100 + second, radio_time(second), from_ethernet=True)
        receive_mark = lambda packet, second: position.feed_marker(
            packet, 100 + second, radio_time(second), from_ethernet=True)
        receive_receipt = lambda packet, second: position.feed_receipt(
            packet, 100 + second, radio_time(second), from_ethernet=True)
        receive_chat = lambda packet, second: position.feed_chat(
            packet, 100 + second, radio_time(second), from_ethernet=True)
        self.assert_stamps(position, 100, radio_time(0), radio_time(0))
        self.assertEqual(receive_sa(sa(), 0).status, "accepted_presence")
        self.assertEqual(position.phone_now(100), utc())
        gps_fix = Fix(40, -105, "gnss")
        selected = position.select(101, radio_time(1), radio_gps=RadioGPS(gps_fix, 101))
        self.assertTrue(selected.own_gnss_selected)
        position.position_sent(selected.fix, 101, radio_time(1))
        self.assert_stamps(position, 101, radio_time(1), utc(1))
        receive_sa(sa(seconds=2), 2)  # Live feed plus phone GPS warns.
        warning = next(e for e in position.outbound_events if e.kind == "geochat")
        self.assertTrue(position.ack_event(warning.event_id, 103, radio_time(3), message_id="warning-1"))
        receipt = ChatReceipt("warning-1", "delivered", GPS.uid, "radio-1",
                              utc(4), utc(4), utc(100))
        for fields, reason in (({"time": utc(-3)}, "predates_message"),
                               ({"time": utc(-100)}, "stale"),
                               ({"stale": utc(4)}, "stale"),
                               ({"time": utc(10)}, "future"),
                               ({"start": utc(10)}, "future")):
            self.assertEqual(receive_receipt(replace(receipt, **fields), 4), reason)
        self.assertEqual(receive_receipt(receipt, 4), "delivered")
        reply = replace(PHONE_CHAT, destination_uid="radio-1", time=utc(4),
                        start=utc(4), stale=utc(100), remarks_time=utc(4))
        self.assertEqual(receive_chat(reply, 4), reply)
        for fields, reason in (({"time": utc(-100)}, "stale"),
                               ({"stale": utc(4)}, "stale"),
                               ({"time": utc(10)}, "future"),
                               ({"start": utc(10)}, "future"),
                               ({"remarks_time": utc(10)}, "future")):
            self.assertEqual(receive_chat(replace(reply, **fields), 4), reason)
        self.assertEqual(receive_receipt(replace(receipt, status="read", time=utc(5), start=utc(5)), 5), "read")
        # Drag after actual feed expiry; then explicit USER selects the last fed point.
        self.assertEqual(receive_sa(sa("manual", 12, lat=40.1, lon=-105), 12).status, "accepted_manual")
        user = sa("manual", 13, geopointsrc="USER", lat=40, lon=-105)
        self.assertEqual(receive_sa(user, 13).status, "accepted_manual")
        selected = position.select(113, radio_time(13), radio_gps=RadioGPS(None, 113, state="GNSS_SUSPECTED"))
        self.assertEqual(selected.location.observation_time, utc(13))
        self.assertFalse(selected.position_trusted)
        self.assertTrue(any(e.code == "gps_untrusted" and e.kind == "audio" for e in position.outbound_events))
        self.assertEqual(receive_mark(mark(created=20), 14).reason, "future_creator_time")
        # Expired Send can supply a persistent landmark, but cannot acknowledge.
        self.assertEqual(receive_mark(mark("A", 14, stale=utc(14)), 14).status, "accepted")
        self.assertTrue(any(e.code == "gps_untrusted" and e.kind == "audio" for e in position.outbound_events))
        self.assertEqual(receive_mark(mark("B", 15), 15).status, "accepted")
        self.assertFalse(any(e.code == "gps_untrusted" and e.kind == "audio" for e in position.outbound_events))
        selected = position.select(116, radio_time(16))
        self.assertEqual(selected.location.marker_uid, "B")
        self.assertEqual(selected.location.age_s, 1)
        self.assertEqual(selected.retire_marker_uids, ("A",))
        self.assertTrue(selected.phone_present)
        self.assertFalse(selected.position_trusted)
        self.assert_stamps(position, 116, radio_time(16), utc(16))
        self.assertIsNone(position.clock_diagnostic)
        self.assertFalse(position.select(188, radio_time(88)).phone_present)

    def test_full_flow_with_arbitrary_radio_offsets(self):
        for offset in (0, -30, 30, -600, 600, -86400, 86400):
            with self.subTest(offset=offset):
                self.full_flow(lambda second: offset)

    def test_full_flow_through_radio_utc_steps(self):
        self.full_flow(lambda second: 600 if second < 3 else
                       -86400 if second < 12 else 86400 if second < 15 else -600)

    def test_least_delay_refinement_preserves_phase_and_shortens_presence(self):
        position = PhonePosition()
        feed(position, sa(seconds=-3))  # First sample arrives three seconds late.
        self.assertEqual(position.phone_now(110), utc(7))
        feed(position, sa(seconds=9), 10)  # One second late: improve the lower bound.
        self.assertEqual(position.phone_now(120), utc(19))
        feed(position, sa(seconds=17), 20)  # Three seconds late: do not regress.
        self.assertEqual(position.phone_now(130), utc(29))
        self.assertIsNone(position.clock_diagnostic)
        self.assertTrue(position.select(192.99, utc(86400)).phone_present)
        self.assertFalse(position.select(193, utc(-86400)).phone_present)

    def test_invalid_or_foreign_sa_cannot_establish_or_rebase_clock(self):
        position = PhonePosition(radio_uid="radio-1")
        for packet, ingress, reason in ((sa(seconds=600), False, "wrong_ingress"),
                                        (sa(seconds=600, uid="radio-1"), True, "radio_uid"),
                                        (sa(seconds=600, lifetime=0), True, "stale")):
            self.assertEqual(position.feed(packet, 100, utc(), from_ethernet=ingress).status, reason)
            self.assertIsNone(position.phone_now(100))
        feed(position)
        for packet, ingress, reason in ((sa(seconds=600), False, "wrong_ingress"),
                                        (sa(seconds=600, uid="other"), True, "uid_mismatch"),
                                        (sa(seconds=600, lifetime=0), True, "stale"),
                                        (replace(sa(seconds=600), start=utc(606)), True, "future")):
            self.assertEqual(position.feed(packet, 101, utc(), from_ethernet=ingress).status, reason)
            self.assertEqual(position.phone_now(101), utc(1))
            self.assertIsNone(position.clock_diagnostic)

    def test_forward_and_backward_steps_keep_landmark_age_and_position_distrust(self):
        for offset in (600, -600):
            with self.subTest(offset=offset):
                position = PhonePosition(radio_uid="radio-1")
                feed(position)
                send(position, mark(created=1), 1)
                before = position.select(101, utc(1), radio_gps=RadioGPS(None, 101, state="GNSS_SUSPECTED"))
                events = position.outbound_events
                update = feed(position, sa(seconds=10 + offset), 10)
                if offset < 0:
                    self.assertEqual(update.status, "replayed_or_out_of_order")
                    self.assertEqual(position.phone_now(174.9), utc(74.9))
                    self.assertTrue(position.select(174.9, utc()).phone_present)
                    at = 75
                    update = feed(position, sa(seconds=at + offset), at)
                    self.assertTrue(update.reappeared)
                else:
                    at = 10
                    self.assertFalse(update.reappeared)
                self.assertEqual(update.status, "accepted_presence")
                self.assertEqual(update.clock_diagnostic.delta_s, offset)
                self.assertEqual(update.clock_diagnostic.reason, "forward_step" if offset > 0 else "backward_step")
                self.assertEqual(position.clock_diagnostic, update.clock_diagnostic)
                self.assertEqual(position.outbound_events, events)  # Diagnostics produce no feedback.
                after = position.select(100 + at, utc(-86400))
                self.assertFalse(after.position_trusted)
                self.assertEqual(after.location.observation_time, before.location.observation_time)
                self.assertEqual(after.location.age_s, at - 1)
                self.assertEqual(after.location.fix.lat, before.location.fix.lat)
                self.assert_stamps(position, 100 + at, utc(-86400), utc(at + offset))
                # Previously admitted SA stays a replay, both while live and after lapse.
                for elapsed in (at + 1, at + 75):
                    self.assertEqual(feed(position, sa(), elapsed).status, "replayed_or_out_of_order")
                    self.assertEqual(position.phone_now(100 + elapsed), utc(elapsed + offset))
                    self.assertEqual(position.clock_diagnostic, update.clock_diagnostic)
                self.assertFalse(position.select(100 + at + 75, utc()).phone_present)

    def test_backward_phase_step_with_numerically_newer_sa_rebases_immediately(self):
        position = PhonePosition()
        feed(position)
        update = feed(position, sa(seconds=40), 60)  # Clock lost 20 s but still newer than first SA.
        self.assertEqual(update.status, "accepted_presence")
        self.assertFalse(update.reappeared)
        self.assertEqual(update.clock_diagnostic.delta_s, -20)
        self.assertEqual(position.phone_now(161), utc(41))
        self.assertEqual(feed(position, sa(), 61).status, "replayed_or_out_of_order")

    def test_steps_convert_send_epochs_for_receipts_drag_and_new_mark_order(self):
        for offset in (600, -600):
            with self.subTest(offset=offset):
                position = PhonePosition(radio_uid="radio-1")
                feed(position)
                selected = position.select(100, utc())
                # An old landmark is held across a clock rebase.
                send(position, mark(created=1), 1)
                position.position_sent(Fix(40, -105, "gnss"), 101, utc(86400))
                for event in selected.outbound_events:
                    position.ack_event(event.event_id, 120, utc(86400),
                                       message_id="warning-1" if event.kind == "geochat" else None)
                at = 30 if offset > 0 else 75
                feed(position, sa(seconds=at + offset), at)
                receipt = ChatReceipt("warning-1", "read", GPS.uid, "radio-1",
                                      utc(offset + 14), utc(offset + 14), utc(offset + at + 75))
                # Send at mono=120 maps to phone date offset+20, regardless of old radio UTC.
                self.assertEqual(position.feed_receipt(receipt, 100 + at, utc(-86400), from_ethernet=True), "predates_message")
                receipt = replace(receipt, time=utc(offset + at), start=utc(offset + at))
                self.assertEqual(position.feed_receipt(receipt, 100 + at, utc(-86400), from_ethernet=True), "read")
                at += 2  # Feed has expired; post-step USER action must pass the old send guard.
                update = feed(position, sa("manual", at + offset, geopointsrc="USER", lat=40, lon=-105), at)
                self.assertEqual(update.status, "accepted_manual")
                self.assertEqual(current(position, at).observation_time, utc(at + offset))
                self.assertEqual(send(position, mark("post-step", at + offset + 1), at + 1).status, "accepted")
                self.assertEqual(current(position, at + 1).marker_uid, "post-step")

    def test_replay_history_survives_opposite_steps_and_is_bounded(self):
        position = PhonePosition()
        feed(position)
        feed(position, sa(seconds=601), 1)
        feed(position, sa(seconds=-524), 76)
        for packet in (sa(), sa(seconds=601)):
            self.assertEqual(feed(position, packet, 77).status, "replayed_or_out_of_order")
        self.assertEqual(position.phone_now(177), utc(-523))
        for index in range(PHONE_SA_HISTORY + 10):
            feed(position, sa(seconds=-522 + index), 78 + index)
        self.assertEqual(len(position._seen_sa_times), PHONE_SA_HISTORY)


if __name__ == "__main__":
    unittest.main()
