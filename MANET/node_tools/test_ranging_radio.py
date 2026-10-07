"""Offline R3 checks: real trace bytes, fake debugfs/tracefs and fake sockets."""
from dataclasses import replace
import json
from pathlib import Path
import shutil
import socket
import struct
import tempfile
import unittest

import manet_ranging as core
import manet_ranging_radio as radio
import ranging_pb2 as pb

PEER = core.Peer("anchor", bytes.fromhex("000a520dd11b"))
SID, CHALLENGE = b"S" * 16, b"C" * 16
DEVICE = "0000:01:00.0"
# First real init-wb-1 record; small fallback for checkouts without local runs.
GOLDEN = ("napi/phy3-0-9596 [002] b.... 1082.831454: mt7915_rx_tmr: "
          "dev=0000:01:00.0 queue=2 len=40 captured=40 copy_error=0 data="
          "28 00 00 23 03 47 50 83 01 00 3b 17 00 00 00 00 34 ec d7 58 "
          "05 aa e6 58 26 03 26 03 01 08 13 00 10 08 01 01 00 00 00 00\n")


def frames(count=3):
    return tuple(core.encode(pb.Envelope(
        version=1, session_id=SID, challenge=CHALLENGE,
        burst=pb.Burst(sequence=seq, done=seq == count - 1))) for seq in range(count))


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, value):
        self.now += value


class FakeIO(radio.FileIO):
    def __init__(self, trace=GOLDEN):
        self.trace = trace
        self.writes = []
        self.fail = None
        self.fail_after = False
        self.registers = {radio.NORMAL_ROUTE: "0x12345678", 0x1234: "0x42"}
        self.saved_ack = None

    def read(self, path, limit=8192):
        if path.name == "regval":
            index = int(super().read(path.parent / "regidx").strip(), 0)
            return self.registers[index]
        return super().read(path, limit)

    def write(self, path, value):
        self.writes.append((path, value))
        failed = self.fail and self.fail(path, value)
        if failed and not self.fail_after:
            raise OSError("injected " + path.name)
        if path.name == "tmr_peer":
            value = "off 00:00:00:00:00:00 marked=0\n" if value.strip() == "off" else (
                "on " + value.strip() + " marked=0\n")
        elif path.name == "tmr_ack_spe":
            if value.strip() == "restore":
                value = self.saved_ack or super().read(path)
                self.saved_ack = None
            else:
                if self.saved_ack is None:
                    self.saved_ack = super().read(path)
                value = "set " + " ".join([value.strip()] * 4) + "\n"
        elif path.name == "tmr_spe" and value.strip() == "off":
            value = "-1\n"
        elif path.name == "regval":
            self.registers[int(super().read(path.parent / "regidx"), 0)] = value.strip()
        super().write(path, value)
        if path.name == "tracing_on" and value.strip() == "1":
            (path.parent / "trace").write_text(self.trace)
        if failed:
            raise OSError("injected partial " + path.name)

    def mkdir_instance(self, path):
        super().mkdir_instance(path)
        contents = {"tracing_on": "0\n", "current_tracer": "nop\n",
                    "trace_clock": "[local] global mono mono_raw\n", "buffer_size_kb": "1\n",
                    "trace": "", "events/mt7915/mt7915_rx_tmr/enable": "0\n",
                    "events/mt7915/mt7915_rx_tmr/filter": "none\n"}
        for cpu in range(4):
            contents[f"per_cpu/cpu{cpu}/stats"] = "overrun: 0\ncommit overrun: 0\ndropped events: 0\n"
        for name, text in contents.items():
            target = path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)

    def remove_instance(self, path):
        shutil.rmtree(path)  # Only fake virtual files under this test's TemporaryDirectory.


class FakeSocket:
    def __init__(self):
        self.options, self.sent = [], []
        self.blocking, self.bound, self.closed = None, None, False
        self.failure = None

    def setblocking(self, blocking):
        self.blocking = blocking

    def setsockopt(self, level, option, value):
        self.options.append((level, option, value))
        if self.failure == "options":
            raise PermissionError("SO_MARK denied")

    def bind(self, address):
        self.bound = address

    def sendto(self, data, destination):
        if self.failure == "send":
            raise BlockingIOError("queue full")
        self.sent.append((data, destination))
        return len(data)

    def close(self):
        self.closed = True


