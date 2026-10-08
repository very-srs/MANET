"""
GPS consistency monitor: compare GNSS fixes with measured radio ranges.

Each range between two radios is compared with the distance between their
GNSS fixes at the moment the range was measured. When both fixes carry a
height the comparison is in 3D, because a range is a slant distance. When
a height is missing, a range longer than the horizontal distance could be
height and proves nothing; a range shorter than it still counts. A pair whose two numbers disagree by more than THRESHOLD_M disagrees.
The threshold is sized to errors a person would notice on a map, not to
ranging precision: a range that reads a few metres long off a reflection
must pass.

A disagreement more than SEVERE_M fails the pair at once. A smaller one
fails it only when an earlier test of the same pair also disagreed, or when
one end also disagrees with a different partner.

A failed pair does not say which receiver is wrong, or whether the range
is, so both ends are distrusted for time
and stay distrusted for QUARANTINE_S after their last failure, whatever
passes they collect. Passes cannot clear a node: a spoofed fix reflected
across its partners passes them, and receivers captured together keep
their shared geometry and pass among themselves. A captured
radio keeps failing audits, so it keeps renewing its own quarantine. If
nothing can be measured any more, the quarantine still ends: losing the
means to check is reported as UNCHECKED, never as a pass.

Attribution only labels the map. It asks which sets of bad receivers would
explain every failed pair, with no member it could do without, and keeps
every such set no more than COVER_MARGIN receivers larger than the
smallest. A receiver in all of them is suspected. Preferring fewer bad
receivers is an assumption, not evidence, so it never clears anyone.
Attribution does not count passed pairs in anyone's favour either: a
spoofed fix can agree with honest partners, for example when it is
reflected across the line they stand on.

States, per node:
  NO_FIX                     no current fix
  UNCHECKED                  no recent pair test that could catch an error
  RANGE_CONSISTENT           recent tests passed, no recent failure; never "authentic"
  INCONSISTENT_UNATTRIBUTED  failed within QUARANTINE_S, not attributed
  GNSS_SUSPECTED             in a live failed pair, and failures point at it

Groups closer than SCOPE_M are out of scope: ranging
cannot separate them from a spoof that moves them together. Scope is judged
by the measured range, never by GNSS distance, because a spoofer that puts
separated receivers on one point is exactly what makes them look close.

Pure logic: no I/O, no clocks. Times are the caller's monotonic seconds,
in one clock domain: the caller maps each radio's clock into it. Inputs
must arrive in time order per node and per pair; repeats and older inputs
are ignored. A GNSS sample with the same time as the last one is the
reader revising that epoch: it replaces the sample, and range results that
used it are recomputed.
"""

from collections import deque
from itertools import combinations
import math

THRESHOLD_M = 20.0       # |expected - measured range| that counts as a disagreement
SEVERE_M = 40.0          # a disagreement this large fails the pair at once
COVER_MARGIN = 1         # explanations this many receivers larger also count
SCOPE_M = 20.0           # pairs measured closer than this cannot test anything
COLLAPSE_M = 8.0         # GNSS distance reported as "on top of each other"
ALIGN_S = 1.5            # GNSS sample must be this close to the range epoch
EDGE_TTL_S = 120.0       # a pair result older than this no longer counts
FIX_TTL_S = 5.0          # a node's latest fix older than this is no fix
HISTORY_S = 30.0         # GNSS samples kept per node for alignment
# Above this many receivers in failed pairs, the exact search is too slow
# for a CM4. All of them are then inconsistent and none is attributed.
MAX_COVER_NODES = 12
AUDIT_S = 30.0           # target: each node gets a pair test this often
CONFIRM_PARTNERS = 2     # fresh partners tested after a node's first disagreement
QUARANTINE_S = 300.0     # a node stays distrusted this long after its last failure
FAIL_COOLDOWN_S = 60.0   # a pair whose ranging failed is not retried sooner
EARTH_RADIUS_M = 6371008.8

