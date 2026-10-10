#!/usr/bin/env python3
"""Run the R4 positioning service or a paced simulation without sockets.

One event-loop thread owns Ranging, Monitor, Scheduler and the solver.
Linux discovery is asynchronous and read-only. HelperRadio is the unavailable
R3 helper boundary, selected by positioning_radio=radio_adapter. It does not
select diagnostic_only or provide privileged radio access or a listener.
Production requires helper authorization, mesh-port ingress proof, scoped
probes, a watchdog and verified capture capabilities.

Optional mesh.conf settings:
  positioning=n, positioning_radio=radio_adapter
  positioning_interface=wlan1, positioning_node_id=<br0 MAC>
  positioning_gnss_sigma_m=5, positioning_vertical_sigma_m=8
  positioning_phone_best_guess=n, positioning_mark_priority=manual|newer
Sigmas are modelling assumptions, not conversions of gpsd eph/HDOP.
The last two switches set the policy exported to the ATAK consumer.

--simulate 4 uses three GNSS anchors and one GPS-less node. N=4..16 runs
real service objects, wire messages, jittered bursts and timestamp pairing
without sockets, devices, wall-clock sleeps or /run writes.
"""

import argparse
import asyncio
import base64
from collections import deque
from dataclasses import dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import statistics
import time
import uuid

from google.protobuf.message import DecodeError
import NodeInfo_pb2 as node_pb
import manet_locate as locate
import manet_ranging as ranging
import manet_ranging_radio as radio_api
import manet_spoof as spoof
import ranging_pb2 as pb

# Freshness limits in seconds for status files, fixes and Alfred hints.
INPUT_TTL = 6.0
GPS_TTL = 5.0
HINT_TTL = 240.0
# Bound each JSON input and publication to 128 KiB.
MAX_JSON = 131072
# Match six colon-separated hexadecimal MAC octets.
MAC = r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}'
QUALITIES = {'good', 'best_guess', 'candidates', 'ring', 'none'}


def number(value):
    """Require a finite numeric value, excluding booleans."""
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('finite number required')
    return float(value)


def read_json(path):
    """Read a bounded JSON object and reject nonfinite constants."""
    with open(path, 'rb') as stream:
        data = stream.read(MAX_JSON + 1)
    if len(data) > MAX_JSON:
        raise ValueError('oversized JSON')
    # Reject JSON NaN/Infinity tokens through the numeric validator.
    doc = json.loads(data, parse_constant=lambda x: number(x))
    if not isinstance(doc, dict):
        raise ValueError('object required')
    return doc


def atomic_json(path, value):
    """Replace a JSON publication without exposing a partial write."""
    path = Path(path)
    data = json.dumps(value, allow_nan=False, separators=(',', ':')).encode()
    if len(data) > MAX_JSON:
        raise ValueError('oversized output')
    # The single writer holds the runtime lock and uses fixed temp paths.
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('wb') as stream:
        stream.write(data)
    os.replace(temporary, path)


def read_config(path):
    """Read literal mesh.conf assignments without shell evaluation."""
    result = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            result[key.strip()] = value.strip().strip('\"\'')
    return result


@dataclass(frozen=True)
class Config:
    """Hold positioning policy, uncertainty assumptions and solve limits."""
    node_id: str
    enabled: bool = False
    radio: str = 'radio_adapter'
    interface: str = 'wlan1'
    gnss_sigma_m: float = 5.0
    vertical_sigma_m: float = 8.0
    phone_best_guess: bool = False
    mark_priority: str = 'manual'
    solve_interval_s: float = 15.0
    max_backoff_s: float = 60.0
    result_ttl_s: float = 90.0

    def __post_init__(self):
        """Validate identifiers, policy values and positive numeric limits."""
        # Interface names allow 15 safe characters with no leading dot.
        if (not isinstance(self.node_id, str) or not 0 < len(self.node_id.encode()) <= 32
                or self.radio not in ('radio_adapter', 'simulated')
                or not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,14}', self.interface)
                or self.mark_priority not in ('manual', 'newer')
                or any(not 0 < number(x) <= 1000 for x in
                       (self.gnss_sigma_m, self.vertical_sigma_m, self.solve_interval_s,
                        self.max_backoff_s, self.result_ttl_s))):
            raise ValueError('invalid positioning configuration')

    @classmethod
    def load(cls, path='/etc/mesh.conf'):
        """Load opt-in positioning settings and the local node identity."""
        c = read_config(path)
        enabled = c.get('positioning', 'n').lower() == 'y'
        node_id = c.get('positioning_node_id')
        # A disabled service needs no live bridge identity.
        if not node_id:
            node_id = Path('/sys/class/net/br0/address').read_text().strip() if enabled else 'disabled'
        return cls(node_id, enabled, c.get('positioning_radio', 'radio_adapter'),
                   c.get('positioning_interface', 'wlan1'),
                   float(c.get('positioning_gnss_sigma_m', 5)),
                   float(c.get('positioning_vertical_sigma_m', 8)),
                   c.get('positioning_phone_best_guess', 'n').lower() == 'y',
                   c.get('positioning_mark_priority', 'manual'))


@dataclass(frozen=True)
class Now:
    """Carry monotonic, raw and suspend-aware boot times in seconds."""
    mono: float
    raw: float
    boot: float

    @classmethod
    def read(cls):
        """Read the three local clocks used for freshness and scheduling."""
        return cls(time.monotonic(), time.clock_gettime(time.CLOCK_MONOTONIC_RAW),
                   time.clock_gettime(time.CLOCK_BOOTTIME))


@dataclass(frozen=True)
class Receiver:
    """Carry a local GNSS fix, its monotonic epoch and trust inputs."""
    fix: dict | None = None
    observed: float = 0.0
    devices: bool | None = None
    had_fix: bool = False
    suspect: bool = False
    jammed: bool = False
    time_ok: bool = False