class Rig:
    def __init__(self, root, *, trace=GOLDEN, diagnostic=True):
        self.root = Path(root)
        self.config = radio.Config("wlan1", "phy3", DEVICE, 49999,
                                   debug_root=self.root / "debug", trace_root=self.root / "trace",
                                   lock_root=self.root / "locks")
        self.debug = self.config.debug_root / self.config.phy / "mt76"
        self.debug.mkdir(parents=True)
        self.config.lock_root.mkdir()
        (self.config.trace_root / "instances").mkdir(parents=True)
        # Global tracing belongs to a different user and must not change.
        (self.config.trace_root / "tracing_on").write_text("1\n")
        (self.config.trace_root / "trace").write_text("OTHER USER\n")
        for name, value in {"tmr_peer": "off 00:00:00:00:00:00 marked=0",
                            "tmr_spe": "-1", "tmr_mark": "0x21", "tmr_rate": "0x01000006",
                            "tmr_ack_spe": "default 25 24 23 22", "tmr_ctrl": "",
                            "tmr_registers": "chip=0x7916 band=1\n0x820f5060=0x0",
                            "regidx": "0x1234", "regval": "0x42"}.items():
            (self.debug / name).write_text(value + "\n")
        self.io, self.clock, self.socket = FakeIO(trace), Clock(), FakeSocket()
        self.link = radio.Link(7, "fe80::1", "fe80::2", PEER.mac, 20)
        self.adapter = radio.RadioAdapter(self.config, lambda peer: self.link,
                                          diagnostic_only=diagnostic, io=self.io, clock=self.clock,
                                          socket_factory=lambda *args: self.socket)

    def send_all(self, count=3):
        self.adapter.send_burst(PEER, frames(count), 20, 50)
        self.clock.advance(.5)
        self.assert_no_event(self.adapter.poll())  # Enable after reset settling.
        self.clock.advance(.5)
        events = ()
        for _ in range(count):
            self.clock.now = self.adapter.next_send
            events += self.adapter.poll()
        return events

    @staticmethod
    def assert_no_event(events):
        assert not events, events


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="r3-radio-")
        self.rig = Rig(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def tearDown(self):
        self.rig.io.fail = None
        self.rig.adapter.close()

    def assert_restored(self):
        rig = self.rig
        self.assertEqual(rig.io.read(rig.debug / "tmr_ack_spe").strip(), "default 25 24 23 22")
        self.assertTrue(rig.io.read(rig.debug / "tmr_peer").startswith("off "))
        self.assertEqual(rig.io.read(rig.debug / "tmr_spe").strip(), "-1")
        self.assertEqual(int(rig.io.read(rig.debug / "tmr_mark"), 0), 0x21)
        self.assertEqual(int(rig.io.read(rig.debug / "tmr_rate"), 0), 0x01000006)
        self.assertEqual(rig.io.registers[radio.NORMAL_ROUTE], "0x12345678")
        self.assertEqual(rig.io.read(rig.debug / "regidx").strip(), "0x1234")
        self.assertIsNone(rig.adapter.capture.instance)
        self.assertFalse(rig.adapter.owned)

    def test_default_mode_refuses_before_any_hardware_or_socket_work(self):
        rig = self.rig
        rig.adapter.diagnostic_only = False
        for action in (lambda: rig.adapter.arm_responder(PEER, SID, 20),
                       lambda: rig.adapter.send_burst(PEER, frames(), 20, 50)):
            with self.assertRaisesRegex(radio.AssociationUnavailable, "WM report"):
                action()
        self.assertFalse(rig.io.writes)
        self.assertFalse(rig.socket.options)
        self.assertFalse(rig.adapter.can_range)
        self.assertFalse(rig.adapter.owned)

    def test_responder_controls_and_independent_ack_restore(self):
        rig = self.rig
        rig.adapter.arm_responder(PEER, SID, 20)
        writes = [(p.name, v.strip()) for p, v in rig.io.writes]
        self.assertIn(("tmr_ctrl", "1 1 2 0 0x22 2 3 3 8 10"), writes)
        self.assertIn(("tmr_ack_spe", "0"), writes)
        self.assertIn(("regval", "0xc003"), writes)
        # The source's tmr_peer is TX-only; do not pretend to RX-filter this peer.
        self.assertNotIn(("tmr_peer", PEER.mac.hex(":")), writes)
        rig.adapter.disarm()
        self.assertTrue(rig.adapter.owned)
        self.assertTrue(rig.io.read(rig.debug / "tmr_ack_spe").startswith("set "))
        rig.adapter.restore_ack()
        self.assert_restored()
        before = rig.io.writes[:]
        rig.adapter.close()
        self.assertEqual(rig.io.writes, before)

    def test_marked_ipv6_sends_are_scoped_nonblocking_and_paced(self):
        rig = self.rig
        events = rig.send_all()
        self.assertEqual(events, (radio.Event("burst_finished", SID),))
        self.assertFalse(rig.socket.blocking)
        self.assertEqual(rig.socket.bound, ("fe80::1", 0, 0, 7))
        self.assertIn((socket.SOL_SOCKET, socket.SO_MARK, 0x77), rig.socket.options)
        self.assertIn((socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"wlan1\0"), rig.socket.options)
        self.assertIn((socket.IPPROTO_IPV6, socket.IPV6_UNICAST_HOPS, 1), rig.socket.options)
        self.assertEqual(rig.socket.sent, [(f, ("fe80::2", 49999, 0, 7)) for f in frames()])
        self.assertEqual(rig.adapter.poll(), ())
        with self.assertRaises(radio.AssociationUnavailable):
            rig.adapter.read_initiator_timestamps(SID)
        rig.adapter.close()
        self.assertTrue(rig.socket.closed)
        self.assert_restored()

    def test_private_trace_does_not_change_global_or_existing_instance(self):
        rig = self.rig
        rig.adapter.arm_responder(PEER, SID, 20)
        instance = rig.adapter.capture.instance
        self.assertEqual(rig.io.read(instance / "events/mt7915/mt7915_rx_tmr/filter").strip(),
                         'device == "0000:01:00.0"')
        self.assertEqual(rig.io.read(rig.config.trace_root / "trace"), "OTHER USER\n")
        self.assertEqual(rig.io.read(rig.config.trace_root / "tracing_on"), "1\n")
        reports = rig.adapter.read_raw_reports(SID)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].departure, (0x0326 << 32) | 0x58D7EC34)
        self.assertEqual(reports[0].arrival, (0x0326 << 32) | 0x58E6AA05)
        with self.assertRaises(radio.AssociationUnavailable):
            rig.adapter.read_responder_report(SID)
        rig.adapter.close()
        existing = rig.config.trace_root / "instances" / ("manet-ranging-" + SID.hex())
        existing.mkdir()
        marker = existing / "owner"
        marker.write_text("someone else")
        with self.assertRaises(FileExistsError):
            rig.adapter.arm_responder(PEER, SID, 20)
        self.assertEqual(marker.read_text(), "someone else")
        self.assert_restored()

    def test_loss_counters_and_missing_stats_invalidate(self):
        rig = self.rig
        for bad in ("overrun: 1\ncommit overrun: 0\ndropped events: 0\n",
                    "overrun: 0\ncommit overrun: 1\ndropped events: 0\n",
                    "overrun: 0\ncommit overrun: 0\ndropped events: 1\n", "overrun: 0\n"):
            rig.adapter.arm_responder(PEER, SID, 20)
            (rig.adapter.capture.instance / "per_cpu/cpu0/stats").write_text(bad)
            with self.assertRaises(radio.CaptureInvalid):
                rig.adapter.read_raw_reports(SID)
            rig.adapter.close()
        rig.adapter.arm_responder(PEER, SID, 20)
        shutil.rmtree(rig.adapter.capture.instance / "per_cpu")
        with self.assertRaises(radio.CaptureInvalid):
            rig.adapter.read_raw_reports(SID)

    def test_lease_cancel_clock_failure_and_channel_event_restore(self):
        rig = self.rig
        for reason in ("lease", "clock", "channel", "close"):
            rig.adapter.arm_responder(PEER, SID, 20)
            if reason == "lease":
                rig.clock.advance(10)
                events = rig.adapter.poll()
            elif reason == "clock":
                rig.clock.advance(-1)
                events = rig.adapter.poll()
                rig.clock.advance(1)
            elif reason == "channel":
                rig.adapter.abort("interface changed to AP")
                events = rig.adapter.poll()
            else:
                rig.adapter.close()
                events = ()
            if reason != "close":
                self.assertEqual(events[0].kind, "radio_error")
            self.assert_restored()

    def test_socket_and_send_failures_cleanup_and_never_retry_a_frame(self):
        rig = self.rig
        rig.socket.failure = "options"
        with self.assertRaises(PermissionError):
            rig.adapter.send_burst(PEER, frames(), 20, 50)
        self.assert_restored()
        rig.socket.failure = "send"
        rig.adapter.send_burst(PEER, frames(), 20, 50)
        rig.clock.advance(.5)
        rig.adapter.poll()
        rig.clock.advance(.5)
        events = rig.adapter.poll()
        self.assertEqual(events[0].kind, "radio_error")
        self.assertFalse(rig.socket.sent)
        self.assert_restored()

    def test_late_scheduler_aborts_instead_of_sending_a_catchup_burst(self):
        rig = self.rig
        rig.adapter.send_burst(PEER, frames(), 20, 50)
        rig.clock.advance(.5)
        rig.adapter.poll()
        rig.clock.advance(.7)
        events = rig.adapter.poll()
        self.assertEqual(events[0].kind, "radio_error")
        self.assertFalse(rig.socket.sent)
        self.assert_restored()

    def test_jittered_schedule_uses_absolute_offsets_without_poll_delay_accumulation(self):
        rig = self.rig
        rig.adapter.send_burst(PEER, frames(50), 20, 50)
        rig.clock.advance(.5)
        rig.adapter.poll()
        start = rig.adapter.next_send
        offsets = core.burst_schedule_us(SID, CHALLENGE, 50, 50)
        sent = []
        for offset in offsets:
            self.assertAlmostEqual(rig.adapter.next_send, start + offset / 1e6)
            rig.clock.now = rig.adapter.next_send - .00001
            self.assertFalse(rig.adapter.poll())
            self.assertEqual(len(rig.socket.sent), len(sent))
            rig.clock.now = start + offset / 1e6 + .0005
            events = rig.adapter.poll()
            sent.append(rig.clock.now)
            self.assertEqual(len(rig.socket.sent), len(sent))
        self.assertEqual(events, (radio.Event("burst_finished", SID),))
        self.assertAlmostEqual(sent[-1] - sent[0], offsets[-1] / 1e6)
        self.assertGreater(len(set(round((y - x) * 1e6) for x, y in zip(sent, sent[1:]))), 40)

    def test_late_midburst_aborts_and_short_lease_refuses_before_writes(self):
        rig = self.rig
        rig.adapter.config = replace(rig.config, lease_s=.5)
        with self.assertRaisesRegex(ValueError, "lease"):
            rig.adapter.send_burst(PEER, frames(50), 20, 50)
        self.assertFalse(rig.io.writes)
        rig.adapter.config = rig.config
        rig.adapter.send_burst(PEER, frames(), 20, 50)
        rig.clock.advance(.5)
        rig.adapter.poll()
        rig.clock.now = rig.adapter.next_send
        rig.adapter.poll()
        rig.clock.now = rig.adapter.next_send + .002
        self.assertEqual(rig.adapter.poll()[0].kind, "radio_error")
        self.assertEqual(len(rig.socket.sent), 1)
        self.assert_restored()

    def test_partial_arm_failure_and_failed_disarm_still_restore_ack(self):
        rig = self.rig
        rig.io.fail = lambda p, v: p.name == "tmr_ctrl" and v.startswith("1 ")
        rig.io.fail_after = True
        with self.assertRaises(OSError):
            rig.adapter.arm_responder(PEER, SID, 20)
        self.assert_restored()
        rig.io.fail = None
        rig.adapter.arm_responder(PEER, SID, 20)
        rig.io.fail = lambda p, v: p.name == "tmr_ctrl" and v.startswith("0 ")
        with self.assertRaises(radio.RadioError):
            rig.adapter.close()
        self.assertEqual(rig.io.read(rig.debug / "tmr_ack_spe").strip(), "default 25 24 23 22")
        self.assertTrue(rig.adapter.owned)  # Failed cleanup keeps exclusive ownership.
        rig.io.fail = None
        rig.adapter.close()
        self.assert_restored()

    def test_ack_restore_failure_keeps_lock_until_retry(self):
        rig = self.rig
        rig.adapter.arm_responder(PEER, SID, 20)
        rig.io.fail = lambda p, v: p.name == "tmr_ack_spe" and v.strip() == "restore"
        with self.assertRaises(radio.RadioError):
            rig.adapter.close()
        self.assertTrue(rig.adapter.owned)
        rig.io.fail = None
        rig.adapter.close()
        self.assert_restored()

    def test_failed_route_selector_never_writes_the_wrong_register(self):
        rig = self.rig
        rig.adapter.arm_responder(PEER, SID, 20)
        self.assertEqual(rig.io.read(rig.debug / "regidx").strip(), "0x1234")
        rig.io.fail = lambda p, v: p.name == "regidx" and v.strip() == hex(radio.NORMAL_ROUTE)
        before = len(rig.io.writes)
        with self.assertRaises(radio.RadioError):
            rig.adapter.close()
        self.assertFalse(any(p.name == "regval" for p, v in rig.io.writes[before:]))
        self.assertEqual(rig.io.registers[0x1234], "0x42")
        self.assertEqual(rig.io.read(rig.debug / "tmr_ack_spe").strip(), "default 25 24 23 22")
        rig.io.fail = None
        rig.adapter.close()
        self.assert_restored()

    def test_missing_driver_knob_and_trace_event_cleanup(self):
        rig = self.rig
        rate = rig.debug / "tmr_rate"
        rate.unlink()
        with self.assertRaises(FileNotFoundError):
            rig.adapter.arm_responder(PEER, SID, 20)
        self.assertFalse(rig.io.writes)
        self.assertFalse(rig.adapter.owned)
        rate.write_text("0x01000006\n")
        rig.io.fail = lambda p, v: p.name == "filter"
        with self.assertRaises(OSError):
            rig.adapter.arm_responder(PEER, SID, 20)
        self.assert_restored()

    def test_restore_before_disarm_cannot_release_active_radio_lock(self):
        rig = self.rig
        rig.adapter.arm_responder(PEER, SID, 20)
        rig.adapter.restore_ack()
        self.assertTrue(rig.adapter.owned)
        self.assertIsNotNone(rig.adapter.lock.fd)

    def test_other_owner_and_device_lock_are_respected(self):
        rig = self.rig
        (rig.debug / "tmr_ack_spe").write_text("set 0 0 0 0\n")
        with self.assertRaises(radio.RadioError):
            rig.adapter.arm_responder(PEER, SID, 20)
        self.assertFalse(rig.io.writes)
        (rig.debug / "tmr_ack_spe").write_text("default 25 24 23 22\n")
        rig.adapter.arm_responder(PEER, SID, 20)
        second = radio.DeviceLock(rig.adapter.lock.path)
        with self.assertRaises(BlockingIOError):
            second.acquire()
        self.assertIsNone(second.fd)

    def test_invalid_bursts_width_scope_and_peer_rejected_before_writes(self):
        rig = self.rig
        bad = pb.Envelope()
        bad.ParseFromString(frames()[0])
        bad.burst.sequence = 2
        with self.assertRaises(ValueError):
            rig.adapter.send_burst(PEER, [core.encode(bad)], 20, 50)
        with self.assertRaises(radio.RadioError):
            rig.adapter.arm_responder(PEER, SID, 80)
        for changed in (replace(rig.link, peer_address="2001:db8::1"),
                        replace(rig.link, peer_address="fe80::2%wlan1"),
                        replace(rig.link, peer_mac=bytes(6)), replace(rig.link, ifindex=0)):
            rig.link = changed
            with self.assertRaises(radio.RadioError):
                rig.adapter.arm_responder(PEER, SID, 20)
        self.assertFalse(rig.io.writes)

    def test_raw_read_wrong_session_and_unfinished_burst_fail(self):
        rig = self.rig
        rig.adapter.send_burst(PEER, frames(), 20, 50)
        with self.assertRaises(radio.CaptureInvalid):
            rig.adapter.read_raw_reports(SID)
        with self.assertRaises(radio.CaptureInvalid):
            rig.adapter.read_raw_reports(b"X" * 16)


