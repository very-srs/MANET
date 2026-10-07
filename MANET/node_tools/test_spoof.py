"""GPS consistency monitor and pair scheduler, on synthetic fixes and ranges."""
import math
import unittest

import manet_spoof as ms

LAT0, LON0 = 39.7392, -104.9903
R = 6371008.8


def fix(east, north, mono, mode=3, up=None):
    lat = LAT0 + math.degrees(north / R)
    lon = LON0 + math.degrees(east / (R * math.cos(math.radians(LAT0))))
    return {"mono": mono, "mode": mode, "lat": lat, "lon": lon, "alt_hae": up}


def nofix(mono):
    return {"mono": mono, "mode": 1, "lat": None, "lon": None}


def rng(a, b, metres, mono):
    return {"mono": mono, "a": a, "b": b, "range_m": metres, "n": 50}


class Bench:
    """Nodes at fixed true positions; reported fixes can be overridden."""

    def __init__(self, true):
        self.true = true
        self.reported = dict(true)
        self.m = ms.Monitor()
        self.t = 0.0

    def tick(self, seconds=1):
        for _ in range(seconds):
            self.t += 1
            for n, pos in self.reported.items():
                self.m.add_gnss(n, fix(pos[0], pos[1], self.t, up=pos[2] if len(pos) > 2 else 0.0))

    def measure(self, a, b, extra=0.0):
        pa, pb = self.true[a], self.true[b]
        ua = pa[2] if len(pa) > 2 else 0.0
        ub = pb[2] if len(pb) > 2 else 0.0
        d = math.dist((pa[0], pa[1], ua), (pb[0], pb[1], ub))
        return self.m.add_range(rng(a, b, d + extra, self.t))

    def state(self, n):
        return self.m.states(self.t)[n][0]


SQUARE = {"A": (0, 0), "B": (60, 0), "C": (60, 60), "D": (0, 60)}