def receiver_document(doc, now, boot_id, jammed=False):
    """Map raw-clock receiver epochs locally without remote or wall time."""
    # BOOTTIME bounds file age across suspend, within the current boot.
    if (doc.get('schema') != 2 or doc.get('boot_id') != boot_id or
            doc.get('clock') != 'monotonic_raw' or
            not 0 <= now.boot - number(doc.get('written_boot')) < INPUT_TTL):
        raise ValueError('stale/wrong-boot receiver')
    devices = doc.get('devices')
    if devices is not None and not isinstance(devices, list):
        raise ValueError('invalid receiver list')
    sample = doc.get('sample')
    fix, observed = None, now.mono
    if doc.get('has_fix') is True and isinstance(sample, dict):
        # Translate only the sample's age between the two local clocks.
        age = now.raw - number(sample.get('mono'))
        if sample.get('mode', 0) >= 2 and 0 <= age < GPS_TTL:
            lat, lon = number(sample.get('lat')), number(sample.get('lon'))
            if not -90 <= lat <= 90 or not -180 <= lon <= 180:
                raise ValueError('invalid GNSS point')
            hae = sample.get('alt_hae')
            fix = {'lat': lat, 'lon': lon, 'hae': None if hae is None else number(hae)}
            observed = now.mono - age
    events = doc.get('events', [])
    if not isinstance(events, list):
        raise ValueError('invalid event list')
    # Mark events within 60 s as suspect input for audit scheduling.
    suspect = any(isinstance(e, dict) and type(e.get('mono')) in (int, float)
                  and 0 <= now.raw - e['mono'] < 60 for e in events)
    return Receiver(fix, observed, None if devices is None else bool(devices),
                    doc.get('first_fix_mono') is not None, suspect, jammed,
                    doc.get('time_ok') is True)


@dataclass(frozen=True)
class Neighbour:
    """Pair a discovered radio peer with optional anchor metadata."""
    peer: ranging.Peer
    anchor: locate.Anchor | None
    gnss: bool = False
    eligible_anchor: bool = False


@dataclass(frozen=True)
class Inbound:
    """Represent a helper-authenticated datagram.

    Direct means verified 5 GHz probe ingress.
    """
    peer: ranging.Peer
    data: bytes
    direct: bool = False


def alfred_records(text, message_type):
    """Decode bounded Alfred MAC/base64 records into protobuf messages."""
    records = {}
    # Alfred emits brace-delimited MAC and base64 strings.
    for mac, payload in re.findall(r'\{\s*"(' + MAC + r')"\s*,\s*"([A-Za-z0-9+/=]+)"\s*\}', text, re.I):
        # Keep at most 64 records, each with at most 16 KiB of base64.
        if len(records) >= 64 or len(payload) > 16384:
            continue
        try:
            message = message_type()
            message.ParseFromString(base64.b64decode(payload, validate=True))
            records[mac.lower()] = (message, payload)
        except (ValueError, DecodeError):
            continue
    return records


class Discovery:
    """Read type 68 candidate hints, never Monitor samples or range fixes.

    Type 67 maps Alfred primary MACs to station secondary MACs. The helper
    must authenticate the map because advertisements do not authorize peers.
    Unchanged payloads age locally even on repeated reads.
    """
    def __init__(self, config):
        """Start a local payload-age cache for the configured interface."""
        self.config, self.seen = config, {}

    def parse(self, identities, telemetry, neighbours, stations, now):
        """Join telemetry hints with direct BATMAN and iw station evidence."""
        identities = alfred_records(identities, node_pb.NodeIdentity)
        telemetry = alfred_records(telemetry, node_pb.NodeTelemetry)
        direct = set()
        # BATMAN brackets the outgoing interface in direct-neighbour rows.
        for line in neighbours.lower().splitlines():
            if self.config.interface.lower() in line.replace('[', ' ').replace(']', ' ').split():
                direct.update(re.findall(MAC, line))
        signals, current = {}, None
        # iw groups signal values in dBm under each Station MAC header.
        for line in stations.lower().splitlines():
            m = re.match(r'station (' + MAC + r')\b', line)
            if m:
                current = m[1]
            m = re.search(r'^\s*signal(?: avg)?:\s*(-?\d+)', line)
            if m and current:
                signals[current] = float(m[1])
        output, retained = {}, {}
        # Stable node order determines which peers fit the 15-peer budget.
        for node_id, (t, payload) in sorted(telemetry.items()):
            # Repeated unchanged advertisements retain their first local age.
            digest = hashlib.sha256(payload.encode()).digest()
            old = self.seen.get(node_id)
            first = old[1] if old and old[0] == digest else now
            retained[node_id] = (digest, first)
            if now - first > HINT_TTL or node_id == self.config.node_id:
                continue
            macs = {node_id}
            if node_id in identities:
                macs.update(m.hex(':') for m in identities[node_id][0].mac_addresses if len(m) == 6)
            # Require one station MAC shared by node aliases and both probes.
            links = sorted(macs & direct & signals.keys())
            if len(links) != 1 or len(output) >= 15:
                continue
            mac = links[0]
            try:
                peer = ranging.Peer(node_id, bytes.fromhex(mac.replace(':', '')))
                anchor, gnss, eligible = None, False, False
                if t.HasField('location'):
                    p = t.location
                    gnss = p.source == p.GNSS
                    source = {p.GNSS: 'gnss', p.RANGED: 'ranged', p.MANUAL: 'manual'}.get(p.source)
                    eligible = (p.HasField('valid') and p.valid and p.anchor_eligible and
                                source is not None and (source != 'ranged' or p.quality == 'good'))
                    if eligible:
                        sigma = p.uncertainty_m if p.HasField('uncertainty_m') else self.config.gnss_sigma_m
                        # Retain a ranged CE95 conservatively as sigma, like R1.
                        if source != 'ranged' and p.HasField('uncertainty_m'):
                            sigma /= locate.CE95
                        anchor = locate.Anchor(node_id, p.latitude_e7 / 1e7, p.longitude_e7 / 1e7,
                            sigma, p.hae_m if p.HasField('hae_m') else None,
                            self.config.vertical_sigma_m, source, p.generation,
                            tuple(p.used_node_ids), signals[mac])
                        if locate._anchor_reason(anchor, self.config.node_id):
                            anchor, eligible = None, False
                output[node_id] = Neighbour(peer, anchor, gnss, eligible)
            except (ValueError, TypeError):
                continue
        self.seen = retained
        return output


