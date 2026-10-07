"""Socket-free, event-driven MT7916 data/ACK ranging (not 802.11mc FTM).

Call start(), receive(), tick(), burst_finished(), cancel() and radio_error()
on ONE event-loop thread; drain outbox and results after each event. Radio
methods must be nonblocking. tick() must run regularly even with no traffic.
An adapter must authenticate/authorize Peer identities, verify direct 5 GHz
neighbours and mesh mode, scope probes to that interface, and associate UDP
sequence numbers with hardware reports. SAE membership is not authorization.

The adapter must retain the original 1-based hardware report indices after
each accumulator reset, including reports later filtered out. Missing trace
records with unknown indices invalidate a capture. They cannot be repaired
by renumbering UDP sequences. No driver/transport adapter is installed here.
"""

from collections import deque
from dataclasses import dataclass, field
import hashlib
import math
import secrets
import statistics
from typing import Callable, Optional, Protocol, Sequence

from google.protobuf.message import DecodeError
import ranging_pb2 as pb

VERSION = 1
MAX_DATAGRAM = 1200
MAX_FRAMES = 64
REPORT_ROWS = 24
CLOCK_MODULUS = 1 << 48


def clock_delta(a, b):
    """Signed difference of two 48-bit hardware timestamps, including wrap."""
    return (a - b + CLOCK_MODULUS // 2) % CLOCK_MODULUS - CLOCK_MODULUS // 2


def burst_schedule_us(session_id, challenge, frames, interval_ms):
    """V1 deterministic send offsets, starting at zero, in microseconds.

    SHA-256(domain || sid || challenge || gap_index_be16), first u32 BE,
    modulo the inclusive gap range. Nominal interval +/-20%, clipped to
    20..100 ms (50 means 40..60 ms). Both roles derive the same schedule;
    no platform PRNG or secret clock is involved. This reduces periodic
    time-pairing aliases; it does not prove radio association or counter origin.
    """
    if (any(not isinstance(n, bytes) or len(n) != 16 or not any(n)
            for n in (session_id, challenge)) or
            type(frames) is not int or not 1 <= frames <= MAX_FRAMES or
            type(interval_ms) is not int or not 20 <= interval_ms <= 100):
        raise ValueError("invalid burst schedule")
    low, high = max(20000, interval_ms * 800), min(100000, interval_ms * 1200)
    seed = b"MANET-ranging-v1-jitter\0" + session_id + challenge
    offsets = [0]
    for gap in range(frames - 1):
        word = hashlib.sha256(seed + gap.to_bytes(2, "big")).digest()[:4]
        offsets.append(offsets[-1] + low + int.from_bytes(word, "big") % (high - low + 1))
    return tuple(offsets)


def _node_id(value):
    return isinstance(value, str) and 0 < len(value.encode("utf-8")) <= 32


def validate_position(position):
    if position.source not in (pb.Position.UNKNOWN, pb.Position.GNSS,
                                pb.Position.RANGED, pb.Position.MANUAL):
        raise ValueError("unknown position source")
    if not (-900000000 <= position.latitude_e7 <= 900000000 and
            -1800000000 <= position.longitude_e7 <= 1800000000):
        raise ValueError("position out of bounds")
    if position.valid and (position.source == pb.Position.UNKNOWN or
                           not position.HasField("fix_age_ms")):
        raise ValueError("valid position needs source and local fix age")
    ids = position.used_node_ids
    if len(ids) > 16 or len(set(ids)) != len(ids) or not all(map(_node_id, ids)):
        raise ValueError("invalid position ancestry")
    if position.source != pb.Position.RANGED and (position.generation or ids):
        raise ValueError("only ranged positions have ancestry")
    if position.valid and position.source == pb.Position.RANGED and (
            not position.generation or not ids):
        raise ValueError("ranged position needs generation and ancestry")


def encode(message):
    data = message.SerializeToString(deterministic=True)
    if len(data) > MAX_DATAGRAM:
        raise ValueError("ranging datagram too large")
    return data


def decode(data):
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_DATAGRAM:
        raise ValueError("invalid ranging datagram size")
    message = pb.Envelope()
    try:
        message.ParseFromString(data)
    except DecodeError as exc:
        raise ValueError("invalid protobuf") from exc
    if (message.version != VERSION or len(message.session_id) != 16 or
            not any(message.session_id) or not message.WhichOneof("body") or
            len(message.challenge) not in (0, 16)):
        raise ValueError("invalid envelope or protocol version")
    if message.HasField("position"):
        validate_position(message.position)
    return message


@dataclass(frozen=True)
class Peer:
    node_id: str
    mac: bytes

    def __post_init__(self):
        if not _node_id(self.node_id) or not isinstance(self.mac, bytes) or (
                len(self.mac) != 6 or self.mac[0] & 1 or not any(self.mac)):
            raise ValueError("peer needs a node id and unicast 5 GHz MAC")


@dataclass(frozen=True)
class Calibration:
    channel_width_mhz: int
    counts_per_metre: float
    constant_counts: float
    accumulator_step: int

    def __post_init__(self):
        if (self.channel_width_mhz not in (20, 40, 80, 160) or
                not math.isfinite(self.counts_per_metre) or self.counts_per_metre <= 0 or
                not math.isfinite(self.constant_counts) or
                not 0 <= self.accumulator_step < (1 << 24)):
            raise ValueError("invalid calibration")

    @classmethod
    def measured_20mhz(cls, constant_counts):
        """Measured slope; caller supplies this node's calibrated intercept."""
        return cls(20, 26.69, constant_counts, 2454)


@dataclass(frozen=True)
class InitiatorStamp:
    sequence: int
    departure: int
    ack_arrival: Optional[int]  # None when the ACK was lost.
    report_index: int


@dataclass(frozen=True)
class ResponderStamp:
    sequence: int
    arrival: int
    ack_departure: int
    report_index: int


@dataclass(frozen=True)
class RemoteStamp:
    sequence: int
    arrival: int
    turnaround: int


@dataclass(frozen=True)
class Estimate:
    range_m: float
    percentile_counts: float
    differences: tuple  # (sequence, corrected RTT minus turnaround), after filtering.
    matched: int
    rejected: int


def _unique(rows):
    """Identical duplicates are harmless; ambiguous retransmissions are excluded."""
    unique, conflicts = {}, set()
    for row in rows:
        if row.sequence in unique and unique[row.sequence] != row:
            conflicts.add(row.sequence)
        unique[row.sequence] = row
    return {seq: row for seq, row in unique.items() if seq not in conflicts}


def _timestamp(value):
    return isinstance(value, int) and 0 <= value < CLOCK_MODULUS


def corrected_responder(rows, calibration):
    result = []
    for row in _unique(rows).values():
        if (not 0 <= row.sequence < MAX_FRAMES or not _timestamp(row.arrival) or
                not _timestamp(row.ack_departure) or not 1 <= row.report_index <= 65535):
            raise ValueError("invalid responder timestamp")
        turn = (clock_delta(row.ack_departure, row.arrival) +
                calibration.accumulator_step * row.report_index)
        if not 0 <= turn < (1 << 31):
            raise ValueError("invalid corrected turnaround")
        result.append(RemoteStamp(row.sequence, row.arrival, turn))
    return sorted(result, key=lambda row: row.sequence)


def pair_timestamps(initiator, responder, calibration, min_pairs=10,
                    tolerance_counts=4000, outlier_counts=1500, max_drift_ppm=40.0):
    """Sequence intersection + sim_pair.walk's tracked clock-offset check.

    Try the first ten common sequence seeds, keeping the longest forward
    walk. The offset allowance is tolerance_counts + elapsed hardware counts
    * max_drift_ppm / 1e6 since the last match. This handles lost frames and
    actual probe pacing without assuming a channel width. Recorded medians
    were 8.2-8.4 ppm, peaking at 29.7 ppm; 40 ppm is a configurable margin,
    not a hardware guarantee. As in estimators.py, discard
    |D - median(D)| >= 1500 counts and
    take sorted(D)[floor(.25*N)], with no interpolation. The measured slope
    already converts ROUND-TRIP counts to metres; do not divide by two again.
    Negative calibrated ranges are retained for callers to diagnose.
    """
    if (min_pairs < 1 or tolerance_counts <= 0 or outlier_counts <= 0 or
            not math.isfinite(max_drift_ppm) or max_drift_ppm < 0):
        raise ValueError("invalid estimator limits")
    local, remote = _unique(initiator), _unique(responder)
    candidates = []
    for seq in sorted(local.keys() & remote.keys()):
        a, b = local[seq], remote[seq]
        if (not 0 <= seq < MAX_FRAMES or not _timestamp(a.departure) or
                not _timestamp(b.arrival) or not 1 <= a.report_index <= 65535 or
                not 0 <= b.turnaround < (1 << 31)):
            raise ValueError("invalid pairing sample")
        if a.ack_arrival is None:
            continue
        if not _timestamp(a.ack_arrival):
            raise ValueError("invalid ACK timestamp")
        rtt = clock_delta(a.ack_arrival, a.departure) - calibration.accumulator_step * a.report_index
        if not 0 <= rtt < (1 << 31):
            continue
        candidates.append((seq, clock_delta(b.arrival, a.departure), rtt - b.turnaround,
                           a.departure))
    best = []
    for seed in range(min(10, len(candidates))):
        offset, departure, walk = candidates[seed][1], candidates[seed][3], []
        for row in candidates[seed:]:
            elapsed = clock_delta(row[3], departure)
            allowance = tolerance_counts + elapsed * max_drift_ppm / 1e6
            if elapsed >= 0 and abs(clock_delta(row[1], offset)) < allowance:
                walk.append((row[0], row[2]))
                offset, departure = row[1], row[3]
        if len(walk) > len(best):
            best = walk
    if not best:
        raise ValueError("no timestamp pairs")
    median = statistics.median(value for _, value in best)
    kept = tuple((seq, value) for seq, value in best if abs(value - median) < outlier_counts)
    if len(kept) < min_pairs:
        raise ValueError("too few timestamp pairs")
    percentile = sorted(value for _, value in kept)[len(kept) // 4]
    return Estimate((percentile - calibration.constant_counts) / calibration.counts_per_metre,
                    percentile, kept, len(best), len(candidates) - len(kept))


class Radio(Protocol):
    """One adapter per radio; all methods may raise on failure.

    arm_responder saves ACK settings BEFORE changing them, resets hardware
    counters and selects this peer's MAC. send_burst resets initiator capture,
    sends only the supplied marked datagrams at burst_schedule_us() offsets
    derived from their sid/challenge and nominal interval_ms, on the direct
    5 GHz link, then signals burst_finished(session_id). Neither method blocks.
    read_* returns bounded, session-scoped snapshots (<=64 rows), preserving
    original report indices. disarm cancels pending TX and clears marking and
    responder state. restore_ack restores every saved response context. Both
    cleanup methods are idempotent, including after partially failed setup.
    """
    def arm_responder(self, peer: Peer, session_id: bytes, width_mhz: int): ...
    def disarm(self): ...
    def restore_ack(self): ...
    def send_burst(self, peer: Peer, frames: Sequence[bytes], width_mhz: int,
                   interval_ms: int): ...
    def read_responder_report(self, session_id: bytes) -> Sequence[ResponderStamp]: ...
    def read_initiator_timestamps(self, session_id: bytes) -> Sequence[InitiatorStamp]: ...


@dataclass(frozen=True)
class Config:
    frames: int = 50
    interval_ms: int = 50
    ready_timeout_s: float = 1.0
    arm_grace_s: float = 1.5  # Includes default helper's 1 s off/on preparation and drain.
    report_timeout_s: float = 1.0
    settle_s: float = 0.1
    peer_cooldown_s: float = 5.0
    request_gap_s: float = 1.0
    replay_ttl_s: float = 60.0
    result_window_s: float = 2.0
    max_drift_ppm: float = 40.0
    min_pairs: int = 10
    max_peers: int = 64

    def __post_init__(self):
        times = (self.ready_timeout_s, self.arm_grace_s, self.report_timeout_s,
                 self.settle_s, self.peer_cooldown_s, self.request_gap_s, self.replay_ttl_s,
                 self.result_window_s)
        if (not 1 <= self.frames <= MAX_FRAMES or not 20 <= self.interval_ms <= 100 or
                not 1 <= self.min_pairs <= self.frames or not 1 <= self.max_peers <= 256 or
                any(not math.isfinite(t) or not 0 < t <= 3600 for t in times) or
                self.arm_grace_s < self.settle_s or self.arm_grace_s > 3 or
                self.result_window_s > 10 or not math.isfinite(self.max_drift_ppm) or
                self.max_drift_ppm < 0):
            raise ValueError("invalid ranging limits")


@dataclass(frozen=True)
class Outbound:
    peer: Peer
    data: bytes  # Control traffic may use any mesh path; bursts go through Radio.


@dataclass(frozen=True)
class PositionSnapshot:
    received_mono: float
    message_kind: str
    position: pb.Position


@dataclass(frozen=True)
class PeerClaim:
    """Unverified peer-reported values; never a locally computed Estimate."""
    range_m: float
    p25_counts: float
    pair_count: int


@dataclass(frozen=True)
class Result:
    peer: Peer
    session_id: bytes
    status: str
    estimate: Optional[Estimate] = None
    positions: tuple = ()  # Ready + each report's fresh snapshot, no Alfred data.
    retry_ms: int = 0
    # Fixed orientation on BOTH roles: initiator at burst start, responder at
    # Ready. Later report snapshots remain in positions for freshness context.
    initiator_position: Optional[pb.Position] = None
    responder_position: Optional[pb.Position] = None
    peer_claim: Optional[PeerClaim] = None


@dataclass(frozen=True)
class _ServedSession:
    challenge: bytes
    expires: float
    responder_position: pb.Position
    reported_pairs: int


@dataclass
class _Session:
    peer: Peer
    session_id: bytes
    role: str
    deadline: float
    request: pb.Request
    challenge: bytes = b""
    state: str = "waiting_ready"
    report_due: Optional[float] = None
    observed: set = field(default_factory=set)
    reports: dict = field(default_factory=dict)
    positions: list = field(default_factory=list)
    burst_done: bool = False
    initiator_position: Optional[pb.Position] = None
    responder_position: Optional[pb.Position] = None
    schedule_us: tuple = ()


class Ranging:
    """Both roles share one radio lock. Authorization defaults to deny.

    eligible(peer) must check the current 5 GHz mesh role, width and direct
    adjacency. position() returns a NEW current Position, with age from a local
    monotonic clock; absent UTC/height/uncertainty stays absent. Unknown/invalid
    positions are allowed for relative ranging. No configuration or trust state
    is changed by an exchange. Call close() in the event loop's finally block.
    Successful initiators send one Result claim, without waiting for a reply.
    Responders expose it as status="peer_claim", peer_claim=PeerClaim, with
    estimate=None. It must not be treated as an independent measurement or
    automatically clear GNSS/time distrust. Both roles expose the same two
    canonical Position snapshots separately from the fresh report snapshots.
    """
    def __init__(self, radio: Radio, clock: Callable[[], float], position,
                 calibration: Calibration, *, authorize=lambda peer: False,
                 eligible=lambda peer: False, config=Config(), nonce=None):
        self.radio, self.clock, self.position = radio, clock, position
        self.calibration, self.config = calibration, config
        self.authorize, self.eligible = authorize, eligible
        self.nonce = nonce or (lambda: secrets.token_bytes(16))
        self.active = None
        self.queue = deque()
        self.outbox = deque(maxlen=256)
        self.results = deque(maxlen=256)
        self.faulted = False
        self.last_error = None
        self.closed = False
        self._peers = {}  # peer -> (next request response, next admitted session)
        self._seen = {}   # (peer, session) -> expiry; bounded replay cache.
        self._served = {} # (peer, session) -> short-lived result acceptance slot.
        self._nonces = deque(maxlen=256)
        self._last_now = -math.inf

    def _now(self):
        now = self.clock()
        if not math.isfinite(now) or now < self._last_now:
            # A broken clock must not leave the responder armed.
            self._cleanup()
            self.active = None
            self.queue.clear()
            self._served.clear()
            self.faulted = True
            raise ValueError("clock must be finite and monotonic")
        self._last_now = now
        return now

    def _nonce(self):
        value = self.nonce()
        if (not isinstance(value, bytes) or len(value) != 16 or not any(value) or
                value in self._nonces):
            raise ValueError("nonce must be a fresh nonzero 128-bit value")
        self._nonces.append(value)
        return value

    def _position(self):
        position = pb.Position()
        position.CopyFrom(self.position())
        validate_position(position)
        return position

    def _message(self, sid, kind, body, challenge=b"", position=None):
        message = pb.Envelope(version=VERSION, session_id=sid, challenge=challenge)
        getattr(message, kind).CopyFrom(body)
        if position is not None:
            message.position.CopyFrom(position)
        return message

    def _send(self, peer, message):
        self.outbox.append(Outbound(peer, encode(message)))

    def _reply(self, peer, sid, kind, body, challenge=b""):
        self._send(peer, self._message(sid, kind, body, challenge, self._position()))

    def _cleanup(self):
        errors = []
        for action in (self.radio.disarm, self.radio.restore_ack):
            try:
                action()
            except Exception as exc:
                errors.append(f"{action.__name__}: {exc}")
        if errors:
            self.last_error = "; ".join(errors)
            self.faulted = True
        return not errors

    def _terminal(self, status, *, estimate=None, retry_ms=0, cancel_reason=None):
        session = self.active
        if session is None:
            return
        clean = self._cleanup()
        self.active = None
        if not clean:
            status, estimate, cancel_reason = "cleanup_failed", None, pb.Cancel.ERROR
        if cancel_reason is not None:
            # Even a failing position provider must not prevent cleanup/cancel.
            position = None
            if session.role == "responder":
                try:
                    position = self._position()
                except Exception:
                    position = pb.Position(valid=False)
            self._send(session.peer, self._message(
                session.session_id, "cancel", pb.Cancel(reason=cancel_reason),
                session.challenge, position))
        if session.role == "initiator":
            self.results.append(Result(session.peer, session.session_id, status, estimate,
                                       tuple(session.positions), retry_ms,
                                       session.initiator_position, session.responder_position))
        if self.faulted:
            while self.queue:
                self.results.append(Result(self.queue.popleft(), b"", "cleanup_failed"))

    def recover(self):
        """Explicitly retry failed cleanup before accepting more radio work."""
        if self.active:
            raise RuntimeError("session active")
        if self._cleanup():
            self.faulted = False
            self.last_error = None
        return not self.faulted

    def _prune(self, now):
        self._seen = {key: expiry for key, expiry in self._seen.items() if expiry > now}
        self._served = {key: served for key, served in self._served.items() if served.expires > now}
        self._peers = {peer: limits for peer, limits in self._peers.items()
                       if max(limits) > now or (self.active and peer == self.active.peer)}

    def start(self, peers):
        """Queue one pass over anchors. Busy results expose retry_ms; no spin retry."""
        if self.closed or self.faulted or self.active or self.queue:
            raise RuntimeError("ranging unavailable or already active")
        peers = tuple(peers)
        if len(peers) > 16 or len(set(peers)) != len(peers):
            raise ValueError("at most 16 distinct anchors")
        self.queue.extend(peers)
        self.tick()

    def _next(self, now):
        while self.queue and not self.active and not self.faulted:
            peer = self.queue.popleft()
            if not self.authorize(peer) or not self.eligible(peer):
                self.results.append(Result(peer, b"", "unavailable"))
                continue
            limit = self._peers.get(peer, (0, 0))[1]
            if limit > now:
                self.results.append(Result(peer, b"", "rate_limited", retry_ms=math.ceil((limit - now) * 1000)))
                continue
            if peer not in self._peers and len(self._peers) >= self.config.max_peers:
                self.results.append(Result(peer, b"", "rate_limited", retry_ms=1000))
                continue
            request = pb.Request(frames=self.config.frames, interval_ms=self.config.interval_ms,
                                 channel_width_mhz=self.calibration.channel_width_mhz)
            sid = self._nonce()
            self.active = _Session(peer, sid, "initiator", now + self.config.ready_timeout_s, request)
            self._peers[peer] = (now, now + self.config.peer_cooldown_s)
            self._send(peer, self._message(sid, "request", request))

    def tick(self):
        now = self._now()
        self._prune(now)
        session = self.active
        if session:
            try:
                if not self.authorize(session.peer) or not self.eligible(session.peer):
                    self._terminal("unavailable", cancel_reason=pb.Cancel.ERROR)
                elif session.role == "responder" and (
                        now >= session.deadline or
                        (session.report_due is not None and now >= session.report_due)):
                    self._report()
                elif now >= session.deadline:
                    self._terminal("timeout", cancel_reason=pb.Cancel.TIMEOUT)
            except Exception as exc:
                self.last_error = str(exc)
                self._terminal("radio_error", cancel_reason=pb.Cancel.ERROR)
        if not self.closed:
            self._next(now)

    def receive(self, peer, data, *, direct=False):
        """Receive authenticated control data, or a probe verified on direct 5 GHz.

        Malformed/unrelated packets are ignored. They never extend a lease or
        abort another session. Authorization is checked before allocating state.
        """
        self.tick()
        if self.closed or self.faulted:
            return
        try:
            if not self.authorize(peer):
                return
            message = decode(data)
        except (ValueError, TypeError):
            return
        kind = message.WhichOneof("body")
        try:
            if kind == "result":
                self._accept_result(peer, message)
                return  # A completed session's claim never touches an active radio.
            if kind == "request":
                self._request(peer, message)
                return
            session = self.active
            if not session or peer != session.peer or message.session_id != session.session_id:
                return
            if session.role == "responder":
                if kind == "cancel" and message.challenge in (b"", session.challenge):
                    self._terminal("cancelled")
                elif kind == "burst" and direct and message.challenge == session.challenge:
                    frame = message.burst
                    if frame.sequence >= session.request.frames or (
                            frame.done != (frame.sequence == session.request.frames - 1)):
                        return
                    session.observed.add(frame.sequence)
                    if frame.done and session.report_due is None:
                        session.report_due = self._last_now + self.config.settle_s
                return
            if not message.HasField("position"):
                return
            if session.state == "waiting_ready":
                if kind == "ready" and len(message.challenge) == 16 and any(message.challenge):
                    schedule = burst_schedule_us(session.session_id, message.challenge,
                                                 session.request.frames, session.request.interval_ms)
                    duration_ms = math.ceil(schedule[-1] / 1000)
                    max_arm_ms, min_arm_ms = duration_ms + 3000, duration_ms + 100
                    if not min_arm_ms <= message.ready.arm_timeout_ms <= max_arm_ms:
                        self._terminal("invalid_ready", cancel_reason=pb.Cancel.ERROR)
                        return
                    session.challenge = message.challenge
                    session.schedule_us = schedule
                    session.responder_position = message.position
                    session.positions.append(PositionSnapshot(self._last_now, kind, message.position))
                    session.state = "waiting_report"
                    session.deadline = (self._last_now + message.ready.arm_timeout_ms / 1000 +
                                        self.config.report_timeout_s)
                    frames = tuple(encode(self._message(session.session_id, "burst", pb.Burst(
                        sequence=seq, done=seq == session.request.frames - 1), session.challenge))
                                   for seq in range(session.request.frames))
                    session.initiator_position = self._position()
                    self.radio.send_burst(peer, frames, self.calibration.channel_width_mhz,
                                          session.request.interval_ms)
                elif kind in ("busy", "reject", "cancel") and not message.challenge:
                    session.positions.append(PositionSnapshot(self._last_now, kind, message.position))
                    retry_ms = min(max(message.busy.retry_ms, 1), 60000) if kind == "busy" else 0
                    if retry_ms:
                        gap, limit = self._peers[peer]
                        self._peers[peer] = (gap, max(limit, self._last_now + retry_ms / 1000))
                    self._terminal(kind, retry_ms=retry_ms)
                return
            if message.challenge != session.challenge:
                return
            if kind in ("cancel", "reject"):
                session.positions.append(PositionSnapshot(self._last_now, kind, message.position))
                self._terminal(kind)
            elif kind == "report":
                self._accept_report(message)
        except Exception as exc:
            self.last_error = str(exc)
            # Request errors for a second peer must never tear down the owner.
            if self.active and self.active.peer == peer and self.active.session_id == message.session_id:
                self._terminal("radio_error", cancel_reason=pb.Cancel.ERROR)

    def _request(self, peer, message):
        now, request = self._last_now, message.request
        if message.challenge:
            return
        if (peer, message.session_id) in self._seen:
            return  # Duplicate request cannot renew arming or elicit a fresh ready.
        limits = self._peers.get(peer)
        if limits is None:
            if len(self._peers) >= self.config.max_peers:
                return
            limits = (0, 0)
        if now < limits[0]:
            return
        self._peers[peer] = (now + self.config.request_gap_s, limits[1])
        if self.active or now < limits[1]:
            delay = max(0.001, limits[1] - now,
                        self.active.deadline - now if self.active else 0)
            self._reply(peer, message.session_id, "busy", pb.Busy(retry_ms=math.ceil(delay * 1000)))
            return
        if (not 1 <= request.frames <= MAX_FRAMES or not 20 <= request.interval_ms <= 100 or
                request.channel_width_mhz != self.calibration.channel_width_mhz):
            self._reply(peer, message.session_id, "reject", pb.Reject(reason=pb.Reject.UNSUPPORTED))
            return
        if not self.eligible(peer):
            self._reply(peer, message.session_id, "reject", pb.Reject(reason=pb.Reject.UNAVAILABLE))
            return
        position = self._position()  # Obtain/validate before touching the radio.
        challenge = self._nonce()
        schedule = burst_schedule_us(message.session_id, challenge, request.frames, request.interval_ms)
        lease = schedule[-1] / 1e6 + self.config.arm_grace_s
        session = _Session(peer, message.session_id, "responder", now + lease, request,
                           challenge, "armed", responder_position=position, schedule_us=schedule)
        self.active = session  # Partial arm failures still execute both cleanup steps.
        self._peers[peer] = (now + self.config.request_gap_s,
                             now + lease + self.config.peer_cooldown_s)
        if len(self._seen) >= 256:
            del self._seen[next(iter(self._seen))]
        self._seen[(peer, message.session_id)] = now + lease + self.config.replay_ttl_s
        self.radio.arm_responder(peer, session.session_id, request.channel_width_mhz)
        self._send(peer, self._message(session.session_id, "ready",
                   pb.Ready(arm_timeout_ms=math.ceil(lease * 1000)), session.challenge, position))

    def _report(self):
        session = self.active
        rows = self.radio.read_responder_report(session.session_id)
        if len(rows) > MAX_FRAMES:
            raise ValueError("oversized radio report")
        rows = [row for row in corrected_responder(rows, self.calibration)
                if row.sequence in session.observed]
        messages = []
        if not rows:
            messages.append(self._message(session.session_id, "reject",
                            pb.Reject(reason=pb.Reject.NO_SAMPLES), session.challenge, self._position()))
        else:
            parts = math.ceil(len(rows) / REPORT_ROWS)
            for part in range(parts):
                chunk = rows[part * REPORT_ROWS:(part + 1) * REPORT_ROWS]
                base = chunk[0].arrival
                report = pb.Report(part=part, parts=parts, base_arrival=base,
                                   sequence=[row.sequence for row in chunk],
                                   arrival_delta=[(row.arrival - base) % CLOCK_MODULUS for row in chunk],
                                   turnaround=[row.turnaround for row in chunk])
                messages.append(self._message(session.session_id, "report", report,
                                              session.challenge, self._position()))
        # Encode before releasing so serialization errors take the same cleanup path.
        payloads = [encode(message) for message in messages]
        self._terminal("reported")
        if not self.faulted:
            if rows:
                # This is bookkeeping only: the radio is already released. Keep
                # only the newest served session per peer, and cap total slots.
                self._served = {key: value for key, value in self._served.items()
                                if key[0] != session.peer}
                if len(self._served) >= self.config.max_peers:
                    del self._served[next(iter(self._served))]
                self._served[(session.peer, session.session_id)] = _ServedSession(
                    session.challenge, self._last_now + self.config.result_window_s,
                    session.responder_position, len(rows))
            self.outbox.extend(Outbound(session.peer, data) for data in payloads)

    def _accept_result(self, peer, message):
        key = (peer, message.session_id)
        served = self._served.get(key)
        if served is None or message.challenge != served.challenge:
            return
        claim = message.result
        if (not claim.HasField("initiator_position") or
                not claim.HasField("range_m") or not claim.HasField("p25_counts") or
                not math.isfinite(claim.range_m) or not math.isfinite(claim.p25_counts) or
                not -(1 << 31) < claim.p25_counts < (1 << 31) or
                not 1 <= claim.pair_count <= served.reported_pairs):
            return
        try:
            validate_position(claim.initiator_position)
        except ValueError:
            return
        del self._served[key]  # Consume once; duplicates cannot repeat evidence.
        self.results.append(Result(
            peer, message.session_id, "peer_claim",
            initiator_position=claim.initiator_position,
            responder_position=served.responder_position,
            peer_claim=PeerClaim(claim.range_m, claim.p25_counts, claim.pair_count)))

    def _accept_report(self, message):
        session, report = self.active, message.report
        count = len(report.sequence)
        if (not 1 <= report.parts <= math.ceil(session.request.frames / REPORT_ROWS) or
                report.part >= report.parts or not 1 <= count <= REPORT_ROWS or
                len(report.arrival_delta) != count or len(report.turnaround) != count or
                not _timestamp(report.base_arrival) or
                any(delta >= CLOCK_MODULUS // 2 for delta in report.arrival_delta) or
                any(turn < 0 for turn in report.turnaround) or
                any(seq >= session.request.frames for seq in report.sequence) or
                len(set(report.sequence)) != count):
            self._terminal("invalid_report", cancel_reason=pb.Cancel.ERROR)
            return
        if report.part in session.reports:
            if session.reports[report.part] != report:
                self._terminal("invalid_report", cancel_reason=pb.Cancel.ERROR)
            return
        for previous in session.reports.values():
            if previous.parts != report.parts or set(previous.sequence) & set(report.sequence):
                self._terminal("invalid_report", cancel_reason=pb.Cancel.ERROR)
                return
        session.reports[report.part] = report
        session.positions.append(PositionSnapshot(self._last_now, "report", message.position))
        self._estimate()

    def _estimate(self):
        session = self.active
        if not session.burst_done or not session.reports:
            return
        parts = next(iter(session.reports.values())).parts
        if len(session.reports) != parts:
            return
        local = self.radio.read_initiator_timestamps(session.session_id)
        if len(local) > MAX_FRAMES:
            raise ValueError("oversized initiator capture")
        remote = [RemoteStamp(seq, (report.base_arrival + delta) % CLOCK_MODULUS, turn)
                  for report in session.reports.values()
                  for seq, delta, turn in zip(report.sequence, report.arrival_delta, report.turnaround)]
        try:
            estimate = pair_timestamps(local, remote, self.calibration, self.config.min_pairs,
                                       max_drift_ppm=self.config.max_drift_ppm)
        except ValueError:
            self._terminal("insufficient_pairs", cancel_reason=pb.Cancel.ERROR)
            return
        claim = pb.Result(range_m=estimate.range_m, p25_counts=estimate.percentile_counts,
                          pair_count=len(estimate.differences),
                          initiator_position=session.initiator_position)
        if not math.isfinite(claim.range_m):
            self._terminal("invalid_estimate", cancel_reason=pb.Cancel.ERROR)
            return
        payload = encode(self._message(session.session_id, "result", claim, session.challenge))
        self._terminal("ok", estimate=estimate)
        if not self.faulted:
            self.outbox.append(Outbound(session.peer, payload))

    def burst_finished(self, session_id):
        self.tick()
        session = self.active
        if (not session or session.role != "initiator" or session.state != "waiting_report" or
                session.session_id != session_id or session.burst_done):
            return
        session.burst_done = True
        try:
            self._estimate()
        except Exception as exc:
            self.last_error = str(exc)
            self._terminal("radio_error", cancel_reason=pb.Cancel.ERROR)

    def radio_error(self, session_id, error):
        if self.active and self.active.session_id == session_id:
            self.last_error = str(error)
            self._terminal("radio_error", cancel_reason=pb.Cancel.ERROR)

    def cancel(self):
        """Cancel this local job and all queued anchors, or the current responder."""
        self.queue.clear()
        self._terminal("cancelled", cancel_reason=pb.Cancel.USER)

    def close(self):
        self.closed = True
        self._served.clear()
        had_session = self.active is not None
        self.cancel()
        if not had_session:
            self._cleanup()