class MonitorTests(unittest.TestCase):
    def test_agreeing_pair_is_range_consistent(self):
        b = Bench(SQUARE)
        b.tick(3)
        r = b.measure("A", "B", extra=3.0)  # a reflection reads long
        self.assertFalse(r["disagrees"])
        self.assertEqual(b.state("A"), ms.RANGE_CONSISTENT)
        self.assertEqual(b.state("C"), ms.UNCHECKED)

    def test_close_pair_tests_nothing(self):
        b = Bench({"A": (0, 0), "B": (12, 0)})
        b.tick(3)
        b.reported["B"] = (500, 0)
        b.tick()
        r = b.measure("A", "B")
        self.assertFalse(r["in_scope"])
        self.assertEqual(b.state("A"), ms.UNCHECKED)

    def test_one_marginal_disagreement_raises_nothing(self):
        b = Bench(SQUARE)
        b.tick(3)
        r = b.measure("A", "B", extra=25.0)
        self.assertTrue(r["disagrees"])
        self.assertFalse(r["failed"])
        self.assertEqual(b.state("A"), ms.UNCHECKED)

    def test_repeated_marginal_disagreement_is_inconsistent(self):
        b = Bench(SQUARE)
        b.tick(3)
        b.measure("A", "B", extra=25.0)
        b.tick(5)
        b.measure("A", "B", extra=25.0)
        self.assertEqual(b.state("A"), ms.INCONSISTENT)
        self.assertEqual(b.state("B"), ms.INCONSISTENT)

    def test_severe_disagreement_flags_both_ends_unattributed(self):
        b = Bench(SQUARE)
        b.tick(3)
        b.measure("A", "B", extra=50.0)
        self.assertEqual(b.state("A"), ms.INCONSISTENT)
        self.assertEqual(b.state("B"), ms.INCONSISTENT)

    def test_receiver_failing_two_partners_is_only_inconsistent(self):
        b = Bench(SQUARE)
        b.tick(3)
        b.reported["A"] = (-35, -35)  # about 50 m off
        b.tick(2)
        b.measure("A", "B")
        b.tick(3)
        b.measure("A", "D")
        self.assertEqual(b.state("A"), ms.INCONSISTENT)

    def test_receiver_failing_three_partners_is_suspected(self):
        b = Bench(SQUARE)
        b.tick(3)
        b.measure("B", "C")
        b.measure("C", "D")
        b.reported["A"] = (-35, -35)
        b.tick(2)
        for p in "BCD":
            b.measure("A", p)
            b.tick(3)
        self.assertEqual(b.state("A"), ms.SUSPECTED)
        # Safety first: honest partners stay distrusted until cleared.
        for n in "BCD":
            self.assertEqual(b.state(n), ms.INCONSISTENT)

    def test_passes_do_not_clear_before_quarantine_ends(self):
        b = Bench({**SQUARE, "E": (120, 0), "F": (120, 60)})
        b.tick(3)
        b.reported["A"] = (-35, -35)
        b.tick(2)
        for p in "BCD":
            b.measure("A", p)
            b.tick(3)
        b.measure("B", "E")
        b.tick(3)
        b.measure("B", "F")
        self.assertEqual(b.state("B"), ms.INCONSISTENT)
        b.tick(int(ms.QUARANTINE_S))
        b.measure("B", "E")
        self.assertEqual(b.state("B"), ms.RANGE_CONSISTENT)

    def test_colluding_partners_cannot_clear_each_other(self):
        # codex-007 colluding_clearance: five receivers shifted together.
        true = {"A": (0, 0), "B": (60, 0), "C": (0, 60), "D": (-60, 0),
                "E": (60, 60), "F": (-60, 60)}
        b = Bench(true)
        b.tick(3)
        for n in "BCDEF":
            b.reported[n] = (true[n][0] + 100, true[n][1] + 100)
        b.tick(2)
        for p in "BCD":
            b.measure("A", p)
        b.tick()
        b.measure("B", "E")
        b.tick()
        b.measure("B", "F")
        self.assertIn(b.state("B"), ms.DISTRUSTED)

    def test_one_same_pair_pass_does_not_erase_a_failure(self):
        # codex-007 single_pass_replaces_failure.
        b = Bench({"A": (0, 0), "B": (60, 0)})
        b.tick(3)
        b.reported["A"] = (200, 0)
        b.tick(2)
        b.measure("A", "B")
        b.reported["A"] = (120, 0)
        b.tick(3)
        self.assertFalse(b.measure("A", "B")["disagrees"])
        self.assertIn(b.state("A"), ms.DISTRUSTED)

    def test_revision_recomputes_a_stored_result(self):
        # codex-007 revision_after_range.
        m = ms.Monitor()
        m.add_gnss("A", fix(0, 0, 1.0, up=0.0))
        m.add_gnss("B", fix(60, 0, 1.0, up=160.0))
        r = m.add_range(rng("A", "B", 100.0, 1.0))
        self.assertTrue(r["failed"])
        m.add_gnss("B", fix(60, 0, 1.0, up=80.0))
        self.assertFalse(m.edges[frozenset("AB")]["disagrees"])

    def test_range_is_not_matched_across_a_no_fix_report(self):
        # codex-007 no_fix_interval.
        m = ms.Monitor()
        m.add_gnss("A", fix(0, 0, 1.0))
        m.add_gnss("B", fix(60, 0, 0.0))
        m.add_gnss("B", nofix(0.5))
        self.assertIsNone(m.add_range(rng("A", "B", 60.0, 1.0)))

    def test_blame_never_clears_captured_receivers(self):
        # codex-006 star_exoneration: B, C, D captured onto honest A's spot.
        b = Bench({"A": (0, 0), "B": (60, 0), "C": (0, 60), "D": (-60, 0)})
        b.tick(3)
        for n in "BCD":
            b.reported[n] = (0, 0)
        b.tick(2)
        for p in "BCD":
            b.measure("A", p)
        for n in "BCD":
            self.assertIn(b.state(n), ms.DISTRUSTED)

    def test_peer_losing_its_fix_clears_nothing(self):
        # codex-006 peer_loss_clears.
        b = Bench(SQUARE)
        b.tick(3)
        b.reported["A"] = (-60, 0)
        b.tick(2)
        b.measure("A", "B")
        del b.reported["B"]
        b.tick()
        b.m.add_gnss("B", nofix(b.t))
        self.assertEqual(b.state("A"), ms.INCONSISTENT)
        self.assertEqual(b.state("B"), ms.NO_FIX)

    def test_reader_revision_of_an_epoch_replaces_the_sample(self):
        # codex-006 merged_sample_dropped.
        m = ms.Monitor()
        m.add_gnss("A", {"mono": 1.0, "mode": 2, "lat": LAT0, "lon": LON0, "alt_hae": None})
        m.add_gnss("A", {"mono": 1.0, "mode": 3, "lat": LAT0, "lon": LON0, "alt_hae": 1680.0})
        self.assertEqual(len(m.samples["A"]), 1)
        self.assertEqual(m.samples["A"][-1]["alt_hae"], 1680.0)

    def test_full_capture_distrusts_everyone_without_blame(self):
        b = Bench(SQUARE)
        b.tick(3)
        for n in SQUARE:
            b.reported[n] = (200, 200)
        b.tick(2)
        for a, c in (("A", "B"), ("C", "D"), ("A", "C"), ("B", "D")):
            r = b.measure(a, c)
            self.assertTrue(r["collapsed"])
        for n in SQUARE:
            self.assertEqual(b.state(n), ms.INCONSISTENT)

    def test_capture_onto_an_honest_spot_blames_the_captured(self):
        # codex-005 collapse_at_honest: B and C are put on A's true position.
        b = Bench({"A": (0, 0), "B": (60, 0), "C": (0, 60)})
        b.tick(3)
        b.reported["B"] = b.reported["C"] = (0, 0)
        b.tick(2)
        for a, c in (("A", "B"), ("A", "C"), ("B", "C")):
            b.measure(a, c)
        self.assertEqual(b.state("A"), ms.INCONSISTENT)
        self.assertNotEqual(b.state("A"), ms.SUSPECTED)
        self.assertIn(b.state("B"), ms.DISTRUSTED)
        self.assertIn(b.state("C"), ms.DISTRUSTED)

    def test_reflection_across_partners_is_not_exonerated(self):
        # codex-005 pass_penalty_flip: X reflected across the A-B-C line.
        b = Bench({"X": (0, 40), "D": (0, 80), "A": (-60, 0), "B": (0, 0), "C": (60, 0)})
        b.tick(3)
        b.reported["X"] = (0, -40)
        b.tick(2)
        b.measure("X", "D")
        for p in "ABC":
            b.tick(3)
            b.measure("X", p)
        self.assertNotEqual(b.state("D"), ms.SUSPECTED)
        self.assertIn(b.state("X"), ms.DISTRUSTED)

    def test_slant_range_uses_heights(self):
        # codex-005 height_false_positive: A 80 m up a hill.
        b = Bench({"A": (0, 0, 80), "B": (30, 0, 0), "C": (-30, 0, 0)})
        b.tick(3)
        for a, c in (("A", "B"), ("A", "C"), ("B", "C")):
            self.assertFalse(b.measure(a, c)["disagrees"])
        for n in "ABC":
            self.assertEqual(b.state(n), ms.RANGE_CONSISTENT)

    def test_unknown_height_only_short_ranges_count(self):
        # codex-006 unknown_height: a long range could be height.
        a = fix(0, 0, 1.0)
        c = fix(30, 0, 1.0)
        self.assertEqual(ms.range_residual(a, c, 85.4)[0], 0.0)
        self.assertAlmostEqual(ms.range_residual(fix(0, 0, 1.0), fix(90, 0, 1.0), 40.0)[0], 50.0, 1)

    def test_common_range_bias_at_one_radio_is_not_blamed(self):
        # codex-005 correlated_range_bias: A's two ranges both read 25 m long.
        b = Bench({"A": (0, 0), "B": (60, 0), "C": (0, 60)})
        b.tick(3)
        b.measure("A", "B", extra=25.0)
        b.measure("A", "C", extra=25.0)
        b.measure("B", "C")
        self.assertEqual(b.state("A"), ms.INCONSISTENT)

    def test_duplicate_range_is_not_confirmation(self):
        # codex-005 duplicate_confirmation.
        b = Bench(SQUARE)
        b.tick(3)
        first = b.measure("A", "B", extra=25.0)
        self.assertIsNone(b.m.add_range(rng("A", "B", first["range_m"], first["mono"])))
        self.assertEqual(b.state("A"), ms.UNCHECKED)

    def test_old_gnss_sample_is_ignored(self):
        m = ms.Monitor()
        m.add_gnss("A", fix(0, 0, 10.0))
        m.add_gnss("A", fix(500, 0, 9.0))
        self.assertEqual(len(m.samples["A"]), 1)

    def test_no_fix(self):
        b = Bench(SQUARE)
        b.tick(3)
        b.t += 1
        b.m.add_gnss("A", nofix(b.t))
        self.assertEqual(b.state("A"), ms.NO_FIX)

    def test_stale_fix_is_no_fix(self):
        b = Bench(SQUARE)
        b.tick(3)
        b.t += ms.FIX_TTL_S + 1
        self.assertEqual(b.state("A"), ms.NO_FIX)

    def test_results_expire(self):
        b = Bench(SQUARE)
        b.tick(3)
        b.measure("A", "B", extra=50.0)
        b.tick(int(ms.EDGE_TTL_S) + 1)
        self.assertEqual(b.state("A"), ms.INCONSISTENT)  # quarantine outlasts the result
        b.tick(int(ms.QUARANTINE_S - ms.EDGE_TTL_S))
        self.assertEqual(b.state("A"), ms.UNCHECKED)

    def test_range_without_a_fix_near_its_epoch_is_untestable(self):
        b = Bench(SQUARE)
        b.tick(3)
        self.assertIsNone(b.m.add_range(rng("A", "B", 60, b.t + 10)))

    def test_large_failure_graph_attributes_nobody(self):
        covers, exact = ms._near_minimum_covers([(f"N{i}", f"N{i + 1}") for i in range(0, 30, 2)])
        self.assertFalse(exact)
        self.assertEqual(set.intersection(*covers), set())


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.b = Bench(SQUARE)
        self.b.tick(3)
        self.s = ms.Scheduler(self.b.m)
        self.all = lambda a, c: True

    def test_audits_untested_nodes_first(self):
        self.b.measure("A", "B")
        pair = self.s.next_pair(self.b.t, list(SQUARE), self.all)
        self.assertEqual(set(pair), {"C", "D"})

    def test_nothing_due_after_recent_tests(self):
        self.b.measure("A", "B")
        self.b.measure("C", "D")
        self.assertIsNone(self.s.next_pair(self.b.t, list(SQUARE), self.all))

    def test_disagreement_triggers_fresh_partners(self):
        self.b.measure("C", "D")
        self.b.tick()
        self.b.measure("A", "B", extra=25.0)
        pair = self.s.next_pair(self.b.t, list(SQUARE), self.all)
        self.assertIn(pair[0], ("A", "B"))
        self.assertIn(pair[1], ("C", "D"))  # consistent partners preferred

    def test_unreachable_pairs_are_never_chosen(self):
        only_ab = lambda a, c: {a, c} == {"A", "B"}
        self.assertEqual(set(self.s.next_pair(self.b.t, list(SQUARE), only_ab)), {"A", "B"})

    def test_pairs_known_to_be_close_are_skipped(self):
        b = Bench({"A": (0, 0), "B": (10, 0)})
        b.tick(3)
        b.measure("A", "B")
        b.tick(int(ms.AUDIT_S) + 1)
        self.assertIsNone(ms.Scheduler(b.m).next_pair(b.t, ["A", "B"], self.all))

    def test_close_pair_is_rechecked_after_its_measurement_expires(self):
        # codex-005 close_then_separate.
        b = Bench({"A": (0, 0), "B": (10, 0)})
        b.tick(3)
        b.measure("A", "B")
        b.tick(int(ms.EDGE_TTL_S) + 1)
        self.assertEqual(set(ms.Scheduler(b.m).next_pair(b.t, ["A", "B"], self.all)), {"A", "B"})

    def test_audits_rotate_partners(self):
        # codex-006 audit_partner_lock: one baseline must not be reused forever.
        b = Bench({"A": (0, 0), "B": (60, 0), "C": (0, 80), "D": (60, 80)})
        s = ms.Scheduler(b.m)
        seen = set()
        for _ in range(12):
            b.tick(10)
            pair = s.next_pair(b.t, list(b.true), self.all)
            if pair:
                b.measure(*pair)
                seen.add(frozenset(pair))
        self.assertGreaterEqual(len(seen), 5)

    def test_failed_acquisition_does_not_starve_others(self):
        # codex-005 failed_attempt_starvation.
        s = ms.Scheduler(self.b.m)
        first = s.next_pair(self.b.t, list(SQUARE), self.all)
        s.failed(*first, self.b.t)
        second = s.next_pair(self.b.t, list(SQUARE), self.all)
        self.assertNotEqual(set(first), set(second))


if __name__ == "__main__":
    unittest.main()