NO_FIX = "NO_FIX"
UNCHECKED = "UNCHECKED"
RANGE_CONSISTENT = "RANGE_CONSISTENT"
INCONSISTENT = "INCONSISTENT_UNATTRIBUTED"
SUSPECTED = "GNSS_SUSPECTED"
# States that take a node out of the time election.
DISTRUSTED = (INCONSISTENT, SUSPECTED)


def horizontal_m(a, b):
    """Horizontal distance between two samples (equirectangular, short range)."""
    lat = math.radians((a["lat"] + b["lat"]) / 2)
    dn = math.radians(b["lat"] - a["lat"]) * EARTH_RADIUS_M
    de = math.radians(b["lon"] - a["lon"]) * EARTH_RADIUS_M * math.cos(lat)
    return math.hypot(dn, de)


def range_residual(a, b, measured):
    """Expected minus measured range, and the expected horizontal distance.

    With both heights the expected range is the 3D distance. Without one,
    the true slant is at least the horizontal distance and its upper bound
    is unknown, so only a range shorter than the horizontal distance leaves
    a residual.
    """
    h = horizontal_m(a, b)
    ha, hb = a.get("alt_hae"), b.get("alt_hae")
    if ha is not None and hb is not None:
        return math.hypot(h, ha - hb) - measured, h
    return max(0.0, h - measured), h


def has_fix(sample):
    return sample is not None and sample.get("mode", 0) >= 2 \
        and sample.get("lat") is not None and sample.get("lon") is not None