class HelperRadio:
    """Define the fixed-operation helper contract without IPC or radio access.

    The helper must support Radio's six operations, poll, authenticated control
    send/receive and authorization. It must check SO_PEERCRED, bound messages,
    reject caller paths/commands, verify bat0 vs EUD and direct 5 GHz ingress,
    and clean up on disconnect/lease expiry. Do not instantiate privileged
    RadioAdapter here.
    """
    can_range = False
    reason = 'R3 helper unavailable: ' + radio_api.ASSOCIATION_GAP

    def authorize(self, peer):
        """Deny peers while the authenticated helper is unavailable."""
        return False

    def poll(self):
        """Return no events from the unavailable helper."""
        return ()

    def arm_responder(self, *args):
        """Reject operations that require an available radio helper."""
        raise radio_api.AssociationUnavailable(self.reason)

    send_burst = arm_responder
    read_responder_report = arm_responder
    read_initiator_timestamps = arm_responder
    send_control = arm_responder

    def disarm(self):
        """Leave the placeholder's unowned radio state untouched."""
        pass

    def restore_ack(self):
        """Leave ACK settings untouched because none are acquired."""
        pass

    def close(self):
        """Finish the placeholder with no helper resources to release."""
        pass


class Service:
    """Coordinate ranging, GNSS audits, fallback solves and publications."""
    def __init__(self, config, radio, clock, boot_id, *, checkpoint=None, nonce=None):
        """Create service state and optionally restore same-boot quarantine."""
        self.config, self.radio, self.clock, self.boot_id = config, radio, clock, boot_id
        self.monitor = spoof.Monitor()
        self.scheduler = spoof.Scheduler(self.monitor)
        self._states_cache, self._states_due = None, 0.0
        self.receiver, self.neighbours, self.mesh = Receiver(), {}, False
        self.producer_id, self.fault_epoch = uuid.uuid4().hex, 0
        self.previous_failure = None
        self.had_fix, self.neighbours_since = False, None
        self.result, self.result_at = None, 0.0
        self.previous = None
        self.job, self.observations, self.sessions = None, [], {}
        self.next_solve, self.next_audit = 0.0, 0.0
        # Spread initial audits over 5..14.99 s using a stable node hash.
        self.next_audit = clock().mono + 5 + int.from_bytes(
            hashlib.sha256(config.node_id.encode()).digest()[:2], 'big') % 1000 / 100
        self.backoff = config.solve_interval_s
        self.events = deque(maxlen=256)
        self.core = ranging.Ranging(radio, lambda: clock().mono, self.position_snapshot,
            ranging.Calibration.measured_20mhz(6500), authorize=radio.authorize,
            eligible=self.eligible, nonce=nonce)
        # 6500 is the simulated intercept only. Hardware requires
        # helper-supplied, validated per-node calibration.
        if checkpoint is not None:
            self.restore(checkpoint)

    def eligible(self, peer):
        """Require enabled ranging and a matching current mesh neighbour."""
        n = self.neighbours.get(peer.node_id)
        return bool(self.config.enabled and self.radio.can_range and self.mesh and
                    n is not None and n.peer == peer)

    def update(self, receiver, neighbours=None, *, mesh=None):
        """Refresh receiver, neighbour and mesh-role inputs."""
        self.receiver = receiver
        self.had_fix |= receiver.had_fix or receiver.fix is not None
        if neighbours is not None:
            # Retain at most 15 peers in node-ID order.
            self.neighbours = dict(sorted(neighbours.items())[:15])
        if mesh is not None:
            self.mesh = mesh
        now = self.clock().mono
        # This map is for current-fix state, not alignment. feed_monitor scopes
        # alignment to the immutable exchange snapshots and restores this map.
        fix = receiver.fix if self.fresh_receiver() else None
        self.monitor.samples[self.config.node_id] = deque([{
            'mono': receiver.observed if fix else now, 'mode': 3 if fix else 0,
            'lat': fix['lat'] if fix else None, 'lon': fix['lon'] if fix else None,
            'alt_hae': fix.get('hae') if fix else None}])
        has_neighbour = any(n.gnss for n in self.neighbours.values())
        self.neighbours_since = (self.neighbours_since if self.neighbours_since is not None else now) if has_neighbour else None

    def fresh_receiver(self):
        """Check the local fix against the five-second freshness budget."""
        return self.receiver.fix is not None and 0 <= self.clock().mono - self.receiver.observed < GPS_TTL

    def monitor_states(self, *, force=False):
        # Attribution can enumerate subsets. Refresh on evidence immediately,
        # otherwise at 4 Hz, not on every probe, poll and publication call.
        """Cache attribution results between evidence or timed refreshes."""
        now = self.clock().mono
        if force or self._states_cache is None or now >= self._states_due:
            self._states_cache = self.monitor.states(now)
            self._states_due = now + .25
        return self._states_cache

    def state(self):
        """Combine local fix freshness, quarantine and range evidence."""
        now = self.clock().mono
        state = self.monitor_states().get(self.config.node_id, (spoof.UNCHECKED, {}))[0]
        last = self.monitor.last_failure.get(self.config.node_id)
        if last is not None and now - last <= spoof.QUARANTINE_S:
            return spoof.SUSPECTED if state == spoof.SUSPECTED else spoof.INCONSISTENT
        if not self.fresh_receiver():
            return spoof.NO_FIX
        # A passing peer claim alone cannot establish local consistency.
        if state == spoof.RANGE_CONSISTENT and not any(
                self.config.node_id in (e['a'], e['b']) and not e.get('peer_claim') and
                e['in_scope'] and not e['disagrees'] and now - e['mono'] <= spoof.EDGE_TTL_S
                for e in self.monitor.edges.values()):
            return spoof.UNCHECKED
        return state if state != spoof.NO_FIX else spoof.UNCHECKED

    def trusted(self):
        """Require a fresh unjammed fix with no distrusted monitor state."""
        return self.fresh_receiver() and not self.receiver.jammed and self.state() not in spoof.DISTRUSTED

    def fallback_due(self):
        """Check whether receiver history permits a fallback solve."""
        now = self.clock()
        # Cold receivers get 180 s from boot or 60 s with GNSS neighbours.
        # Missing devices, fix history and jamming permit earlier fallback.
        return (self.receiver.devices is False or self.had_fix or self.receiver.jammed or
                now.boot >= 180 or self.neighbours_since is not None and
                now.mono - self.neighbours_since >= 60)

    def position_snapshot(self):
        """Build a ranging position with age and explicit anchor validity."""
        now = self.clock().mono
        # Retain fresh raw GNSS for audits, invalidating quarantined fixes for
        # positioning immediately. Alfred may lag three minutes. R4 GNSS +
        # fix_age means raw receiver data, valid permits anchors. Absent/stale
        # fixes use UNKNOWN without age, as do R2 position-provider errors.
        if self.fresh_receiver():
            fix = self.receiver.fix
            p = pb.Position(source=pb.Position.GNSS, valid=self.trusted(),
                latitude_e7=round(fix['lat'] * 1e7), longitude_e7=round(fix['lon'] * 1e7),
                fix_age_ms=round((now - self.receiver.observed) * 1000),
                horizontal_uncertainty_cm=math.ceil(self.config.gnss_sigma_m * 100))
            if fix.get('hae') is not None:
                p.altitude_cm = round(fix['hae'] * 100)
                p.vertical_uncertainty_cm = round(self.config.vertical_sigma_m * 100)
            return p
        doc = self.position_document()
        if doc['anchor_eligible'] and doc['source'] == 'ranged':
            p = pb.Position(source=pb.Position.RANGED, valid=True,
                latitude_e7=round(doc['point']['lat'] * 1e7),
                longitude_e7=round(doc['point']['lon'] * 1e7),
                fix_age_ms=round(doc['age_s'] * 1000),
                horizontal_uncertainty_cm=math.ceil(doc['radius_m'] * 100),
                generation=doc['generation'], used_node_ids=doc['ancestry'])
            # Inferred height is never published as independently known HAE.
            return p
        return pb.Position(valid=False)

    def receive(self, peer, data, *, direct=False):
        """Feed authenticated input to the core and remember its session."""
        self.core.receive(peer, data, direct=direct)
        self.remember_session()

    def remember_session(self):
        """Retain session role and local epoch for result attribution."""
        s = self.core.active
        if s and s.session_id not in self.sessions and s.responder_position is not None:
            self.sessions[s.session_id] = (self.clock().mono, s.role)
        now = self.clock().mono
        # Unfinished exchange context expires after 15 s.
        self.sessions = {k: v for k, v in self.sessions.items() if now - v[0] < 15}

    @staticmethod
    def gnss_sample(p, epoch):
        """Convert an exchange GNSS snapshot to the local sample format."""
        if (p is None or p.source != pb.Position.GNSS or
                not p.HasField('fix_age_ms') or p.fix_age_ms / 1000 > spoof.ALIGN_S):
            return None
        # Wire coordinates are degrees times 1e7, height cm and age ms.
        return {'mode': 3 if p.HasField('altitude_cm') else 2,
                'lat': p.latitude_e7 / 1e7, 'lon': p.longitude_e7 / 1e7,
                'alt_hae': p.altitude_cm / 100 if p.HasField('altitude_cm') else None,
                'mono': epoch - p.fix_age_ms / 1000}

    def feed_monitor(self, result, epoch, measured):
        """Compare canonical GNSS snapshots with a range or peer claim."""
        initiator = self.config.node_id if result.status == 'ok' else result.peer.node_id
        responder = result.peer.node_id if result.status == 'ok' else self.config.node_id
        sa = self.gnss_sample(result.initiator_position, epoch)
        sb = self.gnss_sample(result.responder_position, epoch)
        if sa is None or sb is None:
            return None
        # Compare the two canonical exchange snapshots without re-dating
        # samples. Monitor owns failures/scheduling. Scope its sample map to
        # this comparison, then retain the latest snapshots for
        # state/attribution.
        saved = self.monitor.samples
        temporary = dict(saved)
        temporary[initiator], temporary[responder] = deque([sa]), deque([sb])
        self.monitor.samples = temporary
        edge = self.monitor.add_range({'a': initiator, 'b': responder,
                                      'mono': epoch, 'range_m': measured})
        for n, sample in ((initiator, sa), (responder, sb)):
            if n not in saved or not saved[n] or saved[n][-1]['mono'] < sample['mono']:
                saved[n] = deque([sample])
        self.monitor.samples = saved
        # Claims add labelled failure evidence without becoming observations.
        # Passing claims never shorten quarantine.
        if edge:
            edge['peer_claim'] = result.status == 'peer_claim'
            self.monitor_states(force=True)
        return edge

    def handle_result(self, result):
        """Record range evidence and collect eligible solve observations."""
        now = self.clock().mono
        record = self.sessions.pop(result.session_id, None)
        self.events.append({'peer': result.peer.node_id, 'status': result.status, 'mono': now})
        if result.status not in ('ok', 'peer_claim') or record is None:
            if result.status != 'peer_claim':
                self.scheduler.failed(self.config.node_id, result.peer.node_id, now)
            return
        epoch, _ = record
        measured = result.estimate.range_m if result.status == 'ok' else result.peer_claim.range_m
        if measured < 0 or not math.isfinite(measured):
            self.scheduler.failed(self.config.node_id, result.peer.node_id, now)
            return
        edge = self.feed_monitor(result, epoch, measured)
        if edge:
            self.events[-1].update(failed=edge['failed'], residual_m=edge['residual_m'])
            self.next_audit = now + (5 if edge['disagrees'] or self.receiver.suspect else spoof.AUDIT_S)
        if result.status != 'ok' or self.job != 'solve':
            return
        n, p = self.neighbours.get(result.peer.node_id), result.responder_position
        if n is None or not n.eligible_anchor or not p or not p.valid or not p.HasField('fix_age_ms'):
            return
        age = p.fix_age_ms / 1000
        last_failure = self.monitor.last_failure.get(result.peer.node_id)
        # Ranged anchors expire at 15 s, other fixes at GPS_TTL.
        if (age >= (15 if p.source == pb.Position.RANGED else GPS_TTL) or
                last_failure is not None and now - last_failure <= spoof.QUARANTINE_S):
            return
        source = {pb.Position.GNSS: 'gnss', pb.Position.MANUAL: 'manual', pb.Position.RANGED: 'ranged'}.get(p.source)
        if source is None or not p.HasField('horizontal_uncertainty_cm'):
            return
        anchor = locate.Anchor(result.peer.node_id, p.latitude_e7 / 1e7, p.longitude_e7 / 1e7,
            p.horizontal_uncertainty_cm / 100,
            p.altitude_cm / 100 if p.HasField('altitude_cm') else None,
            p.vertical_uncertainty_cm / 100 if p.HasField('vertical_uncertainty_cm') else None,
            source, p.generation, tuple(p.used_node_ids), n.anchor.signal_dbm if n.anchor else None)
        diffs = [d for _, d in result.estimate.differences]
        center = statistics.median(diffs)
        # Scale median absolute count deviation to sigma, then metres.
        # The estimator slope is 26.69 counts/m, with a 1 m sigma floor.
        spread = max(1.0, 1.4826 * statistics.median(abs(d - center) for d in diffs) / 26.69)
        # Motion since the Ready fix is an additional anchor uncertainty.
        anchor = replace(anchor, h_sigma_m=anchor.h_sigma_m + 1.5 * age)
        self.observations.append(locate.Observation(anchor, measured, epoch, spread, len(diffs)))

    def finish_job(self):
        """Solve collected observations and update the retry interval."""
        now = self.clock().mono
        if self.job == 'solve':
            result = locate.solve(self.config.node_id, self.observations, at_mono=now,
                                  previous=self.previous)
            point = result.get('position')
            # Less than 5 m motion, or no point, doubles the solve delay.
            stable = point and self.previous and spoof.horizontal_m(point, vars(self.previous)) < 5
            self.backoff = min(self.config.max_backoff_s, self.backoff * 2) if stable or not point else self.config.solve_interval_s
            self.result, self.result_at = result, now
            if point:
                self.previous = locate.Previous(point['lat'], point['lon'], result['uncertainty_m'], now)
            self.next_solve = now + self.backoff
        self.job, self.observations = None, []

    def schedule(self):
        """Choose a fallback solve or a due GNSS consistency audit."""
        now = self.clock().mono
        if not (self.config.enabled and self.mesh and self.radio.can_range) or self.core.faulted:
            return
        if self.job or self.core.active or self.core.queue:
            return
        peers = [n for n in self.neighbours.values() if self.eligible(n.peer) and self.radio.authorize(n.peer)]
        if not self.trusted() and self.fallback_due() and now >= self.next_solve:
            anchors = [n.anchor for n in peers if n.anchor and n.eligible_anchor and
                       now - self.monitor.last_failure.get(n.peer.node_id, -math.inf) > spoof.QUARANTINE_S]
            selected = locate.choose_anchors(self.config.node_id, anchors, previous=self.previous)
            if selected:
                self.job, self.observations = 'solve', []
                self.core.start([self.neighbours[a.id].peer for a in selected])
                return
            self.next_solve = now + self.backoff
            self.backoff = min(self.config.max_backoff_s, 2 * self.backoff)
        # Audit suspect raw receivers too. Never invent GNSS.
        if not self.fresh_receiver() or now < self.next_audit:
            return
        ids = [self.config.node_id] + [n.peer.node_id for n in peers if n.gnss]
        pair = self.scheduler.next_pair(now, ids, lambda a, b: self.config.node_id in (a, b))
        if pair is None and (self.receiver.suspect or self.state() in spoof.DISTRUSTED):
            candidates = [n for n in peers if n.gnss and self.scheduler.cooldown.get(
                          frozenset((self.config.node_id, n.peer.node_id)), 0) <= now]
            if candidates:
                # Audit the oldest edge first, breaking ties by node ID.
                n = min(candidates, key=lambda n: (self.monitor.edges.get(frozenset(
                    (self.config.node_id, n.peer.node_id)), {}).get('mono', -math.inf), n.peer.node_id))
                pair = self.config.node_id, n.peer.node_id
        if pair:
            partner = pair[1] if pair[0] == self.config.node_id else pair[0]
            self.job = 'audit'
            self.core.start([self.neighbours[partner].peer])
            # Suspect or distrusted receivers get a five-second audit gap.
            self.next_audit = now + (5 if self.receiver.suspect or self.state() in spoof.DISTRUSTED else spoof.AUDIT_S)

    def tick(self):
        """Process radio events, results and scheduling on the owning loop."""
        for event in self.radio.poll():
            if isinstance(event, Inbound):
                self.receive(event.peer, event.data, direct=event.direct)
            elif event.kind == 'burst_finished':
                self.core.burst_finished(event.session_id)
            elif event.kind == 'radio_error':
                self.core.radio_error(event.session_id, event.detail)
        self.core.tick()
        self.remember_session()
        while self.core.results:
            self.handle_result(self.core.results.popleft())
        if self.job and not self.core.active and not self.core.queue:
            self.finish_job()
        self.schedule()
        while self.core.outbox:
            item = self.core.outbox.popleft()
            self.radio.send_control(item.peer, item.data)
        self.spoof_document()  # Record failure epochs even between publications.

    def spoof_document(self):
        """Build trust output and advance the fault epoch for new failures."""
        now = self.clock()
        state = self.state()
        last = self.monitor.last_failure.get(self.config.node_id)
        if last is not None and (self.previous_failure is None or last > self.previous_failure):
            self.fault_epoch += 1
            self.previous_failure = last
        edges = [e for e in self.monitor.edges.values() if self.config.node_id in (e['a'], e['b'])
                 and now.mono - e['mono'] <= spoof.EDGE_TTL_S and e['in_scope']]
        # Agreement needs a local passing range and no fresh disagreement.
        agrees = (any(not e.get('peer_claim') and not e['disagrees'] for e in edges)
                  and all(not e['disagrees'] for e in edges))
        remaining = max(0, spoof.QUARANTINE_S - (now.mono - last)) if last is not None else 0
        peers = self.monitor_states()
        peer_quarantine = {n: max(0, spoof.QUARANTINE_S - (now.mono - t))
                           for n, t in self.monitor.last_failure.items()
                           if n != self.config.node_id and now.mono - t <= spoof.QUARANTINE_S}
        return {'schema': 1, 'boot_id': self.boot_id, 'written_boot': now.boot,
                'producer_id': self.producer_id, 'fault_epoch': self.fault_epoch,
                'state': state, 'ranges_agree': agrees,
                'time_ok': self.receiver.time_ok and self.trusted() and remaining == 0,
                'quarantine_remaining_s': remaining,
                'peer_quarantine_remaining_s': peer_quarantine,
                'peer_states': {n: (spoof.INCONSISTENT if n in peer_quarantine and
                                   v[0] not in spoof.DISTRUSTED else v[0]) for n, v in peers.items()}}

    def position_document(self):
        """Build consumer geometry with source, age and trust policy."""
        now = self.clock()
        base = {'schema': 1, 'boot_id': self.boot_id, 'written_boot': now.boot,
                'producer_id': self.producer_id, 'quality': 'none', 'point': None,
                'radius_m': None, 'confidence': None, 'flags': [],
                'candidates': [], 'rings': [], 'area': None,
                'anchors_used': [], 'generation': None, 'ancestry': [], 'age_s': 0,
                'source': 'none', 'anchor_eligible': False,
                # Separate trust from geometry: none/ring can mean insufficient
                # anchors, not distrusted GNSS. The encoder needs an explicit
                # fresh verdict before suppressing the manager's raw GNSS fix.
                'gnss_state': self.state(),
                'phone_best_guess': self.config.phone_best_guess,
                'mark_priority': self.config.mark_priority,
                'radio_available': self.radio.can_range,
                'radio_reason': getattr(self.radio, 'reason', 'simulated')}
        if self.trusted():
            base.update(quality='good', point=self.receiver.fix,
                        radius_m=self.config.gnss_sigma_m * locate.CE95,
                        age_s=now.mono - self.receiver.observed, source='gnss',
                        generation=0, anchor_eligible=True, confidence=.95,
                        uncertainty_model='configured GNSS sigma; not gpsd accuracy')
        elif self.result and 0 <= now.mono - self.result_at < self.config.result_ttl_s:
            r, age = self.result, now.mono - self.result_at
            # Expand published uncertainty for motion at 1.5 m/s.
            growth = 1.5 * age
            base.update(quality=r['quality'], point=r['position'],
                        radius_m=r['uncertainty_m'] + growth if r['uncertainty_m'] is not None else None,
                        candidates=r['candidates'], rings=r.get('rings', []), area=r.get('area'),
                        anchors_used=r['used'], generation=r['generation'], ancestry=r['used_ids'],
                        age_s=age, source='ranged', reason=r['reason'],
                        confidence=r.get('confidence'), flags=r.get('flags', []),
                        anchor_eligible=r['quality'] == 'good' and r['anchor_eligible'] and age < 15,
                        motion_allowance_m=growth)
            # Inflate each drawable branch as well as the point enclosure.
            base['candidates'] = [dict(c, uncertainty_m=c['uncertainty_m'] + growth)
                                  for c in base['candidates']]
            base['rings'] = [dict(ring, width_m=ring['width_m'] + growth,
                                  inner_radius_m=max(0, ring['inner_radius_m'] - growth),
                                  outer_radius_m=ring['outer_radius_m'] + growth)
                             for ring in base['rings']]
            if base['area']:
                base['area'] = dict(base['area'], radius_m=base['area']['radius_m'] + growth)
        return base

    def checkpoint(self):
        """Save quarantine as boot-clock failure epochs for restart."""
        now = self.clock()
        self.spoof_document()
        # Restore ages these epochs with BOOTTIME, which includes suspend.
        return {'schema': 1, 'boot_id': self.boot_id, 'node_id': self.config.node_id,
                'producer_id': self.producer_id, 'fault_epoch': self.fault_epoch,
                'last_failures_boot': {n: now.boot - (now.mono - t)
                                      for n, t in self.monitor.last_failure.items()
                                      if now.mono - t <= spoof.QUARANTINE_S}}

    def restore(self, doc):
        """Restore validated quarantine state from this node and boot."""
        if doc.get('boot_id') != self.boot_id:
            return
        if doc.get('schema') != 1 or doc.get('node_id') != self.config.node_id:
            raise ValueError('invalid positioning checkpoint')
        now = self.clock()
        # Producer IDs are 32 lowercase hex digits from uuid4().hex.
        if (not re.fullmatch('[0-9a-f]{32}', doc.get('producer_id', '')) or
                type(doc.get('fault_epoch')) is not int or doc['fault_epoch'] < 0):
            raise ValueError('invalid producer checkpoint')
        failures = doc['last_failures_boot']
        if not isinstance(failures, dict) or len(failures) > 16:
            raise ValueError('invalid quarantine checkpoint')
        for node, boot in failures.items():
            age = now.boot - number(boot)
            if age < 0:
                raise ValueError('future failure checkpoint')
            if age <= spoof.QUARANTINE_S:
                self.monitor.last_failure[node] = now.mono - age
        self.producer_id, self.fault_epoch = doc['producer_id'], doc['fault_epoch']
        self.previous_failure = self.monitor.last_failure.get(self.config.node_id)

    def close(self):
        """Close protocol state and release the radio even on failure."""
        try:
            self.core.close()
        finally:
            self.radio.close()