class TimeJoinTests(unittest.TestCase):
    @staticmethod
    def train(jitter=True, foreign=False, start=10_000_000_000):
        offsets = core.burst_schedule_us(SID, CHALLENGE, 50, 50) if jitter else tuple(i * 50000 for i in range(50))
        a, arrivals = [], []
        offset = 1_000_000_000_000
        for i, elapsed in enumerate(offsets):
            departure = start + elapsed * 4000
            arrival = departure + offset + round(elapsed * 4000 * 8.5 / 1e6)
            a.append(radio.RawReport(i + 1, elapsed / 1e6, 2, departure % core.CLOCK_MODULUS,
                                    (departure + 100000 + 8400 + 2454 * (i + 1)) % core.CLOCK_MODULUS, ()))
            arrivals.append(arrival)
            if foreign:
                arrivals.append(arrival - 1000000)  # Unmarked data before every probe.
        if foreign:
            arrivals.extend(start + offset - i * 1000000 for i in range(2, 15))
        b = [radio.RawReport(j + 1, j / 100, 2,
                            (arrival + 100000 - 2454 * (j + 1)) % core.CLOCK_MODULUS,
                            arrival % core.CLOCK_MODULUS, ())
             for j, arrival in enumerate(sorted(arrivals))]
        bound = radio.ClockOffsetBound(start % core.CLOCK_MODULUS, offset, 1000)
        return a[1:], b, bound

    def test_jitter_removes_periodic_alias_and_keeps_independent_bound(self):
        for jitter in (False, True):
            a, b, bound = self.train(jitter)
            # This deliberately broad bound admits the one-frame alias;
            # the second guard must reject competing supported alignments.
            broad = replace(bound, uncertainty=250_000_000)
            if not jitter:
                with self.assertRaisesRegex(radio.CaptureInvalid, "competing"):
                    radio.time_pair_reports(a, b, initial_offset=broad)
            else:
                pairs = radio.time_pair_reports(a, b, initial_offset=broad)
                self.assertEqual(pairs, tuple((i, i + 1) for i in range(49)))
            self.assertEqual(radio.time_pair_reports(a, b, initial_offset=bound),
                             tuple((i, i + 1) for i in range(49)))
        with self.assertRaisesRegex(radio.CaptureInvalid, "independent"):
            radio.time_pair_reports(a, b, initial_offset=None)
        with self.assertRaisesRegex(radio.CaptureInvalid, "too few"):
            radio.time_pair_reports(a, b, initial_offset=replace(bound, offset=bound.offset + 500000000))

    def test_foreign_rows_keep_indices_and_prefix_over_ten_does_not_hide_seed(self):
        a, b, bound = self.train(foreign=True)
        pairs = radio.time_pair_reports(a, b, initial_offset=bound)
        self.assertEqual(len(pairs), 49)
        self.assertGreater(pairs[0][1], 10)
        local, remote = [], []
        for i, j in pairs:
            seq = a[i].record_number - 1
            local.append(core.InitiatorStamp(seq, a[i].departure, a[i].arrival, a[i].record_number))
            remote.append(core.ResponderStamp(seq, b[j].arrival, b[j].departure, b[j].record_number))
        calibration = core.Calibration.measured_20mhz(0)
        estimate = core.pair_timestamps(local, core.corrected_responder(remote, calibration), calibration)
        self.assertEqual(estimate.percentile_counts, 8400)
        # Dropping background BEFORE counting creates a growing error.
        bad = [replace(row, report_index=i + 1) for i, row in enumerate(remote)]
        self.assertNotEqual([r.turnaround for r in core.corrected_responder(bad[:10], calibration)],
                            [r.turnaround for r in core.corrected_responder(remote[:10], calibration)])

    def test_nearby_foreign_report_or_retry_is_ambiguous_even_late_in_burst(self):
        a, b, bound = self.train()
        extra = replace(b[25], arrival=b[25].arrival - 1000)
        with self.assertRaisesRegex(radio.CaptureInvalid, "multiple responder"):
            radio.time_pair_reports(a, b[:25] + [extra] + b[25:], initial_offset=bound)
        retry = replace(a[25], departure=a[25].departure + 1000)
        with self.assertRaisesRegex(radio.CaptureInvalid, "too close"):
            radio.time_pair_reports(a[:26] + [retry] + a[26:], b, initial_offset=bound)

    def test_loss_and_clock_wrap_preserve_time_join(self):
        a, b, bound = self.train(start=core.CLOCK_MODULUS - 300000000)
        a = [r for r in a if r.record_number not in (12, 13, 14)]
        b = [r for r in b if r.record_number not in (20, 21)]
        pairs = radio.time_pair_reports(a, b, initial_offset=bound)
        self.assertEqual(len(pairs), 44)
        self.assertTrue(all(a[i].record_number == b[j].record_number for i, j in pairs))
        with self.assertRaisesRegex(radio.CaptureInvalid, "unordered"):
            radio.time_pair_reports(a[::-1], b, initial_offset=bound)