class Monitor:
    def __init__(self):
        self.samples = {}       # node -> deque of samples, oldest first
        self.edges = {}         # frozenset(a, b) -> latest test result
        self.log = deque()      # every test result within EDGE_TTL_S, oldest first
        self.last_failure = {}  # node -> epoch of its latest failed pair

    def add_gnss(self, node, sample):
        hist = self.samples.setdefault(node, deque())
        if hist and sample["mono"] < hist[-1]["mono"]:
            return
        if hist and sample["mono"] == hist[-1]["mono"]:
            hist[-1] = sample  # the reader revised this epoch; not new evidence
            self._recompute(node, sample["mono"])
            return
        hist.append(sample)
        while hist and sample["mono"] - hist[0]["mono"] > HISTORY_S:
            hist.popleft()

    def _at(self, node, mono):
        """The node's fix nearest the epoch, or None.

        None if no fix is within ALIGN_S, or if the receiver said it had no
        fix between that fix and the epoch.
        """
        hist = self.samples.get(node, ())
        best = None
        for s in hist:
            if has_fix(s) and abs(s["mono"] - mono) <= ALIGN_S \
                    and (best is None or abs(s["mono"] - mono) < abs(best["mono"] - mono)):
                best = s
        if best is None:
            return None
        lo, hi = sorted((best["mono"], mono))
        if any(not has_fix(s) and lo < s["mono"] < hi for s in hist):
            return None
        return best

    def add_range(self, rng):
        """Test one range. Returns the stored result, or None if untestable."""
        a, b, mono, r = rng["a"], rng["b"], rng["mono"], rng["range_m"]
        key = frozenset((a, b))
        prev = self.edges.get(key)
        if prev is not None and mono <= prev["mono"]:
            return None
        sa, sb = self._at(a, mono), self._at(b, mono)
        if sa is None or sb is None:
            return None
        repeated = prev is not None and prev["disagrees"] and mono - prev["mono"] <= EDGE_TTL_S
        result = {"mono": mono, "a": a, "b": b, "range_m": r, "repeated": repeated}
        self._evaluate(result, sa, sb)
        self._note_failure(result)
        self.edges[key] = result
        self.log.append(result)
        while self.log and mono - self.log[0]["mono"] > EDGE_TTL_S:
            self.log.popleft()
        return result

    @staticmethod
    def _evaluate(result, sa, sb):
        r = result["range_m"]
        residual, h = range_residual(sa, sb, r)
        in_scope = r >= SCOPE_M
        disagrees = in_scope and abs(residual) > THRESHOLD_M
        result.update({
            "a_mono": sa["mono"], "b_mono": sb["mono"],
            "gnss_m": round(h, 2), "residual_m": round(residual, 2),
            "in_scope": in_scope, "disagrees": disagrees,
            "failed": disagrees and (abs(residual) > SEVERE_M or result["repeated"]),
            "collapsed": r >= SCOPE_M + THRESHOLD_M and h <= COLLAPSE_M,
        })

    def _recompute(self, node, mono):
        """Re-evaluate stored results that used a revised sample."""
        for e in self.log:
            if (e["a"] == node and e["a_mono"] == mono) or (e["b"] == node and e["b_mono"] == mono):
                sa, sb = self._at(e["a"], e["a_mono"]), self._at(e["b"], e["b_mono"])
                if sa is not None and sb is not None:
                    self._evaluate(e, sa, sb)
                    self._note_failure(e)

    def _note_failure(self, e):
        """Quarantine starts when a failure is measured, not when observed."""
        if e["failed"]:
            for n in (e["a"], e["b"]):
                self.last_failure[n] = max(self.last_failure.get(n, e["mono"]), e["mono"])

    def live_edge(self, a, b, now):
        e = self.edges.get(frozenset((a, b)))
        return e if e is not None and now - e["mono"] <= EDGE_TTL_S else None

    def onsets(self, now):
        """Per node, the epoch of its earliest live disagreement."""
        out = {}
        for e in self.log:
            if e["disagrees"] and now - e["mono"] <= EDGE_TTL_S:
                for n in (e["a"], e["b"]):
                    out.setdefault(n, e["mono"])
        return out

    def current_fix(self, node, now):
        hist = self.samples.get(node)
        return bool(hist) and has_fix(hist[-1]) and now - hist[-1]["mono"] <= FIX_TTL_S

    def states(self, now):
        """Classify every node seen. Returns {node: (state, detail)}."""
        live = [e for e in self.edges.values() if now - e["mono"] <= EDGE_TTL_S]
        nodes = set(self.samples) | {n for e in live for n in (e["a"], e["b"])}
        fixed = {n for n in nodes if self.current_fix(n, now)}
        disagree = [e for e in live if e["disagrees"]]
        partners = {}
        for e in disagree:
            partners.setdefault(e["a"], set()).add(e["b"])
            partners.setdefault(e["b"], set()).add(e["a"])
        failed = [e for e in disagree if e["failed"]
                  or len(partners[e["a"]]) >= 2 or len(partners[e["b"]]) >= 2]
        for e in failed:
            self._note_failure(dict(e, failed=True))
        covers, exact = _near_minimum_covers([(e["a"], e["b"]) for e in failed])
        suspected = set.intersection(*covers) if covers else set()

        out = {}
        for n in sorted(nodes | set(self.last_failure)):
            mine = [e for e in live if n in (e["a"], e["b"])]
            last = self.last_failure.get(n)
            quarantined = last is not None and now - last <= QUARANTINE_S
            detail = {
                "tested": len([e for e in mine if e["in_scope"]]),
                "failed_with": sorted(e["b"] if e["a"] == n else e["a"]
                                      for e in failed if n in (e["a"], e["b"])),
                "last_failure": last,
                "attributed": exact,
            }
            if n not in fixed:
                state = NO_FIX
            elif n in suspected:
                state = SUSPECTED
            elif quarantined:
                state = INCONSISTENT
            elif any(e["in_scope"] and not e["disagrees"] for e in mine):
                state = RANGE_CONSISTENT
            else:
                state = UNCHECKED
            out[n] = (state, detail)
        return out