async def command(args):
    # Popen with a nonblocking pipe keeps child reaping on this loop.
    # The default asyncio Unix watcher creates a reaper thread.
    """Collect bounded command output while yielding to the event loop."""
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               bufsize=0)
    os.set_blocking(process.stdout.fileno(), False)
    # Cap discovery at two seconds and 256 KiB while polling every 10 ms.
    deadline, data, eof = time.monotonic() + 2, bytearray(), False
    try:
        while not eof or process.poll() is None:
            try:
                chunk = os.read(process.stdout.fileno(), 8192)
                eof = not chunk
                data.extend(chunk)
            except BlockingIOError:
                pass
            if len(data) > 262144:
                raise ValueError('oversized discovery response')
            if time.monotonic() >= deadline:
                raise asyncio.TimeoutError('discovery command timed out')
            await asyncio.sleep(.01)
        if process.returncode:
            raise ValueError('discovery command failed/oversized')
        return data.decode('utf-8')
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        process.stdout.close()


class LinuxInputs:
    """Read local receiver status and mesh discovery inputs."""
    def __init__(self, config, boot_id, run='/run', sysnet='/sys/class/net'):
        """Set input paths and initialize discovery and jamming history."""
        self.config, self.boot_id = config, boot_id
        self.run, self.sysnet = Path(run), Path(sysnet)
        self.discovery = Discovery(config)
        self.jam_seen = False

    def receiver(self, now):
        """Validate receiver and jamming files against this boot and age."""
        jammed = False
        try:
            jam = read_json(self.run / 'manet-lc76g-jam.json')
            if (jam.get('schema') != 1 or jam.get('boot_id') != self.boot_id or
                    not 0 <= now.boot - number(jam.get('written_boot')) < INPUT_TTL or
                    type(jam.get('asserted')) is not bool):
                raise ValueError('invalid jamming input')
            self.jam_seen, jammed = True, jam['asserted']
        except FileNotFoundError:
            # Once present, a missing jamming report loses its clear verdict.
            jammed = self.jam_seen
        except (OSError, ValueError, TypeError):
            self.jam_seen, jammed = True, True
        try:
            return receiver_document(read_json(self.run / 'gps_status.json'), now, self.boot_id, jammed)
        except (OSError, ValueError, TypeError):
            return Receiver(jammed=jammed)

    async def neighbours(self):
        """Discover peers only on the configured 20 MHz 5 GHz mesh link."""
        iface = self.config.interface
        try:
            if (self.sysnet / iface / 'master').resolve(strict=True).name != 'bat0':
                return {}, False
            info, identities, telemetry, neighbours, stations = await asyncio.gather(
                command(['iw', 'dev', iface, 'info']), command(['alfred', '-r', '67']),
                command(['alfred', '-r', '68']), command(['batctl', 'meshif', 'bat0', 'n']),
                command(['iw', 'dev', iface, 'station', 'dump']))
            # iw reports primary frequency and width on the channel line.
            ch = re.search(r'channel\s+\d+\s+\((\d+) MHz\), width:\s*(\d+) MHz', info)
            mesh = bool(re.search(r'^\s*type mesh point\s*$', info, re.M) and ch and
                        5000 <= int(ch[1]) < 5900 and int(ch[2]) == 20)
            return (self.discovery.parse(identities, telemetry, neighbours, stations, time.monotonic())
                    if mesh else {}), mesh
        except (OSError, ValueError, asyncio.TimeoutError):
            return {}, False


