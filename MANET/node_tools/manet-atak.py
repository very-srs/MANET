#!/usr/bin/env python3
"""Tethered ATAK service; IPv4 protocol-0 CoT, no multicast or clock authority.

Reads mesh.conf (enabled unless atak=n/no/0/false), gps_status.json and optional monitor/jam
interfaces. The unit installs the policy shown by --print-firewall before
starting and removes it after stopping. The daemon only verifies that policy.
Like mesh-voice.py, logs go to the journal and atomic /run JSON serves the UI.
The versioned persistence adapter below intentionally owns the selector's
private state; changing that selector layout requires a schema review.
"""

import argparse
from dataclasses import asdict, dataclass, fields, is_dataclass, replace
from datetime import datetime, timezone
import fcntl
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid

import manet_cot as cot
import manet_phone as phone

MAX_XML = 65536
MAX_STATE = 512 * 1024
MAX_CONNECTIONS = 8
MAX_PER_PEER = 4
MAX_STREAM_BYTES = 256 * 1024
MAX_STREAM_EVENTS = 256
IDLE_S = 45
PARTIAL_S = 5
LIFETIME_S = 300
INPUT_TTL = 5
MAX_RETIRE = 128
CODES = {'gps_untrusted', 'gps_override', 'confirm_position', 'phone_gps'}
KINDS = {'geochat', 'audio', 'web_ui'}
MAC = re.compile(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\Z')
TABLE = 'manet_atak'


class PersistenceError(RuntimeError):
    """Fatal: continuing could forget an association or acknowledge lost work."""


def state_digest(data):
    """Detect damaged checkpoints; not authentication against a privileged editor."""
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def log(message):
    print('[manet-atak] ' + str(message), flush=True)


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('expected finite number')
    return float(value)


def bounded_text(value, maximum=128):
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError('invalid or oversized text')
    return value


def ipv4(value):
    address = ipaddress.IPv4Address(value)
    if address.is_multicast or address.is_unspecified or address.is_loopback or int(address) == 0xffffffff:
        raise ValueError('unicast IPv4 required')
    return str(address)


def read_json(path, limit=MAX_STATE):
    with open(path, 'rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('JSON exceeds size limit')
    return json.loads(data, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def atomic_json(path, data, *, durable=False):
    data = json.dumps(data, separators=(',', ':'), allow_nan=False).encode()
    if len(data) > MAX_STATE:
        raise ValueError('state exceeds size limit')
    path = Path(path)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('wb') as stream:
        stream.write(data)
        stream.flush()
        if durable:
            os.fsync(stream.fileno())
    os.replace(tmp, path)
    if durable:
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def read_kv(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            result[key.strip()] = value.strip().strip('\"\'')
    return result


def atak_enabled(conf):
    return conf.get('atak', '').strip().lower() not in {'n', 'no', '0', 'false'}


def file_stamp(path):
    try:
        s = Path(path).stat()
        return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns
    except FileNotFoundError:
        return None


@dataclass(frozen=True)
class Config:
    uid: str
    callsign: str = 'MANET RADIO'
    enabled: bool = True
    bind_ip: str | None = None

    @classmethod
    def load(cls, path='/etc/mesh.conf'):
        conf = read_kv(path)
        machine = Path('/etc/machine-id').read_text().strip()
        if not re.fullmatch('[0-9a-f]{32}', machine):
            raise ValueError('machine-id unavailable')
        return cls('MANET-' + machine,
                   bounded_text(conf.get('atak_callsign', 'MANET ' + socket.gethostname()), 64),
                   atak_enabled(conf),
                   ipv4(conf['atak_bind_ip']) if conf.get('atak_bind_ip') else None)


@dataclass(frozen=True)
class Now:
    mono: float
    raw: float
    boot: float
    utc: datetime


class Clock:
    def now(self):
        return Now(time.monotonic(), time.clock_gettime(time.CLOCK_MONOTONIC_RAW),
                   time.clock_gettime(time.CLOCK_BOOTTIME), datetime.now(timezone.utc))


def match(left, right, op='=='):
    return {'match': {'op': op, 'left': left, 'right': right}}


def meta(key):
    return {'meta': {'key': key}}


def payload(protocol, field):
    return {'payload': {'protocol': protocol, 'field': field}}


def firewall_objects():
    """Canonical nft JSON; counters/handles are the only ignored runtime data.

    Bridge input covers local delivery, output covers unknown-unicast flooding.
    Inet input excludes routed/loopback ingress and IPv6. Before bridge/IP
    defragmentation, reject first IPv4 fragments addressed to TCP/UDP 4242 on
    any bridge, including forwarded traffic. Without an offset-zero fragment those
    packets cannot reassemble, even when later fragments arrive on other ports.
    Other destination ports/protocols and noninitial fragments are unaffected.
    """
    objects = []
    for family, hooks in (('bridge', ('prerouting', 'input', 'output')), ('inet', ('input',))):
        objects.append({'table': {'family': family, 'name': TABLE}})
        for hook in hooks:
            objects.append({'chain': {'family': family, 'table': TABLE, 'name': hook,
                                     'type': 'filter', 'hook': hook,
                                     'prio': -450 if hook == 'prerouting' else 0, 'policy': 'accept'}})
    def rule(family, chain, expr):
        objects.append({'rule': {'family': family, 'table': TABLE, 'chain': chain, 'expr': expr + [{'drop': None}]}})
    for protocol in ('tcp', 'udp'):
        # No ibrname match: node kernels lack CONFIG_NFT_BRIDGE_META, and a
        # first fragment to 4242 has no legitimate use on any bridge.
        rule('bridge', 'prerouting', [match(payload('ether', 'type'), 'ip'),
                                     match({'&': [payload('ip', 'frag-off'), 0x3fff]}, 0x2000),
                                     match(meta('l4proto'), protocol),
                                     match(payload(protocol, 'dport'), 4242)])
        rule('bridge', 'input', [match(meta('iifname'), 'end0', '!='),
                                match(payload('ether', 'type'), 'ip'),
                                match(meta('l4proto'), protocol),
                                match(payload(protocol, 'dport'), 4242)])
        for field, ports in (('dport', {'set': [4242, 4349]}), ('sport', 4242)):
            rule('bridge', 'output', [match(meta('oifname'), 'end0', '!='),
                                     match(payload('ether', 'type'), 'ip'),
                                     match(meta('l4proto'), protocol),
                                     match(payload(protocol, field), ports)])
        rule('inet', 'input', [match(meta('nfproto'), 'ipv6'), match(meta('l4proto'), protocol), match(payload(protocol, 'dport'), 4242)])
        rule('inet', 'input', [match(meta('iifname'), 'br0', '!='), match(meta('l4proto'), protocol), match(payload(protocol, 'dport'), 4242)])
    return objects


def firewall_transaction(*, install=True):
    """One atomic batch; add/delete also handles missing or partly present tables."""
    commands = []
    for family in ('bridge', 'inet'):
        table = {'table': {'family': family, 'name': TABLE}}
        commands += [{'add': table}, {'delete': table}]
    if install:
        commands.extend({'add': entry} for entry in firewall_objects())
    return {'nftables': commands}


def apply_firewall(*, install):
    """Unit pre/post hook only; never called by the running daemon."""
    try:
        subprocess.run(['nft', '-j', '-f', '-'],
                       input=json.dumps(firewall_transaction(install=install)),
                       check=True, capture_output=True, text=True, timeout=5)
    except subprocess.CalledProcessError as exc:
        raise OSError('ATAK firewall transaction failed: ' + (exc.stderr or '').strip()) from exc
    except subprocess.TimeoutExpired as exc:
        raise OSError('ATAK firewall transaction timed out') from exc


def _canonical_expr(expr):
    """Drop matches nft re-derives from later payloads, as its dumps do.

    nft adds 'meta l4proto X' for a later X payload and 'ether type ip'
    for a later ip payload when loading, and omits them when listing, so a
    readback never repeats them. Same packet semantics either way.
    """
    later = set()
    kept = []
    for entry in reversed(expr):
        m = entry.get('match') if isinstance(entry, dict) else None
        left = m and m.get('left')
        if m and m.get('op') == '==' and isinstance(left, dict):
            if left == meta('l4proto') and m.get('right') in later:
                continue
            if left == payload('ether', 'type') and m.get('right') == 'ip' and 'ip' in later:
                continue
        if m and isinstance(left, dict):
            inner = left.get('&', [left])[0] if '&' in left else left
            protocol = inner.get('payload', {}).get('protocol') if isinstance(inner, dict) else None
            if protocol:
                later.add(protocol)
        kept.append(entry)
    return list(reversed(kept))


def valid_firewall(data):
    actual = []
    for item in data.get('nftables', []):
        for kind, keys in (('table', ('family', 'name')),
                           ('chain', ('family', 'table', 'name', 'type', 'hook', 'prio', 'policy')),
                           ('rule', ('family', 'table', 'chain', 'expr'))):
            obj = item.get(kind)
            if obj is None or obj.get('family') not in ('bridge', 'inet'):
                continue
            if obj.get('name' if kind == 'table' else 'table') != TABLE:
                continue
            if kind == 'table' and obj.get('flags'):
                return False  # in particular, a dormant table provides no proof
            obj = {key: obj.get(key) for key in keys}
            if kind == 'rule':
                obj['expr'] = [entry for entry in obj['expr'] if 'counter' not in entry]
            actual.append({kind: obj})
        # Reject auxiliary sets/maps/flowtables in this private policy table.
        for kind, obj in item.items():
            if kind not in ('table', 'chain', 'rule', 'metainfo') and isinstance(obj, dict) and obj.get('table') == TABLE:
                return False
    # Kernel dumps tables grouped by family; retain rule order per chain.
    def grouped(items):
        result = {}
        for entry in items:
            kind, obj = next(iter(entry.items()))
            key = (obj['family'], kind, obj.get('chain', obj.get('name', '')))
            result.setdefault(key, []).append(obj)
        return result
    def canonical(items):
        result = []
        for entry in items:
            if 'rule' in entry:
                rule = dict(entry['rule'])
                rule['expr'] = _canonical_expr(rule['expr'])
                entry = {'rule': rule}
            result.append(entry)
        return result
    return grouped(canonical(actual)) == grouped(canonical(firewall_objects()))


@dataclass(frozen=True)
class Proof:
    local_ip: str
    peers: dict  # IP -> current dynamic MAC learned on end0
    generation: str

    def peer(self, address):
        return self.peers.get(address)


class GuardFailure(RuntimeError):
    pass


class NetworkEvents:
    """Kernel notifications in the existing select loop, never a monitor child.

    Subscribe before taking a snapshot. Lost/truncated notifications are fatal:
    the unit restarts with new subscriptions and no previously admitted peers.
    """
    def __init__(self, factory=socket.socket):
        self.sockets = []
        try:
            # NETLINK_ROUTE: LINK, NEIGH (including bridge FDB), IPv4_IFADDR.
            # NETLINK_NETFILTER: NFNLGRP_NFTABLES, including ruleset commits.
            for protocol, groups in ((0, 1 | 4 | 16), (12, 1 << 6)):
                sock = factory(socket.AF_NETLINK, socket.SOCK_RAW, protocol)
                self.sockets.append(sock)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024)
                sock.setblocking(False)
                sock.bind((0, groups))
        except BaseException:
            self.close()
            raise

    def close(self):
        for sock in self.sockets:
            sock.close()
        self.sockets.clear()

    def changed(self):
        changed = False
        for sock in self.sockets:
            # Bound work under churn; exhausting the queue budget fails closed.
            for _ in range(256):
                try:
                    data, _, flags, sender = sock.recvmsg(65536)
                except BlockingIOError:
                    break
                if not data or flags & socket.MSG_TRUNC or sender[0] != 0:
                    raise OSError('unreliable network notification')
                offset = 0
                while offset < len(data):
                    if len(data) - offset < 16:
                        raise OSError('short netlink header')
                    length, kind, _, _, _ = struct.unpack_from('=IHHII', data, offset)
                    if length < 16 or offset + length > len(data) or kind in (2, 4):
                        raise OSError('lost or invalid network notification')
                    # NOOP/DONE carry no change; every other multicast message
                    # invalidates the proof. Include all bridge ports: duplicate
                    # learning on bat0 must revoke an end0 peer too.
                    changed |= kind not in (1, 3)
                    offset += (length + 3) & ~3
            else:
                raise OSError('network notification backlog exceeded')
        return changed


class LinuxGuard:
    """Read-only policy/path verification. No shell and no firewall mutation.

    The unit owns these tables. PartOf/After=nftables.service orders shutdown
    before its ruleset flush and reinstalls the tables before daemon restart.
    Netlink invalidates the cached snapshot on policy/link/address/FDB/ARP
    changes. The kernel firewall remains the packet-level enforcement boundary.
    """
    def __init__(self, config, runner=subprocess.run, sysnet='/sys/class/net', run='/run'):
        self.config, self.runner = config, runner
        self.sysnet, self.run = Path(sysnet), Path(run)
        self.reason = 'not checked'
        self.events = None
        self.cached = None
        self.dirty = True
        self.signature = None

    def start(self):
        self.events = NetworkEvents()

    def close(self):
        if self.events is not None:
            self.events.close()

    def readers(self):
        return self.events.sockets if self.events is not None else []

    def invalidate(self):
        self.cached, self.dirty = None, True

    def file_signature(self):
        return tuple(file_stamp(self.run / name)
                     for name in ('ethernet_detection_state', 'eth-carrier-generation'))

    def current(self, force=False):
        if self.events is None:
            raise OSError('network subscriptions unavailable')
        try:
            signature = self.file_signature()
            if self.events.changed() or signature != self.signature or force:
                self.invalidate()
            if self.dirty:
                self.signature = signature
                proof = self.inspect()
                # A snapshot spanning a change cannot admit anyone. Retry on
                # the next existing loop cycle; never spin on a changing path.
                if self.events.changed() or self.file_signature() != signature:
                    self.invalidate()
                    self.reason = 'network changed during verification'
                else:
                    # Retry a failed readback at the existing cycle cadence;
                    # a transient command failure must not park us forever
                    # waiting for an unrelated kernel event.
                    self.cached, self.dirty = proof, self.retry
            return self.cached
        except OSError as exc:
            self.invalidate()
            # Bypass per-packet OSError handling: close every socket in main's
            # finally and let systemd establish fresh subscriptions on restart.
            raise GuardFailure(str(exc)) from exc

    def command(self, *args):
        result = self.runner(args, check=True, capture_output=True, text=True, timeout=1)
        if len(result.stdout) > 2 * 1024 * 1024:
            raise ValueError('network snapshot too large')
        return json.loads(result.stdout)

    def inspect(self):
        self.retry = False
        try:
            if not self.config.enabled:
                raise ValueError('atak is disabled in mesh.conf')
            role = read_kv(self.run / 'ethernet_detection_state')
            if role.get('ETH_MODE') != 'WIRED_EUD' or role.get('ETH_BRIDGE') != 'br0':
                raise ValueError('end0 is not a wired EUD')
            if (self.sysnet / 'end0/master').resolve().name != 'br0':
                raise ValueError('end0 is not enslaved to br0')
            if (self.sysnet / 'end0/carrier').read_text().strip() != '1' or (self.sysnet / 'br0/brif/end0/state').read_text().strip() != '3':
                raise ValueError('end0 is not forwarding with carrier')
            generation = (self.run / 'eth-carrier-generation').read_text().strip()
            if not re.fullmatch(r'end0 wired-eud \S+', generation):
                raise ValueError('wired EUD generation unavailable')
            if not valid_firewall(self.command('nft', '-j', 'list', 'ruleset')):
                raise ValueError('ATAK firewall missing or changed')
            links = self.command('ip', '-j', '-4', 'address', 'show', 'dev', 'br0')
            addresses = [ipv4(a['local']) for link in links for a in link.get('addr_info', [])
                         if a.get('family') == 'inet' and a.get('scope') == 'global' and not a.get('secondary', False)]
            local = self.config.bind_ip or (addresses[0] if addresses else None)
            if local not in addresses:
                raise ValueError('ATAK br0 IPv4 not ready')
            fdb = self.command('bridge', '-j', 'fdb', 'show', 'br', 'br0')
            ports = {}
            for entry in fdb:
                mac = entry.get('mac', '').lower()
                if entry.get('state') in ('permanent', 'static') or 'self' in entry.get('flags', []):
                    continue
                ports.setdefault(mac, set()).add(entry.get('ifname', entry.get('dev')))
            peers = {}
            for entry in self.command('ip', '-j', '-4', 'neigh', 'show', 'dev', 'br0'):
                mac = entry.get('lladdr', '').lower()
                states = entry.get('state', [])
                if isinstance(states, str):
                    states = [states]
                if (MAC.fullmatch(mac) and ports.get(mac) == {'end0'}
                        and set(states) & {'REACHABLE', 'STALE', 'DELAY', 'PROBE'}):
                    address = ipv4(entry['dst'])
                    if address != local:
                        peers[address] = mac
            self.reason = 'end0 firewall and EUD path verified'
            return Proof(local, peers, generation)
        except (OSError, subprocess.SubprocessError) as exc:
            self.retry = True
            self.reason = str(exc)
            return None
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            self.reason = str(exc)
            return None


# JSON tags are a closed allowlist, never pickle, eval or arbitrary imports.
CLASSES = {c.__name__: c for c in (cot.Fix, cot.ErrorEstimate, phone.PhoneFix,
                                  phone.OutboundEvent, phone.PhoneClockDiagnostic, phone._Warning)}


def encode(value):
    if isinstance(value, datetime):
        return {'date': value.isoformat()}
    if is_dataclass(value):
        return {'class': type(value).__name__, 'fields': {f.name: encode(getattr(value, f.name)) for f in fields(value)}}
    if isinstance(value, tuple):
        return {'tuple': [encode(v) for v in value]}
    if isinstance(value, dict):
        return {'dict': [[encode(k), encode(v)] for k, v in value.items()]}
    if isinstance(value, list):
        return [encode(v) for v in value]
    return value


def decode(value, depth=0):
    if depth > 20:
        raise ValueError('state nesting exceeds limit')
    sub = lambda v: decode(v, depth + 1)
    if isinstance(value, dict):
        if set(value) == {'date'}:
            return cot._utc(datetime.fromisoformat(value['date']))
        if set(value) == {'tuple'}:
            return tuple(sub(v) for v in value['tuple'])
        if set(value) == {'dict'}:
            pairs = [(sub(k), sub(v)) for k, v in value['dict']]
            result = dict(pairs)
            if len(result) != len(pairs):
                raise ValueError('duplicate state keys')
            return result
        if set(value) == {'class', 'fields'} and value['class'] in CLASSES:
            cls = CLASSES[value['class']]
            if set(value['fields']) != {f.name for f in fields(cls)}:
                raise ValueError('state class fields changed')
            return cls(**{k: sub(v) for k, v in value['fields'].items()})
        raise ValueError('unknown state tag')
    if isinstance(value, list):
        if len(value) > 1024:
            raise ValueError('state collection exceeds limit')
        return [sub(v) for v in value]
    if isinstance(value, str) and len(value) > 4096:
        raise ValueError('state string exceeds limit')
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        number(value)
    return value


def validate_selector(position, config):
    defaults = phone.PhonePosition(radio_uid=config.uid).__dict__
    state = vars(position)
    if set(state) != set(defaults) or position.radio_uid != config.uid:
        raise ValueError('selector schema/radio identity changed; explicit state migration required')
    for name, default in defaults.items():
        value = state[name]
        if default is not None and not isinstance(value, type(default)):
            raise ValueError('invalid selector field ' + name)
    if position.pinned_uid is not None:
        bounded_text(position.pinned_uid)
        if position.pinned_uid == config.uid:
            raise ValueError('radio cannot be pinned')
    if len(position._seen_sa_times) > phone.PHONE_SA_HISTORY or len(position._retirements) > MAX_RETIRE:
        raise ValueError('selector history exceeds bound')
    if set(position._warnings) - CODES or len(position._events) > len(CODES) * len(KINDS):
        raise ValueError('invalid selector events')
    for (code, kind), event in position._events.items():
        if code not in CODES or kind not in KINDS or not isinstance(event, phone.OutboundEvent) or (event.code, event.kind) != (code, kind):
            raise ValueError('invalid pending event')
    for code, warning in position._warnings.items():
        if not isinstance(warning, phone._Warning):
            raise ValueError('invalid warning')
    if position._manual is not None:
        if not isinstance(position._manual, phone.PhoneFix) or not isinstance(position._manual.fix, cot.Fix):
            raise ValueError('invalid manual provenance')
        cot.external_position(config.uid, position._manual.fix, datetime.now(timezone.utc))
    for name in ('_clock', '_phone_mono_ref', '_presence', '_distrust_since', '_feed_until', '_last_sent_mono'):
        if state[name] is not None:
            number(state[name])
    for name in ('_latest_time', '_phone_ref'):
        if state[name] is not None:
            cot._utc(state[name])


class Store:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        try:
            return read_json(self.path)
        except FileNotFoundError:
            return None

    def save(self, data):
        atomic_json(self.path, data, durable=True)


def rebase_selector(position, saved, now, new_boot):
    if not new_boot:
        if now.mono < saved:
            raise ValueError('monotonic clock regressed in same boot')
    else:
        # Downtime cannot be inferred from an untrusted/restepped wall clock.
        # Retain elapsed age at checkpoint + current uptime as a LOWER BOUND.
        shift = now.mono - saved - now.boot
        for name in ('_distrust_since', '_last_sent_mono'):
            if getattr(position, name) is not None:
                setattr(position, name, getattr(position, name) + shift)
        if position._manual is not None:
            mark = position._manual
            position._manual = replace(mark, observation_mono=mark.observation_mono + shift,
                                       received_mono=mark.received_mono + shift)
        for warning in position._warnings.values():
            warning.raised_mono += shift
            if warning.sent_mono is not None:
                warning.sent_mono += shift
            if warning.deadline is not None:
                warning.deadline = now.mono  # one reminder; never a catch-up burst
        position._phone_ref = position._phone_mono_ref = position._latest_time = None
        position._phone_generation += 1
        position.clock_diagnostic = None
        position._presence = None
        position._clock = None
    if position._last_sent_mono is not None:
        position._feed_until = max(position._feed_until or 0, now.mono + position.feed_timeout_s) if not new_boot else now.mono + position.feed_timeout_s
        position._last_sent_mono = now.mono  # guard buffered echoes after restart


class Inputs:
    """Own receiver only. Optional producer files are local, versioned interfaces.

    Monitor: schema=1, boot_id, written_boot, producer_id, fault_epoch,
    state (RadioGPS vocabulary), ranges_agree. Jam: same envelope + asserted
    (bool). Missing from the outset means unwired/unknown; loss after a valid
    producer fails closed. GPIO numbers and GPS time permissions are not guessed.
    """
    def __init__(self, boot_id, run='/run'):
        self.boot_id, self.run = boot_id, Path(run)
        self.cached = self.signature = None
        self.expiries = []
        self.revision = 0

    def cached_read(self, now, memory, *, receiver=True):
        names = ('manet-spoof.json', 'manet-lc76g-jam.json')
        if receiver:
            names += ('gps_status.json',)
        try:
            signature = (receiver, tuple(file_stamp(self.run / name) for name in names))
        except OSError as exc:
            signature = ('unreadable', exc.errno)
        if (self.cached is None or signature != self.signature or
                any(getattr(now, clock) >= deadline for clock, deadline in self.expiries)):
            self.signature = signature
            self.cached = self.read(now, memory, receiver=receiver)
            self.revision += 1
        return self.cached

    def document(self, name, now, schema):
        data = read_json(self.run / name, 65536)
        if data.get('schema') != schema or data.get('boot_id') != self.boot_id:
            raise ValueError('stale or wrong-boot ' + name)
        written = number(data.get('written_boot'))
        if written > now.boot:
            self.expiries.append(('boot', written))
        if not 0 <= now.boot - written < INPUT_TTL:
            raise ValueError('stale or wrong-boot ' + name)
        self.expiries.append(('boot', written + INPUT_TTL))
        return data

    def read(self, now, memory, *, receiver=True):
        self.expiries = []
        fix, observed, gps_reason = None, now.mono, 'GPS unavailable'
        try:
            if not receiver:
                raise ValueError('waiting for phone')
            data = self.document('gps_status.json', now, 2)
            sample = data['sample']
            if sample is None:
                raise ValueError('no receiver sample' if data.get('devices') else 'no GPS receiver')
            age = now.raw - number(sample['mono'])
            if age < 0:
                self.expiries.append(('raw', number(sample['mono'])))
            if data.get('clock') != 'monotonic_raw' or data.get('has_fix') is not True or sample.get('mode', 0) < 2 or not 0 <= age < phone.FIX_TTL_S:
                raise ValueError('no fresh receiver fix')
            self.expiries.append(('raw', number(sample['mono']) + phone.FIX_TTL_S))
            lat, lon = number(sample['lat']), number(sample['lon'])
            if not -90 <= lat <= 90 or not -180 <= lon <= 180 or (lat, lon) == (0, 0):
                raise ValueError('invalid receiver geography')
            hae = sample.get('alt_hae')
            hae = None if hae is None else number(hae)
            # altMSL/legacy altitude and eph/epv with unspecified confidence
            # cannot be relabelled as HAE or CE90/LE90.
            fix = cot.Fix(lat, lon, 'gnss', hae=hae)
            observed, gps_reason = now.mono - age, 'fresh receiver fix'
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            gps_reason = str(exc)
        state, agrees, monitor = 'UNCHECKED', False, 'unwired'
        try:
            doc = self.document('manet-spoof.json', now, 1)
            state = doc['state']
            if state not in phone._GPS_STATES or type(doc['ranges_agree']) is not bool:
                raise ValueError('invalid monitor state')
            producer = bounded_text(doc['producer_id'])
            epoch = doc['fault_epoch']
            if type(epoch) is not int or epoch < 0:
                raise ValueError('invalid monitor epoch')
            token = [producer, epoch]
            old = memory.get('monitor_token')
            if old and producer == old[0] and epoch < old[1]:
                raise ValueError('monitor epoch regressed')
            if old != token:
                if old is not None or epoch > 0:
                    memory['fault_epoch'] += 1
                memory['monitor_token'] = token
            memory['monitor_seen'] = True
            agrees, monitor = doc['ranges_agree'], 'fresh'
        except FileNotFoundError:
            if memory.get('monitor_seen'):
                state, monitor = 'GNSS_SUSPECTED', 'unavailable after activation'
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            memory['monitor_seen'] = True
            state, monitor = 'GNSS_SUSPECTED', 'invalid or stale monitor input'
        jam, jam_reason = None, 'GPIO not configured'
        try:
            doc = self.document('manet-lc76g-jam.json', now, 1)
            if type(doc['asserted']) is not bool:
                raise ValueError('invalid jam assertion')
            bounded_text(doc['producer_id'])
            jam, jam_reason = doc['asserted'], 'fresh GPIO input'
            memory['jam_seen'] = True
        except FileNotFoundError:
            if memory.get('jam_seen'):
                jam_reason = 'GPIO input unavailable after activation'
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            memory['jam_seen'] = True
            jam_reason = 'invalid or stale GPIO input'
        jam_gate = jam is True or (jam is None and memory.get('jam_seen', False))
        evidence = [state in ('GNSS_SUSPECTED', 'INCONSISTENT_UNATTRIBUTED'), jam_gate, monitor]
        if evidence != memory.get('evidence') and (evidence[0] or evidence[1]):
            memory['fault_epoch'] += 1
        memory['evidence'] = evidence
        return phone.RadioGPS(fix, observed, state, jam_gate, memory['fault_epoch'], agrees), {
            'gps': gps_reason, 'monitor': monitor, 'jamming_pin': jam, 'jamming_input': jam_reason}


class Service:
    """Serialized application core. Transport.send returns successful local UDP handoff."""
    def __init__(self, config, boot_id, store, transport, now):
        self.config, self.boot_id, self.store, self.transport = config, boot_id, store, transport
        self.revision = 0
        self.position = phone.PhonePosition(radio_uid=config.uid)
        self.peer = None
        self.session = str(uuid.uuid4())
        self.retire = {}
        self.messages = {}
        self.audio = {}
        self.banners = {}
        self.chats = []
        self.input_memory = {'fault_epoch': 0}
        self.age_lower_bound = False
        self.last_contact = self.last_feed = None
        self.contact_attempt = self.feed_attempt = None
        self.contact_signature = None
        self.contact_changed = True
        self.last_checkpoint = now.mono
        self.saved_input_memory = encode(self.input_memory)
        self.last_rejection = None
        self.rejected = 0
        self.send_error = None
        saved = store.load()
        if saved is not None:
            self.restore(saved, now)
        self.commit(now)

    def restore(self, saved, now):
        payload = {key: value for key, value in saved.items() if key != 'sha256'}
        if saved.get('sha256') != state_digest(payload):
            raise ValueError('state checksum mismatch; refusing implicit reassociation')
        if saved.get('schema') != 1 or saved.get('uid') != self.config.uid:
            raise ValueError('state schema/identity mismatch; refusing implicit reassociation')
        state = decode(saved['state'])
        required = {'selector', 'peer', 'session', 'retire', 'messages', 'audio', 'banners', 'chats', 'input_memory', 'age_lower_bound'}
        if not isinstance(state, dict) or set(state) != required:
            raise ValueError('invalid service state')
        self.position.__dict__ = state.pop('selector')
        validate_selector(self.position, self.config)
        for key, value in state.items():
            setattr(self, key, value)
        bounded_text(self.session)
        if (self.peer is None) != (self.position.pinned_uid is None):
            raise ValueError('pin/path persistence inconsistent')
        if self.peer is not None:
            if set(self.peer) != {'ip', 'mac'} or not MAC.fullmatch(self.peer['mac']):
                raise ValueError('invalid saved peer')
            ipv4(self.peer['ip'])
        for mapping, bound in ((self.retire, MAX_RETIRE), (self.messages, 4), (self.audio, 4), (self.banners, 4)):
            if not isinstance(mapping, dict) or len(mapping) > bound:
                raise ValueError('invalid service queue')
        if not isinstance(self.chats, list) or len(self.chats) > 32:
            raise ValueError('invalid chat history')
        if set(self.audio) - CODES or set(self.banners) - CODES:
            raise ValueError('invalid service event code')
        for uid, item in self.retire.items():
            bounded_text(uid)
            if uid in (self.config.uid, self.position.pinned_uid, self.config.uid + '.external-position'):
                raise ValueError('reserved retirement UID')
            self.validate_job(item, retirement=True)
        for item in self.messages.values():
            self.validate_job(item)
        if type(self.input_memory.get('fault_epoch')) is not int or self.input_memory['fault_epoch'] < self.position._fault_epoch:
            raise ValueError('invalid evidence generation')
        new_boot = saved['boot_id'] != self.boot_id
        rebase_selector(self.position, number(saved['saved_mono']), now, new_boot)
        if new_boot:
            self.age_lower_bound = True
            for item in self.retire.values():
                item['due'] = now.mono
                if item['done_until'] is not None:
                    item['done_until'] = now.mono + 120
            for item in self.messages.values():
                item['due'] = now.mono
                if item['sent_mono'] is not None:
                    item['sent_mono'] += now.mono - number(saved['saved_mono']) - now.boot

    @staticmethod
    def validate_job(item, retirement=False):
        keys = {'id', 'attempts', 'due'} | ({'successes', 'done_until'} if retirement else {'sent_mono'})
        if not isinstance(item, dict) or set(item) != keys:
            raise ValueError('invalid persisted transport job')
        bounded_text(item['id'])
        if type(item['attempts']) is not int or not 0 <= item['attempts'] <= 5:
            raise ValueError('invalid retry count')
        number(item['due'])
        if not retirement and item['sent_mono'] is not None:
            number(item['sent_mono'])
        if retirement and (type(item['successes']) is not int or not 0 <= item['successes'] <= 3):
            raise ValueError('invalid retirement progress')
        if retirement and item['done_until'] is not None:
            number(item['done_until'])

    def commit(self, now):
        state = {'selector': vars(self.position), 'peer': self.peer, 'session': self.session,
                 'retire': self.retire, 'messages': self.messages, 'audio': self.audio,
                 'banners': self.banners, 'chats': self.chats, 'input_memory': self.input_memory,
                 'age_lower_bound': self.age_lower_bound}
        try:
            payload = {'schema': 1, 'uid': self.config.uid, 'boot_id': self.boot_id,
                       'saved_mono': now.mono, 'state': encode(state)}
            self.store.save(dict(payload, sha256=state_digest(payload)))
        except (OSError, ValueError, TypeError) as exc:
            raise PersistenceError('durable state write failed: ' + str(exc)) from exc
        self.last_checkpoint = now.mono
        self.saved_input_memory = encode(self.input_memory)
        self.revision += 1

    def reject(self, reason):
        self.last_rejection = str(reason)[:200]
        self.rejected = min(self.rejected + 1, 2 ** 31 - 1)
        return self.last_rejection

    def phone_ready(self, now, proof):
        return (self.config.enabled and proof is not None and self.peer is not None
                and self.position._presence is not None and now.mono < self.position._presence
                and proof.peer(self.peer['ip']) == self.peer['mac']
                and self.position.phone_now(now.mono) is not None)

    def retry_parked(self):
        for item in list(self.messages.values()) + list(self.retire.values()):
            if item['attempts'] >= 5 and item.get('done_until') is None:
                item['attempts'] = 0
                item['due'] = 0

    def ingest(self, packet, address, proof, now):
        if not self.config.enabled:
            return self.reject('ATAK disabled')
        mac = None if proof is None else proof.peer(address)
        if not mac:
            return self.reject('ingress/path unproven')
        if self.peer is not None and mac != self.peer['mac']:
            return self.reject('pinned MAC mismatch; explicit reassociation required')
        try:
            root = cot._parse_document(packet, MAX_XML)
            bounded_text(root.get('uid'), 512)
            for element in root.iter():
                for key, value in element.attrib.items():
                    bounded_text(value, 2048 if key == 'remarks' else 512)
                if element.text is not None and len(element.text) > 2048:
                    raise ValueError('oversized CoT text')
            kind = root.get('type')
            is_sa = kind == 'a-f-G-U-C' and root.find('detail/creator') is None
            if not is_sa and (self.peer is None or self.peer['ip'] != address or self.position.phone_now(now.mono) is None):
                return self.reject('admitted SA required before dependent traffic')
            if is_sa:
                parsed = cot.parse_self_sa(packet)
                if isinstance(parsed, str):
                    return self.reject(parsed)
                bounded_text(parsed.uid)
                update = self.position.feed(parsed, now.mono, now.utc, from_ethernet=True)
                if update.status not in ('accepted_presence', 'accepted_manual', 'retirement_queue_full'):
                    return self.reject(update.status)
                changed = self.peer != {'ip': address, 'mac': mac}
                self.peer = {'ip': address, 'mac': mac}
                if update.reappeared:
                    self.retry_parked()
                self.contact_changed |= changed or update.reappeared or update.status == 'accepted_manual'
                result = update.status
            elif kind in ('b-t-f-d', 'b-t-f-r'):
                parsed = cot.parse_chat_receipt(packet)
                if isinstance(parsed, str):
                    return self.reject(parsed)
                self.reconcile_uncertain_chat(parsed, now)
                result = self.position.feed_receipt(parsed, now.mono, now.utc, from_ethernet=True)
            elif kind == 'b-t-f':
                parsed = cot.parse_phone_chat(packet)
                if isinstance(parsed, str):
                    return self.reject(parsed)
                bounded_text(parsed.message_id)
                result = self.position.feed_chat(parsed, now.mono, now.utc, from_ethernet=True)
                if isinstance(result, cot.PhoneChat):
                    if not any(c['sender_uid'] == result.sender_uid and c['message_id'] == result.message_id for c in self.chats):
                        self.chats.append({'sender_uid': result.sender_uid, 'message_id': result.message_id,
                                           'callsign': result.callsign, 'text': result.text,
                                           'phone_time': result.time.isoformat()})
                        self.chats = self.chats[-32:]
                    result = 'chat'
            else:
                parsed = cot.parse_marker(packet)
                if isinstance(parsed, str):
                    return self.reject(parsed)
                bounded_text(parsed.uid)
                if parsed.uid in self.retire:
                    return self.reject('retirement in flight')
                update = self.position.feed_marker(parsed, now.mono, now.utc, from_ethernet=True)
                result = update.status + ':' + update.reason
                self.contact_changed |= update.status == 'accepted'
            self.reconcile_audio()
            self.commit(now)  # A persistent choice/pin precedes any output derived from it.
            return result
        except (ValueError, KeyError, TypeError, cot.ET.ParseError) as exc:
            # commit wraps write failures as fatal PersistenceError, which
            # must propagate instead of quietly discarding a durable pin.
            return self.reject(str(exc))

    def reconcile_audio(self):
        for code, item in list(self.audio.items()):
            warning = self.position._warnings.get(code)
            if warning is None or (warning.acknowledged and not item['one_shot']):
                del self.audio[code]

    def reconcile_uncertain_chat(self, receipt, now):
        """A valid receipt can confirm a send interrupted before durable ack.

        Its random message ID was persisted before the attempted UDP handoff.
        Preserve the first attempt's monotonic epoch, including after reboot.
        """
        if self.position._message_rejection(receipt, self.position.phone_now(now.mono), True) is not None:
            return
        for key, job in list(self.messages.items()):
            sent = job['sent_mono']
            if job['id'] != receipt.message_id or sent is None:
                continue
            if (self.position.phone_now(sent) - receipt.time).total_seconds() > self.position.future_tolerance_s:
                return
            event = next((e for e in self.position.outbound_events if str(e.event_id) == key and e.kind == 'geochat'), None)
            if event is None:
                return
            self.position.ack_event(event.event_id, now.mono, now.utc, message_id=job['id'])
            warning = self.position._warnings[event.code]
            warning.sent_mono = sent
            if not warning.acknowledged:
                warning.deadline = sent + self.position.warning_read_timeout_s
            del self.messages[key]
            return

    def audio_ack(self, data, now):
        if not isinstance(data, dict) or data.get('schema') != 1 or data.get('session') != self.session:
            return
        ids = data.get('ids')
        if not isinstance(ids, list) or len(ids) > 16 or not all(isinstance(v, str) for v in ids):
            return
        before = len(self.audio)
        self.audio = {code: item for code, item in self.audio.items() if item['id'] not in ids}
        if len(self.audio) != before:
            self.commit(now)

    def local_actions(self, now):
        self.reconcile_audio()
        for event in self.position.outbound_events:
            if event.kind == 'web_ui':
                if event.active:
                    self.banners[event.code] = event.text
                else:
                    self.banners.pop(event.code, None)
            elif event.kind == 'audio':
                key = self.session + ':' + str(event.event_id)
                self.audio[event.code] = {'id': key, 'code': event.code, 'text': event.text,
                                          'one_shot': event.code == 'gps_override'}
            else:
                continue
            self.commit(now)  # durable handoff before acknowledging selector output
            self.position.ack_event(event.event_id, now.mono, now.utc)
            self.commit(now)

    def send(self, data, port, now):
        try:
            if not self.transport.send(data, (self.peer['ip'], port)):
                raise OSError('short UDP send')
            self.send_error = None
            return True
        except OSError as exc:
            self.send_error = str(exc)[:200]
            return False

    def tick(self, now, proof, gps, input_status):
        if encode(self.input_memory) != self.saved_input_memory:
            self.commit(now)  # never forget that a formerly unwired producer became active
        # Capture policy transitions separately from 1 Hz feed/deadline timestamps.
        before = encode((self.position._warnings, self.position._events,
                         self.position._distrusted, self.position._overridden,
                         self.position._position_confirmed, self.position._checked_manual_epoch,
                         self.position._fault_epoch, self.position._retirements))
        selected = self.position.select(now.mono, now.utc, radio_gps=gps)
        after = encode((self.position._warnings, self.position._events,
                        self.position._distrusted, self.position._overridden,
                        self.position._position_confirmed, self.position._checked_manual_epoch,
                        self.position._fault_epoch, self.position._retirements))
        if before != after:
            self.commit(now)
        self.local_actions(now)
        for uid in selected.retire_marker_uids:
            if uid not in self.retire:
                if len(self.retire) >= MAX_RETIRE:
                    break
                self.retire[uid] = {'id': str(uuid.uuid4()), 'attempts': 0, 'successes': 0,
                                    'due': now.mono, 'done_until': None}
                self.commit(now)
            self.position.ack_retired((uid,))
            self.commit(now)
        ready = (self.config.enabled and proof is not None and self.peer is not None and selected.phone_present
                 and proof.peer(self.peer['ip']) == self.peer['mac'] and self.position.phone_now(now.mono) is not None)
        stamp = self.position.outbound_time(now.mono, now.utc)
        fix = selected.fix
        signature = (None if fix is None else (fix.lat, fix.lon, fix.hae, fix.source),
                     selected.gps_overridden, selected.position_trusted, tuple(sorted(self.banners.items())), self.config.callsign,
                     None if proof is None else (proof.local_ip, proof.generation), self.peer)
        if ready:
            if ((self.contact_attempt is None or now.mono - self.contact_attempt >= 1)
                    and (self.contact_changed or signature != self.contact_signature or cot.contact_refresh_due(now.mono, self.last_contact))):
                self.contact_attempt = now.mono
                if self.send(cot.hidden_contact(self.config.uid, self.config.callsign, (proof.local_ip, 4242), stamp), 4242, now):
                    self.last_contact, self.contact_signature, self.contact_changed = now.mono, signature, False
            if fix is not None and (self.feed_attempt is None or now.mono - self.feed_attempt >= 1):
                self.feed_attempt = now.mono
                packet = cot.external_position(self.config.uid, fix, stamp)
                if packet is not None and self.send(packet, 4349, now):
                    self.position.position_sent(fix, now.mono, now.utc)
                    self.last_feed = now.mono
            pending = {str(e.event_id): e for e in self.position.outbound_events if e.kind == 'geochat'}
            self.messages = {key: item for key, item in self.messages.items() if key in pending}
            for key, event in pending.items():
                item = self.messages.get(key)
                if item is None:
                    item = self.messages[key] = {'id': str(uuid.uuid4()), 'attempts': 0, 'due': now.mono, 'sent_mono': None}
                    self.commit(now)  # message ID stable through failure/crash/retry
                if item['attempts'] >= 5 or now.mono < item['due']:
                    continue
                item['attempts'] += 1
                item['due'] = now.mono + min(30, 2 ** item['attempts'])
                if item['sent_mono'] is None:
                    item['sent_mono'] = now.mono
                self.commit(now)
                if self.send(cot.geochat_report(self.config.uid, self.config.callsign, self.position.pinned_uid,
                                                event.text, item['id'], stamp), 4242, now):
                    self.position.ack_event(event.event_id, now.mono, now.utc, message_id=item['id'])
                    del self.messages[key]
                    self.commit(now)  # before any fast receipt is dispatched
                break
            for uid, item in list(self.retire.items()):
                if item['done_until'] is not None:
                    if now.mono >= item['done_until']:
                        del self.retire[uid]
                        self.commit(now)
                    continue
                if item['attempts'] >= 5 or now.mono < item['due']:
                    continue
                item['attempts'] += 1
                item['due'] = now.mono + min(30, 2 ** item['attempts'])
                self.commit(now)
                if self.send(cot.retire_marker(uid, item['id'], stamp), 4242, now):
                    item['successes'] += 1
                    if item['successes'] >= 3:
                        item['done_until'] = now.mono + 120
                self.commit(now)
                break
        if now.mono - self.last_checkpoint >= 30:
            self.commit(now)
        return self.status(now, proof, selected, input_status)

    def status(self, now, proof, selected=None, input_status=None):
        # An unassociated node has no phone policy to evaluate or announce.
        # Keep the same status schema without manufacturing a warning/audio job.
        clock = self.position.phone_now(now.mono)
        location = None if selected is None else selected.location
        fix = None if selected is None else selected.fix
        return {'schema': 1, 'running': True, 'boot_id': self.boot_id, 'written_boot': now.boot,
                'written_mono': now.mono,
                'enabled': self.config.enabled, 'path_ready': proof is not None,
                'phone_present': bool(self.phone_ready(now, proof)), 'phone_uid': self.position.pinned_uid,
                'peer': self.peer, 'selected': None if fix is None else asdict(fix),
                'manual_age_s': None if location is None else location.age_s,
                'manual_observation_mono': None if location is None else location.observation_mono,
                'manual_received_mono': None if location is None else location.received_mono,
                'manual_observation_time': None if location is None else location.observation_time.isoformat(),
                'manual_marker_uid': None if location is None else location.marker_uid,
                'manual_age_is_lower_bound': self.age_lower_bound,
                'gps_state': 'NO_FIX' if selected is None else selected.gps_state,
                'position_trusted': False if selected is None else selected.position_trusted,
                'gps_overridden': False if selected is None else selected.gps_overridden,
                'inputs': input_status or {},
                'banners': self.banners, 'phone_clock_offset_s': None if clock is None else (clock - now.utc).total_seconds(),
                'clock_diagnostic': None if self.position.clock_diagnostic is None else asdict(self.position.clock_diagnostic),
                'chat': self.chats, 'audio_pending': len(self.audio), 'retirements': len(self.retire),
                'retirement_delivery_verified': False,
                'parked_retries': sum(i['attempts'] >= 5 and i.get('done_until') is None for i in list(self.messages.values()) + list(self.retire.values())),
                'rejected': self.rejected, 'last_rejection': self.last_rejection, 'send_error': self.send_error}


class Application:
    """Skip idle application work, retaining event and safety deadline handling."""
    def __init__(self, service, inputs, runtime):
        self.service, self.inputs, self.runtime = service, inputs, Path(runtime)
        self.last_key = self.last_audio = self.ack_stamp = None
        self.input_revision = None

    def update(self, now, proof, reason):
        service = self.service
        if service.audio:
            path = self.runtime / 'audio-ack.json'
            stamp = file_stamp(path)
            if stamp is not None and stamp != self.ack_stamp:
                try:
                    service.audio_ack(read_json(path, 8192), now)
                    self.ack_stamp = stamp
                except (FileNotFoundError, ValueError):
                    pass
        gps, input_status = self.inputs.cached_read(now, service.input_memory, receiver=service.peer is not None)
        if self.input_revision != self.inputs.revision:
            # Observe producer activation/loss even before the first phone, so
            # an absent formerly-active monitor cannot later look "unwired".
            if encode(service.input_memory) != service.saved_input_memory:
                service.commit(now)
            self.input_revision = self.inputs.revision
        present = service.position._presence is not None and now.mono < service.position._presence
        gps_key = None if service.peer is None else (
            gps.fix, gps.observed_mono if gps.fix is not None else None,
            gps.state, gps.jammed, gps.fault_epoch, gps.ranges_agree)
        def key():
            return (service.revision, service.rejected, service.last_rejection,
                    service.config, proof, reason, gps_key, input_status, present)
        deadline = any(not w.acknowledged and w.deadline is not None and now.mono >= w.deadline
                       for w in service.position._warnings.values())
        # These timestamps preserve manual-age lower bounds and warning history
        # across reboot. A never-associated node needs no periodic checkpoint.
        checkpoint = (service.peer is not None and
                      (service.position._manual is not None or service.position._warnings) and
                      now.mono - service.last_checkpoint >= 30)
        if (not service.phone_ready(now, proof) and self.last_key == key()
                and not deadline and not checkpoint):
            return False
        status = (service.status(now, proof, input_status=input_status) if service.peer is None else
                  service.tick(now, proof, gps, input_status))
        status['path_reason'] = reason
        status['idle'] = not status['phone_present']
        atomic_json(self.runtime / 'status.json', status)
        audio = {'schema': 1, 'session': service.session, 'events': list(service.audio.values())}
        if audio != self.last_audio:
            atomic_json(self.runtime / 'audio.json', audio)
            self.last_audio = audio
        self.last_key = key()
        return True


class Budget:
    def __init__(self, now):
        self.at, self.bytes, self.events, self.accepts = now, 128 * 1024, 64, 16

    def take(self, now, *, size=0, events=0, accepts=0):
        elapsed = max(0, now - self.at)
        self.at = now
        self.bytes = min(128 * 1024, self.bytes + elapsed * 65536)
        self.events = min(64, self.events + elapsed * 32)
        self.accepts = min(16, self.accepts + elapsed * 8)
        if size > self.bytes or events > self.events or accepts > self.accepts:
            return False
        self.bytes -= size
        self.events -= events
        self.accepts -= accepts
        return True


class Stream:
    def __init__(self, sock, address, mac, now):
        self.sock, self.address, self.mac = sock, address, mac
        self.created = self.last_read = now
        self.partial_since = None
        self.total = self.events = 0
        self.framer = cot.CotStreamFramer(MAX_XML)

    def expired(self, now):
        return (now - self.last_read >= IDLE_S or now - self.created >= LIFETIME_S
                or (self.partial_since is not None and now - self.partial_since >= PARTIAL_S))

    def receive(self, data, now, budget):
        if self.expired(now):
            raise ValueError('stream timeout')
        if not data:
            self.framer.finish()
            return None
        self.total += len(data)
        if self.total > MAX_STREAM_BYTES or not budget.take(now, size=len(data)):
            raise ValueError('stream byte budget exceeded')
        documents = self.framer.feed(data)
        self.events += len(documents)
        if self.events > MAX_STREAM_EVENTS or not budget.take(now, events=len(documents)):
            raise ValueError('stream event budget exceeded')
        self.last_read = now
        if self.framer.buffered_bytes:
            if self.partial_since is None or documents:
                self.partial_since = now
        else:
            self.partial_since = None
        return documents


class Network:
    """Nonblocking IPv4 sockets; fake socket factory/readiness supplied by tests."""
    def __init__(self, factory=socket.socket, verify=None):
        self.factory = factory
        self.verify = verify
        self.listener = self.udp = self.sender = None
        self.proof = None
        self.streams = {}
        self.datagram_peers = {}
        self.budget = Budget(0)

    def close_stream(self, sock):
        self.streams.pop(sock, None)
        sock.close()

    def close(self):
        for sock in list(self.streams):
            self.close_stream(sock)
        for name in ('listener', 'udp', 'sender'):
            sock = getattr(self, name)
            if sock is not None:
                sock.close()
                setattr(self, name, None)
        self.proof = None
        self.datagram_peers.clear()

    def configure(self, proof):
        old = None if self.proof is None else (self.proof.local_ip, self.proof.generation)
        new = None if proof is None else (proof.local_ip, proof.generation)
        if old != new:
            self.close()
        if proof is not None and self.listener is None:
            try:
                self.listener = self.make(socket.SOCK_STREAM, proof.local_ip, 4242)
                self.listener.listen(MAX_CONNECTIONS)
                self.udp = self.make(socket.SOCK_DGRAM, proof.local_ip, 4242)
                self.sender = self.make(socket.SOCK_DGRAM, proof.local_ip, 0)
                # Linux IP_MTU_DISCOVER=10 / IP_PMTUDISC_DO=2. Never fragment an
                # output packet around the bridge's port-based egress filter.
                self.sender.setsockopt(socket.IPPROTO_IP, 10, 2)
                self.sender.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, 1)
            except OSError:
                self.close()
                raise
        self.proof = proof
        self.datagram_peers = {ip: mac for ip, mac in self.datagram_peers.items()
                               if proof is not None and proof.peer(ip) == mac}
        for sock, stream in list(self.streams.items()):
            if proof is None or proof.peer(stream.address) != stream.mac:
                self.close_stream(sock)

    def make(self, kind, ip, port):
        sock = self.factory(socket.AF_INET, kind)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b'br0\0')
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
            sock.setblocking(False)
            sock.bind((ip, port))
            return sock
        except BaseException:
            sock.close()
            raise

    def send(self, packet, destination):
        if self.verify is not None:
            self.configure(self.verify())
        if self.sender is None or self.proof is None or not self.proof.peer(destination[0]):
            raise OSError('local phone path unavailable')
        if len(packet) > 65507:
            raise OSError('CoT exceeds UDP limit')
        return self.sender.sendto(packet, destination) == len(packet)

    def readers(self):
        return ([self.listener, self.udp] if self.listener is not None else []) + list(self.streams)

    def admit(self, address):
        """Every new TCP stream / UDP peer gets a fresh, stable snapshot."""
        old = self.proof
        if self.verify is not None:
            self.configure(self.verify(force=True))
        if (old is None or self.proof is None or
                (old.local_ip, old.generation) != (self.proof.local_ip, self.proof.generation)):
            return None
        return self.proof.peer(address)

    def handle(self, readable, now, service):
        for sock, stream in list(self.streams.items()):
            if stream.expired(now.mono):
                self.close_stream(sock)
        if self.proof is None:
            return
        for sock in readable:
            try:
                if sock is self.listener:
                    client, endpoint = sock.accept()
                    address = endpoint[0]
                    if (len(self.streams) >= MAX_CONNECTIONS
                            or sum(s.address == address for s in self.streams.values()) >= MAX_PER_PEER
                            or not self.budget.take(now.mono, accepts=1)):
                        client.close()
                        continue
                    try:
                        mac = self.admit(address)
                    except BaseException:
                        client.close()
                        raise
                    if not mac:
                        client.close()
                        continue
                    client.setblocking(False)
                    self.streams[client] = Stream(client, address, mac, now.mono)
                elif sock is self.udp:
                    data, endpoint = sock.recvfrom(MAX_XML + 1)
                    if not self.budget.take(now.mono, size=len(data), events=1):
                        continue
                    framer = cot.CotStreamFramer(MAX_XML)
                    documents = framer.feed(data)
                    framer.finish()
                    if len(documents) != 1:
                        raise ValueError('one CoT document required per datagram')
                    if endpoint[0] not in self.datagram_peers:
                        if not self.budget.take(now.mono, accepts=1):
                            continue
                        mac = self.admit(endpoint[0])
                        if not mac:
                            continue
                        if len(self.datagram_peers) >= MAX_STREAM_EVENTS:
                            self.datagram_peers.pop(next(iter(self.datagram_peers)))
                        self.datagram_peers[endpoint[0]] = mac
                    service.ingest(documents[0], endpoint[0], self.proof, now)
                elif sock in self.streams:
                    stream = self.streams[sock]
                    documents = stream.receive(sock.recv(16384), now.mono, self.budget)
                    if documents is None:
                        self.close_stream(sock)
                    else:
                        for packet in documents:
                            service.ingest(packet, stream.address, self.proof, now)
            except (BlockingIOError, InterruptedError):
                pass
            except (OSError, ValueError) as exc:
                service.reject(str(exc))
                if sock in self.streams:
                    self.close_stream(sock)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/etc/mesh.conf')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--print-firewall', action='store_true', help='print the unit\'s nft JSON policy')
    action.add_argument('--install-firewall', action='store_true', help='unit pre-start: install unless explicitly disabled')
    action.add_argument('--remove-firewall', action='store_true', help='unit post-stop: remove both private tables')
    args = parser.parse_args(argv)
    if args.print_firewall:
        print(json.dumps(firewall_transaction(), indent=2))
        return
    if args.remove_firewall:
        apply_firewall(install=False)  # cleanup must work even with a missing/broken config
        return
    if args.install_firewall:
        # A disabled start also removes stale tables; no rules become active.
        apply_firewall(install=atak_enabled(read_kv(args.config)))
        return
    config = Config.load(args.config)
    runtime, state_dir = Path('/run/manet-atak'), Path('/var/lib/manet-atak')
    runtime.mkdir(mode=0o750, exist_ok=True)
    state_dir.mkdir(mode=0o700, exist_ok=True)
    if not config.enabled:
        atomic_json(runtime / 'status.json', {'schema': 1, 'running': False, 'enabled': False, 'phone_present': False})
        atomic_json(runtime / 'audio.json', {'schema': 1, 'session': '', 'events': []})
        log('disabled by mesh.conf (atak=n)')
        return
    with (state_dir / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        bounded_text(boot_id)
        clock, network = Clock(), Network()
        service = Service(config, boot_id, Store(state_dir / 'state.json'), network, clock.now())
        inputs, guard = Inputs(boot_id), LinuxGuard(config)
        application = Application(service, inputs, runtime)
        network.verify = guard.current
        flags = {'stop': False, 'reload': False}
        signal.signal(signal.SIGTERM, lambda *_: flags.update(stop=True))
        signal.signal(signal.SIGINT, lambda *_: flags.update(stop=True))
        signal.signal(signal.SIGHUP, lambda *_: flags.update(reload=True))
        log('starting; IPv4 TCP/UDP 4242; waiting for verified wired EUD policy')
        next_tick, next_cycle, last_reason = 0, 0, None
        try:
            guard.start()
            while not flags['stop']:
                if flags['reload']:
                    config = Config.load(args.config)
                    if config.uid != service.config.uid:
                        raise ValueError('radio identity changed')
                    service.config = config
                    guard.config = config
                    guard.invalidate()
                    network.close()
                    if not config.enabled:
                        break
                    service.retry_parked()
                    service.contact_changed = True
                    flags['reload'] = False
                readers = network.readers() + guard.readers()
                ready = select.select(readers, [], [], .2)[0] if readers else []
                if not readers:
                    time.sleep(.2)
                now = clock.now()
                if now.mono < next_cycle:
                    time.sleep(min(.2, next_cycle - now.mono))
                    continue
                if ready or now.mono >= next_tick:
                    # Cached between kernel events / role-generation changes.
                    # New admissions force a full readback inside handle().
                    proof = guard.current()
                    try:
                        network.configure(proof)
                    except OSError as exc:
                        proof = None
                        guard.reason = 'socket setup failed: ' + str(exc)
                    if guard.reason != last_reason:
                        log(guard.reason)
                        last_reason = guard.reason
                    now = clock.now()
                    next_cycle = now.mono + .2  # bound expensive path readbacks under a flood
                    network.handle(ready, now, service)
                    proof = network.proof  # admission may have revoked it
                    application.update(now, proof, guard.reason)
                    if now.mono >= next_tick:
                        next_tick = now.mono + 1  # incoming traffic never postpones the timer
        finally:
            network.close()
            guard.close()
            service.commit(clock.now())
            atomic_json(runtime / 'audio.json', {'schema': 1, 'session': service.session, 'events': []})
            atomic_json(runtime / 'status.json', {'schema': 1, 'boot_id': boot_id,
                                                  'written_boot': clock.now().boot, 'running': False,
                                                  'phone_present': False})


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, PersistenceError, GuardFailure) as exc:
        log('fatal: ' + str(exc))
        sys.exit(1)