class AccumulatorTests(unittest.TestCase):
    def test_modular_count_and_unusable_deltas(self):
        for width, step in ((20, 2454), (40, 1254), (80, 750)):
            for before in (0, 100, 0x80000000, 0xFFFFFFFF):
                for count in (0, 1, 64, radio.MAX_RAW_REPORTS):
                    after = (before - step * count) & 0xFFFFFFFF
                    self.assertEqual(radio.accumulator_report_count(before, after, width), count)
            for delta in (1, step * (radio.MAX_RAW_REPORTS + 1)):
                with self.assertRaises(radio.CaptureInvalid):
                    radio.accumulator_report_count(0, (-delta) & 0xFFFFFFFF, width)
        with self.assertRaises(ValueError):
            radio.accumulator_report_count(-1, 0, 20)
        with self.assertRaises(ValueError):
            radio.accumulator_report_count(0, 0, 160)

    def test_exact_fixed_dump_format_and_rejects_wrong_address(self):
        data = bytearray(104)
        struct.pack_into("<I", data, 0, 0x38000068)
        struct.pack_into("<II", data, 32, 0x57, 0xE00F20C0)
        struct.pack_into("<I", data, 68, (-2454 * 203) & 0xFFFFFFFF)
        self.assertEqual(radio.accumulator_report_count(0, radio.decode_wm_accumulator(bytes(data)), 20), 203)
        for invalid in (b"", bytes(data[:-1]), bytes(data) + b"\0"):
            with self.assertRaises(radio.CaptureInvalid):
                radio.decode_wm_accumulator(invalid)
        data[36] ^= 4
        with self.assertRaises(radio.CaptureInvalid):
            radio.decode_wm_accumulator(bytes(data))

    def test_saved_dumps_match_three_captures_and_detect_n3pace_shortfall(self):
        runs = Path(__file__).resolve().parents[2] / "kernel-work/ftm/runs"
        expected = {"wb-1": (101, 101), "sae72a": (400, 400),
                    "n3pace-a": (402, 401), "n3pace-c": (401, 401)}
        if not all((runs / f"wmr-{tag}/wmstate.txt").exists() and
                   (runs / f"wmr-{tag}/trace.txt").exists() for tag in expected):
            self.skipTest("optional saved WM dumps absent")
        for tag, counts in expected.items():
            directory = runs / f"wmr-{tag}"
            snapshots = []
            for line in (directory / "wmstate.txt").read_text().splitlines():
                try:
                    snapshots.append(radio.decode_wm_accumulator(bytes.fromhex(line)))
                except (ValueError, radio.CaptureInvalid):
                    continue
            self.assertEqual(len(snapshots), 1)
            rows = radio.parse_trace((directory / "trace.txt").read_text(), DEVICE)
            count = radio.accumulator_report_count(0, snapshots[0], 20)
            self.assertEqual((count, len(rows)), counts)
            # Every deliberately removed host row makes the shortfall grow;
            # the end snapshot detects amount, not where the row was lost.
            for length in (1, 3, 5):
                for start in (0, 1, len(rows) // 2, len(rows) - length):
                    remaining = rows[:start] + rows[start + length:]
                    self.assertEqual(count - len(remaining), counts[0] - counts[1] + length)


class DecoderTests(unittest.TestCase):
    def test_exact_record_and_timestamp_wrap_bits(self):
        row = radio.parse_trace(GOLDEN, DEVICE)[0]
        self.assertEqual(row.queue, 2)
        self.assertEqual(row.words[9], 0)
        self.assertEqual(row.record_number, 1)
        self.assertFalse(hasattr(row, "report_index"))
        self.assertFalse(hasattr(row, "sequence"))
        self.assertEqual(radio.parse_trace(GOLDEN, "0000:02:00.0"), ())

    def test_malformed_truncated_copied_error_and_wrong_type_fail(self):
        for trace in (GOLDEN.replace("len=40", "len=128"), GOLDEN.replace("captured=40", "captured=28"),
                      GOLDEN.replace("copy_error=0", "copy_error=-14"), GOLDEN[:-5] + "\n",
                      GOLDEN.replace("data=28", "data=29"), GOLDEN.replace("00 23", "00 83", 1),
                      GOLDEN.replace("data=28", "data=zz"), "mt7915_rx_tmr: corrupt\n"):
            with self.assertRaises(radio.CaptureInvalid):
                radio.parse_trace(trace, DEVICE)

    def test_fake_trace_tree_from_recorded_captures(self):
        runs = Path(__file__).resolve().parents[2] / "kernel-work/ftm/runs"
        paths = [runs / f"{role}-{tag}/trace.txt" for tag in ("wb-1", "sae72a", "n3pace-a")
                 for role in ("init", "wmr")]
        if not all(path.is_file() for path in paths):
            self.skipTest("untracked recorded captures unavailable")
        for path in paths:
            with self.subTest(capture=path.parent.name), tempfile.TemporaryDirectory(prefix="r3-traces-") as root:
                text = path.read_text()  # READ ONLY; copies go into the fake tracefs tree.
                rig = Rig(root, trace=text)
                try:
                    if path.parent.name.startswith("init-"):
                        rig.send_all()
                        read = rig.adapter.read_initiator_timestamps
                    else:
                        rig.adapter.arm_responder(PEER, SID, 20)
                        read = rig.adapter.read_responder_report
                    reports = rig.adapter.read_raw_reports(SID)
                    reference = []
                    for line in text.splitlines():
                        if "mt7915_rx_tmr:" not in line:
                            continue
                        words = struct.unpack("<10I", bytes.fromhex(line.split("data=")[1]))
                        reference.append((((words[6] & 65535) << 32) | words[4],
                                          ((words[6] >> 16) << 32) | words[5]))
                    self.assertGreater(len(reports), 50)
                    self.assertEqual([(r.departure, r.arrival) for r in reports], reference)
                    with self.assertRaises(radio.AssociationUnavailable):
                        read(SID)
                finally:
                    rig.adapter.close()


class LinkProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="r3-link-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.cfg = radio.Config("wlan1", "phy3", DEVICE, 49999)
        self.iface = root / "net/wlan1"
        self.iface.mkdir(parents=True)
        (self.iface / "ifindex").write_text("7\n")
        phy, device, bat = root / "phy3", root / DEVICE, root / "net/bat0"
        phy.mkdir()
        device.mkdir()
        bat.mkdir()
        (self.iface / "phy80211").symlink_to(phy)
        (phy / "device").symlink_to(device)
        (self.iface / "master").symlink_to(bat)
        self.info = "Interface wlan1\n\ttype mesh point\n\tchannel 36 (5180 MHz), width: 20 MHz, center1: 5180 MHz\n"
        self.station = "Station 00:0a:52:0d:d1:1b (on wlan1)\n"
        self.neighbours = [{"dst": "fe80::2", "lladdr": PEER.mac.hex(":"), "state": ["STALE"]}]
        self.addresses = [{"addr_info": [{"family": "inet6", "scope": "link", "local": "fe80::1"}]}]
        self.commands = []
        self.probe = radio.LinuxLinkProbe(self.cfg, lambda peer: "fe80::2", sys_root=root / "net", run=self.run_command)

    def run_command(self, args):
        self.commands.append(args)
        if args[-1] == "info":
            return self.info
        if "station" in args:
            return self.station
        return json.dumps(self.neighbours if "neigh" in args else self.addresses)

    def test_valid_mesh_neighbor(self):
        self.assertEqual(self.probe(PEER), radio.Link(7, "fe80::1", "fe80::2", PEER.mac, 20))
        self.assertTrue(all(isinstance(command, list) for command in self.commands))

    def test_ap_and_non_5ghz_rejected(self):
        for info in (self.info.replace("mesh point", "AP"), self.info.replace("5180", "2412")):
            self.info = info
            with self.assertRaises(radio.RadioError):
                self.probe(PEER)

    def test_wrong_peer_neighbor_and_tentative_address_rejected(self):
        self.neighbours[0]["lladdr"] = "02:00:00:00:00:03"
        with self.assertRaises(radio.RadioError):
            self.probe(PEER)
        self.neighbours[0]["lladdr"] = PEER.mac.hex(":")
        self.addresses[0]["addr_info"][0]["flags"] = ["tentative"]
        with self.assertRaises(radio.RadioError):
            self.probe(PEER)

    def test_non_batman_master_rejected(self):
        (self.iface / "master").unlink()
        (self.iface / "master").symlink_to(self.iface)
        with self.assertRaises(radio.RadioError):
            self.probe(PEER)


if __name__ == "__main__":
    unittest.main()