def _near_minimum_covers(edges):
    """Irredundant node sets touching every edge, up to COVER_MARGIN larger
    than the smallest. Irredundant: no member can be dropped.

    Returns (covers, exact). exact is False when the graph was too large to
    search; the result then implicates every node and attributes none.
    """
    if not edges:
        return [], True
    nodes = sorted({n for e in edges for n in e})
    if len(nodes) > MAX_COVER_NODES:
        return [set(nodes), set()], False
    found, smallest = [], None
    for k in range(1, len(nodes) + 1):
        if smallest is not None and k > smallest + COVER_MARGIN:
            break
        for c in combinations(nodes, k):
            if all(a in c or b in c for a, b in edges) and _irredundant(set(c), edges):
                found.append(set(c))
                if smallest is None:
                    smallest = k
    return found, True


def _irredundant(cover, edges):
    """Every member covers some edge no other member covers."""
    return all(any(n in e and (e[0] if e[1] == n else e[1]) not in cover for e in edges)
               for n in cover)


class Scheduler:
    """Picks the next pair to range. One burst at a time per channel.

    First, every node in a recent disagreement is tested against
    CONFIRM_PARTNERS fresh partners, preferring partners whose own checks
    pass. Then the node whose last useful test is oldest is audited once
    AUDIT_S has passed. AUDIT_S is a target: a node with many partners and
    a busy channel can wait longer. A pair measured closer than SCOPE_M is
    skipped until that measurement expires, so a group that starts together
    is checked again once it spreads out. A pair whose ranging failed waits
    FAIL_COOLDOWN_S (report it with failed()).
    """

    def __init__(self, monitor):
        self.m = monitor
        self.cooldown = {}  # frozenset(a, b) -> mono before which it is not tried

    def failed(self, a, b, now):
        """The caller could not get a usable range for this pair."""
        self.cooldown[frozenset((a, b))] = now + FAIL_COOLDOWN_S

    def next_pair(self, now, nodes, reachable):
        """nodes: radios with a current fix. reachable(a, b): a direct 5 GHz link."""
        nodes = sorted(nodes)
        states = self.m.states(now)
        onsets = self.m.onsets(now)
        log = [e for e in self.m.log if now - e["mono"] <= EDGE_TTL_S]

        def useful(a, b):
            if self.cooldown.get(frozenset((a, b)), 0) > now or not reachable(a, b):
                return False
            e = self.m.live_edge(a, b, now)
            return e is None or e["range_m"] >= SCOPE_M

        def edge_age(a, b):
            e = self.m.edges.get(frozenset((a, b)))
            return now - e["mono"] if e else float("inf")

        for u in sorted((n for n in onsets if n in nodes), key=onsets.get):
            since = onsets[u]
            tested = {e["b"] if e["a"] == u else e["a"] for e in log
                      if u in (e["a"], e["b"]) and e["mono"] > since}
            if len(tested) >= CONFIRM_PARTNERS:
                continue
            candidates = [p for p in nodes if p != u and p not in tested and useful(u, p)]
            if candidates:
                candidates.sort(key=lambda p: (states.get(p, (UNCHECKED,))[0] != RANGE_CONSISTENT,
                                               p in onsets, -edge_age(u, p)))
                return u, candidates[0]

        last_test = {}
        for e in log:
            if e["in_scope"]:
                for n in (e["a"], e["b"]):
                    last_test[n] = e["mono"]
        due = sorted((n for n in nodes if now - last_test.get(n, -math.inf) >= AUDIT_S),
                     key=lambda n: last_test.get(n, -math.inf))
        for u in due:
            candidates = [p for p in nodes if p != u and useful(u, p)]
            if candidates:
                # The least recently measured pair first, so one baseline is
                # never reused while others go untested; a due partner only
                # breaks ties.
                candidates.sort(key=lambda p: (-edge_age(u, p), p not in due))
                return u, candidates[0]
        return None