def make_radio(config, simulated=None):
    """Select the helper placeholder or an explicitly supplied simulator."""
    if config.radio == 'simulated':
        if simulated is None:
            raise ValueError('simulated radio requires --simulate; never publish synthetic live-node fixes')
        return simulated
    return HelperRadio()


async def run_linux(config, run=Path('/run')):
    """Own the runtime lock and drive discovery, ranging and publication."""
    import fcntl
    runtime = run / 'manet-positioning'
    runtime.mkdir(exist_ok=True, mode=0o750)
    # Public filenames are stable symlinks created by the unit's fixed pre-start
    # operation. The unprivileged service writes only its own runtime directory.
    with (runtime / 'lock').open('w') as lock:
        # Fixed publication temp paths require one writer per runtime dir.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        inputs = LinuxInputs(config, boot_id, run)
        checkpoint = None
        try:
            checkpoint = read_json(runtime / 'state.json')
        except FileNotFoundError:
            pass
        service = Service(config, make_radio(config), Now.read, boot_id, checkpoint=checkpoint)
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            loop.add_signal_handler(sig, stopped.set)
        discovery, next_discovery, next_publish, saved_fault = None, 0.0, 0.0, -1
        try:
            while not stopped.is_set():
                now = Now.read()
                if discovery and discovery.done():
                    neighbours, mesh = discovery.result()
                    service.update(service.receiver, neighbours, mesh=mesh)
                    discovery = None
                # One in-flight discovery task, scheduled every five seconds.
                if discovery is None and now.mono >= next_discovery:
                    discovery = asyncio.create_task(inputs.neighbours())
                    next_discovery = now.mono + 5
                service.update(inputs.receiver(now))
                service.tick()
                # Publish twice per second, or immediately on a new fault.
                if now.mono >= next_publish or service.fault_epoch != saved_fault:
                    # Persist quarantine before publishing its trust verdict.
                    atomic_json(runtime / 'state.json', service.checkpoint())
                    saved_fault = service.fault_epoch
                    atomic_json(runtime / 'spoof.json', service.spoof_document())
                    atomic_json(runtime / 'position.json', service.position_document())
                    next_publish = now.mono + .5
                # Yield between 20 ms service ticks without delaying discovery.
                await asyncio.sleep(.02)
        finally:
            if discovery:
                discovery.cancel()
                await asyncio.gather(discovery, return_exceptions=True)
            service.close()
            atomic_json(runtime / 'state.json', service.checkpoint())
            # Let publications expire without publishing a healthy shutdown.


