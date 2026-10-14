#!/usr/bin/env python3
"""Serve local ATAK phones with passive mesh SA and unicast protocol-0 CoT.

Read mesh.conf (enabled unless atak=n/no/0/false), gps_status.json and
optional monitor/jam interfaces without supplying clock authority. The unit
installs --print-firewall policy before start and removes it after stop.
The daemon verifies policy, logs to the journal and writes atomic /run JSON
for the UI, as mesh-voice.py does. The versioned persistence adapter owns
selector private state, whose layout changes require a schema review.

Persistence schema 2 adds a bounded phone table with independent SA clocks,
controller mode, per-recipient notices and retirement ownership. Schema 1 is
explicitly discarded (test installations); unknown schemas and damaged state
still fail closed. The radio's manual location and GPS distrust belong to the
single selector, never to a controller's lifetime.
"""

import argparse
import ctypes
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
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
import secrets
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid

import manet_cot as cot
import manet_phone as phone
import manet_tak as tak

# Bound XML/state bytes and per-stream work before application dispatch.
MAX_XML = 65536
MAX_STATE = 8 * 1024 * 1024
MAX_CONNECTIONS = 8
MAX_PER_PEER = 4
MAX_STREAM_BYTES = 256 * 1024
MAX_STREAM_EVENTS = 256
# TCP deadlines in monotonic seconds: inactivity, partial frame and lifetime.
IDLE_S = 45
PARTIAL_S = 5
LIFETIME_S = 300
# Producer envelopes expire five BOOTTIME seconds after their write timestamp.
INPUT_TTL = 5
GPS_UNAVAILABLE_DELAY_S = 3
MAX_RETIRE = 128
CODES = {'gps_untrusted', 'gps_override', 'confirm_position', 'phone_gps'}
KINDS = {'geochat', 'audio', 'web_ui'}
# Require exactly six lowercase hexadecimal octets separated by colons.
MAC = re.compile(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\Z')
TABLE = 'manet_atak'
REWRITE_SET = 'rewrite_phones'
REWRITE_TIMEOUT_S = 3
TAK_SERVER_PORTS = (8087, 8089)
TAK_SERVER_HOLD_S = 600
TAK_SET = 'tak_phones'
MAX_TAK_PHONES = 1024
TAK_READ_INTERVAL_S = 5
TAK_READ_MAX_AGE_S = 5
TAK_READ_TIMEOUT_S = 2
WAIT_TAK = object()
# Two assumed 10 m horizontal error budgets plus 30 m local AP separation.
GPS_DISAGREE_M = 50
GPS_AGREE_M = 35
GPS_AGREE_S = 30
# Operational allowances for phone GPS noise and deliberate small corrections.
MANUAL_MOVE_M = 30
MANUAL_MOVE_S = 15
MANUAL_MAX_OFFSET_M = 200
HELD_MARKER_REFRESH_S = 60
DEFAULT_PHONE_LIMIT = 8
MAX_PHONE_LIMIT = 253
AP_ROLE = '/var/lib/ap_interface'
HOSTAPD_CONF = '/etc/hostapd/hostapd.conf'


class PersistenceError(RuntimeError):
    """Fatal: continuing could forget an association or acknowledge lost work."""


def state_digest(data):
    """Detect damaged checkpoints without authenticating privileged edits."""
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def log(message):
    """Write a tagged, flushed journal message."""
    print('[manet-atak] ' + str(message), flush=True)


def number(value):
    """Require a finite numeric value, excluding booleans."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('expected finite number')
    return float(value)


def bounded_text(value, maximum=128):
    """Require nonempty text within the character limit."""
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError('invalid or oversized text')
    return value


def ipv4(value):
    """Validate and normalize an IPv4 endpoint address."""
    address = ipaddress.IPv4Address(value)
    if address.is_multicast or address.is_unspecified or address.is_loopback or int(address) == 0xffffffff:
        raise ValueError('unicast IPv4 required')
    return str(address)


def read_json(path, limit=MAX_STATE):
    """Read bounded JSON and reject nonfinite numeric constants."""
    with open(path, 'rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('JSON exceeds size limit')
    # Python's decoder otherwise accepts NaN and Infinity as JSON numbers.
    return json.loads(data, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def atomic_json(path, data, *, durable=False):
    """Replace bounded JSON atomically, optionally syncing it to disk."""
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
        # Sync the directory entry as well as file contents across power loss.
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def read_kv(path):
    """Read quoted key/value assignments without evaluating shell syntax."""
    result = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            result[key.strip()] = value.strip().strip('\"\'')
    return result


def atak_enabled(conf):
    """Enable ATAK unless configuration explicitly disables it."""
    return conf.get('atak', '').strip().lower() not in {'n', 'no', '0', 'false'}


def file_stamp(path):
    """Return file identity and change metadata, or None when absent."""
    try:
        s = Path(path).stat()
        return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns
    except FileNotFoundError:
        return None


@dataclass(frozen=True)
class Config:
    """Hold the radio identity, contact label and listener settings."""
    uid: str
    callsign: str = 'MANET RADIO'
    enabled: bool = True
    bind_ip: str | None = None
    max_phones: int = DEFAULT_PHONE_LIMIT
    beacon_name: str | None = None

    @classmethod
    def load(cls, path='/etc/mesh.conf'):
        """Read ATAK settings and derive the UID from machine-id."""
        conf = read_kv(path)
        machine = Path('/etc/machine-id').read_text().strip()
        # A stable 32-digit hexadecimal machine ID namespaces the radio UID.
        if not re.fullmatch('[0-9a-f]{32}', machine):
            raise ValueError('machine-id unavailable')
        capacity = conf.get('max_euds_per_node', '')
        if (not capacity.isascii() or not capacity.isdigit() or len(capacity) > 3
                or int(capacity) > MAX_PHONE_LIMIT):
            log('max_euds_per_node absent or invalid; using bounded default ' + str(DEFAULT_PHONE_LIMIT))
            limit = DEFAULT_PHONE_LIMIT
        else:
            # Wired provisioning stores zero Wi-Fi EUDs; retain its primary phone.
            limit = max(1, int(capacity))
        return cls('MANET-' + machine,
                   bounded_text(conf.get('atak_callsign', 'MANET ' + socket.gethostname()), 64),
                   atak_enabled(conf),
                   ipv4(conf['atak_bind_ip']) if conf.get('atak_bind_ip') else None, limit,
                   bounded_text(conf.get('atak_callsign', socket.gethostname()), 64))


@dataclass(frozen=True)
class Now:
    """Carry monotonic, raw and boot seconds alongside aware radio UTC."""
    mono: float
    raw: float
    boot: float
    utc: datetime


class Clock:
    """Sample the clock domains used by deadlines and producer freshness."""
    def now(self):
        """Read local clocks into one application sample."""
        return Now(time.monotonic(), time.clock_gettime(time.CLOCK_MONOTONIC_RAW),
                   time.clock_gettime(time.CLOCK_BOOTTIME), datetime.now(timezone.utc))


def match(left, right, op='=='):
    """Build an nft JSON comparison expression."""
    return {'match': {'op': op, 'left': left, 'right': right}}


def meta(key):
    """Build an nft JSON packet-metadata reference."""
    return {'meta': {'key': key}}


def payload(protocol, field):
    """Build an nft JSON protocol-field reference."""
    return {'payload': {'protocol': protocol, 'field': field}}


def atak_ap_interface(path=AP_ROLE):
    """Read the shared AP role, accepting only a literal Linux interface name."""
    try:
        name = Path(path).read_text().strip()
    except FileNotFoundError:
        return None
    if not name:
        return None
    if not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,14}', name) or name in {'end0', 'br0', 'bat0', 'lo'}:
        raise ValueError('invalid AP interface role')
    return name


def atak_eud_ports(ap=None):
    """Return the explicit policy allow-list; runtime verifies each port's role."""
    return ('end0',) if ap is None else ('end0', ap)


def firewall_objects(ports=('end0',)):
    """Return canonical nft JSON for explicit local EUD interface names.

    Bridge input covers local delivery, output unknown-unicast flooding.
    Inet input excludes routed/loopback ingress and IPv6. Before bridge/IP
    defragmentation, reject first IPv4 fragments addressed to TCP/UDP 4242/4349 on
    any bridge, including forwarded traffic. Without an offset-zero fragment,
    packets cannot reassemble even if later fragments arrive on other ports.
    Other ports/protocols and noninitial fragments are unaffected.
    """
    allowed = ports[0] if len(ports) == 1 else {'set': list(ports)}
    service_ports = {'set': [4242, 4349]}
    objects = []
    for family, hooks in (('bridge', ('prerouting', 'input', 'output', 'forward')), ('inet', ('input',))):
        objects.append({'table': {'family': family, 'name': TABLE}})
        for hook in hooks:
            # Priority -450 checks fragment headers before defragmentation.
            objects.append({'chain': {'family': family, 'table': TABLE, 'name': hook,
                                     'type': 'filter', 'hook': hook,
                                     'prio': -450 if hook == 'prerouting' else 0, 'policy': 'accept'}})
    objects.append({'set': {'family': 'bridge', 'table': TABLE, 'name': REWRITE_SET,
                            'type': ['ifname', 'ether_addr', 'ipv4_addr'], 'flags': ['timeout'],
                            'timeout': REWRITE_TIMEOUT_S, 'size': MAX_PHONE_LIMIT}})
    objects.append({'set': {'family': 'bridge', 'table': TABLE, 'name': TAK_SET,
                            'type': 'ether_addr', 'flags': ['dynamic', 'timeout'],
                            'timeout': TAK_SERVER_HOLD_S, 'size': MAX_TAK_PHONES}})
    for protocol in ('ip', 'ip6'):
        objects.append({'rule': {'family': 'bridge', 'table': TABLE, 'chain': 'prerouting', 'expr': [
            match(meta('iifname'), allowed), match(payload('ether', 'type'), protocol),
            match(meta('l4proto'), 'tcp'), match(payload('tcp', 'dport'), {'set': list(TAK_SERVER_PORTS)}),
            {'set': {'op': 'update', 'elem': payload('ether', 'saddr'), 'set': '@' + TAK_SET}}]}})
    def rule(family, chain, expr):
        """Append a rule dropping packets matched by the expressions."""
        objects.append({'rule': {'family': family, 'table': TABLE, 'chain': chain, 'expr': expr + [{'drop': None}]}})
    for protocol in ('tcp', 'udp'):
        # No ibrname match: node kernels lack CONFIG_NFT_BRIDGE_META, and a
        # first fragment to either ATAK port has no legitimate use on any bridge.
        # Mask offset and MF bits: offset zero with more fragments set.
        rule('bridge', 'prerouting', [match(payload('ether', 'type'), 'ip'),
                                     match({'&': [payload('ip', 'frag-off'), 0x3fff]}, 0x2000),
                                     match(meta('l4proto'), protocol),
                                     match(payload(protocol, 'dport'), service_ports)])
        rule('bridge', 'input', [match(meta('iifname'), allowed, '!='),
                                match(payload('ether', 'type'), 'ip'),
                                match(meta('l4proto'), protocol),
                                match(payload(protocol, 'dport'), service_ports)])
        for field, ports in (('dport', {'set': [4242, 4349]}), ('sport', 4242)):
            rule('bridge', 'output', [match(meta('oifname'), allowed, '!='),
                                     match(payload('ether', 'type'), 'ip'),
                                     match(meta('l4proto'), protocol),
                                     match(payload(protocol, field), ports)])
        rule('inet', 'input', [match(meta('nfproto'), 'ipv6'), match(meta('l4proto'), protocol), match(payload(protocol, 'dport'), service_ports)])
        rule('inet', 'input', [match(meta('iifname'), 'br0', '!='), match(meta('l4proto'), protocol), match(payload(protocol, 'dport'), service_ports)])
    # Host-originated radio SA crosses only bat0. This does not affect bridged
    # phone SA or mesh traffic forwarded to another radio's local EUDs.
    rule('bridge', 'output', [match(meta('oifname'), 'bat0', '!='),
                             match(payload('ether', 'type'), 'ip'),
                             match(payload('ip', 'daddr'), tak.MESH_GROUP),
                             match(meta('l4proto'), 'udp'),
                             match(payload('udp', 'dport'), tak.MESH_PORT)])
    rule('bridge', 'forward', [match(meta('iifname'), allowed), match(meta('oifname'), 'bat0'),
                              match(payload('ether', 'saddr'), '@' + TAK_SET, '!='),
                              match(payload('ether', 'type'), 'ip'),
                              match(payload('ip', 'version'), 4),
                              match(payload('ip', 'hdrlength'), 5, '>='),
                              match({'&': [payload('ip', 'frag-off'), 0xbfff]}, 0),
                              match(payload('ip', 'length'), MAX_MESH_FRAME - 14, '<='),
                              match(meta('length'), MAX_MESH_FRAME - 14, '<='),
                              match(payload('ip', 'daddr'), tak.MESH_GROUP),
                              match(meta('l4proto'), 'udp'), match(payload('udp', 'dport'), tak.MESH_PORT),
                              match({'concat': [meta('iifname'), payload('ether', 'saddr'), payload('ip', 'saddr')]},
                                    '@' + REWRITE_SET)])
    return objects


def firewall_transaction(*, install=True, ports=None):
    """Build an atomic batch, tolerating missing or partly present tables."""
    commands = []
    for family in ('bridge', 'inet'):
        table = {'table': {'family': family, 'name': TABLE}}
        # nft add permits an existing table, so delete always has a target.
        commands += [{'add': table}, {'delete': table}]
    if install:
        commands.extend({'add': entry} for entry in firewall_objects(
            atak_eud_ports(atak_ap_interface()) if ports is None else ports))
    return {'nftables': commands}


def apply_firewall(*, install):
    """Run only as a unit pre/post hook, never inside the daemon."""
    try:
        # Bound privileged unit hooks to five seconds per transaction.
        subprocess.run(['nft', '-j', '-f', '-'],
                       input=json.dumps(firewall_transaction(install=install)),
                       check=True, capture_output=True, text=True, timeout=5)
    except subprocess.CalledProcessError as exc:
        raise OSError('ATAK firewall transaction failed: ' + (exc.stderr or '').strip()) from exc
    except subprocess.TimeoutExpired as exc:
        raise OSError('ATAK firewall transaction timed out') from exc


def _canonical_expr(expr):
    """Drop implicit nft matches omitted from readback.

    Loading a payload adds 'meta l4proto X' for X or 'ether type ip' for ip.
    Listing omits these redundant matches without changing packet semantics.
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
        if m and isinstance(m.get('right'), dict) and set(m['right']) == {'set'}:
            # nft may reorder anonymous set members on readback; membership is
            # unordered, but duplicate members or other expressions stay strict.
            values = m['right']['set']
            if isinstance(values, list) and all(type(v) in (int, str) for v in values):
                entry = {'match': dict(m, right={'set': sorted(values, key=lambda v: (type(v).__name__, v))})}
        kept.append(entry)
    return list(reversed(kept))


def valid_firewall(data, ports=('end0',)):
    """Compare owned nft policy with the required tables and rules."""
    actual = []
    for item in data.get('nftables', []):
        for kind, keys in (('table', ('family', 'name')),
                           ('chain', ('family', 'table', 'name', 'type', 'hook', 'prio', 'policy')),
                           ('set', ('family', 'table', 'name', 'type', 'flags', 'timeout', 'size')),
                           ('rule', ('family', 'table', 'chain', 'expr'))):
            obj = item.get(kind)
            if obj is None or obj.get('family') not in ('bridge', 'inet'):
                continue
            if obj.get('name' if kind == 'table' else 'table') != TABLE:
                continue
            if kind == 'table' and obj.get('flags'):
                return False  # in particular, a dormant table provides no proof
            if kind == 'set':
                if obj.get('map') is not None:
                    return False
                elements = obj.get('elem', [])
                hold = TAK_SERVER_HOLD_S if obj.get('name') == TAK_SET else REWRITE_TIMEOUT_S
                capacity = MAX_TAK_PHONES if obj.get('name') == TAK_SET else MAX_PHONE_LIMIT
                if not isinstance(elements, list) or len(elements) > capacity:
                    return False
                for element in elements:
                    value = element.get('elem') if isinstance(element, dict) else None
                    if (not isinstance(value, dict) or
                            any(type(value.get(key, hold)) not in (int, float) or
                                not math.isfinite(value.get(key, hold)) for key in ('timeout', 'expires')) or
                            not 0 < value.get('timeout', hold) <= hold or
                            not 0 <= value.get('expires', hold) <= hold):
                        return False  # No unbounded or extended block lease.
                    if obj.get('name') == TAK_SET and (
                            not isinstance(value.get('val'), str) or not MAC.fullmatch(value['val'])
                            or value.get('timeout', hold) != hold):
                        return False
            obj = {key: obj.get(key) for key in keys}
            if kind == 'set' and isinstance(obj.get('flags'), list):
                obj['flags'] = sorted(obj['flags'])
            if kind == 'rule':
                obj['expr'] = [entry for entry in obj['expr'] if 'counter' not in entry]
            actual.append({kind: obj})
        # Reject auxiliary sets/maps/flowtables in this private policy table.
        for kind, obj in item.items():
            if kind not in ('table', 'chain', 'rule', 'set', 'metainfo') and isinstance(obj, dict) and obj.get('table') == TABLE:
                return False
    # Kernel dumps group tables by family. Preserve rule order per chain.
    def grouped(items):
        """Group nft objects while retaining rule order within each chain."""
        result = {}
        for entry in items:
            kind, obj = next(iter(entry.items()))
            key = (obj['family'], kind, obj.get('chain', obj.get('name', '')))
            result.setdefault(key, []).append(obj)
        return result
    def canonical(items):
        """Normalize implicit matches before policy comparison."""
        result = []
        for entry in items:
            if 'rule' in entry:
                rule = dict(entry['rule'])
                rule['expr'] = _canonical_expr(rule['expr'])
                entry = {'rule': rule}
            result.append(entry)
        return result
    return grouped(canonical(actual)) == grouped(canonical(firewall_objects(ports)))


@dataclass(frozen=True)
class Proof:
    """Record the verified address, local EUD peers and role generation."""
    local_ip: str
    peers: dict  # IP -> current dynamic MAC learned exclusively on a local EUD port
    generation: str
    ports: dict = field(default_factory=dict)  # IP -> wired or wifi
    interfaces: dict = field(default_factory=dict)  # IP -> exact ingress interface
    local_ports: dict = field(default_factory=dict)  # interface -> role/lifetime identity
    local_ips: tuple = ()  # all global br0 IPv4 addresses, including secondary
    mesh_path: tuple = ()  # verified br0/bat0 ifindices for beacon egress
    fdb_flags: dict = field(default_factory=dict)  # MAC -> recognized dynamic ndm_flags

    def local_address(self, address):
        """Require a unicast destination actually assigned globally to br0."""
        return address in (self.local_ips or (self.local_ip,))

    def port(self, address):
        """Return the proven local attachment type (legacy fixtures are wired)."""
        return self.ports.get(address, 'wired') if address in self.peers else None

    def peer(self, address):
        """Return the verified Ethernet MAC for a peer address."""
        return self.peers.get(address)


class GuardFailure(RuntimeError):
    """Terminate service when network notifications cannot be trusted."""
    pass


def fdb_refresh_flags(entry):
    """Recognize plain dynamic bridge JSON, retaining exact non-static flags.

    iproute2 prints NDA_MASTER separately from its ndm_flags array. VLAN,
    extended/unknown metadata and ambiguous rows require conservative readback.
    """
    bits = {'master': 0x04, 'extern_learn': 0x10, 'offload': 0x20,
            'sticky': 0x40, 'router': 0x80}
    flags = entry.get('flags', [])
    if (set(entry) - {'mac', 'ifname', 'dev', 'master', 'state', 'flags'}
            or entry.get('master', 'br0') != 'br0'
            or entry.get('state', '') not in ('', 'stale')
            or not isinstance(flags, list)
            or any(not isinstance(flag, str) or flag not in bits for flag in flags)
            or len(set(flags)) != len(flags)):
        return None
    return sum(bits[flag] for flag in flags)


def netlink_attr(kind, data):
    """Encode a four-byte aligned netlink attribute."""
    return struct.pack('=HH', len(data) + 4, kind) + data + b'\0' * (-len(data) % 4)


def netlink_attrs(data):
    """Decode bounded attributes, preserving repeated nested list members."""
    result = []
    while data:
        if len(data) < 4:
            raise OSError('short netlink attribute')
        size, kind = struct.unpack_from('=HH', data)
        if size < 4 or size > len(data) or (size + 3) & ~3 > len(data):
            raise OSError('invalid netlink attribute length')
        result.append((kind & 0x3fff, data[4:size]))
        data = data[(size + 3) & ~3:]
    return result


def netlink_fields(data):
    """Require unique attributes, allowing only empty alignment padding."""
    fields = {}
    for kind, value in netlink_attrs(data):
        if kind == 8 and not value:  # NFTA_SET_ELEM_PAD
            continue
        if kind in fields:
            raise OSError('duplicate netlink attribute')
        fields[kind] = value
    return fields


class NetworkEvents:
    """Kernel notifications in the existing select loop, never a monitor child.

    Subscribe before taking a snapshot. Lost/truncated notifications are fatal:
    the unit restarts with new subscriptions and no previously admitted peers.
    """
    def __init__(self, factory=socket.socket, monotonic=time.monotonic):
        """Subscribe to route and nftables changes with nonblocking sockets."""
        self.sockets = []
        self.scope = None
        self.rewrite_sequence = 0
        self.rewrite_acks = set()
        self.rewrite_notifications = {}
        self.nft_portid = None
        self.monotonic = monotonic
        self.tak_pending = None
        self.tak_requested = None
        self.tak_snapshot = None
        self.tak_observed = None
        self.tak_partial = {}
        self.tak_bytes = 0
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
        """Close every notification subscription."""
        for sock in self.sockets:
            sock.close()
        self.sockets.clear()

    def read_tak(self, now):
        """Request a bounded dump on demand, reusing a five-second snapshot."""
        if self.tak_pending is not None:
            if now - self.tak_requested >= TAK_READ_TIMEOUT_S:
                raise OSError('TAK set read timed out')
            return
        if self.rewrite_acks:
            return  # Do not overlap our ruleset generation change with a dump.
        if self.tak_requested is not None and now - self.tak_requested < TAK_READ_INTERVAL_S:
            return
        self.rewrite_sequence += 1
        self.tak_pending = self.rewrite_sequence
        self.tak_requested = now
        self.tak_partial, self.tak_bytes = {}, 0
        body = struct.pack('!BBH', socket.AF_BRIDGE, 0, 0)
        body += netlink_attr(1, TABLE.encode() + b'\0') + netlink_attr(2, TAK_SET.encode() + b'\0')
        message = struct.pack('=IHHII', 16 + len(body), 0xa0d, 0x301, self.tak_pending, 0) + body
        if self.sockets[1].sendto(message, (0, 0)) != len(message):
            raise OSError('short TAK set request')

    def tak_members(self, now):
        """Return a recent complete kernel snapshot, never partial/unknown empty."""
        if self.tak_observed is None or not 0 <= now - self.tak_observed < TAK_READ_MAX_AGE_S:
            return None
        return {mac: end for mac, end in self.tak_snapshot.items() if now < end}

    def tak_reply(self, kind, flags, body):
        """Accumulate a bounded multipart GETSETELEM reply without dirtying proof."""
        self.tak_bytes += len(body)
        if flags & 0x10 or self.tak_bytes > 128 * 1024:
            raise OSError('interrupted or oversized TAK set dump')
        if kind == 3:  # NLMSG_DONE, including a signed dump completion status.
            if len(body) != 4 or struct.unpack('=i', body)[0] != 0:
                raise OSError('TAK set dump completion failed')
            self.tak_snapshot = self.tak_partial
            self.tak_observed = self.tak_requested
            self.tak_pending = None
            self.tak_partial = {}
            return
        if kind != 0xa0c or not flags & 2 or len(body) < 4 or body[:2] != b'\x07\0':
            raise OSError('invalid TAK set reply')
        fields = netlink_fields(body[4:])
        if set(fields) != {1, 2, 3} or fields[1] != TABLE.encode() + b'\0' or fields[2] != TAK_SET.encode() + b'\0':
            raise OSError('wrong TAK set reply identity')
        for entry, value in netlink_attrs(fields[3]):
            element = netlink_fields(value)
            if entry != 1 or set(element) - {1, 4, 5} or 1 not in element or 5 not in element:
                raise OSError('invalid TAK set element')
            key = netlink_fields(element[1])
            if set(key) != {1} or len(key[1]) != 6 or len(element[5]) != 8:
                raise OSError('invalid TAK MAC or expiration')
            if 4 in element and (len(element[4]) != 8 or struct.unpack('!Q', element[4])[0] != TAK_SERVER_HOLD_S * 1000):
                raise OSError('invalid TAK timeout')
            expires = struct.unpack('!Q', element[5])[0] / 1000
            mac = key[1].hex(':')
            if expires > TAK_SERVER_HOLD_S or mac in self.tak_partial or len(self.tak_partial) >= MAX_TAK_PHONES:
                raise OSError('invalid TAK expiry or capacity')
            # Receipt-based expiry errs toward longer exclusion during a dump.
            self.tak_partial[mac] = self.monotonic() + expires

    def rewrite_members(self, members):
        """Atomically flush/rearm three-second elements using our subscription.

        Own multicast notifications can arrive despite omitting NLM_F_ECHO.
        Remember their originating port and transaction sequences separately
        from ACKs, which may be drained first. External changes still invalidate
        proof, and ACK errors/loss still fail closed.
        """
        if self.rewrite_acks:
            raise OSError('rewrite transaction acknowledgement missing')
        if len(members) > MAX_PHONE_LIMIT:
            raise ValueError('too many rewrite peers')
        if self.nft_portid is None:
            self.nft_portid = self.sockets[1].getsockname()[0]
        sent = {}
        def attr(kind, data):
            """Pack a native netlink attribute with four-byte alignment."""
            return struct.pack('=HH', len(data) + 4, kind) + data + b'\0' * (-len(data) % 4)
        def message(kind, body=b'', flags=1, family=socket.AF_BRIDGE):
            """Number one batch message, recording only requested ACKs."""
            self.rewrite_sequence += 1
            seq = self.rewrite_sequence
            sent[seq] = kind
            if flags & 4:
                self.rewrite_acks.add(seq)
            body = struct.pack('!BBH', family, 0, 10 if kind in (16, 17) else 0) + body
            return struct.pack('=IHHII', 16 + len(body), kind, flags, seq, 0) + body
        names = attr(1, TABLE.encode() + b'\0') + attr(2, REWRITE_SET.encode() + b'\0')
        batch = message(16, family=0)  # NFNL_MSG_BATCH_BEGIN / NFTABLES subsystem.
        batch += message(10 << 8 | 14, names, 1 | 4)  # DELSETELEM without list flushes.
        if members:
            elements = b''
            for iface, address, mac in sorted(members):
                if (not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', iface) or iface in ('br0', 'bat0', 'lo')
                        or not MAC.fullmatch(mac)):
                    raise ValueError('invalid rewrite identity')
                key = iface.encode().ljust(16, b'\0') + bytes.fromhex(mac.replace(':', '')) + b'\0\0' + socket.inet_aton(address)
                element = attr(1 | 0x8000, attr(1, key)) + attr(4, struct.pack('!Q', REWRITE_TIMEOUT_S * 1000))
                elements += attr(1 | 0x8000, element)
            batch += message(10 << 8 | 12, names + attr(3 | 0x8000, elements), 1 | 4 | 0x400)
        batch += message(17, family=0)  # NFNL_MSG_BATCH_END, no echo.
        if self.sockets[1].sendto(batch, (0, 0)) != len(batch):
            raise OSError('short rewrite netlink transaction')
        # Bounded history handles notifications drained after ACKs or a later
        # dump, without including any GETSETELEM sequences in this allowance.
        self.rewrite_notifications.update(sent)
        self.rewrite_notifications = dict(list(self.rewrite_notifications.items())[-32:])

    def own_rewrite_notification(self, kind, flags, seq, portid, group, body):
        """Recognize only kernel multicast from our bounded rewrite operations."""
        if (group != 1 << 6 or portid != self.nft_portid or not self.rewrite_notifications
                or flags & 2 or len(body) < 4):
            return False
        if kind == 0xa0f:  # NEWGEN uses the first operation's sequence on Linux 6.18.
            if seq not in self.rewrite_notifications or body[:2] != b'\0\0':
                return False
        elif kind in (0xa0c, 0xa0e):
            # Linux 6.18 sends per-element multicast with sequence zero.
            if (seq and self.rewrite_notifications.get(seq) != kind) or body[:2] != b'\x07\0':
                return False
        else:
            return False
        try:
            fields = netlink_fields(body[4:])
        except OSError:
            return False
        if kind == 0xa0f:
            return len(fields.get(1, b'')) == 4  # NFTA_GEN_ID
        return (set(fields) == {1, 2, 3} and fields[1] == TABLE.encode() + b'\0'
                and fields[2] == REWRITE_SET.encode() + b'\0')

    def relevant(self, kind, body):
        """Ignore unrelated events and refreshes that preserve verified proof.

        Every learned peer in the snapshot is protected, including phones and
        peers already admitted by transport. IP/MAC and MAC/port associations
        must match, not just membership in identity sets. Unknown/malformed
        events stay conservative. Notifications never grant new admission.
        """
        if self.scope is None:
            return True
        indices, bridge, peers, learning, names = self.scope
        try:
            if kind in (16, 17):  # ifinfomsg
                if len(body) < 16:
                    return True
                index = struct.unpack_from('=i', body, 4)[0]
                offset = 16
            elif kind in (20, 21):  # ifaddrmsg
                if len(body) < 8:
                    return True
                return struct.unpack_from('=I', body, 4)[0] == bridge
            elif kind in (28, 29):  # ndmsg: IPv4 ARP or bridge FDB
                if len(body) < 12 or body[0] not in (socket.AF_INET, socket.AF_INET6, socket.AF_BRIDGE):
                    return True
                family, pad1, pad2, index, state, flags, ndm_type = struct.unpack_from('=BBHiHBB', body)
                if pad1 or pad2 or index <= 0:
                    return True
                offset = 12
            else:
                return True
            attrs = {}
            while offset < len(body):
                if len(body) - offset < 4:
                    return True
                size, attr = struct.unpack_from('=HH', body, offset)
                if size < 4 or offset + ((size + 3) & ~3) > len(body) or attr in attrs:
                    return True
                attrs[attr] = body[offset + 4:offset + size]
                offset += (size + 3) & ~3
            if kind in (16, 17):
                name = attrs.get(3, b'').rstrip(b'\0').decode('ascii')  # IFLA_IFNAME
                return index in indices or name in names
            # NDA_DST / NDA_LLADDR. Missing identity cannot prove irrelevance.
            destination, mac = attrs.get(1), attrs.get(2)
            if destination is None and mac is None:
                return True
            if mac is not None and len(mac) != 6:
                return True
            if body[0] == socket.AF_INET and destination is not None and len(destination) != 4:
                return True
            if body[0] == socket.AF_INET6 and destination is not None and len(destination) != 16:
                return True
            # Known ancillary attributes do not change the identity. Unknown
            # attributes, even well framed, cannot qualify for refresh elision.
            sizes = {1: 4 if family == socket.AF_INET else 16, 2: 6,
                     3: 16, 4: 4, 9: 4, 12: 1, 15: 4}
            if family == socket.AF_BRIDGE:
                sizes = {2: 6, 3: 16, 9: 4, 15: 4}
            if any(attr not in sizes or len(value) != sizes[attr] for attr, value in attrs.items()):
                return True
            if destination not in peers and mac not in learning:
                return False
            # RTM_DELNEIGH, unsupported states/flags, and extended flags always
            # invalidate protected identities. A batch cannot cancel a delete
            # by following it with a healthy refresh.
            if kind != 28 or (15 in attrs and struct.unpack('=I', attrs[15])[0] != 0):
                return True
            if 9 in attrs and struct.unpack('=I', attrs[9])[0] != bridge:
                return True
            if family == socket.AF_BRIDGE:
                expected = learning.get(mac)
                return not (expected == (index, flags) and 9 in attrs
                            and state in (2, 4) and ndm_type == 0)
            # Neighbour proof is IPv4-only. A healthy IPv6 refresh of the same
            # protected MAC on br0 also cannot change it. FDB events still
            # invalidate a MAC moving to any different physical port.
            same = peers.get(destination) == mac if family == socket.AF_INET else mac in learning
            return not (same and destination is not None and mac is not None
                        and index == bridge and state in (2, 4, 8, 16)
                        and flags in (0, 0x80) and ndm_type == 1)
        except (ValueError, UnicodeError, struct.error):
            return True

    def changed(self):
        """Drain bounded netlink queues and report proof-invalidating events."""
        changed = False
        for channel, sock in enumerate(self.sockets):
            # Fail closed if notification churn exhausts the queue budget.
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
                    length, kind, msg_flags, seq, portid = struct.unpack_from('=IHHII', data, offset)
                    if length < 16 or offset + length > len(data) or kind == 4:
                        raise OSError('lost or invalid network notification')
                    if channel == 1 and sender[1] == 0 and self.tak_pending is not None and seq == self.tak_pending:
                        self.tak_reply(kind, msg_flags, data[offset + 16:offset + length])
                        offset += (length + 3) & ~3
                        continue
                    if kind == 2:
                        if (channel != 1 or seq not in self.rewrite_acks or length < 36
                                or struct.unpack_from('=i', data, offset + 16)[0] != 0):
                            raise OSError('rewrite transaction failed or unexpected netlink error')
                        self.rewrite_acks.remove(seq)
                        offset += (length + 3) & ~3
                        continue
                    if channel == 1 and self.own_rewrite_notification(
                            kind, msg_flags, seq, portid, sender[1], data[offset + 16:offset + length]):
                        offset += (length + 3) & ~3
                        continue
                    # External/unknown policy notifications always invalidate.
                    # Route elision requires a previously verified scope.
                    changed |= kind not in (1, 3) and (
                        channel == 1 or self.relevant(kind, data[offset + 16:offset + length]))
                    # Each netlink message occupies a four-byte aligned slot.
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
    def __init__(self, config, runner=subprocess.run, sysnet='/sys/class/net', run='/run',
                 ap_role=AP_ROLE, hostapd_conf=HOSTAPD_CONF, proc='/proc'):
        """Start with no verified path and injectable snapshot commands."""
        self.config, self.runner = config, runner
        self.sysnet, self.run = Path(sysnet), Path(run)
        self.ap_role, self.hostapd_conf, self.proc = Path(ap_role), Path(hostapd_conf), Path(proc)
        self.reason = 'not checked'
        self.events = None
        self.cached = None
        self.dirty = True
        self.signature = None
        self.local_ports = {}
        self.paths_dirty = True

    def start(self):
        """Subscribe before taking any network snapshot."""
        self.events = NetworkEvents()

    def close(self):
        """Close active network notification subscriptions."""
        if self.events is not None:
            self.events.close()

    def readers(self):
        """Return notification sockets for the shared select loop."""
        return self.events.sockets if self.events is not None else []

    def invalidate(self):
        """Discard cached proof and require another inspection."""
        self.cached, self.dirty = None, True
        self.paths_dirty = True
        self.reason = 'path unverified; waiting for phone traffic'

    def listen_address(self):
        """Find a br0 listener address without treating it as admission proof."""
        if self.config.bind_ip:
            return self.config.bind_ip
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                # SIOCGIFADDR returns the primary IPv4 address; the full dump
                # still verifies its scope and br0 ownership before admission.
                result = fcntl.ioctl(probe.fileno(), 0x8915, struct.pack('256s', b'br0'))
            return socket.inet_ntoa(result[20:24])
        except OSError:
            return None

    def hostapd_identity(self):
        """Identify the live hostapd unit process without a polling subprocess.

        Debian's hostapd PID file plus proc identity/start time handles stop,
        restart and stale/reused PIDs. Link notifications verify port readiness.
        """
        try:
            pid = (self.run / 'hostapd.pid').read_text().strip()
            if not pid.isdecimal():
                return None
            base = self.proc / pid
            stat = (base / 'stat').read_text()
            name, rest = stat.split(' (', 1)[1].rsplit(') ', 1)
            parts = rest.split()
            if name != 'hostapd' or parts[0] in {'Z', 'X'}:
                return None
            if not any(line.rstrip().endswith('/hostapd.service') for line in (base / 'cgroup').read_text().splitlines()):
                return None
            return pid, parts[19]  # field 22: process start time, not CPU usage
        except (OSError, ValueError, IndexError):
            return None

    def file_signature(self):
        """Track role/config changes and hostapd lifetime without child processes."""
        return (tuple(file_stamp(self.run / name) for name in
                      ('ethernet_detection_state', 'eth-carrier-generation')),
                file_stamp(self.ap_role), file_stamp(self.hostapd_conf), self.hostapd_identity())

    def forwarding(self, iface):
        """Require br0 membership, carrier and bridge forwarding independently."""
        try:
            return ((self.sysnet / iface / 'master').resolve().name == 'br0'
                    and (self.sysnet / iface / 'carrier').read_text().strip() == '1'
                    and (self.sysnet / 'br0/brif' / iface / 'state').read_text().strip() == '3')
        except OSError:
            return False

    def current(self, force=False, refresh=True):
        """Consume invalidations, deferring readback while no phone needs it."""
        if self.events is None:
            raise OSError('network subscriptions unavailable')
        try:
            signature = self.file_signature()
            if self.events.changed() or signature != self.signature or force:
                self.invalidate()
            self.signature = signature
            if self.paths_dirty:
                self.local_ports, _ = self.local_paths()
                self.paths_dirty = False
            if self.dirty and (refresh or force):
                proof = self.inspect()
                if proof is not None:
                    self.events.scope = self.event_scope(proof)
                # A snapshot spanning a change cannot admit peers. Retry next
                # cycle.
                if self.events.changed() or self.file_signature() != signature:
                    self.invalidate()
                    self.reason = 'network changed during verification'
                else:
                    # Retry failed readback next cycle without waiting for a
                    # kernel event.
                    self.cached, self.dirty = proof, self.retry
            return self.cached
        except OSError as exc:
            self.invalidate()
            # Bypass per-packet OSError handling: close every socket in main's
            # finally and let systemd establish fresh subscriptions on restart.
            raise GuardFailure(str(exc)) from exc

    def command(self, *args):
        """Run a network probe with a one-second timeout and parse its JSON."""
        result = self.runner(args, check=True, capture_output=True, text=True, timeout=1)
        # Refuse oversized readback before allocating the parsed JSON tree.
        if len(result.stdout) > 2 * 1024 * 1024:
            raise ValueError('network snapshot too large')
        return json.loads(result.stdout)

    def event_scope(self, proof):
        """Cache ifindices and peer identities only when establishing proof."""
        names = {'br0', 'end0', 'bat0'} | set(proof.local_ports)
        ap = atak_ap_interface(self.ap_role)
        if ap:
            names.add(ap)
        indices = {}
        for name in names:
            try:
                indices[name] = int((self.sysnet / name / 'ifindex').read_text().strip())
            except (OSError, ValueError):
                if name == 'br0' or name in proof.local_ports:
                    return None  # Cannot classify interfaces safely.
        peers = {socket.inet_aton(ip): bytes.fromhex(mac.replace(':', ''))
                 for ip, mac in proof.peers.items()}
        learning = {peers[socket.inet_aton(ip)]: (indices.get(proof.interfaces.get(ip)), proof.fdb_flags.get(mac))
                    for ip, mac in proof.peers.items()}
        return frozenset(indices.values()), indices['br0'], peers, learning, frozenset(names)

    def inspect(self):
        """Verify each local EUD port and admit exclusively learned IPv4 peers."""
        self.retry = False
        try:
            if not self.config.enabled:
                raise ValueError('atak is disabled in mesh.conf')
            paths, generation = self.local_paths()
            local_ports = {iface: identity[0] for iface, identity in paths.items()}
            ap = atak_ap_interface(self.ap_role)
            mesh_path = ()
            if self.forwarding('bat0'):
                mesh_path = tuple((self.sysnet / name / 'ifindex').read_text().strip()
                                  for name in ('br0', 'bat0'))
            if not local_ports and not mesh_path:
                raise ValueError('no forwarding local EUD or mesh port')
            if not valid_firewall(self.command('nft', '-j', 'list', 'ruleset'), atak_eud_ports(ap)):
                raise ValueError('ATAK firewall missing or changed; restart service after AP role changes')
            links = self.command('ip', '-j', '-4', 'address', 'show', 'dev', 'br0')
            addresses = [a for link in links for a in link.get('addr_info', [])
                         if a.get('family') == 'inet' and a.get('scope') == 'global']
            primary = [ipv4(a['local']) for a in addresses if not a.get('secondary', False)]
            addresses = tuple(ipv4(a['local']) for a in addresses)
            local = self.config.bind_ip or (primary[0] if primary else None)
            if local not in primary:
                raise ValueError('ATAK br0 IPv4 not ready')
            fdb = self.command('bridge', '-j', 'fdb', 'show', 'br', 'br0')
            ports, learning = {}, {}
            for entry in fdb:
                mac = entry.get('mac', '').lower()
                # Static and self entries do not prove a learned ingress port.
                if entry.get('state') in ('permanent', 'static') or 'self' in entry.get('flags', []):
                    continue
                ports.setdefault(mac, set()).add(entry.get('ifname', entry.get('dev')))
                learning.setdefault(mac, []).append(fdb_refresh_flags(entry))
            peers, peer_ports, interfaces = {}, {}, {}
            for entry in self.command('ip', '-j', '-4', 'neigh', 'show', 'dev', 'br0'):
                mac = entry.get('lladdr', '').lower()
                states = entry.get('state', [])
                if isinstance(states, str):
                    states = [states]
                # Admit resolved neighbors learned on exactly one local EUD port.
                # A MAC also learned on a mesh port fails this test.
                if (MAC.fullmatch(mac) and len(ports.get(mac, ())) == 1
                        and next(iter(ports[mac])) in local_ports
                        and set(states) & {'REACHABLE', 'STALE', 'DELAY', 'PROBE'}):
                    address = ipv4(entry['dst'])
                    if address not in addresses:
                        peers[address] = mac
                        interfaces[address] = next(iter(ports[mac]))
                        peer_ports[address] = local_ports[interfaces[address]]
            self.reason = 'local EUD firewall and paths verified: ' + ', '.join(local_ports) if local_ports else 'mesh beacon path verified'
            flags = {mac: learning[mac][0] for mac in set(peers.values())
                     if len(learning[mac]) == 1 and learning[mac][0] is not None}
            return Proof(local, peers, generation, peer_ports, interfaces, paths, addresses, mesh_path, flags)
        except (OSError, subprocess.SubprocessError) as exc:
            self.retry = True
            self.reason = str(exc)
            return None
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            self.reason = str(exc)
            return None

    def local_paths(self):
        """Read only local role/link facts when existing invalidators fire."""
        paths, generation = {}, ''
        if not self.config.enabled:
            return paths, generation
        try:
            role = read_kv(self.run / 'ethernet_detection_state')
            token = (self.run / 'eth-carrier-generation').read_text().strip()
            if (role.get('ETH_MODE') == 'WIRED_EUD' and role.get('ETH_BRIDGE') == 'br0'
                    and self.forwarding('end0') and re.fullmatch(r'end0 wired-eud \S+', token)):
                paths['end0'] = ('wired', token)
                generation = token
        except OSError:
            pass
        ap = atak_ap_interface(self.ap_role)
        hostapd = self.hostapd_identity()
        if ap and hostapd and self.forwarding(ap):
            try:
                conf = read_kv(self.hostapd_conf)
                if conf.get('interface') == ap and conf.get('bridge') == 'br0':
                    token = ' ap ' + ap + ' ' + ':'.join(hostapd)
                    paths[ap] = ('wifi', token)
                    generation += token
            except OSError:
                pass
        for iface, identity in paths.items():
            try:
                index = (self.sysnet / iface / 'ifindex').read_text().strip()
            except OSError:
                index = ''  # The bind still requires a live named interface.
            paths[iface] = identity + (index,)
        return paths, generation


# JSON tags are a closed allowlist, never pickle, eval or arbitrary imports.
CLASSES = {c.__name__: c for c in (cot.Fix, cot.ErrorEstimate, phone.PhoneFix,
                                  phone.OutboundEvent, phone.PhoneClockDiagnostic, phone._Warning)}


def encode(value):
    """Tag dates, dataclasses and compound keys for JSON persistence."""
    if isinstance(value, datetime):
        return {'date': value.isoformat()}
    if is_dataclass(value):
        return {'class': type(value).__name__, 'fields': {f.name: encode(getattr(value, f.name)) for f in fields(value)}}
    if isinstance(value, tuple):
        return {'tuple': [encode(v) for v in value]}
    if isinstance(value, dict):
        # Pair arrays retain tuple keys used by selector event queues.
        return {'dict': [[encode(k), encode(v)] for k, v in value.items()]}
    if isinstance(value, list):
        return [encode(v) for v in value]
    return value


def decode(value, depth=0):
    """Restore bounded tagged state using the fixed class allowlist."""
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
            # Require an exact layout before restoring private selector state.
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
    """Reject incompatible selector state, identity and bounded queues."""
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
    if not 1 <= position.sa_history_limit <= phone.PHONE_SA_HISTORY:
        raise ValueError('invalid SA history bound')
    if len(position._seen_sa_times) > position.sa_history_limit or len(position._retirements) > MAX_RETIRE:
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
    """Load and durably replace the service checkpoint."""
    def __init__(self, path):
        """Record the checkpoint path."""
        self.path = Path(path)

    def load(self):
        """Read saved JSON, treating an absent checkpoint as initial state."""
        try:
            saved = read_json(self.path)
        except FileNotFoundError:
            return None
        if not isinstance(saved, dict):
            raise ValueError('invalid state object; refusing implicit reassociation')
        return saved

    def save(self, data):
        """Write and sync a replacement checkpoint."""
        atomic_json(self.path, data, durable=True)


def rebase_selector(position, saved, now, new_boot):
    """Preserve age lower bounds and reset boot-dependent phone timing."""
    if not new_boot:
        if now.mono < saved:
            raise ValueError('monotonic clock regressed in same boot')
    else:
        # An untrusted or stepped wall clock cannot establish downtime.
        # Checkpoint age + current uptime is a lower bound.
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
                warning.deadline = now.mono  # One reminder, no catch-up burst.
        position._phone_ref = position._phone_mono_ref = position._latest_time = None
        position._phone_generation += 1
        position.clock_diagnostic = None
        position._presence = None
        position._clock = None
    if position._last_sent_mono is not None:
        position._feed_until = max(position._feed_until or 0, now.mono + position.feed_timeout_s) if not new_boot else now.mono + position.feed_timeout_s
        position._last_sent_mono = now.mono  # guard buffered echoes after restart


class Inputs:
    """Read the local receiver and optional versioned producer files.

    Monitor fields: schema=1, boot_id, written_boot, producer_id, fault_epoch,
    state (RadioGPS vocabulary), ranges_agree. Jam uses that envelope plus
    asserted (bool). Initially missing means unwired/unknown. Loss after valid
    producer data fails closed. GPIO and GPS time permissions stay explicit.
    """
    def __init__(self, boot_id, run='/run'):
        """Initialize producer-file caching for the current boot."""
        self.boot_id, self.run = boot_id, Path(run)
        self.cached = self.signature = None
        self.expiries = []
        self.revision = 0

    def cached_read(self, now, memory, *, receiver=True):
        """Reuse inputs until metadata changes or freshness expires."""
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
        """Require the expected schema, boot and five-second write freshness."""
        data = read_json(self.run / name, 65536)
        if data.get('schema') != schema or data.get('boot_id') != self.boot_id:
            raise ValueError('stale or wrong-boot ' + name)
        written = number(data.get('written_boot'))
        if written > now.boot:
            # Retry future-dated input when its claimed write time arrives.
            self.expiries.append(('boot', written))
        if not 0 <= now.boot - written < INPUT_TTL:
            raise ValueError('stale or wrong-boot ' + name)
        self.expiries.append(('boot', written + INPUT_TTL))
        return data

    def read(self, now, memory, *, receiver=True):
        """Validate local inputs and retain producer fault history."""
        self.expiries = []
        fix, observed, gps_reason = None, now.mono, 'GPS unavailable'
        receiver_present = None
        try:
            if not receiver:
                raise ValueError('waiting for phone')
            data = self.document('gps_status.json', now, 2)
            devices = data.get('devices')
            if isinstance(devices, list):
                receiver_present = bool(devices)
                if not receiver_present:
                    raise ValueError('no GPS receiver')
            sample = data['sample']
            if sample is None:
                if not data.get('devices'):
                    receiver_present = False
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
            # Transfer raw-clock sample age into selector monotonic time.
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
            # Producer changes and later epochs advance a durable local counter.
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
        # Unknown GPIO blocks trust after producer activation, including loss.
        jam_gate = jam is True or (jam is None and memory.get('jam_seen', False))
        evidence = [state in ('GNSS_SUSPECTED', 'INCONSISTENT_UNATTRIBUTED'), jam_gate, monitor]
        if evidence != memory.get('evidence') and (evidence[0] or evidence[1]):
            memory['fault_epoch'] += 1
        memory['evidence'] = evidence
        return phone.RadioGPS(fix, observed, state, jam_gate, memory['fault_epoch'], agrees, receiver_present), {
            'gps': gps_reason, 'monitor': monitor, 'jamming_pin': jam, 'jamming_input': jam_reason}


class RadioBeacon:
    """Schedule small mesh announcements and retire only this radio's marker."""
    def __init__(self):
        """Start without a location, retaining one startup handoff cleanup."""
        self.last_sent = self.last_fix = self.last_name = self.next_due = None
        self.last_attempt = None
        self.active = False
        self.startup_cleanup = True
        self.retirement = None

    def due(self, now):
        """Use the existing application cycle for refresh/retry deadlines."""
        return self.next_due is not None and now.mono >= self.next_due

    def update(self, config, now, fix, occupied, send, *, expires=None):
        """Refresh at 30 seconds or five metres, with a five-second send bound."""
        if not config.enabled:
            return
        name = config.beacon_name or config.callsign
        usable = (fix is not None and (fix.lat, fix.lon) != (0, 0)
                  and (expires is None or expires - now.mono >= .001))
        if occupied or not usable:
            if self.retirement is None and (self.active or occupied and self.startup_cleanup):
                # Force-delete is immediate in ATAK's external CoT importer.
                # Staleness alone waits for the receiving app's purge settings.
                self.retirement = (str(uuid.uuid4()), 0, 0)
                self.active = self.startup_cleanup = False
                self.next_due = now.mono
            if self.retirement is not None and self.due(now):
                task, attempts, successes = self.retirement
                packet = cot.retire_marker(config.uid + '-beacon', task, now.utc)
                attempts += 1
                successes += bool(send(packet))
                self.retirement = (task, attempts, successes) if attempts < 5 and successes < 3 else None
                self.next_due = now.mono + 2 if self.retirement else None
            elif self.retirement is None:
                self.next_due = None
            return
        self.retirement = None  # A new usable announcement replaces old cleanup.
        moved = self.last_fix is None or phone._distance_m(
            (fix.lat, fix.lon), (self.last_fix.lat, self.last_fix.lon)) >= 5
        changed = (not self.active or moved or name != self.last_name or self.last_fix is not None and
                   (fix.source != self.last_fix.source or
                    fix.hae is not None and self.last_fix.hae is not None and abs(fix.hae - self.last_fix.hae) >= 5))
        elapsed = float('inf') if self.last_sent is None else now.mono - self.last_sent
        if elapsed >= 30 or changed:
            if self.last_attempt is not None and now.mono < self.last_attempt + 5:
                self.next_due = self.last_attempt + 5
                return
            packet = cot.radio_beacon(config.uid, name, fix, now.utc,
                                       stale_s=cot.BEACON_STALE_S if expires is None else min(cot.BEACON_STALE_S, expires - now.mono))
            self.last_attempt = now.mono
            self.next_due = now.mono + 5
            if packet is not None and send(packet):
                self.active, self.startup_cleanup = True, False
                self.last_sent, self.last_fix, self.last_name = now.mono, fix, name
                self.next_due = now.mono + 30
        else:
            self.next_due = self.last_sent + 30


class Service:
    """Serialize application work.

    Transport.send reports successful local UDP handoff.
    """
    def __init__(self, config, boot_id, store, transport, now):
        """Restore or initialize phone state and checkpoint before use."""
        self.config, self.boot_id, self.store, self.transport = config, boot_id, store, transport
        self.revision = 0
        self.position = phone.PhonePosition(radio_uid=config.uid)
        self.peer = None  # controller path; guest paths live in phones
        self.phones = {}
        self.manual_uid = None
        self.noticed_present = ()
        self.noticed_controller = None
        self.delivery = {}  # transient per-guest contact/feed cadence
        self.choice_request = None
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
        self.unavailable_since = None
        self.logged_warnings = set()
        self.beacon = RadioBeacon()
        self.phone_gps = {}  # Ephemeral admitted observations, never trust/clock evidence.
        self.manual_anchor = None  # Persisted point identity, controller and GPS anchor.
        self.manual_motion = None  # Only distinct live observations establish movement.
        self.held_marker = None  # One accepted Send, retained exactly for quiet refreshes.
        self.held_notice = None  # Acceptance only, never replayed after restart/re-anchor.
        self.rewrite_fix = None
        self.rewrite_deadline = 0
        self.tak_servers = {}  # Cached kernel MAC membership and expiration, not producer evidence.
        self.disagreements = {}  # Phone UID -> evidence episode, never trust state.
        saved = store.load()
        if saved is not None:
            self.restore(saved, now)
        for selector in [self.position] + [item['clock'] for item in self.phones.values()]:
            selector._clear_warning('phone_gps')
        self.banners.pop('phone_gps', None)
        self.audio.pop('phone_gps', None)
        for item in self.phones.values():
            item['outbox'].pop('phone_gps', None)
            item['warned'].pop('phone_gps', None)
        self.commit(now)

    def restore(self, saved, now):
        """Validate persisted service state and rebase timing after reboot."""
        payload = {key: value for key, value in saved.items() if key != 'sha256'}
        if saved.get('sha256') != state_digest(payload):
            raise ValueError('state checksum mismatch; refusing implicit reassociation')
        if saved.get('schema') == 1 and saved.get('uid') == self.config.uid:
            log('discarding obsolete single-pin ATAK state (schema 1)')
            return
        if saved.get('schema') != 2 or saved.get('uid') != self.config.uid:
            raise ValueError('state schema/identity mismatch; refusing implicit reassociation')
        state = decode(saved['state'])
        if isinstance(state, dict):
            state.setdefault('manual_anchor', None)  # Preserve already-held points on upgrade.
            state.setdefault('held_marker', None)  # Earlier checkpoints did not retain Send XML.
        required = {'selector', 'peer', 'session', 'retire', 'messages', 'audio', 'banners', 'chats', 'input_memory', 'age_lower_bound',
                    'phones', 'manual_uid', 'noticed_present', 'noticed_controller', 'choice_request', 'manual_anchor',
                    'held_marker'}
        if not isinstance(state, dict) or set(state) != required:
            raise ValueError('invalid service state')
        self.position.__dict__ = state.pop('selector')
        validate_selector(self.position, self.config)
        for key, value in state.items():
            setattr(self, key, value)
        if self.manual_anchor is not None:
            anchor = self.manual_anchor
            if (not isinstance(anchor, dict) or set(anchor) != {'id', 'uid', 'point'}
                    or not re.fullmatch('[0-9a-f]{64}', str(anchor['id']))):
                raise ValueError('invalid manual anchor')
            bounded_text(anchor['uid'])
            point = anchor['point']
            if point is not None and (not isinstance(point, tuple) or len(point) != 2
                    or not -90 <= number(point[0]) <= 90 or not -180 <= number(point[1]) <= 180):
                raise ValueError('invalid manual anchor point')
        if self.held_marker is not None:
            record = self.held_marker
            if (not isinstance(record, dict) or set(record) != {'id', 'xml', 'due'}
                    or not isinstance(record['xml'], list) or not 1 <= len(record['xml']) <= MAX_XML // 2048
                    or any(not isinstance(chunk, str) or not re.fullmatch('[0-9a-f]{2,4096}', chunk)
                           or len(chunk) % 2 for chunk in record['xml'])):
                raise ValueError('invalid held marker record')
            packet = bytes.fromhex(''.join(record['xml']))
            mark, parsed = self.position._manual, cot.parse_marker(packet)
            if (mark is None or isinstance(parsed, str) or record['id'] != self.manual_id()
                    or parsed.uid != mark.marker_uid or parsed.creator_uid != mark.phone_uid
                    or (parsed.lat, parsed.lon, parsed.hae) != (mark.fix.lat, mark.fix.lon, mark.fix.hae)):
                raise ValueError('held marker does not match selected gesture')
            number(record['due'])
        bounded_text(self.session)
        if not isinstance(self.phones, dict) or len(self.phones) > MAX_PHONE_LIMIT:
            raise ValueError('invalid saved phone table')
        for uid, item in self.phones.items():
            bounded_text(uid)
            if set(item) != {'ip', 'mac', 'port', 'callsign', 'clock', 'outbox', 'warned', 'guest_notified'}:
                raise ValueError('invalid saved phone fields')
            ipv4(item['ip'])
            if not MAC.fullmatch(item['mac']) or item['port'] not in {'wired', 'wifi'}:
                raise ValueError('invalid saved phone path')
            bounded_text(item['callsign'], 128)
            clock = phone.PhonePosition(radio_uid=self.config.uid)
            clock.__dict__ = item['clock']
            validate_selector(clock, self.config)
            if clock.pinned_uid != uid:
                raise ValueError('phone table identity mismatch')
            item['clock'] = clock
            if not isinstance(item['guest_notified'], bool) or set(item['warned']) - CODES:
                raise ValueError('invalid saved phone notice state')
            if not isinstance(item['outbox'], dict) or set(item['outbox']) - (CODES | {'group', 'guest'}):
                raise ValueError('invalid phone outbox')
            for job in item['outbox'].values():
                if set(job) != {'id', 'text', 'attempts', 'due'}:
                    raise ValueError('invalid phone notice')
                bounded_text(job['id'])
                bounded_text(job['text'], 2048)
                number(job['due'])
                if type(job['attempts']) is not int or not 0 <= job['attempts'] <= 5:
                    raise ValueError('invalid phone notice attempts')
        for uid in (self.manual_uid, self.noticed_controller):
            if uid is not None:
                bounded_text(uid)
        if not isinstance(self.noticed_present, tuple) or len(self.noticed_present) > MAX_PHONE_LIMIT:
            raise ValueError('invalid notice membership')
        for uid in self.noticed_present:
            bounded_text(uid)
        if self.choice_request is not None:
            bounded_text(self.choice_request)
        controller = self.phones.get(self.position.pinned_uid)
        if self.peer != (None if controller is None else {k: controller[k] for k in ('ip', 'mac')}):
            raise ValueError('controller path persistence inconsistent')
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
        for item in self.phones.values():
            rebase_selector(item['clock'], number(saved['saved_mono']), now, new_boot)
            if new_boot:
                for job in item['outbox'].values():
                    job['due'] = now.mono
        if new_boot:
            self.age_lower_bound = True
            if self.held_marker is not None:
                self.held_marker['due'] = now.mono + HELD_MARKER_REFRESH_S
            for item in self.retire.values():
                item['due'] = now.mono
                if item['done_until'] is not None:
                    # Keep completed retirement UIDs blocked for a fresh 120 s.
                    item['done_until'] = now.mono + 120
            for item in self.messages.values():
                item['due'] = now.mono
                if item['sent_mono'] is not None:
                    item['sent_mono'] += now.mono - number(saved['saved_mono']) - now.boot

    @staticmethod
    def validate_job(item, retirement=False):
        """Validate the persisted retry schedule and delivery progress."""
        keys = {'id', 'attempts', 'due'} | ({'successes', 'done_until', 'phone_uid'} if retirement else {'sent_mono'})
        if not isinstance(item, dict) or set(item) != keys:
            raise ValueError('invalid persisted transport job')
        bounded_text(item['id'])
        if retirement:
            bounded_text(item['phone_uid'])
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
        """Checkpoint policy and queues, failing fatally on write loss."""
        state = {'selector': vars(self.position), 'peer': self.peer, 'session': self.session,
                 'retire': self.retire, 'messages': self.messages, 'audio': self.audio,
                 'banners': self.banners, 'chats': self.chats, 'input_memory': self.input_memory,
                 'age_lower_bound': self.age_lower_bound,
                 'phones': {uid: dict(item, clock=vars(item['clock'])) for uid, item in self.phones.items()},
                 'manual_uid': self.manual_uid, 'noticed_present': self.noticed_present,
                 'noticed_controller': self.noticed_controller, 'choice_request': self.choice_request,
                 'manual_anchor': self.manual_anchor, 'held_marker': self.held_marker}
        try:
            payload = {'schema': 2, 'uid': self.config.uid, 'boot_id': self.boot_id,
                       'saved_mono': now.mono, 'state': encode(state)}
            self.store.save(dict(payload, sha256=state_digest(payload)))
        except (OSError, ValueError, TypeError) as exc:
            raise PersistenceError('durable state write failed: ' + str(exc)) from exc
        self.last_checkpoint = now.mono
        self.saved_input_memory = encode(self.input_memory)
        self.revision += 1

    def reject(self, reason):
        """Record a bounded rejection reason and saturating counter."""
        self.last_rejection = str(reason)[:200]
        self.rejected = min(self.rejected + 1, 2 ** 31 - 1)
        return self.last_rejection

    def present_phones(self, now, proof):
        """Return admitted live leases whose MAC and local port are still proven."""
        if not self.config.enabled or proof is None:
            return {}
        return {uid: item for uid, item in self.phones.items()
                if item['clock']._presence is not None and now.mono < item['clock']._presence
                and item['clock'].phone_now(now.mono) is not None
                and proof.peer(item['ip']) == item['mac'] and proof.port(item['ip']) == item['port']}

    def has_live_lease(self, now):
        """Keep active readback/recovery until all admitted SA leases expire."""
        return self.config.enabled and any(
            item['clock']._presence is not None and now.mono < item['clock']._presence
            and item['clock'].phone_now(now.mono) is not None for item in self.phones.values())

    def phone_ready(self, now, proof):
        """Require at least one present local phone before any phone output."""
        return bool(self.present_phones(now, proof))

    def queue_notice(self, item, code, text, now):
        """Coalesce each kind of per-phone notice into a bounded durable slot."""
        item['outbox'][code] = {'id': str(uuid.uuid4()), 'text': text,
                                'attempts': 0, 'due': now.mono}

    def tak_server(self, mac, now):
        """Retain a detected server connection across SA absence and IP changes."""
        return now.mono < self.tak_servers.get(mac, -math.inf)

    def compare_phone_gps(self, now, proof, selected):
        """Record disagreement without feeding the location trust state machine."""
        self.disagreements = {uid: item for uid, item in self.disagreements.items() if uid in self.phones}
        present = self.present_phones(now, proof)
        for uid, item in self.phones.items():
            episode = self.disagreements.setdefault(uid, {'active': False, 'agree_since': None,
                                                         'distance': None, 'deadline': 0,
                                                         'valid_until': 0})
            observed = self.phone_gps.get(uid)
            episode['distance'] = None
            if now.mono >= episode['valid_until']:
                episode['agree_since'] = None
            if (uid not in present or observed is None or not selected.own_gnss_selected
                    or not selected.position_trusted):
                episode['agree_since'] = None
                continue
            episode['valid_until'] = min(observed[1], self.rewrite_deadline)
            distance = phone._distance_m((selected.fix.lat, selected.fix.lon),
                                         (observed[0].lat, observed[0].lon))
            if distance > GPS_DISAGREE_M:
                episode['distance'] = round(distance)
                episode['deadline'] = episode['valid_until']
                episode['agree_since'] = None
                if not episode['active']:
                    # One best-effort handoff, without audio or a retry job.
                    episode['active'] = True
                    text = f'Radio and phone GPS disagree by {round(distance)} m. Check your position and GPS setup.'
                    log(f'phone GPS disagreement uid={json.dumps(uid)} distance_m={round(distance)}')
                    stamp = item['clock'].outbound_time(now.mono, now.utc)
                    self.send(cot.geochat_report(self.config.uid, self.config.callsign, uid,
                                                text, str(uuid.uuid4()), stamp), 4242, now, item)
            elif distance <= GPS_AGREE_M:
                if episode['agree_since'] is None:
                    episode['agree_since'] = now.mono
                if now.mono - episode['agree_since'] >= GPS_AGREE_S:
                    episode['active'] = False
            else:
                episode['agree_since'] = None

    def choose_controller(self, now, proof, manual_uid=None):
        """Apply manual, sole wired, current, then stable random preference."""
        present = self.present_phones(now, proof)
        if manual_uid is not None:
            if manual_uid not in present:
                return False
            self.manual_uid = manual_uid
        current = self.position.pinned_uid
        wired = [uid for uid, item in present.items() if item['port'] == 'wired']
        if self.manual_uid in present:
            chosen = self.manual_uid
        elif len(wired) == 1:
            chosen = wired[0]
        elif current in present:
            chosen = current
        elif present:
            chosen = secrets.choice(sorted(present))
        else:
            chosen = current  # retain preference and held radio state while idle
        if chosen != current:
            self.position.follow_phone(present[chosen]['clock'], changed=True)
            self.messages.clear()  # old controller receipts cannot acknowledge new chat
            for item in self.phones.values():
                item['guest_notified'] = False
                item['outbox'].pop('guest', None)
            self.contact_changed = True
            self.retry_parked()
        item = self.phones.get(chosen)
        self.peer = None if item is None else {k: item[k] for k in ('ip', 'mac')}
        # Notices follow membership growth (also replacements), or a new controller,
        # and only with company: a lone phone needs no group explanation.
        members = tuple(sorted(present))
        grew = len(members) > 1 and bool(set(members) - set(self.noticed_present))
        changed = chosen != self.noticed_controller
        if members != self.noticed_present or changed:
            for peer in self.phones.values():
                peer['outbox'].pop('group', None)
        if len(present) > 1 and (grew or changed):
            callsign = present[chosen]['callsign']
            text = (f'{len(present)} ATAK phones are connected to {self.config.callsign}. '
                    f'{callsign} sets this radio\'s location. To change it, open manet.local in your browser.')
            for peer in present.values():
                self.queue_notice(peer, 'group', text, now)
        if members != self.noticed_present or chosen != self.noticed_controller or manual_uid is not None:
            self.noticed_present, self.noticed_controller = members, chosen
            self.commit(now)
        return True

    def controller_request(self, data, now, proof):
        """Consume a boot/session-bound local UI choice once, without clock authority."""
        if (not isinstance(data, dict) or data.get('schema') != 1
                or data.get('session') != self.session or data.get('boot_id') != self.boot_id):
            return False
        try:
            request = bounded_text(data.get('id'))
            uid = bounded_text(data.get('uid'))
            age = now.boot - number(data.get('written_boot'))
            if not 0 <= age <= 5 or request == self.choice_request:
                return False
            self.choice_request = request
            accepted = self.choose_controller(now, proof, manual_uid=uid)
            self.commit(now)
            return accepted
        except (ValueError, TypeError):
            return False

    def manual_id(self):
        """Identify the held gesture across reboot without exposing private provenance."""
        mark = self.position._manual
        return None if mark is None else state_digest(encode((
            mark.phone_uid, mark.source, mark.marker_uid, mark.observation_time, mark.fix)))

    def accepted_manual(self, now, packet=None):
        """Record one new gesture, keeping only its accepted Send for refresh."""
        self.held_notice = self.manual_id()
        # Hex chunks preserve the original XML bytes, including unknown detail,
        # within the existing checkpoint decoder's per-string limit.
        self.held_marker = (None if packet is None else {
            'id': self.held_notice,
            'xml': [packet[i:i + 2048].hex() for i in range(0, len(packet), 2048)],
            'due': now.mono + HELD_MARKER_REFRESH_S})

    def held_point_output(self, now, proof, selected):
        """Explain new held selections once and refresh only unretired Sent markers."""
        mark, identity = self.position._manual, self.manual_id()
        record = self.held_marker
        if record is not None and (record['id'] != identity or self.position._manual_retired
                or mark.marker_uid in self.position._retirements or mark.marker_uid in self.retire):
            self.held_marker = record = None
            self.commit(now)  # Retirement wins even after its replay hold expires.
        notice, self.held_notice = self.held_notice, None
        item = self.present_phones(now, proof).get(self.position.pinned_uid)
        if item is None or selected.location is None:
            return
        if notice is not None and notice == identity:
            text = ("The radio is holding your point. Deleting it in ATAK does not release it on the radio. "
                    "To release it, open manet.local in your browser and tap \"Use phone GPS again\", "
                    "or move on with your phone's GPS available.")
            self.send(cot.geochat_report(self.config.uid, self.config.callsign, self.position.pinned_uid,
                                        text, str(uuid.uuid4()), item['clock'].outbound_time(now.mono, now.utc)),
                      4242, now, item)
        if record is not None and now.mono >= record['due']:
            record['due'] = now.mono + HELD_MARKER_REFRESH_S
            self.commit(now)  # Bound attempts, including failed sends and same-boot restarts.
            self.send(bytes.fromhex(''.join(record['xml'])), 4242, now, item)

    def anchor_manual(self, now, observed=None):
        """Remember current controller GPS at acceptance, or its first fresh fix."""
        identity, uid = self.manual_id(), self.position.pinned_uid
        if identity is None or uid is None:
            self.manual_anchor = self.manual_motion = None
            return
        anchor = self.manual_anchor
        if anchor is None or (anchor['id'], anchor['uid']) != (identity, uid):
            anchor = {'id': identity, 'uid': uid, 'point': None}
            self.manual_motion = None
        observed = self.phone_gps.get(uid) if observed is None else observed
        if anchor['point'] is None and observed is not None and now.mono < observed[1]:
            anchor = dict(anchor, point=(observed[0].lat, observed[0].lon))
        if anchor != self.manual_anchor:
            self.manual_anchor = anchor
            self.commit(now)

    def release_manual(self, now, proof, *, moved, offset=False):
        """Retire one held point and make one quiet best-effort controller notice."""
        uid = self.position.pinned_uid
        item = self.present_phones(now, proof).get(uid)
        if item is None or not self.position.release_manual():
            return False
        self.manual_anchor = self.manual_motion = None
        self.held_marker = self.held_notice = None
        self.rewrite_fix = None
        self.rewrite_deadline = 0
        self.contact_changed = True
        self.commit(now)  # Never announce a release which was not saved.
        if offset:
            text = "Your phone's GPS is far from the held point, so the radio is using your phone's GPS again."
        elif moved:
            text = "You moved, so the radio is using your phone's GPS again."
        else:
            text = "Held point released. The radio will use your phone's GPS when available."
        reason = 'offset' if offset else 'movement' if moved else 'request'
        log(f'held point released controller={json.dumps(uid)} reason={reason}')
        self.send(cot.geochat_report(self.config.uid, self.config.callsign, uid, text,
                                    str(uuid.uuid4()), item['clock'].outbound_time(now.mono, now.utc)),
                  4242, now, item)
        return True

    def release_request(self, data, now, proof):
        """Recheck the staged request's controller path, point, session and age."""
        if not isinstance(data, dict):
            return False
        item = self.present_phones(now, proof).get(self.position.pinned_uid)
        try:
            return bool(item is not None and data.get('schema') == 1
                        and data.get('session') == self.session and data.get('boot_id') == self.boot_id
                        and data.get('uid') == self.position.pinned_uid
                        and (data.get('ip'), data.get('mac')) == (item['ip'], item['mac'])
                        and data.get('manual_id') == self.manual_id()
                        and 0 <= now.boot - number(data.get('written_boot')) <= 5
                        and self.release_manual(now, proof, moved=False))
        except (ValueError, TypeError):
            return False

    def moved_from_manual(self, now, proof, own_gnss_selected):
        """Require distinct fresh phone reports across the movement interval."""
        uid = self.position.pinned_uid
        observed = self.phone_gps.get(uid)
        if (self.position._manual is None or own_gnss_selected
                or uid not in self.present_phones(now, proof) or observed is None or now.mono >= observed[1]):
            self.manual_motion = None
            return False
        self.anchor_manual(now)
        point = observed[0].lat, observed[0].lon
        mark = self.position._manual.fix
        if phone._distance_m(point, (mark.lat, mark.lon)) > MANUAL_MAX_OFFSET_M:
            return self.release_manual(now, proof, moved=True, offset=True)
        if phone._distance_m(point, self.manual_anchor['point']) <= MANUAL_MOVE_M:
            self.manual_motion = None
            return False
        sample = self.phones[uid]['clock']._latest_time
        previous = self.manual_motion
        if previous is None or now.mono >= previous[2]:
            self.manual_motion = (sample, now.mono, observed[1])
        elif sample != previous[0]:
            self.manual_motion = (sample, previous[1], observed[1])
            if now.mono - previous[1] >= MANUAL_MOVE_S:
                return self.release_manual(now, proof, moved=True)
        return False

    def retry_parked(self):
        """Restart exhausted jobs that have no completed-retirement hold."""
        for item in list(self.messages.values()) + list(self.retire.values()):
            if item['attempts'] >= 5 and item.get('done_until') is None:
                item['attempts'] = 0
                item['due'] = 0

    def ingest(self, packet, address, proof, now):
        """Admit proven phone CoT and checkpoint changes before output."""
        if not self.config.enabled:
            return self.reject('ATAK disabled')
        mac = None if proof is None else proof.peer(address)
        if not mac:
            return self.reject('ingress/path unproven')
        try:
            if isinstance(packet, cot.PhoneSA):
                kind, is_sa = 'a-f-G-U-C', True
            else:
                root = cot._parse_document(packet, MAX_XML)
                bounded_text(root.get('uid'), 512)
                for element in root.iter():
                    for key, value in element.attrib.items():
                        bounded_text(value, 2048 if key == 'remarks' else 512)
                    if element.text is not None and len(element.text) > 2048:
                        raise ValueError('oversized CoT text')
                kind = root.get('type')
                # A creator-tagged person marker is a Sent point, not self SA.
                is_sa = kind == 'a-f-G-U-C' and root.find('detail/creator') is None
            present = self.present_phones(now, proof)
            sender = next((uid for uid, item in present.items() if item['ip'] == address and item['mac'] == mac), None)
            if not is_sa and sender is None:
                return self.reject('admitted SA required before dependent traffic')
            if is_sa:
                parsed = packet if isinstance(packet, cot.PhoneSA) else cot.parse_self_sa(packet)
                if isinstance(parsed, str):
                    return self.reject(parsed)
                bounded_text(parsed.uid)
                if parsed.uid == self.config.uid + '-beacon':
                    return self.reject('reserved radio beacon UID')
                callsign = bounded_text(parsed.callsign or parsed.uid)
                item = self.phones.get(parsed.uid)
                if item is not None and item['mac'] != mac and item['clock']._presence is not None and now.mono < item['clock']._presence:
                    return self.reject('pinned MAC mismatch while phone is present')
                if any(uid != parsed.uid and peer['ip'] == address for uid, peer in present.items()):
                    return self.reject('endpoint already has a present phone identity')
                if item is None:
                    clock = phone.PhonePosition(radio_uid=self.config.uid)
                    # Bound aggregate replay memory even at the configuration's 253 maximum.
                    clock.sa_history_limit = min(phone.PHONE_SA_HISTORY, max(8, 2048 // self.config.max_phones))
                    item = {'ip': address, 'mac': mac, 'port': proof.port(address),
                            'callsign': callsign, 'clock': clock,
                            'outbox': {}, 'warned': {}, 'guest_notified': False}
                clock = item['clock']
                baseline = {name: getattr(clock, name) for name in
                            ('_last_manual_sa_point', '_last_user_sa_point', '_last_sa_geopointsrc')}
                update = clock.feed(parsed, now.mono, now.utc, from_ethernet=True, allow_location=False)
                if update.status != 'accepted_presence':
                    return self.reject(update.status)
                if parsed.uid not in self.phones:
                    if len(self.phones) >= self.config.max_phones:
                        expired = [uid for uid in self.phones if uid not in present]
                        if not expired:
                            return self.reject('phone table full')
                        evict = min(expired, key=lambda uid: self.phones[uid]['clock']._presence or 0)
                        del self.phones[evict]
                        self.delivery.pop(evict, None)
                changed = item['ip'] != address or item['port'] != proof.port(address)
                item.update(ip=address, mac=mac, port=proof.port(address), callsign=callsign)
                self.phones[parsed.uid] = item
                self.phone_gps = {uid: value for uid, value in self.phone_gps.items() if uid in self.phones}
                previous_gps = self.phone_gps.get(parsed.uid)
                if parsed.source == 'gps' and parsed.geopointsrc == 'GPS' and not parsed.parent_uids:
                    fix = cot.Fix(parsed.lat, parsed.lon, 'gnss', hae=parsed.hae,
                                  horizontal_error=None if parsed.ce is None else cot.ErrorEstimate(parsed.ce, .9),
                                  vertical_error=None if parsed.le is None else cot.ErrorEstimate(parsed.le, .9),
                                  phone_uid=parsed.uid)
                    self.phone_gps[parsed.uid] = (fix, clock._presence, callsign)
                else:
                    self.phone_gps.pop(parsed.uid, None)
                previous = self.position.pinned_uid
                self.choose_controller(now, proof)
                result = update.status
                if parsed.uid == self.position.pinned_uid:
                    self.position.follow_phone(clock)
                    if previous != parsed.uid:
                        for name, value in baseline.items():
                            setattr(self.position, name, value)
                    update = self.position.admitted_sa(parsed, now.mono, clock.phone_now(now.mono),
                                                        update.reappeared, update.clock_diagnostic)
                    result = update.status
                    self.peer = {'ip': address, 'mac': mac}
                    self.contact_changed |= changed or update.reappeared or result == 'accepted_manual'
                    if result == 'accepted_manual':
                        self.accepted_manual(now)
                        self.anchor_manual(now, previous_gps)
                    elif parsed.source == 'gps' and self.position._manual is not None:
                        self.anchor_manual(now)
                        own_gps = (self.rewrite_fix is not None and self.rewrite_fix.source == 'gnss'
                                   and self.rewrite_fix.phone_uid is None
                                   and now.mono < self.rewrite_deadline)
                        self.moved_from_manual(now, proof, own_gps)
                if update.reappeared:
                    self.retry_parked()
                    self.delivery.pop(parsed.uid, None)
                    for job in item['outbox'].values():
                        job['attempts'], job['due'] = 0, now.mono
            elif kind in ('b-t-f-d', 'b-t-f-r'):
                parsed = cot.parse_chat_receipt(packet)
                if isinstance(parsed, str):
                    return self.reject(parsed)
                if sender != self.position.pinned_uid:
                    return 'guest_receipt'
                self.reconcile_uncertain_chat(parsed, now)
                result = self.position.feed_receipt(parsed, now.mono, now.utc, from_ethernet=True)
            elif kind == 'b-t-f':
                parsed = cot.parse_phone_chat(packet)
                if isinstance(parsed, str):
                    return self.reject(parsed)
                bounded_text(parsed.message_id)
                result = self.phones[sender]['clock'].feed_chat(parsed, now.mono, now.utc, from_ethernet=True)
                if isinstance(result, cot.PhoneChat):
                    # Deduplicate within the bounded 32-message display history.
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
                if sender != self.position.pinned_uid:
                    item = self.phones[sender]
                    if not item['guest_notified']:
                        controller = self.phones.get(self.position.pinned_uid)
                        if controller is not None:
                            self.queue_notice(item, 'guest',
                                f"{self.config.callsign} follows {controller['callsign']} for its location. "
                                "To change it, open manet.local in your browser.", now)
                            item['guest_notified'] = True
                            self.commit(now)
                    return self.reject('guest marker; only controller can set location')
                if parsed.uid == self.config.uid + '-beacon':
                    return self.reject('reserved radio beacon UID')
                if parsed.uid in self.phones:
                    return self.reject('reserved phone UID')
                if parsed.uid in self.retire:
                    return self.reject('retirement in flight')
                update = self.position.feed_marker(parsed, now.mono, now.utc, from_ethernet=True)
                result = update.status + ':' + update.reason
                self.contact_changed |= update.status == 'accepted'
                if update.status == 'accepted':
                    self.accepted_manual(now, packet)
                    self.anchor_manual(now)
            self.reconcile_audio()
            self.commit(now)  # A persistent controller choice precedes derived output.
            return result
        except (ValueError, KeyError, TypeError, cot.ET.ParseError) as exc:
            # commit wraps write failures as fatal PersistenceError, which
            # must propagate instead of quietly discarding a durable pin.
            return self.reject(str(exc))

    def reconcile_audio(self):
        """Drop cleared warnings and acknowledged repeating audio jobs."""
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
        """Remove acknowledged audio IDs from this session and checkpoint."""
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
        """Persist audio and banner handoffs before acknowledging events."""
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

    def send(self, data, port, now, peer=None):
        """Attempt UDP handoff and retain a bounded transport failure reason."""
        try:
            if not self.transport.send(data, ((peer or self.peer)['ip'], port)):
                raise OSError('short UDP send')
            self.send_error = None
            return True
        except OSError as exc:
            self.send_error = str(exc)[:200]
            return False

    def tick(self, now, proof, gps, input_status, release_request=None):
        """Select location, persist actions and send due phone output."""
        self.choose_controller(now, proof)
        if encode(self.input_memory) != self.saved_input_memory:
            self.commit(now)  # never forget that a formerly unwired producer became active
        # Capture policy transitions separately from 1 Hz feed/deadline timestamps.
        before = encode((self.position._warnings, self.position._events,
                         self.position._distrusted, self.position._overridden,
                         self.position._position_confirmed, self.position._checked_manual_epoch,
                         self.position._fault_epoch, self.position._retirements))
        age = now.mono - gps.observed_mono if gps.fix is not None else None
        fresh = age is not None and 0 <= age < self.position.gps_max_age_s and gps.state != 'NO_FIX'
        if fresh or gps.receiver_present is False:
            self.unavailable_since = None
        elif self.unavailable_since is None:
            self.unavailable_since = now.mono
        defer = (not self.phones or gps.receiver_present is False or not fresh and now.mono - self.unavailable_since < GPS_UNAVAILABLE_DELAY_S
                 and 'gps_untrusted' not in self.position._warnings)
        selected = self.position.select(now.mono, now.utc, radio_gps=gps, defer_unavailable=defer)
        if (not selected.own_gnss_selected and self.release_request(release_request, now, proof)
                or self.moved_from_manual(now, proof, selected.own_gnss_selected)):
            selected = replace(selected, location=None, fix=None,
                               retire_marker_uids=tuple(self.position._retirements))
        self.rewrite_fix = selected.fix  # Only the radio selector can authorize rewriting/4349.
        self.rewrite_deadline = (gps.observed_mono + self.position.gps_max_age_s
                                 if selected.own_gnss_selected else now.mono + INPUT_TTL)
        self.phone_gps = {uid: value for uid, value in self.phone_gps.items() if now.mono < value[1]}
        self.tak_servers = {mac: end for mac, end in self.tak_servers.items() if now.mono < end}
        self.compare_phone_gps(now, proof, selected)
        fallback = self.phone_gps.get(self.position.pinned_uid)
        if selected.fix is None and fallback is not None:
            selected = replace(selected, fix=fallback[0], position_trusted=False)
        if self.rewrite_fix is not None or not self.phones:
            self.unavailable_since = None
        warnings = set(self.position._warnings)
        for code in sorted(warnings ^ self.logged_warnings):
            transition = 'raised' if code in warnings else 'cleared'
            log(f'warning {transition} code={code} '
                f'gps_reason={json.dumps(str(input_status.get("gps", "unspecified"))[:200])} '
                f'state={gps.state} fresh={fresh} age_s={None if age is None else round(age, 3)} '
                f'distrusted={self.position._distrusted}')
        self.logged_warnings = warnings
        after = encode((self.position._warnings, self.position._events,
                        self.position._distrusted, self.position._overridden,
                        self.position._position_confirmed, self.position._checked_manual_epoch,
                        self.position._fault_epoch, self.position._retirements))
        if before != after:
            self.commit(now)
        if self.phones:
            self.local_actions(now)
        # A live lease suppresses beacon churn during temporary proof loss.
        phone_fix = selected.fix is not None and selected.fix.source == 'gnss' and selected.fix.phone_uid is not None
        occupied = bool(self.present_phones(now, proof)) if phone_fix else self.has_live_lease(now)
        self.beacon.update(self.config, now, selected.fix, occupied, self.send_beacon,
                           expires=None if not phone_fix else fallback[1])
        for uid in selected.retire_marker_uids:
            if uid not in self.retire:
                if len(self.retire) >= MAX_RETIRE:
                    break
                self.retire[uid] = {'id': str(uuid.uuid4()), 'attempts': 0, 'successes': 0,
                                    'due': now.mono, 'done_until': None,
                                    'phone_uid': self.position._retirements[uid]}
                self.commit(now)
            self.position.ack_retired((uid,))
            self.commit(now)
        present = self.present_phones(now, proof)
        ready = self.position.pinned_uid in present
        self.held_point_output(now, proof, selected)
        self.send_attached(now, proof, selected, present)
        stamp = self.position.outbound_time(now.mono, now.utc)
        fix = selected.fix
        signature = (None if fix is None else (fix.lat, fix.lon, fix.hae, fix.source),
                     selected.gps_overridden, selected.position_trusted, tuple(sorted(self.banners.items())), self.config.callsign,
                     None if proof is None else (proof.local_ip, proof.generation), self.peer)
        if ready:
            # Contacts refresh every 30 s, with a one-second attempt backoff.
            # Changes can trigger an earlier refresh after that backoff.
            if ((self.contact_attempt is None or now.mono - self.contact_attempt >= 1)
                    and (self.contact_changed or signature != self.contact_signature or cot.contact_refresh_due(now.mono, self.last_contact))):
                self.contact_attempt = now.mono
                if self.send(cot.hidden_contact(self.config.uid, self.config.callsign, (proof.local_ip, 4242), stamp), 4242, now):
                    self.last_contact, self.contact_signature, self.contact_changed = now.mono, signature, False
            # Feed selected geometry at most once per second while present.
            if self.rewrite_fix is not None and (self.feed_attempt is None or now.mono - self.feed_attempt >= 1):
                self.feed_attempt = now.mono
                packet = cot.external_position(self.config.uid, fix, stamp)
                if packet is not None and self.send(packet, 4349, now):
                    self.position.position_sent(fix, now.mono, now.utc)
                    present[self.position.pinned_uid]['clock'].position_sent(fix, now.mono, now.utc)
                    self.last_feed = now.mono
            pending = {str(e.event_id): e for e in self.position.outbound_events if e.kind == 'geochat'}
            self.messages = {key: item for key, item in self.messages.items() if key in pending}
            # Attempt one due chat per tick. Five attempts park unfinished work.
            for key, event in pending.items():
                item = self.messages.get(key)
                if item is None:
                    item = self.messages[key] = {'id': str(uuid.uuid4()), 'attempts': 0, 'due': now.mono, 'sent_mono': None}
                    self.commit(now)  # message ID stable through failure/crash/retry
                if item['attempts'] >= 5 or now.mono < item['due']:
                    continue
                item['attempts'] += 1
                # Retry delays are 2, 4, 8, 16 and 30 monotonic seconds.
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
        # Retire one due marker per tick with the same five-attempt backoff.
        for uid, item in list(self.retire.items()):
            if item['done_until'] is not None:
                if now.mono >= item['done_until']:
                    del self.retire[uid]
                    self.commit(now)
                continue
            if item['attempts'] >= 5 or now.mono < item['due']:
                continue
            owner = present.get(item['phone_uid'])
            if owner is None:
                continue
            item['attempts'] += 1
            item['due'] = now.mono + min(30, 2 ** item['attempts'])
            self.commit(now)
            owner_stamp = owner['clock'].outbound_time(now.mono, now.utc)
            if self.send(cot.retire_marker(uid, item['id'], owner_stamp), 4242, now, owner):
                item['successes'] += 1
                if item['successes'] >= 3:
                    # Three handoffs start a 120 s replay hold, not an ACK.
                    item['done_until'] = now.mono + 120
            self.commit(now)
            break
        if (self.phones or self.position._manual is not None) and now.mono - self.last_checkpoint >= 30:
            self.commit(now)
        return self.status(now, proof, selected, input_status)

    def send_beacon(self, packet):
        """Attempt only the separately confined mesh transport."""
        try:
            return bool(self.transport.send_beacon(packet))
        except OSError:
            return False

    def send_attached(self, now, proof, selected, present):
        """Feed every guest, fan out GPS warnings, and send bounded notices."""
        for uid, item in present.items():
            clock = item['clock']
            stamp = clock.outbound_time(now.mono, now.utc)
            controller = uid == self.position.pinned_uid
            if not controller:
                delivery = self.delivery.setdefault(uid, {})
                fix = selected.fix
                geometry = None if fix is None else (fix.lat, fix.lon, fix.hae, fix.source)
                signature = (proof.local_ip, proof.generation, item['ip'], geometry,
                             selected.position_trusted, tuple(sorted(self.banners.items())), self.config.callsign)
                if (now.mono - delivery.get('contact_attempt', -math.inf) >= 1
                        and (signature != delivery.get('signature') or
                             cot.contact_refresh_due(now.mono, delivery.get('contact')))):
                    delivery['contact_attempt'] = now.mono
                    if self.send(cot.hidden_contact(self.config.uid, self.config.callsign,
                                                   (proof.local_ip, 4242), stamp), 4242, now, item):
                        delivery.update(contact=now.mono, signature=signature)
                if self.rewrite_fix is not None and now.mono - delivery.get('feed_attempt', -math.inf) >= 1:
                    delivery['feed_attempt'] = now.mono
                    packet = cot.external_position(self.config.uid, selected.fix, stamp)
                    if packet is not None and self.send(packet, 4349, now, item):
                        clock.position_sent(selected.fix, now.mono, now.utc)
            warnings = {code: warning for code, warning in self.position._warnings.items() if code != 'phone_gps'}
            for code in list(item['warned']):
                if code not in warnings:
                    del item['warned'][code]
                    item['outbox'].pop(code, None)
            for code, warning in warnings.items():
                signature = (warning.raised_mono, warning.text)
                if controller:
                    item['outbox'].pop(code, None)
                if item['warned'].get(code) != signature:
                    if not controller:
                        self.queue_notice(item, code, warning.text, now)
                    item['warned'][code] = signature
                    self.commit(now)
            for code, job in list(item['outbox'].items()):
                if job['attempts'] >= 5 or now.mono < job['due']:
                    continue
                job['attempts'] += 1
                job['due'] = now.mono + min(30, 2 ** job['attempts'])
                self.commit(now)
                if self.send(cot.geochat_report(self.config.uid, self.config.callsign, uid,
                                                job['text'], job['id'], stamp), 4242, now, item):
                    del item['outbox'][code]
                    self.commit(now)
                # At most one notice per recipient per cycle; latest change coalesces.
                break

    def status(self, now, proof, selected=None, input_status=None):
        """Build the runtime snapshot from current policy and path state."""
        # An unassociated node has no phone policy to evaluate or announce.
        # Keep the same status schema without manufacturing a warning/audio job.
        clock = self.position.phone_now(now.mono)
        location = None if selected is None else selected.location
        fix = None if selected is None else selected.fix
        return {'schema': 1, 'running': True, 'boot_id': self.boot_id, 'written_boot': now.boot,
                'written_mono': now.mono,
                'enabled': self.config.enabled, 'path_ready': proof is not None,
                'phone_present': bool(self.phone_ready(now, proof)), 'phone_uid': self.position.pinned_uid,
                'peer': self.peer, 'session': self.session, 'controller_uid': self.position.pinned_uid,
                'manual_controller_uid': self.manual_uid, 'phone_limit': self.config.max_phones,
                'phones': [dict(uid=uid, callsign=item['callsign'], ip=item['ip'], mac=item['mac'],
                                port=item['port'], presence_deadline=item['clock']._presence,
                                tak_server=self.tak_server(item['mac'], now),
                                tak_server_deadline=self.tak_servers.get(item['mac'], 0),
                                gps_disagreement_m=self.disagreements.get(uid, {}).get('distance'),
                                gps_disagreement_deadline=self.disagreements.get(uid, {}).get('deadline', 0),
                                controller=uid == self.position.pinned_uid)
                           for uid, item in self.present_phones(now, proof).items()],
                'selected': None if fix is None else asdict(fix),
                'selected_source': (None if fix is None else
                    'phone GPS (' + self.phone_gps[fix.phone_uid][2] + ')' if fix.source == 'gnss' and fix.phone_uid else fix.source),
                'selected_deadline': (self.phone_gps[fix.phone_uid][1] if fix is not None and
                                      fix.source == 'gnss' and fix.phone_uid else
                                      self.rewrite_deadline if fix is not None and fix.source == 'gnss' else None),
                'manual_age_s': None if location is None else location.age_s,
                'manual_observation_mono': None if location is None else location.observation_mono,
                'manual_received_mono': None if location is None else location.received_mono,
                'manual_observation_time': None if location is None else location.observation_time.isoformat(),
                'manual_marker_uid': None if location is None else location.marker_uid,
                'manual_release_id': None if location is None else self.manual_id(),
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
    def __init__(self, service, inputs, runtime, tak_update=None):
        """Track inputs, acknowledgements and the last runtime publication."""
        self.service, self.inputs, self.runtime = service, inputs, Path(runtime)
        self.last_key = self.last_audio = self.ack_stamp = None
        self.input_revision = None
        self.controller_stamp = None
        self.release_stamp = None
        self.tak_update, self.tak_status_key = tak_update, None

    def update(self, now, proof, reason):
        """Process changed inputs or due work and publish runtime snapshots."""
        service = self.service
        release_request = None
        path = self.runtime / 'release.json'
        stamp = file_stamp(path)
        if stamp is not None and stamp != self.release_stamp:
            self.release_stamp = stamp
            try:
                release_request = read_json(path, 2048)
            except (FileNotFoundError, ValueError):
                pass
        path = self.runtime / 'controller.json'
        stamp = file_stamp(path)
        if stamp is not None and stamp != self.controller_stamp:
            self.controller_stamp = stamp
            try:
                service.controller_request(read_json(path, 2048), now, proof)
            except (FileNotFoundError, ValueError):
                pass
        if service.audio:
            path = self.runtime / 'audio-ack.json'
            stamp = file_stamp(path)
            if stamp is not None and stamp != self.ack_stamp:
                try:
                    service.audio_ack(read_json(path, 8192), now)
                    self.ack_stamp = stamp
                except (FileNotFoundError, ValueError):
                    pass
        gps, input_status = self.inputs.cached_read(now, service.input_memory, receiver=service.config.enabled)
        if self.input_revision != self.inputs.revision:
            # Observe producer activation/loss even before the first phone, so
            # an absent formerly-active monitor cannot later look "unwired".
            if encode(service.input_memory) != service.saved_input_memory:
                service.commit(now)
            self.input_revision = self.inputs.revision
        present = tuple(sorted(service.present_phones(now, proof)))
        gps_key = (
            gps.fix, gps.observed_mono if gps.fix is not None else None,
            gps.state, gps.jammed, gps.fault_epoch, gps.ranges_agree)
        def key():
            """Capture state changes that require an idle snapshot refresh."""
            return (service.revision, service.rejected, service.last_rejection,
                    service.config, proof, reason, gps_key, input_status, present,
                    service.has_live_lease(now))
        deadline = any(not w.acknowledged and w.deadline is not None and now.mono >= w.deadline
                       for w in service.position._warnings.values())
        deadline |= (service.unavailable_since is not None
                     and now.mono >= service.unavailable_since + GPS_UNAVAILABLE_DELAY_S
                     and 'gps_untrusted' not in service.position._warnings)
        deadline |= service.beacon.due(now)
        deadline |= any(now.mono >= value[1] for value in service.phone_gps.values())
        deadline |= any(now.mono >= end for end in service.tak_servers.values())
        # These timestamps preserve manual-age lower bounds and warning history
        # across reboot. A never-associated node needs no periodic checkpoint.
        checkpoint = (bool(service.phones) and
                      (service.position._manual is not None or service.position._warnings) and
                      now.mono - service.last_checkpoint >= 30)
        if (not service.phone_ready(now, proof) and self.last_key == key()
                and not deadline and not checkpoint):
            return False
        status = service.tick(now, proof, gps, input_status, release_request=release_request)
        if self.tak_update is not None:
            # Meaningful status changes create demand. Tick timestamps, lease
            # deadlines, manual age and producer receipt times do not.
            selected = status['selected']
            tak_key = (tuple((p['uid'], p['ip'], p['mac'], p['callsign'], p['port'], p['controller'],
                              p['tak_server'], p['gps_disagreement_m'])
                             for p in status['phones']), status['manual_release_id'],
                       None if selected is None else tuple(selected[k] for k in ('lat', 'lon', 'hae', 'source')),
                       status['position_trusted'], status['gps_state'], status['gps_overridden'],
                       status['path_ready'], tuple(sorted(service.banners.items())))
            self.tak_update(service, now, request=tak_key != self.tak_status_key)
            self.tak_status_key = tak_key
            for peer in status['phones']:
                peer['tak_server'] = service.tak_server(peer['mac'], now)
                peer['tak_server_deadline'] = service.tak_servers.get(peer['mac'], 0)
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
    """Share replenishing byte, event and connection limits across peers."""
    def __init__(self, now):
        """Initialize full byte, event and admission buckets."""
        self.at, self.bytes, self.events, self.accepts = now, 128 * 1024, 64, 16

    def take(self, now, *, size=0, events=0, accepts=0):
        """Refill by elapsed seconds and debit only if every limit permits."""
        elapsed = max(0, now - self.at)
        self.at = now
        # Refill 64 KiB, 32 events and 8 admissions per second. Each bucket
        # holds two seconds of work and is shared across TCP and UDP input.
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
    """Track one TCP peer, XML frame and cumulative stream limits."""
    def __init__(self, sock, address, mac, now):
        """Start a bounded framer and timeout history for an admitted peer."""
        self.sock, self.address, self.mac = sock, address, mac
        self.created = self.last_read = now
        self.partial_since = None
        self.total = self.events = 0
        self.framer = cot.CotStreamFramer(MAX_XML)

    def expired(self, now):
        """Check idle, total-lifetime and incomplete-frame timeouts."""
        return (now - self.last_read >= IDLE_S or now - self.created >= LIFETIME_S
                or (self.partial_since is not None and now - self.partial_since >= PARTIAL_S))

    def receive(self, data, now, budget):
        """Frame bytes within stream/global limits, returning None at EOF."""
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
            # Partial-frame age starts anew only after a complete document.
            if self.partial_since is None or documents:
                self.partial_since = now
        else:
            self.partial_since = None
        return documents


MAX_MESH_FRAME = 14 + 60 + 8 + tak.MAX_SA_BYTES


def mesh_sa_bpf():
    """Build classic BPF for local IPv4 UDP mesh SA, excluding egress/fragments."""
    # Linux ancillary SKF_AD_PKTTYPE prevents remote frames forwarded OUT of
    # an EUD port from reaching userspace. No multicast membership is needed.
    program = [
        (0x20, 0, 0, 0xfffff004),       # LD W ABS: packet type
        (0x35, 'drop', 0, 4),          # outgoing/loopback or unknown type
        (0x80, 0, 0, 0),              # LD W LEN
        (0x25, 'drop', 0, MAX_MESH_FRAME),
        (0x28, 0, 0, 12),             # Ethernet protocol
        (0x15, 0, 'drop', 0x0800),
        (0x30, 0, 0, 14),             # IPv4 version and header length
        (0x54, 0, 0, 0xf0),
        (0x15, 0, 'drop', 0x40),
        (0x30, 0, 0, 14),
        (0x54, 0, 0, 0x0f),
        (0x35, 0, 'drop', 5),
        (0x30, 0, 0, 23),             # IP protocol: UDP
        (0x15, 0, 'drop', 17),
        (0x28, 0, 0, 20),             # reject reserved/MF/offset; allow DF
        (0x45, 'drop', 0, 0xbfff),
        (0x20, 0, 0, 30),             # IPv4 destination 239.2.3.1
        (0x15, 0, 'drop', int(ipaddress.IPv4Address(tak.MESH_GROUP))),
        (0xb1, 0, 0, 14),             # LDX MSH: variable IPv4 header length
        (0x48, 0, 0, 16),             # LD H IND: UDP destination
        (0x15, 0, 'drop', tak.MESH_PORT),
        (0x06, 0, 0, MAX_MESH_FRAME),
        (0x06, 0, 0, 0),
    ]
    return tuple((code, len(program) - i - 2 if jt == 'drop' else jt,
                  len(program) - i - 2 if jf == 'drop' else jf, value)
                 for i, (code, jt, jf, value) in enumerate(program))


def make_mesh_sa_socket(iface, factory=socket.socket):
    """Attach and lock a filter before enabling reception on one EUD port."""
    if iface in ('bat0', 'br0', 'lo') or not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', iface):
        raise ValueError('invalid mesh SA ingress interface')

    class MeshFilter(ctypes.Structure):
        """Native Linux sock_filter instruction layout."""
        _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte),
                    ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint32)]

    class MeshFilterProgram(ctypes.Structure):
        """Native Linux sock_fprog, with architecture-correct pointer alignment."""
        _fields_ = [('length', ctypes.c_ushort), ('instructions', ctypes.POINTER(MeshFilter))]

    program = mesh_sa_bpf()
    instructions = (MeshFilter * len(program))(*(MeshFilter(*row) for row in program))
    descriptor = MeshFilterProgram(len(program), instructions)
    # Protocol zero receives nothing until the filtered socket is bound.
    sock = factory(socket.AF_PACKET, socket.SOCK_RAW, 0)
    try:
        sock.setsockopt(socket.SOL_SOCKET, 26, bytes(descriptor))  # SO_ATTACH_FILTER
        sock.setsockopt(socket.SOL_SOCKET, 44, 1)                 # SO_LOCK_FILTER
        sock.setsockopt(263, 23, 1)                             # PACKET_IGNORE_OUTGOING
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
        sock.setblocking(False)
        # ETH_P_ALL taps ingress before the bridge RX handler consumes it;
        # an ETH_P_IP socket bound to a bridge slave can miss that traffic.
        # The locked BPF admits only mesh SA. TAK TCP detection stays in nftables.
        sock.bind((iface, 3))
        return sock
    except BaseException:
        sock.close()
        raise


def mesh_checksum(data):
    """Return an Internet checksum for bounded IP/UDP headers and payload."""
    if len(data) % 2:
        data += b'\0'
    total = sum(struct.unpack('!' + 'H' * (len(data) // 2), data))
    while total >> 16:
        total = (total & 0xffff) + (total >> 16)
    return (~total) & 0xffff


def mesh_sa_frame(frame):
    """Validate Ethernet/IPv4/UDP without decoding CoT or retaining raw bytes."""
    if not 42 <= len(frame) <= MAX_MESH_FRAME or frame[12:14] != b'\x08\x00':
        raise ValueError('invalid mesh SA Ethernet frame')
    header = (frame[14] & 15) * 4
    total = int.from_bytes(frame[16:18], 'big')
    if (frame[14] >> 4 != 4 or header < 20 or total < header + 8
            or len(frame) < 14 + total or frame[23] != 17
            or int.from_bytes(frame[20:22], 'big') & 0xbfff
            or frame[30:34] != socket.inet_aton(tak.MESH_GROUP)
            or mesh_checksum(frame[14:14 + header])):
        raise ValueError('invalid mesh SA IPv4 header')
    udp = frame[14 + header:14 + total]
    length = int.from_bytes(udp[4:6], 'big')
    if (int.from_bytes(udp[2:4], 'big') != tak.MESH_PORT or length != len(udp)
            or not 0 < length - 8 <= tak.MAX_SA_BYTES):
        raise ValueError('invalid mesh SA UDP header')
    if udp[6:8] != b'\0\0':
        pseudo = frame[26:34] + b'\0\x11' + udp[4:6]
        if mesh_checksum(pseudo + udp):
            raise ValueError('invalid mesh SA UDP checksum')
    if frame[6] & 1 or frame[6:12] == b'\0' * 6:
        raise ValueError('invalid mesh SA source MAC')
    return ipv4(frame[26:30]), frame[6:12].hex(':'), udp[8:]


def replace_mesh_payload(frame, packet):
    """Preserve Ethernet/IP/UDP identity, updating lengths and checksums only."""
    header = (frame[14] & 15) * 4
    result = bytearray(frame[:14 + header + 8] + packet)
    result[16:18] = struct.pack('!H', header + 8 + len(packet))
    result[24:26] = b'\0\0'
    result[24:26] = struct.pack('!H', mesh_checksum(bytes(result[14:14 + header])))
    udp = 14 + header
    result[udp + 4:udp + 6] = struct.pack('!H', 8 + len(packet))
    result[udp + 6:udp + 8] = b'\0\0'
    pseudo = bytes(result[26:34]) + b'\0\x11' + bytes(result[udp + 4:udp + 6])
    result[udp + 6:udp + 8] = struct.pack('!H', mesh_checksum(pseudo + bytes(result[udp:])) or 0xffff)
    return bytes(result)


class Network:
    """Provide nonblocking IPv4 sockets with injectable factory/readiness."""
    def __init__(self, factory=socket.socket, verify=None):
        """Initialize transport state and a budget shared by all sockets."""
        self.factory = factory
        self.verify = verify
        self.listener = self.udp = self.sender = None
        self.beacon_sender = self.beacon_path = None
        self.local_ip = None
        self.proof = None
        self.streams = {}
        self.datagram_peers = {}
        self.mesh_sockets = {}  # iface -> (role/lifetime identity, socket)
        self.mesh_peers = set()
        self.mesh_pending = {}  # One admitted SA frame per EUD port awaiting TAK membership.
        self.budget = Budget(0)
        self.events = None
        self.rewrite_members = set()
        self.rewrite_at = None
        self.rewrite_retiring = {}
        self.tak_ready = False
        self.tak_demand = False
        self.raw_sender = self.raw_path = None
        self.rewrite_suspended_until = 0

    def close_stream(self, sock):
        """Forget and close one TCP connection."""
        self.streams.pop(sock, None)
        sock.close()

    def close(self):
        """Close all transport sockets and discard admitted network paths."""
        for iface in list(self.mesh_pending):
            self.flush_mesh_pending(iface)
        self.close_unicast()
        self.close_beacon()
        for _, sock in self.mesh_sockets.values():
            sock.close()
        self.mesh_sockets.clear()
        # ExecStopPost removes the table too. A crash or failed flush still
        # releases every element in three seconds, independently of userspace.
        if self.rewrite_members and self.events is not None:
            try:
                self.events.rewrite_members(set())
            except OSError:
                pass
        self.rewrite_members.clear()
        self.rewrite_retiring.clear()
        self.tak_ready = False
        if self.raw_sender is not None:
            self.raw_sender.close()
        self.raw_sender = self.raw_path = None

    def update_tak(self, service, now, *, request=False):
        """Share demand-driven kernel membership with rewrite selection and status."""
        self.tak_ready = False
        if self.events is None:
            return
        if not service.has_live_lease(now) and not self.rewrite_members and not self.mesh_pending:
            return
        members = self.events.tak_members(now.mono)
        if members is not None:
            service.tak_servers = dict(members)
            self.tak_ready = True
            self.tak_demand = False
        else:
            self.tak_demand |= request or bool(self.mesh_pending)
        if self.events.tak_pending is not None or self.tak_demand:
            self.events.read_tak(now.mono)

    def refresh_rewrite(self, service, now):
        """Renew admitted peers on the existing cycle only with a radio fix."""
        if self.events is None:
            return
        self.update_tak(service, now)
        if self.events.tak_pending is not None:
            return  # Let the multipart read finish before changing generation.
        proof = self.proof
        members = set()
        if (proof is not None and proof.mesh_path and service.rewrite_fix is not None
                and now.mono < service.rewrite_deadline and now.mono >= self.rewrite_suspended_until):
            for item in service.present_phones(now, proof).values():
                if service.tak_server(item['mac'], now):
                    continue
                iface = proof.interfaces.get(item['ip'])
                key = iface, item['ip'], item['mac']
                if (iface in self.mesh_sockets and proof.local_ports.get(iface) == self.mesh_sockets[iface][0]
                        and (self.tak_ready or key in self.rewrite_members)):
                    # Existing leases remain safe: the native TAK rule exempts
                    # members immediately. Every replacement needs a fresh read.
                    members.add(key)
        if members and self.raw_path != proof.mesh_path:
            if self.raw_sender is not None:
                self.raw_sender.close()
            self.raw_sender = self.factory(socket.AF_PACKET, socket.SOCK_RAW, 0)
            self.raw_sender.setblocking(False)
            self.raw_sender.bind(('bat0', 0))  # Protocol zero: output only, no tap/join.
            self.raw_path = proof.mesh_path
        if (members != self.rewrite_members or members and
                (self.rewrite_at is None or now.mono - self.rewrite_at >= 1)):
            self.events.rewrite_members(members)
            # Preserve queued frames caught by the old lease during removal.
            # Native forwarding may produce an identical duplicate in this window.
            for key in self.rewrite_members - members:
                self.rewrite_retiring[key] = now.mono + REWRITE_TIMEOUT_S
            self.rewrite_retiring = {key: end for key, end in self.rewrite_retiring.items()
                                     if key not in members}
            if len(self.rewrite_retiring) > MAX_PHONE_LIMIT * 4:
                raise GuardFailure('mesh rewrite retirement queue saturated')
            self.rewrite_members, self.rewrite_at = members, now.mono

    def relay_mesh_frame(self, original, packet=None):
        """Send only on bat0, falling back to original bytes on rewrite failure."""
        if self.raw_sender is None:
            raise OSError('rewrite output unavailable')
        rewritten = original if packet is None else replace_mesh_payload(original, packet)
        try:
            if self.raw_sender.sendto(rewritten, ('bat0', 0)) != len(rewritten):
                raise OSError('short raw mesh send')
        except OSError:
            if rewritten == original or self.raw_sender.sendto(original, ('bat0', 0)) != len(original):
                raise OSError('raw mesh forwarding failed') from None

    def close_beacon(self):
        """Forget a multicast socket whose verified bridge path changed."""
        if self.beacon_sender is not None:
            self.beacon_sender.close()
        self.beacon_sender = self.beacon_path = None

    def close_unicast(self):
        """Revoke unicast proof without removing passive SA observation."""
        for sock in list(self.streams):
            self.close_stream(sock)
        for name in ('listener', 'udp', 'sender'):
            sock = getattr(self, name)
            if sock is not None:
                sock.close()
                setattr(self, name, None)
        self.proof = None
        self.local_ip = None
        self.datagram_peers.clear()
        self.mesh_peers.clear()

    def sync_mesh_ports(self, paths):
        """Apply changed local role/link identities without a lifecycle timer."""
        for iface, (identity, sock) in list(self.mesh_sockets.items()):
            if paths.get(iface) != identity:
                self.flush_mesh_pending(iface)
                sock.close()
                del self.mesh_sockets[iface]
                self.mesh_peers = {key for key in self.mesh_peers if key[0] != iface}
        for iface, identity in paths.items():
            if iface not in self.mesh_sockets:
                try:
                    self.mesh_sockets[iface] = (identity, make_mesh_sa_socket(iface, self.factory))
                except OSError as exc:
                    # Fail closed rather than retrying privileged socket setup
                    # every timer tick when a filter/bind cannot be installed.
                    raise GuardFailure('mesh SA socket setup failed') from exc

    def listen(self, address):
        """Open only br0 input listeners; these do not authorize phone I/O."""
        if address != self.local_ip:
            self.close_unicast()
        if address is not None and self.listener is None:
            try:
                self.listener = self.make(socket.SOCK_STREAM, '0.0.0.0', 4242)
                self.listener.listen(MAX_CONNECTIONS)
                self.udp = self.make(socket.SOCK_DGRAM, '0.0.0.0', 4242)
                self.udp.setsockopt(socket.IPPROTO_IP, 8, 1)   # IP_PKTINFO
                self.udp.setsockopt(socket.IPPROTO_IP, 49, 0)  # IP_MULTICAST_ALL
                self.local_ip = address
            except OSError:
                self.close_unicast()
                raise

    def idle(self, address):
        """Retain only listeners while invalid proof awaits incoming traffic."""
        for sock in list(self.streams):
            self.close_stream(sock)
        if self.sender is not None:
            self.sender.close()
            self.sender = None
        self.proof = None
        self.datagram_peers.clear()
        self.mesh_peers.clear()
        self.listen(address)

    def configure(self, proof):
        """Rebind changed local paths and evict peers absent from new proof."""
        if (proof is None or self.local_ip != proof.local_ip
                or self.proof is not None and self.proof.generation != proof.generation):
            self.close_unicast()
        if proof is not None:
            try:
                self.listen(proof.local_ip)
                if self.sender is None:
                    self.sender = self.make(socket.SOCK_DGRAM, proof.local_ip, 0)
                    # Linux IP_MTU_DISCOVER=10 / IP_PMTUDISC_DO=2. Never fragment
                    # around the bridge's port-based egress filter.
                    self.sender.setsockopt(socket.IPPROTO_IP, 10, 2)
                    self.sender.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, 1)
            except OSError:
                self.close_unicast()
                raise
        self.proof = proof
        self.datagram_peers = {ip: mac for ip, mac in self.datagram_peers.items()
                               if proof is not None and proof.peer(ip) == mac}
        for sock, stream in list(self.streams.items()):
            if (proof is None or proof.peer(stream.address) != stream.mac
                    or not proof.local_address(sock.getsockname()[0])):
                self.close_stream(sock)
        self.mesh_peers = {key for key in self.mesh_peers if proof is not None
                           and proof.peer(key[1]) == key[2] and proof.interfaces.get(key[1]) == key[0]}

    def make(self, kind, ip, port):
        """Create a nonblocking IPv4 socket restricted to br0."""
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
        """Recheck the path and send one bounded UDP packet to a proven peer."""
        if self.verify is not None:
            self.configure(self.verify())
        if self.sender is None or self.proof is None or not self.proof.peer(destination[0]):
            raise OSError('local phone path unavailable')
        # IPv4's 65535-byte maximum leaves 65507 bytes after IP/UDP headers.
        if len(packet) > 65507:
            raise OSError('CoT exceeds UDP limit')
        return self.sender.sendto(packet, destination) == len(packet)

    def send_beacon(self, packet):
        """Verify policy before sending XML through br0, confined to bat0."""
        proof = self.verify() if self.verify is not None else self.proof
        if proof is None or not proof.mesh_path:
            self.close_beacon()
            raise OSError('mesh beacon path unavailable')
        if len(packet) > 1200:
            raise OSError('radio beacon exceeds datagram budget')
        identity = proof.local_ip, proof.mesh_path
        if identity != self.beacon_path:
            self.close_beacon()
            try:
                self.beacon_sender = self.make(socket.SOCK_DGRAM, proof.local_ip, 0)
                self.beacon_sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                              socket.inet_aton(proof.local_ip))
                self.beacon_sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
                self.beacon_sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
                self.beacon_sender.setsockopt(socket.IPPROTO_IP, 10, 2)  # Never fragment.
                self.beacon_path = identity
            except OSError:
                self.close_beacon()
                raise
        return self.beacon_sender.sendto(packet, (tak.MESH_GROUP, tak.MESH_PORT)) == len(packet)

    def readers(self):
        """Return listeners and admitted streams for the select loop."""
        return (([self.listener, self.udp] if self.listener is not None else []) + list(self.streams)
                + [sock for iface, (_, sock) in self.mesh_sockets.items() if iface not in self.mesh_pending])

    def flush_mesh_pending(self, iface):
        """Best-effort original forwarding when a pending frame loses its path."""
        pending = self.mesh_pending.pop(iface, None)
        if pending is not None:
            try:
                self.relay_mesh_frame(pending[0])
            except OSError:
                pass  # Teardown still opens native forwarding via cleanup/timeout.

    def mesh_replacement(self, data, iface, now, service):
        """Recheck authority after a bounded asynchronous kernel set read."""
        address, mac, packet = mesh_sa_frame(data)
        key = iface, address, mac
        proof = self.proof
        if (proof is not None and iface in self.mesh_sockets
                and proof.peer(address) == mac and proof.interfaces.get(address) == iface
                and proof.local_ports.get(iface) == self.mesh_sockets[iface][0]
                and proof.mesh_path == self.raw_path and key in self.rewrite_members
                and self.tak_ready and not service.tak_server(mac, now)
                and now.mono >= self.rewrite_suspended_until
                and service.rewrite_fix is not None and now.mono < service.rewrite_deadline):
            source = cot.EXTERNAL_SOURCE_PREFIX + (service.config.beacon_name or service.config.callsign)
            rewritten = tak.rewrite_mesh_sa(packet, service.rewrite_fix, source)
            return rewritten if rewritten != packet else None

    def receive_mesh_sa(self, sock, iface, now, service):
        """Prove physical ingress before invoking either SA payload decoder."""
        data, _, flags, endpoint = sock.recvmsg(MAX_MESH_FRAME, 0)
        if (flags & socket.MSG_TRUNC or len(endpoint) < 5 or endpoint[0] != iface
                or endpoint[1] != 0x0800 or endpoint[2] >= 4 or endpoint[3] != 1):
            return
        if len(data) < 34:
            return
        # SA capture loss still warrants opening native forwarding, but cannot
        # lose TAK detection now that TCP updates never enter this socket.
        try:
            stats = sock.getsockopt(263, 6, 8)
            if len(stats) != 8:
                raise OSError('invalid packet statistics')
            _, dropped = struct.unpack('=II', stats)
        except OSError as exc:
            raise GuardFailure('mesh rewrite capture health unavailable') from exc
        if dropped:
            self.rewrite_suspended_until = max(self.rewrite_suspended_until, now.mono + REWRITE_TIMEOUT_S)
        key = (iface, socket.inet_ntoa(data[26:30]), data[6:12].hex(':'))
        blocked = key in self.rewrite_members or key in self.rewrite_retiring
        replacement = None
        try:
            if dropped:
                return
            replacement = self.process_mesh_sa(data, iface, now, service, blocked)
        finally:
            if blocked and isinstance(replacement, tuple) and replacement[0] is WAIT_TAK:
                self.mesh_pending[iface] = (data, self.proof.generation, replacement[1])
            elif blocked:
                try:
                    self.relay_mesh_frame(data, replacement)
                except OSError as exc:
                    # Stop refreshing, including when the original also cannot
                    # be sent. The unit cleanup/three-second timeout opens SA.
                    raise GuardFailure('mesh rewrite forwarding failed') from exc

    def process_mesh_sa(self, data, iface, now, service, blocked):
        """Admit presence and return a replacement only for an active member."""
        if not self.budget.take(now.mono, size=len(data), events=1):
            self.rewrite_suspended_until = max(self.rewrite_suspended_until, now.mono + REWRITE_TIMEOUT_S)
            return
        address, mac, packet = mesh_sa_frame(data)
        key = (iface, address, mac)
        fresh = key not in self.mesh_peers
        if fresh and not self.budget.take(now.mono, accepts=1):
            return
        identity = self.mesh_sockets[iface][0]
        if self.verify is not None:
            self.configure(self.verify(force=fresh))
        proof = self.proof
        if (proof is None or proof.peer(address) != mac or proof.interfaces.get(address) != iface
                or proof.local_ports.get(iface) != identity):
            return
        # Bound admission memory independently of the phone table.
        if len(self.mesh_peers) >= MAX_STREAM_EVENTS:
            self.mesh_peers.clear()
        self.mesh_peers.add(key)
        parsed = tak.decode_mesh_sa(packet)
        if parsed is None:
            return
        if isinstance(parsed, str):
            service.reject(parsed)
            return
        # A multicast claim must name this source, never a hostname or reply
        # placeholder. Replies always use the proven IPv4 source, not this text.
        endpoint = re.fullmatch(r'([0-9.]+):([0-9]{1,5}):(tcp|udp)', parsed.endpoint or '')
        if endpoint is None or endpoint[1] != address or not 1 <= int(endpoint[2]) <= 65535:
            service.reject('mesh SA endpoint does not match proven source')
            return
        accepted = service.ingest(parsed, address, proof, now)
        if (accepted in ('accepted_presence', 'accepted_manual') and blocked and key in self.rewrite_members
                and service.rewrite_fix is not None and now.mono < service.rewrite_deadline):
            self.update_tak(service, now, request=True)
            if self.events is not None and not self.tak_ready:
                return WAIT_TAK, service.phones[parsed.uid]['clock']._presence
            return self.mesh_replacement(data, iface, now, service)

    def admit(self, address):
        """Every new TCP stream / UDP peer gets a fresh, stable snapshot."""
        old = self.proof
        address_before = self.local_ip
        if self.verify is not None:
            self.configure(self.verify(force=True))
        if (self.proof is None or address_before != self.proof.local_ip
                or old is not None and old.generation != self.proof.generation):
            return None
        return self.proof.peer(address)

    def handle(self, readable, now, service):
        """Expire streams and dispatch ready input within shared budgets."""
        for iface, (data, generation, deadline) in list(self.mesh_pending.items()):
            if (not self.tak_ready and now.mono < deadline
                    and self.proof is not None and self.proof.generation == generation):
                continue
            self.mesh_pending.pop(iface)
            replacement = None
            try:
                if now.mono < deadline and self.proof is not None and self.proof.generation == generation:
                    replacement = self.mesh_replacement(data, iface, now, service)
                self.relay_mesh_frame(data, replacement)
            except (OSError, ValueError) as exc:
                raise GuardFailure('pending mesh forwarding failed') from exc
        # A busy queue may still contain SA intercepted before set removal.
        # Forget retiring keys only after lease expiry AND a drained queue.
        for key, end in list(self.rewrite_retiring.items()):
            current = self.mesh_sockets.get(key[0])
            if now.mono >= end and key[0] not in self.mesh_pending and (current is None or current[1] not in readable):
                del self.rewrite_retiring[key]
        for sock, stream in list(self.streams.items()):
            if stream.expired(now.mono):
                self.close_stream(sock)
        for sock in readable:
            try:
                iface = next((name for name, (_, packet_sock) in self.mesh_sockets.items()
                              if packet_sock is sock), None)
                if iface is not None:
                    self.receive_mesh_sa(sock, iface, now, service)
                    continue
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
                        if mac and not self.proof.local_address(client.getsockname()[0]):
                            mac = None
                    except BaseException:
                        client.close()
                        raise
                    if not mac:
                        client.close()
                        continue
                    client.setblocking(False)
                    self.streams[client] = Stream(client, address, mac, now.mono)
                elif sock is self.udp:
                    data, ancillary, flags, endpoint = sock.recvmsg(MAX_XML + 1, socket.CMSG_SPACE(12))
                    destinations = [socket.inet_ntoa(value[8:12]) for level, kind, value in ancillary
                                    if level == socket.IPPROTO_IP and kind == 8 and len(value) == 12]
                    if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or len(destinations) != 1:
                        continue
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
                            # Evicted peers need fresh proof on return.
                            self.datagram_peers.pop(next(iter(self.datagram_peers)))
                        self.datagram_peers[endpoint[0]] = mac
                    if self.proof is not None and self.proof.local_address(destinations[0]):
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
    """Run firewall unit hooks or the single-process ATAK event loop."""
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
        # A disabled start removes stale tables without activating rules.
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
        # One process owns the durable queues and the fixed JSON temp paths.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        bounded_text(boot_id)
        clock, network = Clock(), Network()
        service = Service(config, boot_id, Store(state_dir / 'state.json'), network, clock.now())
        inputs, guard = Inputs(boot_id), LinuxGuard(config)
        application = Application(service, inputs, runtime, tak_update=network.update_tak)
        network.verify = guard.current
        flags = {'stop': False, 'reload': False}
        signal.signal(signal.SIGTERM, lambda *_: flags.update(stop=True))
        signal.signal(signal.SIGINT, lambda *_: flags.update(stop=True))
        signal.signal(signal.SIGHUP, lambda *_: flags.update(reload=True))
        log('starting; IPv4 TCP/UDP 4242; waiting for verified local EUD policy')
        next_tick, next_cycle, last_reason = 0, 0, None
        try:
            # Subscribe before the first proof to catch intervening changes.
            guard.start()
            network.events = guard.events
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
                    # Idle mesh churn only invalidates proof. Passive listeners
                    # use a cheap address ioctl; admission performs readback.
                    active = service.has_live_lease(now)
                    proof = guard.current(refresh=active)
                    network.sync_mesh_ports(guard.local_ports)
                    try:
                        if proof is None and not active:
                            network.idle(guard.listen_address())
                        else:
                            network.configure(proof)
                    except OSError as exc:
                        proof = None
                        guard.reason = 'socket setup failed: ' + str(exc)
                    if guard.reason != last_reason:
                        log(guard.reason)
                        last_reason = guard.reason
                    now = clock.now()
                    next_cycle = now.mono + .2  # bound expensive path readbacks under a flood
                    network.update_tak(service, now)
                    network.handle(ready, now, service)
                    proof = network.proof  # admission may have revoked it
                    # Admission may launch bounded readbacks. A producer can
                    # publish during them, so assess inputs against fresh clocks.
                    now = clock.now()
                    network.update_tak(service, now)
                    application.update(now, proof, guard.reason)
                    network.refresh_rewrite(service, now)
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
