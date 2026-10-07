"""R2 protocol/state-machine tests, simulated radio and optional real bench traces."""
from dataclasses import replace
import importlib.util
from pathlib import Path
import statistics
import sys
import unittest
from unittest import mock

import manet_ranging as mr
import ranging_pb2 as pb

A = mr.Peer("A", bytes.fromhex("020000000001"))
B = mr.Peer("B", bytes.fromhex("020000000002"))
C = mr.Peer("C", bytes.fromhex("020000000003"))
CAL = mr.Calibration.measured_20mhz(6500)


def fix(age=10):
    return pb.Position(source=pb.Position.GNSS, valid=True,
                       latitude_e7=397392000, longitude_e7=-1049903000,
                       altitude_cm=160000, fix_time_unix_ms=1791331200000,
                       fix_age_ms=age, horizontal_uncertainty_cm=200,
                       vertical_uncertainty_cm=400)


def envelope(kind, body, sid=b"S" * 16, challenge=b"", position=None):
    message = pb.Envelope(version=1, session_id=sid, challenge=challenge)
    getattr(message, kind).CopyFrom(body)
    if position is not None:
        message.position.CopyFrom(position)
    return message


def request(sid=b"S" * 16, frames=50, width=20):
    return mr.encode(envelope("request", pb.Request(frames=frames, interval_ms=50,
                                                     channel_width_mhz=width), sid))


def sample_rows(n=50, lost_frames=(), lost_acks=(), start=10000000000, difference=7300,
                schedule_us=None):
    local, remote = [], []
    # The counters differ with packet/ACK loss; UDP sequence must NOT replace them.
    for seq in range(n):
        elapsed = seq * 200000000 if schedule_us is None else schedule_us[seq] * 4000
        departure = (start + elapsed) % mr.CLOCK_MODULUS
        arrival = (departure + 999999 + seq * 20) % mr.CLOCK_MODULUS
        if seq in lost_frames:
            continue
        ri = len(remote) + 1
        remote.append(mr.ResponderStamp(seq, arrival,
                      (arrival + 60000 - 2454 * ri) % mr.CLOCK_MODULUS, ri))
        if seq in lost_acks:
            continue
        ii = len(local) + 1
        local.append(mr.InitiatorStamp(seq, departure,
                     (departure + 60000 + difference + 2454 * ii) % mr.CLOCK_MODULUS, ii))
    return local, remote


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class SimRadio:
    def __init__(self):
        self.calls, self.local, self.remote, self.frames = [], [], [], ()
        self.fail = set()
        self.armed = False
        self.ack = "original"

    def check(self, name):
        self.calls.append(name)
        if name in self.fail:
            raise OSError(name)

    def arm_responder(self, peer, sid, width):
        self.armed, self.ack = True, "chain2"  # Simulate a partially failed setup too.
        self.check("arm")

    def disarm(self):
        self.check("disarm")
        self.armed = False

    def restore_ack(self):
        self.check("restore")
        self.ack = "original"

    def send_burst(self, peer, frames, width, interval_ms):
        self.frames = frames
        message = mr.decode(frames[0])
        self.schedule_us = mr.burst_schedule_us(message.session_id, message.challenge,
                                                len(frames), interval_ms)
        self.check("send")

    def read_responder_report(self, sid):
        self.check("read_responder")
        return self.remote

    def read_initiator_timestamps(self, sid):
        self.check("read_initiator")
        return self.local


class Rig:
    def __init__(self, config=mr.Config()):
        self.clock = Clock()
        self.ar, self.br = SimRadio(), SimRadio()
        self.counter = 0
        self.current_fix = fix()
        self.a = self.core(self.ar, config)
        self.b = self.core(self.br, config)

    def nonce(self):
        self.counter += 1
        return self.counter.to_bytes(16, "big")

    def core(self, radio, config):
        return mr.Ranging(radio, self.clock, lambda: self.current_fix, CAL,
                          authorize=lambda peer: peer in (A, B, C),
                          eligible=lambda peer: True, nonce=self.nonce, config=config)

    def ready(self):
        self.a.start([B])
        req = self.a.outbox.popleft().data
        self.b.receive(A, req)
        ready = self.b.outbox.popleft().data
        self.a.receive(B, ready)
        return req, ready

    def burst(self, lost_frames=(), lost_acks=(), *, finished=True):
        self.ar.local, self.br.remote = sample_rows(
            len(self.ar.frames), lost_frames, lost_acks, schedule_us=self.ar.schedule_us)
        sid = self.a.active.session_id
        for seq, frame in enumerate(self.ar.frames):
            if seq:
                self.clock.advance((self.ar.schedule_us[seq] - self.ar.schedule_us[seq - 1]) / 1e6)
            if seq not in lost_frames:
                self.b.receive(A, frame, direct=True)
        if finished:
            self.a.burst_finished(sid)
        return sid

    def reports(self, wait=.11):
        self.clock.advance(wait)
        self.b.tick()
        messages = [item.data for item in self.b.outbox]
        self.b.outbox.clear()
        return messages

    def deliver(self, reports):
        for report in reports:
            self.a.receive(B, report)