class SimRadio:
    """Supply paced synthetic timestamps and wire events without sockets."""
    can_range = True
    reason = 'simulated timestamps; no hardware validation'

    def __init__(self, network, node_id):
        """Initialize simulated radio queues, captures and ACK state."""
        self.network, self.node_id = network, node_id
        self.frames, self.local, self.remote, self.events = deque(), {}, {}, deque()
        self.armed, self.ack = False, 'original'

    def authorize(self, peer):
        """Match the peer against the simulation's identity map."""
        return self.network.peers.get(peer.node_id) == peer

    def arm_responder(self, peer, sid, width):
        """Open a synthetic responder capture and change simulated ACKs."""
        self.armed, self.ack = True, 'chain2'
        self.remote = {sid: []}

    def disarm(self):
        """Stop synthetic reception and discard pending burst frames."""
        self.armed = False
        self.frames.clear()

    def restore_ack(self):
        """Restore the simulated ACK setting."""
        self.ack = 'original'

    def send_control(self, peer, data):
        """Queue a control datagram for the simulated recipient."""
        self.network.messages.append((self.node_id, peer.node_id, data, False))

    def send_burst(self, peer, frames, width, interval_ms):
        """Queue probe frames at the protocol's deterministic offsets."""
        first = ranging.decode(frames[0])
        sid = first.session_id
        schedule = ranging.burst_schedule_us(sid, first.challenge, len(frames), interval_ms)
        self.local = {sid: []}
        start = self.network.now
        for seq, (offset, frame) in enumerate(zip(schedule, frames)):
            self.frames.append((start + offset / 1e6, peer.node_id, sid, seq, frame, seq == len(frames) - 1))

    def poll(self):
        """Emit due probes and synthetic reports for armed responders."""
        while self.frames and self.frames[0][0] <= self.network.now:
            due, peer, sid, seq, frame, last = self.frames.popleft()
            other = self.network.services[peer].radio
            if other.armed and sid in other.remote:
                truth_a, truth_b = self.network.truth[self.node_id], self.network.truth[peer]
                distance = math.dist(truth_a, truth_b)
                # Invert the synthetic calibration to create round-trip counts.
                difference = round(6500 + distance * 26.69)
                # Model a 4 GHz 48-bit clock plus offset and sequence drift.
                departure = round(due * 4e9) % ranging.CLOCK_MODULUS
                arrival = (departure + 999999 + seq * 20) % ranging.CLOCK_MODULUS
                # Accumulator correction follows each role's report ordinal.
                ii, ri = len(self.local[sid]) + 1, len(other.remote[sid]) + 1
                self.local[sid].append(ranging.InitiatorStamp(seq, departure,
                    (departure + 60000 + difference + 2454 * ii) % ranging.CLOCK_MODULUS, ii))
                other.remote[sid].append(ranging.ResponderStamp(seq, arrival,
                    (arrival + 60000 - 2454 * ri) % ranging.CLOCK_MODULUS, ri))
                self.network.messages.append((self.node_id, peer, frame, True))
            if last:
                self.events.append(radio_api.Event('burst_finished', sid))
        events = tuple(self.events)
        self.events.clear()
        return events

    def read_responder_report(self, sid):
        """Return the synthetic responder capture for a session."""
        return self.remote.get(sid, [])

    def read_initiator_timestamps(self, sid):
        """Return the synthetic initiator capture for a session."""
        return self.local.get(sid, [])

    def close(self):
        """Disarm the simulated radio and restore its ACK state."""
        self.disarm()
        self.restore_ack()