class ScheduleTests(unittest.TestCase):
    def test_portable_vector_bounds_and_both_nonces_affect_schedule(self):
        sid, challenge = b"S" * 16, b"C" * 16
        schedule = mr.burst_schedule_us(sid, challenge, 50, 50)
        self.assertEqual(schedule[:6], (0, 45557, 88511, 146186, 190468, 231647))
        self.assertEqual(schedule[-1], 2417293)
        self.assertNotEqual(schedule, mr.burst_schedule_us(b"T" * 16, challenge, 50, 50))
        self.assertNotEqual(schedule, mr.burst_schedule_us(sid, b"D" * 16, 50, 50))
        for nominal in (20, 50, 100):
            for frames in (1, 2, 50, 64):
                offsets = mr.burst_schedule_us(sid, challenge, frames, nominal)
                self.assertEqual((len(offsets), offsets[0]), (frames, 0))
                gaps = [y - x for x, y in zip(offsets, offsets[1:])]
                self.assertTrue(all(max(20000, nominal * 800) <= g <= min(100000, nominal * 1200)
                                    for g in gaps))
        for args in ((b"", challenge, 50, 50), (sid, bytes(16), 50, 50),
                     (sid, challenge, 65, 50), (sid, challenge, 50, 101)):
            with self.assertRaises(ValueError):
                mr.burst_schedule_us(*args)

    def test_roles_and_radio_share_schedule_and_lease_covers_preparation_and_drain(self):
        for nominal in (20, 50, 100):
            rig = Rig(mr.Config(frames=64, interval_ms=nominal))
            _, data = rig.ready()
            self.assertEqual(rig.a.active.schedule_us, rig.b.active.schedule_us)
            self.assertEqual(rig.ar.schedule_us, rig.b.active.schedule_us)
            duration = rig.ar.schedule_us[-1] / 1e6
            ready = mr.decode(data)
            self.assertGreaterEqual(ready.ready.arm_timeout_ms / 1000, duration + 1.5)
            # Default helper needs 1 s before frame zero, plus final .1 s drain.
            rig.clock.advance(1)
            rig.burst()
            self.assertLess(rig.clock.now + rig.b.config.settle_s, rig.b.active.deadline)
            rig.deliver(rig.reports())
            self.assertEqual(rig.a.results[-1].status, "ok")

    def test_ready_timeout_bounds_use_jitter_duration(self):
        for extra in (99, 3001):
            rig = Rig(mr.Config(frames=64, interval_ms=20))
            rig.a.start([B])
            rig.b.receive(A, rig.a.outbox.popleft().data)
            ready = mr.decode(rig.b.outbox.popleft().data)
            duration_ms = (rig.b.active.schedule_us[-1] + 999) // 1000
            ready.ready.arm_timeout_ms = duration_ms + extra
            rig.a.receive(B, mr.encode(ready))
            self.assertEqual(rig.a.results[-1].status, "invalid_ready")
            self.assertNotIn("send", rig.ar.calls)