class Simulation:
    """Run service instances on a shared synthetic radio network."""
    def __init__(self, count=4, clock_offsets=None):
        """Create a GPS-less center node and a ring of GNSS anchors."""
        if not 4 <= count <= 16:
            raise ValueError('simulation needs 4..16 nodes')
        self.now, self.messages, self.services, self.peers = 200.0, deque(), {}, {}
        self.frame = locate.LocalFrame(39.7, -105.0)
        self.truth, self.claimed = {'P': (0, 0, 1600)}, {}
        self.ids = ['P'] + [f'A{i}' for i in range(count - 1)]
        self.counter = 0
        self.clock_offsets = clock_offsets or {}
        for i, node_id in enumerate(self.ids):
            self.peers[node_id] = ranging.Peer(node_id, bytes([2, 0, 0, 0, 0, i + 1]))
            if node_id != 'P':
                # Anchors lie on a 60 m circle at the center node's height.
                angle = 2 * math.pi * (i - 1) / (count - 1)
                self.truth[node_id] = (60 * math.cos(angle), 60 * math.sin(angle), 1600)
                self.claimed[node_id] = self.truth[node_id]
            cfg = Config(node_id, True, 'simulated', gnss_sigma_m=1, vertical_sigma_m=1)
            radio = make_radio(cfg, SimRadio(self, node_id))
            clock = lambda node_id=node_id: self.node_clock(node_id)
            service = Service(cfg, radio, clock, 'simulation-boot', nonce=self.nonce)
            # Stable phases prevent symmetric start/busy lockstep.
            # R2 arbitrates contention and both roles share one lease.
            service.next_audit = clock().mono + 12 + i * 3
            self.services[node_id] = service

    def clock(self):
        """Return the shared simulation clocks in seconds."""
        return Now(self.now, self.now + 321, self.now + 100)

    def node_clock(self, node_id):
        """Apply the node's configured offset to all three clocks."""
        offset = self.clock_offsets.get(node_id, 0)
        return Now(self.now + offset, self.now + 321 + offset, self.now + 100 + offset)

    def nonce(self):
        """Return a deterministic nonzero session or challenge value."""
        self.counter += 1
        return self.counter.to_bytes(16, 'big')

    def inputs(self):
        """Refresh synthetic fixes and neighbour advertisements."""
        for node_id, service in self.services.items():
            xyz = self.claimed.get(node_id)
            fix = self.frame.position(*xyz[:2], xyz[2]) if xyz else None
            service.update(Receiver(fix, service.clock().mono, xyz is not None,
                                    xyz is not None, time_ok=xyz is not None))
        for node_id, service in self.services.items():
            ns = {}
            for other_id, other in self.services.items():
                if other_id == node_id:
                    continue
                doc = other.position_document()
                anchor = None
                if doc['anchor_eligible']:
                    p = doc['point']
                    anchor = locate.Anchor(other_id, p['lat'], p['lon'],
                        doc['radius_m'] / locate.CE95 if doc['source'] == 'gnss' else doc['radius_m'],
                        p.get('hae'), 1, doc['source'], doc['generation'], tuple(doc['ancestry']), -55)
                ns[other_id] = Neighbour(self.peers[other_id], anchor, other.fresh_receiver(), doc['anchor_eligible'])
            service.update(service.receiver, ns, mesh=True)

    def step(self, seconds=.02):
        """Advance time, tick every service and deliver queued datagrams."""
        self.now += seconds
        self.inputs()
        for service in self.services.values():
            service.tick()
        # Serialize control/probe delivery on the same event loop, replying next
        # tick. Simulated snapshot clocks differ by node. Remote monotonic
        # epochs never cross the wire.
        while self.messages:
            sender, recipient, data, direct = self.messages.popleft()
            self.services[recipient].receive(self.peers[sender], data, direct=direct)

    def advance(self, seconds):
        """Advance in 20 ms steps, rounding the duration upward."""
        for _ in range(math.ceil(seconds / .02)):
            self.step()

    def summary(self):
        """Summarize the GPS-less solution and each node's trust state."""
        p = self.services['P'].position_document()
        xy = self.frame.xy(p['point']['lat'], p['point']['lon']) if p['point'] else None
        return {'nodes': len(self.services), 'elapsed_s': round(self.now - 200, 2),
                'gpsless': {'quality': p['quality'], 'point': p['point'],
                            'radius_m': p['radius_m'], 'anchors': p['anchors_used'],
                            'anchor_eligible': p['anchor_eligible'],
                            'error_m': math.hypot(*xy) if xy else None},
                'states': {n: s.spoof_document()['state'] for n, s in self.services.items()},
                'failed_peer_claims': [{'node': n, **e} for n, s in self.services.items()
                                       for e in s.events if e['status'] == 'peer_claim' and e.get('failed')]}

    def close(self):
        """Release all simulated service resources."""
        for service in self.services.values():
            service.close()


def simulate(count):
    """Run benign ranging followed by a displaced anchor GNSS claim."""
    sim = Simulation(count)
    try:
        sim.advance(12 if count == 4 else 18)
        initial = sim.summary()
        sim.advance(100)
        benign = sim.summary()
        victim = 'A0'
        x, y, z = sim.truth[victim]
        # Shift the GNSS claim 120 m east while radio geometry stays fixed.
        sim.claimed[victim] = (x + 120, y, z)
        onset = sim.now
        detected = None
        for _ in range(120):
            sim.advance(1)
            if sim.services[victim].state() in spoof.DISTRUSTED and detected is None:
                detected = round(sim.now - onset, 2)
        return {'initial': initial, 'benign': benign, 'attack': sim.summary(),
                'spoof_detection_s': detected}
    finally:
        sim.close()


def main():
    """Run the requested simulation or an enabled Linux service."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/etc/mesh.conf')
    parser.add_argument('--simulate', type=int, metavar='N')
    args = parser.parse_args()
    if args.simulate is not None:
        print(json.dumps(simulate(args.simulate), indent=2, allow_nan=False))
        return
    config = Config.load(args.config)
    if not config.enabled:
        return
    asyncio.run(run_linux(config))


if __name__ == '__main__':
    main()