class MessagesTests(unittest.TestCase):
    def test_roundtrip_every_message(self):
        bodies = dict(request=pb.Request(frames=50, channel_width_mhz=20, interval_ms=50),
                      ready=pb.Ready(arm_timeout_ms=3450), busy=pb.Busy(retry_ms=5000),
                      reject=pb.Reject(reason=pb.Reject.UNSUPPORTED),
                      burst=pb.Burst(sequence=49, done=True),
                      report=pb.Report(parts=1, base_arrival=10000, sequence=[1, 3],
                                       arrival_delta=[0, 400000000], turnaround=[60000, 60001]),
                      cancel=pb.Cancel(reason=pb.Cancel.TIMEOUT),
                      result=pb.Result(range_m=29.97, p25_counts=7300, pair_count=50,
                                       initiator_position=fix()))
        for kind, body in bodies.items():
            with self.subTest(kind=kind):
                message = envelope(kind, body, challenge=b"C" * 16,
                                   position=None if kind in ("request", "burst", "result") else fix())
                self.assertEqual(mr.decode(mr.encode(message)), message)

    def test_optional_height_accuracy_and_time_stay_unknown(self):
        pos = pb.Position(source=pb.Position.MANUAL, valid=True, fix_age_ms=0)
        parsed = mr.decode(mr.encode(envelope("ready", pb.Ready(), position=pos))).position
        for key in ("altitude_cm", "horizontal_uncertainty_cm", "vertical_uncertainty_cm",
                    "fix_time_unix_ms"):
            self.assertFalse(parsed.HasField(key))
        self.assertTrue(parsed.HasField("fix_age_ms"))

    def test_position_validation_and_ancestry(self):
        pos = fix()
        pos.source, pos.generation = pb.Position.RANGED, 3
        pos.used_node_ids.extend(["node-1", "node-2"])
        mr.validate_position(pos)
        for change in (lambda p: p.used_node_ids.append("node-1"),
                       lambda p: p.used_node_ids.extend(["x"] * 17),
                       lambda p: setattr(p, "source", pb.Position.GNSS),
                       lambda p: setattr(p, "latitude_e7", 900000001),
                       lambda p: p.ClearField("fix_age_ms")):
            copy = pb.Position()
            copy.CopyFrom(pos)
            change(copy)
            with self.assertRaises(ValueError):
                mr.validate_position(copy)

    def test_bad_envelopes(self):
        for payload in (b"", b"x" * 1201, b"\xff", b"\x0a\xff\xff\xff\xff\x7f"):
            with self.assertRaises(ValueError):
                mr.decode(payload)
        for change in (lambda m: setattr(m, "version", 2),
                       lambda m: setattr(m, "session_id", b"short"),
                       lambda m: setattr(m, "session_id", bytes(16)),
                       lambda m: setattr(m, "challenge", b"short"),
                       lambda m: m.ClearField("request")):
            message = mr.decode(request())
            change(message)
            with self.assertRaises(ValueError):
                mr.decode(mr.encode(message))

    def test_maximum_report_fits_datagram(self):
        pos = pb.Position(source=pb.Position.RANGED, valid=True, latitude_e7=-900000000,
                          longitude_e7=-1800000000, altitude_cm=-(1 << 31),
                          fix_time_unix_ms=(1 << 64) - 1, fix_age_ms=(1 << 64) - 1,
                          horizontal_uncertainty_cm=(1 << 32) - 1,
                          vertical_uncertainty_cm=(1 << 32) - 1, generation=(1 << 32) - 1,
                          used_node_ids=[f"{i:032d}" for i in range(16)])
        report = pb.Report(part=2, parts=3, base_arrival=mr.CLOCK_MODULUS - 1,
                           sequence=list(range(40, 64)),
                           arrival_delta=[mr.CLOCK_MODULUS // 2 - 1] * 24,
                           turnaround=[(1 << 31) - 1] * 24)
        self.assertLessEqual(len(mr.encode(envelope("report", report, challenge=b"C" * 16,
                                                   position=pos))), mr.MAX_DATAGRAM)
        claim = pb.Result(range_m=1e100, p25_counts=(1 << 31) - 1,
                          pair_count=64, initiator_position=pos)
        self.assertLessEqual(len(mr.encode(envelope("result", claim, challenge=b"C" * 16))),
                             mr.MAX_DATAGRAM)


class PairingTests(unittest.TestCase):
    def test_loss_counter_correction_wrap_and_duplicates(self):
        for start in (10000000000, mr.CLOCK_MODULUS - 200000000):
            local, raw = sample_rows(lost_frames=(0, 3, 9), lost_acks=(2, 7, 20), start=start)
            remote = mr.corrected_responder(raw + raw[:1], CAL)
            result = mr.pair_timestamps(local + local[:1], remote, CAL)
            self.assertAlmostEqual(result.range_m, 800 / 26.69)
            self.assertEqual(result.matched, 44)
            self.assertTrue(all(value == 7300 for _, value in result.differences))

    def test_no_ack_and_ambiguous_retransmission_excluded(self):
        local, raw = sample_rows()
        local[0] = replace(local[0], ack_arrival=None)
        local.append(replace(local[1], ack_arrival=local[1].ack_arrival + 1))
        result = mr.pair_timestamps(local, mr.corrected_responder(raw, CAL), CAL)
        self.assertEqual(result.matched, 48)

    def test_tracked_offset_rejects_wrong_time_even_with_correct_sequence(self):
        local, raw = sample_rows()
        remote = mr.corrected_responder(raw, CAL)
        remote[0] = replace(remote[0], arrival=remote[0].arrival + 500000)
        remote[20] = replace(remote[20], arrival=remote[20].arrival + 500000)
        result = mr.pair_timestamps(local, remote, CAL)
        self.assertEqual(result.matched, 48)

    def test_consecutive_losses_with_measured_drift_and_crystal_margin(self):
        # Trace medians reach 8.4 ppm; largest apparent slope is 29.7 ppm.
        # Also check the review's 20 ppm example and the 40 ppm design margin.
        lost_frames, lost_acks = tuple(range(5, 13)), tuple(range(21, 28))
        for ppm in (8.4, 20, 29.7, 40):
            for interval_ms in (20, 50, 100):
                with self.subTest(ppm=ppm, interval_ms=interval_ms):
                    start = mr.CLOCK_MODULUS - 200000000
                    local, raw = sample_rows(lost_frames=lost_frames, lost_acks=lost_acks,
                                             start=start)
                    # Rewrite the hardware schedule and drift while preserving
                    # each timestamp's RTT/turnaround and original counter.
                    def reschedule(row):
                        shift = row.sequence * (interval_ms - 50) * 4000000
                        return replace(row, departure=(row.departure + shift) % mr.CLOCK_MODULUS,
                                       ack_arrival=(row.ack_arrival + shift) % mr.CLOCK_MODULUS)
                    local = [reschedule(row) for row in local]
                    remote = []
                    for row in raw:
                        drift = round(row.sequence * interval_ms * 4000000 * ppm / 1e6)
                        shift = row.sequence * (interval_ms - 50) * 4000000 + drift - row.sequence * 20
                        remote.append(replace(row, arrival=(row.arrival + shift) % mr.CLOCK_MODULUS,
                                              ack_departure=(row.ack_departure + shift) % mr.CLOCK_MODULUS))
                    result = mr.pair_timestamps(local, mr.corrected_responder(remote, CAL), CAL)
                    self.assertEqual(result.matched, 35)
                    self.assertAlmostEqual(result.range_m, 800 / 26.69)

    def test_drift_allowance_does_not_accept_bad_time_or_out_of_order_clock(self):
        local, raw = sample_rows()
        remote = mr.corrected_responder(raw, CAL)
        remote[20] = replace(remote[20], arrival=remote[20].arrival + 500000)
        local[30] = replace(local[30], departure=local[0].departure,
                            ack_arrival=local[0].departure + 60000 + 7300 + 2454 * 31)
        result = mr.pair_timestamps(local, remote, CAL)
        self.assertEqual(result.matched, 48)
        for ppm in (-1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                mr.pair_timestamps(local, remote, CAL, max_drift_ppm=ppm)

    def test_bench_percentile_and_step_slip_filter(self):
        local, raw = sample_rows()
        # 25% low, 75% long. Rank floor(.25*n), not interpolated quantile.
        local = [replace(row, ack_arrival=row.ack_arrival + (100 if i >= 12 else 0))
                 for i, row in enumerate(local)]
        local[-1] = replace(local[-1], ack_arrival=local[-1].ack_arrival + 2454)
        result = mr.pair_timestamps(local, mr.corrected_responder(raw, CAL), CAL)
        self.assertEqual(result.percentile_counts, 7400)
        self.assertEqual(result.rejected, 1)

    def test_calibration_explicit_for_other_widths_and_negative_retained(self):
        local, raw = sample_rows()
        result = mr.pair_timestamps(local, mr.corrected_responder(raw, CAL),
                                    replace(CAL, constant_counts=7500))
        self.assertLess(result.range_m, 0)
        with self.assertRaises(ValueError):
            mr.Calibration(80, float("nan"), 0, 750)
        # Construct independent 80 MHz samples to prove no hidden 20 MHz correction.
        wide = mr.Calibration(80, 100, 20, 750)
        local = [mr.InitiatorStamp(i, i * 100000, i * 100000 + 10000 + 750 * (i + 1), i + 1)
                 for i in range(20)]
        raw = [mr.ResponderStamp(i, i * 100000 + 42,
                                i * 100000 + 42 + 9000 - 750 * (i + 1), i + 1)
               for i in range(10)]
        self.assertAlmostEqual(mr.pair_timestamps(local, mr.corrected_responder(raw, wide), wide).range_m, 9.8)

    def test_empty_and_insufficient_pairs_fail(self):
        for n in (0, 3):
            local, raw = sample_rows(n)
            with self.assertRaises(ValueError):
                mr.pair_timestamps(local, mr.corrected_responder(raw, CAL), CAL)


class StateMachineTests(unittest.TestCase):
    def assert_clean(self, radio):
        self.assertFalse(radio.armed)
        self.assertEqual(radio.ack, "original")
        self.assertEqual(radio.calls[-2:], ["disarm", "restore"])

    def test_both_roles_success_and_fresh_position_each_reply(self):
        rig = Rig()
        rig.ready()
        rig.current_fix = fix(77)
        rig.burst()
        reports = rig.reports()
        self.assertEqual(len(reports), 3)
        self.assert_clean(rig.br)  # Released BEFORE the report is delivered.
        rig.deliver(reports)
        result = rig.a.results.popleft()
        self.assertEqual(result.status, "ok")
        self.assertAlmostEqual(result.estimate.range_m, 800 / 26.69)
        self.assertEqual([p.position.fix_age_ms for p in result.positions], [10, 77, 77, 77])
        self.assert_clean(rig.ar)
        self.assertIsNone(rig.a.active)
        self.assertIsNone(rig.b.active)

    def test_second_initiator_busy_and_duplicate_request_never_rearms(self):
        rig = Rig()
        req, _ = rig.ready()
        original = rig.b.active
        deadline = original.deadline
        rig.b.receive(A, req)
        self.assertFalse(rig.b.outbox)
        rig.b.receive(C, request(b"T" * 16))
        busy = mr.decode(rig.b.outbox.popleft().data)
        self.assertEqual(busy.WhichOneof("body"), "busy")
        self.assertGreater(busy.busy.retry_ms, 0)
        self.assertTrue(busy.HasField("position"))
        self.assertIs(rig.b.active, original)
        self.assertEqual(rig.b.active.deadline, deadline)
        self.assertEqual(rig.br.calls, ["arm"])

    def test_initiator_busy_reject_and_sequential_next_anchor(self):
        for kind, body in (("busy", pb.Busy(retry_ms=12000)),
                           ("reject", pb.Reject(reason=pb.Reject.UNAVAILABLE))):
            rig = Rig()
            rig.a.start([B, C])
            sid = rig.a.active.session_id
            rig.a.receive(B, mr.encode(envelope(kind, body, sid, position=fix())))
            self.assertEqual(rig.a.results[-1].status, kind)
            self.assertIsNone(rig.a.active)
            self.assert_clean(rig.ar)
            rig.a.tick()
            self.assertEqual(rig.a.active.peer, C)
            self.assertNotEqual(rig.a.active.session_id, sid)

    def test_successful_anchors_never_overlap(self):
        rig = Rig()
        rig.ready()
        rig.a.queue.append(C)
        rig.burst()
        rig.deliver(rig.reports())
        self.assertEqual(rig.ar.calls.count("send"), 1)
        self.assertIsNone(rig.a.active)
        rig.a.tick()
        self.assertEqual(rig.a.active.peer, C)
        self.assertEqual(rig.ar.calls.count("send"), 1)

    def test_ready_loss_arm_timeout_and_replay_after_completion(self):
        rig = Rig()
        rig.a.start([B])
        req = rig.a.outbox.popleft().data
        rig.b.receive(A, req)
        rig.b.outbox.clear()  # Ready lost; initiator vanishes without cancel.
        rig.clock.now = rig.b.active.deadline + .001
        rig.b.tick()
        self.assertIsNone(rig.b.active)
        self.assert_clean(rig.br)
        rig.b.outbox.clear()
        rig.b.receive(A, req)
        self.assertFalse(rig.b.outbox)
        self.assertEqual(rig.br.calls.count("arm"), 1)
        rig.a.tick()
        self.assertEqual(rig.a.results[-1].status, "timeout")
        self.assert_clean(rig.ar)

    def test_cancel_each_role_and_before_ready(self):
        for ready in (False, True):
            rig = Rig()
            if ready:
                rig.ready()
            else:
                rig.a.start([B])
                rig.b.receive(A, rig.a.outbox.popleft().data)
            rig.a.cancel()
            rig.b.receive(A, rig.a.outbox.pop().data)
            self.assertIsNone(rig.b.active)
            self.assert_clean(rig.ar)
            self.assert_clean(rig.br)
        rig = Rig()
        rig.ready()
        rig.b.cancel()
        msg = rig.b.outbox.pop().data
        self.assertTrue(mr.decode(msg).HasField("position"))
        rig.a.receive(B, msg)
        self.assertEqual(rig.a.results[-1].status, "cancel")
        self.assert_clean(rig.ar)
        self.assert_clean(rig.br)

    def test_lost_frames_acks_and_final_done_still_produce_partial_report(self):
        rig = Rig()
        rig.ready()
        rig.burst(lost_frames=(0, 3, 49), lost_acks=(2, 7, 20))
        rig.deliver(rig.reports(wait=rig.b.active.deadline - rig.clock.now + .001))
        self.assertEqual(rig.a.results[-1].status, "ok")
        self.assertEqual(rig.a.results[-1].estimate.matched, 44)
        self.assert_clean(rig.br)

    def test_lost_report_or_one_part_times_out(self):
        for deliver_one in (False, True):
            rig = Rig()
            rig.ready()
            rig.burst()
            reports = rig.reports()
            if deliver_one:
                rig.deliver(reports[:1])
            rig.clock.advance(3)
            rig.a.tick()
            self.assertEqual(rig.a.results[-1].status, "timeout")
            self.assert_clean(rig.ar)
            self.assert_clean(rig.br)

    def test_duplicate_and_reordered_reports_wait_for_burst_finished(self):
        rig = Rig()
        _, ready = rig.ready()
        rig.a.receive(B, ready)
        self.assertEqual(rig.ar.calls.count("send"), 1)
        sid = rig.burst(finished=False)
        reports = rig.reports()
        rig.deliver([reports[2], reports[0], reports[0], reports[1]])
        self.assertFalse(rig.a.results)
        rig.a.burst_finished(sid)
        rig.a.burst_finished(sid)
        rig.deliver(reports)
        self.assertEqual(len(rig.a.results), 1)
        self.assertEqual(rig.a.results[-1].status, "ok")
        self.assertEqual(rig.ar.calls.count("read_initiator"), 1)

    def test_stale_replies_wrong_peer_and_wrong_challenge_ignored(self):
        rig = Rig()
        _, ready = rig.ready()
        sid = rig.a.active.session_id
        for field in ("session_id", "challenge"):
            bad = mr.decode(ready)
            setattr(bad, field, b"X" * 16)
            bad.cancel.reason = pb.Cancel.ERROR
            rig.a.receive(B, mr.encode(bad))
        bad = mr.decode(ready)
        bad.cancel.reason = pb.Cancel.ERROR
        rig.a.receive(C, mr.encode(bad))
        rig.a.burst_finished(b"X" * 16)
        rig.a.radio_error(b"X" * 16, "stale")
        self.assertEqual(rig.a.active.session_id, sid)
        self.assertEqual(rig.ar.calls, ["send"])

    def test_previous_session_replies_cannot_start_a_new_burst(self):
        rig = Rig()
        _, ready = rig.ready()
        old_sid = rig.a.active.session_id
        rig.a.cancel()
        rig.clock.advance(6)
        rig.a.start([B])
        new_sid = rig.a.active.session_id
        rig.a.receive(B, ready)
        rig.a.burst_finished(old_sid)
        self.assertNotEqual(new_sid, old_sid)
        self.assertEqual(rig.a.active.state, "waiting_ready")
        self.assertEqual(rig.ar.calls.count("send"), 1)

    def test_burst_requires_direct_ingress_and_nonce(self):
        rig = Rig()
        rig.ready()
        frame = rig.ar.frames[0]
        rig.b.receive(A, frame)  # Routed over bat0, never usable as a probe.
        bad = mr.decode(frame)
        bad.challenge = b"X" * 16
        rig.b.receive(A, mr.encode(bad), direct=True)
        rig.b.receive(C, frame, direct=True)
        self.assertFalse(rig.b.active.observed)
        rig.b.receive(A, frame, direct=True)
        rig.b.receive(A, frame, direct=True)
        self.assertEqual(rig.b.active.observed, {0})

    def test_invalid_or_conflicting_reports_fail_and_cleanup(self):
        for change in (lambda p: p.report.arrival_delta.pop(),
                       lambda p: p.report.sequence.__setitem__(0, 63),
                       lambda p: setattr(p.report, "parts", 99),
                       lambda p: p.report.turnaround.__setitem__(0, -1)):
            rig = Rig()
            rig.ready()
            rig.burst()
            report = mr.decode(rig.reports()[0])
            change(report)
            rig.deliver([mr.encode(report)])
            self.assertEqual(rig.a.results[-1].status, "invalid_report")
            self.assert_clean(rig.ar)
        rig = Rig()
        rig.ready()
        rig.burst()
        reports = rig.reports()
        rig.deliver(reports[:1])
        changed = mr.decode(reports[0])
        changed.report.turnaround[0] += 1
        rig.deliver([mr.encode(changed)])
        self.assertEqual(rig.a.results[-1].status, "invalid_report")

    def test_disarm_and_restore_on_radio_failures(self):
        for failure in ("arm", "send", "read_responder", "read_initiator"):
            with self.subTest(failure=failure):
                rig = Rig()
                failing = rig.br if failure in ("arm", "read_responder") else rig.ar
                failing.fail.add(failure)
                rig.ready()
                if failure in ("read_responder", "read_initiator"):
                    rig.burst()
                    rig.deliver(rig.reports())
                self.assert_clean(failing)
                self.assertIn(failure, failing.calls)
                self.assertIsNone((rig.b if failing is rig.br else rig.a).active)

    def test_invalid_radio_data_and_too_few_pairs_cleanup(self):
        for role in ("initiator", "responder"):
            rig = Rig()
            rig.ready()
            rig.burst()
            if role == "responder":
                rig.br.remote[0] = replace(rig.br.remote[0], report_index=0)
            else:
                rig.ar.local = rig.ar.local[:2]
            rig.deliver(rig.reports())
            self.assertNotEqual(rig.a.results[-1].status, "ok")
            self.assert_clean(rig.ar)
            self.assert_clean(rig.br)

    def test_unobserved_samples_and_all_lost_frames_cannot_be_used(self):
        rig = Rig()
        rig.ready()
        rig.ar.local, rig.br.remote = sample_rows()
        rig.a.burst_finished(rig.a.active.session_id)
        # Hardware data alone is insufficient without matching direct probe events.
        rig.clock.now = rig.b.active.deadline + .001
        rig.b.tick()
        rig.deliver([item.data for item in rig.b.outbox])
        self.assertEqual(rig.a.results[-1].status, "reject")
        self.assert_clean(rig.ar)
        self.assert_clean(rig.br)

    def test_position_provider_error_restores_armed_radio(self):
        rig = Rig()
        rig.ready()
        rig.burst()
        def fail():
            raise OSError("position unavailable")
        rig.b.position = fail
        messages = rig.reports()
        self.assert_clean(rig.br)
        cancel = mr.decode(messages[-1])
        self.assertEqual(cancel.WhichOneof("body"), "cancel")
        self.assertTrue(cancel.HasField("position"))
        self.assertFalse(cancel.position.valid)

    def test_cleanup_failure_attempts_both_and_blocks_radio_until_recovered(self):
        for failure in ("disarm", "restore"):
            rig = Rig()
            rig.ready()
            rig.br.fail.add(failure)
            rig.b.cancel()
            self.assertEqual(rig.br.calls[-2:], ["disarm", "restore"])
            self.assertTrue(rig.b.faulted)
            rig.b.receive(C, request(b"N" * 16))
            self.assertEqual(rig.br.calls.count("arm"), 1)
            self.assertFalse(rig.b.recover())
            rig.br.fail.clear()
            self.assertTrue(rig.b.recover())
            self.assert_clean(rig.br)

    def test_external_error_and_eligibility_loss(self):
        for reason in ("external", "eligibility", "authorization", "close"):
            rig = Rig()
            rig.ready()
            if reason == "external":
                rig.b.radio_error(rig.b.active.session_id, OSError("link went away"))
            elif reason == "eligibility":
                rig.b.eligible = lambda peer: False
                rig.b.tick()
            elif reason == "authorization":
                rig.b.authorize = lambda peer: False
                rig.b.tick()
            else:
                rig.b.close()
            self.assert_clean(rig.br)
            self.assertIsNone(rig.b.active)

    def test_rate_limits_bounded_state_and_busy_retry(self):
        rig = Rig()
        rig.ready()
        rig.b.cancel()
        rig.b.outbox.clear()
        rig.b.receive(A, request(b"N" * 16))
        self.assertFalse(rig.b.outbox)  # Request-response throttle.
        rig.clock.advance(1.1)
        rig.b.receive(A, request(b"N" * 16))
        busy = mr.decode(rig.b.outbox.pop().data)
        self.assertEqual(busy.WhichOneof("body"), "busy")
        rig.clock.advance(busy.busy.retry_ms / 1000 + .01)
        rig.b.receive(A, request(b"N" * 16))
        self.assertEqual(mr.decode(rig.b.outbox.pop().data).WhichOneof("body"), "ready")
        self.assertEqual(rig.br.calls.count("arm"), 2)
        small = Rig(mr.Config(max_peers=1))
        small.ready()
        small.b.receive(C, request())
        self.assertEqual(len(small.b._peers), 1)

    def test_initiator_honours_busy_delay_and_local_cooldown(self):
        rig = Rig()
        rig.a.start([B])
        sid = rig.a.active.session_id
        rig.a.receive(B, mr.encode(envelope("busy", pb.Busy(retry_ms=12000), sid, position=fix())))
        rig.clock.advance(6)
        rig.a.start([B])
        self.assertIsNone(rig.a.active)
        self.assertEqual(rig.a.results[-1].status, "rate_limited")
        self.assertEqual(rig.a.results[-1].retry_ms, 6000)
        rig.clock.advance(6)
        rig.a.start([B])
        self.assertIsNotNone(rig.a.active)

    def test_default_deny_and_unsupported_width(self):
        rig = Rig()
        core = mr.Ranging(rig.br, rig.clock, fix, CAL)
        core.receive(A, request())
        self.assertFalse(rig.br.calls)
        self.assertFalse(core.outbox)
        rig.b.receive(A, request(width=80))
        message = mr.decode(rig.b.outbox.pop().data)
        self.assertEqual(message.reject.reason, pb.Reject.UNSUPPORTED)
        self.assertTrue(message.HasField("position"))
        self.assertFalse(rig.br.calls)

    def test_malformed_datagram_does_not_abort_owner(self):
        rig = Rig()
        rig.ready()
        for data in (b"\xff", b"x" * 1201):
            rig.b.receive(A, data)
        self.assertTrue(rig.br.armed)

    def test_clock_reversal_fails_closed(self):
        rig = Rig()
        rig.ready()
        rig.clock.advance(-1)
        with self.assertRaises(ValueError):
            rig.b.tick()
        self.assert_clean(rig.br)
        self.assertTrue(rig.b.faulted)


class ResultExchangeTests(unittest.TestCase):
    def completed(self):
        rig = Rig()
        rig.ready()
        rig.burst()
        rig.deliver(rig.reports())
        claim = rig.a.outbox.popleft().data
        self.assertEqual(mr.decode(claim).WhichOneof("body"), "result")
        return rig, claim

    def test_both_ends_keep_the_same_two_fixes_and_responder_gets_only_a_claim(self):
        rig = Rig()
        own = fix(123)
        own.source, own.latitude_e7 = pb.Position.MANUAL, 410000000
        own.ClearField("altitude_cm")
        at_burst = pb.Position()
        at_burst.CopyFrom(own)
        rig.a.position = lambda: own
        rig.ready()
        anchor_at_ready = rig.a.active.responder_position
        # Both providers move after the burst was scheduled. Neither canonical
        # fix may be sampled anew when the estimate/claim finally arrives.
        own.latitude_e7 += 100000
        rig.current_fix = fix(500)
        rig.current_fix.latitude_e7 = 420000000
        rig.burst()
        rig.deliver(rig.reports())
        initiator = rig.a.results[-1]
        packet = rig.a.outbox.popleft().data
        message = mr.decode(packet)
        self.assertEqual(message.result.initiator_position, at_burst)
        calls = rig.br.calls[:]
        rig.b.receive(A, packet)
        responder = rig.b.results[-1]
        self.assertEqual(responder.status, "peer_claim")
        self.assertIsNone(responder.estimate)
        self.assertIsNone(initiator.peer_claim)
        self.assertEqual(responder.peer, A)
        self.assertEqual(responder.peer_claim.range_m, initiator.estimate.range_m)
        self.assertEqual(responder.peer_claim.p25_counts, initiator.estimate.percentile_counts)
        self.assertEqual(responder.peer_claim.pair_count, len(initiator.estimate.differences))
        for result in (initiator, responder):
            self.assertEqual(result.initiator_position, at_burst)
            self.assertEqual(result.responder_position, anchor_at_ready)
            self.assertFalse(result.initiator_position.HasField("altitude_cm"))
        self.assertEqual(rig.br.calls, calls)  # No rearm or other radio work.
        self.assertFalse(rig.b.outbox)        # No acknowledgment.

    def test_pair_count_is_retained_count_after_loss_and_outlier_filter(self):
        rig = Rig()
        rig.ready()
        rig.burst(lost_frames=(0, 3), lost_acks=(2, 7))
        rig.ar.local[-1] = replace(rig.ar.local[-1], ack_arrival=rig.ar.local[-1].ack_arrival + 2454)
        rig.deliver(rig.reports())
        packet = rig.a.outbox.popleft().data
        rig.b.receive(A, packet)
        self.assertEqual(rig.b.results[-1].peer_claim.pair_count, 45)

    def test_unknown_fix_and_negative_range_are_preserved(self):
        rig = Rig()
        rig.a.position = lambda: pb.Position(valid=False)
        rig.a.calibration = replace(CAL, constant_counts=7500)
        rig.ready()
        rig.burst()
        rig.deliver(rig.reports())
        rig.b.receive(A, rig.a.outbox.popleft().data)
        result = rig.b.results[-1]
        self.assertEqual(result.initiator_position, pb.Position(valid=False))
        self.assertLess(result.peer_claim.range_m, 0)

    def test_duplicate_claim_is_one_event_even_if_duplicate_changes_values(self):
        rig, packet = self.completed()
        rig.b.receive(A, packet)
        rig.b.receive(A, packet)
        changed = mr.decode(packet)
        changed.result.range_m += 100
        rig.b.receive(A, mr.encode(changed))
        self.assertEqual(len(rig.b.results), 1)

    def test_wrong_peer_session_challenge_and_revoked_authorization_ignored(self):
        rig, packet = self.completed()
        rig.b.receive(C, packet)
        for key in ("session_id", "challenge"):
            bad = mr.decode(packet)
            setattr(bad, key, b"X" * 16)
            rig.b.receive(A, mr.encode(bad))
        rig.b.authorize = lambda peer: False
        rig.b.receive(A, packet)
        self.assertFalse(rig.b.results)
        rig.b.authorize = lambda peer: True
        rig.b.receive(A, packet)
        self.assertEqual(rig.b.results[-1].status, "peer_claim")

    def test_malformed_claims_do_not_consume_slot_or_touch_next_session(self):
        rig, packet = self.completed()
        rig.b.receive(C, request(b"N" * 16))
        current, calls = rig.b.active, rig.br.calls[:]
        mutations = [lambda c: c.ClearField("initiator_position"),
                     lambda c: c.ClearField("range_m"), lambda c: c.ClearField("p25_counts"),
                     lambda c: setattr(c, "pair_count", 0), lambda c: setattr(c, "pair_count", 51),
                     lambda c: setattr(c.initiator_position, "source", pb.Position.UNKNOWN),
                     lambda c: c.initiator_position.ClearField("fix_age_ms"),
                     lambda c: setattr(c.initiator_position, "latitude_e7", 900000001)]
        for name in ("range_m", "p25_counts"):
            for value in (float("nan"), float("inf"), float("-inf")):
                mutations.append(lambda c, n=name, v=value: setattr(c, n, v))
        mutations.append(lambda c: setattr(c, "p25_counts", 1 << 31))
        for mutate in mutations:
            bad = mr.decode(packet)
            mutate(bad.result)
            rig.b.receive(A, mr.encode(bad))
            self.assertFalse(rig.b.results)
            self.assertIs(rig.b.active, current)
            self.assertEqual(rig.br.calls, calls)
        rig.b.receive(A, packet)
        self.assertEqual(rig.b.results[-1].status, "peer_claim")
        self.assertIs(rig.b.active, current)
        self.assertEqual(rig.br.calls, calls)

    def test_lost_and_expired_result_do_not_block_other_sessions(self):
        rig, packet = self.completed()
        self.assertIsNone(rig.a.active)
        self.assertIsNone(rig.b.active)
        rig.b.receive(C, request(b"N" * 16))
        self.assertEqual(mr.decode(rig.b.outbox.popleft().data).WhichOneof("body"), "ready")
        active, calls = rig.b.active, rig.br.calls[:]
        rig.clock.advance(rig.b.config.result_window_s)  # Exact expiry is excluded.
        rig.b.receive(A, packet)
        self.assertFalse(rig.b.results)
        self.assertFalse(rig.b._served)
        self.assertIs(rig.b.active, active)
        self.assertEqual(rig.br.calls, calls)

    def test_no_acceptance_slot_before_report_or_after_cancel_failure_and_close(self):
        for action in ("active", "cancel", "cleanup_failure", "close"):
            with self.subTest(action=action):
                rig = Rig()
                rig.ready()
                sid, challenge = rig.a.active.session_id, rig.a.active.challenge
                packet = mr.encode(envelope("result", pb.Result(
                    range_m=30, p25_counts=7300, pair_count=50, initiator_position=fix()), sid, challenge))
                if action == "cancel":
                    rig.b.cancel()
                elif action == "cleanup_failure":
                    rig.burst()
                    rig.br.fail.add("restore")
                    rig.reports()
                elif action == "close":
                    rig.burst()
                    rig.reports()
                    rig.b.close()
                active, calls = rig.b.active, rig.br.calls[:]
                rig.b.receive(A, packet)
                self.assertFalse(rig.b.results)
                self.assertFalse(rig.b._served)
                self.assertIs(rig.b.active, active)
                self.assertEqual(rig.br.calls, calls)

    def test_initiator_never_sends_result_on_failure(self):
        for action in ("few_pairs", "cleanup_failure", "position_error"):
            with self.subTest(action=action):
                rig = Rig()
                if action == "position_error":
                    def broken():
                        raise OSError("position")
                    rig.a.position = broken
                rig.ready()
                if action != "position_error":
                    rig.burst()
                    if action == "few_pairs":
                        rig.ar.local = rig.ar.local[:2]
                    else:
                        rig.ar.fail.add("restore")
                    rig.deliver(rig.reports())
                self.assertFalse(any(mr.decode(item.data).WhichOneof("body") == "result"
                                     for item in rig.a.outbox))
                self.assertNotEqual(rig.a.results[-1].status, "ok")
                self.assertEqual(rig.ar.calls[-2:], ["disarm", "restore"])

    def test_served_cache_is_bounded_and_only_keeps_latest_session_per_peer(self):
        config = mr.Config(max_peers=2, frames=10, arm_grace_s=.1, settle_s=.01,
                           peer_cooldown_s=.01, request_gap_s=.01, result_window_s=10)
        rig = Rig(config)
        def serve(peer, index):
            sid = index.to_bytes(16, "big")
            rig.b.receive(peer, request(sid, frames=10))
            ready = mr.decode(rig.b.outbox.popleft().data)
            self.assertEqual(ready.WhichOneof("body"), "ready")
            rig.br.remote = sample_rows(10)[1]
            for seq in range(10):
                rig.b.receive(peer, mr.encode(envelope("burst", pb.Burst(sequence=seq, done=seq == 9),
                                                      sid, ready.challenge)), direct=True)
                rig.clock.advance(.05)
            rig.clock.advance(.02)
            rig.b.tick()
            rig.b.outbox.clear()
            rig.clock.advance(.1)
            rig.b.tick()
            return peer, sid
        first = serve(A, 100)
        second = serve(B, 101)
        third = serve(C, 102)
        self.assertEqual(set(rig.b._served), {second, third})
        self.assertNotIn(first, rig.b._served)
        fourth = serve(C, 103)
        self.assertEqual(set(rig.b._served), {second, fourth})


class RecordedBenchTests(unittest.TestCase):
    def test_recorded_pairing_matches_bench_counts_and_estimator(self):
        root = Path(__file__).resolve().parents[2]
        bench = root / "docs/ftm/bench"
        runs = root / "kernel-work/ftm/runs"
        tags = ("wb-1", "sae72a", "n3pace-a")
        if not all((runs / f"{role}-{tag}/trace.txt").exists()
                   for tag in tags for role in ("init", "wmr")):
            self.skipTest("optional local bench captures absent")
        # Keep imports of bench-only scripts out of production and off global sys.path.
        def load_module(name, path):
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        original_path = sys.path[:]
        original_modules = {name: sys.modules.get(name) for name in ("decode5g", "sim_pair")}
        sys.path.insert(0, str(bench))
        try:
            decode5g = load_module("r2_decode5g", bench / "decode5g.py")
            pairing = load_module("r2_sim_pair", bench / "sim_pair.py")
            estimators = load_module("r2_estimators", bench / "estimators.py")
            def load_trace(path):
                # The reference reader leaves its file open; own its lifetime here.
                with path.open() as stream:
                    with mock.patch.object(decode5g, "open", return_value=stream, create=True):
                        return decode5g.load(path)
            for tag in tags:
                with self.subTest(tag=tag):
                    a = load_trace(runs / f"init-{tag}/trace.txt")
                    b = load_trace(runs / f"wmr-{tag}/trace.txt")
                    pairs = pairing.pair(a, b)
                    self.assertGreater(len(pairs), 10)
                    offsets = [mr.clock_delta(b[j][2], a[i][1]) for i, j in pairs]
                    intervals = [mr.clock_delta(a[k][1], a[i][1])
                                 for (i, j), (k, l) in zip(pairs, pairs[1:])]
                    drift_ppm = [mr.clock_delta(y, x) / dt * 1e6
                                 for x, y, dt in zip(offsets, offsets[1:], intervals)]
                    self.assertLess(max(map(abs, drift_ppm)), 40)
                    # Old ping traces have no R2 sequence IDs. Assign them from the
                    # bench time pairing; this checks normalization, not a new driver.
                    pairs = pairs[:50]
                    local = [mr.InitiatorStamp(seq, a[i][1], a[i][2], i + 1)
                             for seq, (i, j) in enumerate(pairs)]
                    remote = [mr.ResponderStamp(seq, b[j][2], b[j][1], j + 1)
                              for seq, (i, j) in enumerate(pairs)]
                    expected = [(a[i][2] - a[i][1] - 2454 * (i + 1)) -
                                (b[j][1] - b[j][2] + 2454 * (j + 1)) for i, j in pairs]
                    median = statistics.median(expected)
                    kept = [value for value in expected if abs(value - median) < 1500]
                    result = mr.pair_timestamps(local, mr.corrected_responder(remote, CAL), CAL)
                    self.assertEqual([value for _, value in result.differences], kept)
                    self.assertEqual(result.percentile_counts, estimators.pct(kept, .25))
                    # Lose different rows on each side without renumbering counters.
                    result = mr.pair_timestamps(local[::2], mr.corrected_responder(remote[1:], CAL), CAL)
                    self.assertGreaterEqual(len(result.differences), 10)
                    # Five consecutive frames lost in the real-clock samples.
                    remaining = [row for row in local if not 10 <= row.sequence < 15]
                    result = mr.pair_timestamps(remaining, mr.corrected_responder(remote, CAL), CAL)
                    expected_loss = [value for seq, value in enumerate(expected) if not 10 <= seq < 15]
                    median = statistics.median(expected_loss)
                    expected_loss = [v for v in expected_loss if abs(v - median) < 1500]
                    self.assertEqual([v for _, v in result.differences], expected_loss)
                    # The old fixed tolerance loses valid pairs across this gap.
                    fixed = mr.pair_timestamps(remaining, mr.corrected_responder(remote, CAL), CAL,
                                               max_drift_ppm=0)
                    self.assertLess(fixed.matched, result.matched)
        finally:
            sys.path[:] = original_path
            for name, module in original_modules.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module


if __name__ == "__main__":
    unittest.main()
