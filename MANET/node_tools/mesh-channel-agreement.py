#!/usr/bin/env python3
"""One ACS decision domain across every BATMAN radio, with live recovery."""
import argparse
from collections import Counter
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import time

from manet_admin import AdminTransport, clock_ready, private_json_write, require_clock
import manet_acs_agreement as protocol
import manet_rendezvous as rendezvous

TOOLS = Path(__file__).resolve().parent
FRESH_SECONDS = 45
PROBE_LIFETIME = 60  # Request/reply replication plus a 15-second manager wakeup.
PROBE_INTERVAL = 60
RECORD = re.compile(r'\{\s*"([0-9a-fA-F:]{17})"\s*,\s*("(?:\\.|[^"\\])*")\s*\}')


def command(args, timeout=5, input=None):
    return subprocess.run([str(a) for a in args], input=input, capture_output=True,
                          text=True, check=True, timeout=timeout).stdout


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {} if default is None else default


def authenticated_records(transport, type_id, raw, now):
    from cryptography.exceptions import InvalidTag
    result, orders = {}, {}
    for match in RECORD.finditer(raw):
        mac = match[1].lower()
        try:
            message = transport.open(type_id, json.loads(json.loads(match[2])))
            payload = message.payload
            if (payload.get('node') != mac or not -5 <= now - message.sent_ns / 1e9 <= FRESH_SECONDS
                    or message.order <= orders.get(mac, [0, ''])):
                continue
            result[mac], orders[mac] = payload, message.order
            result[mac]['_fresh_until'] = message.sent_ns / 1e9 + FRESH_SECONDS
        except (ValueError, TypeError, KeyError, InvalidTag):
            continue
    return result


def challenge_records(transport, type_id, raw):
    """Authentication without wall time, exclusively for nonce-bound recovery."""
    from cryptography.exceptions import InvalidTag
    result = {}
    for match in RECORD.finditer(raw):
        mac = match[1].lower()
        try:
            payload = transport.open_challenge(type_id, json.loads(json.loads(match[2]))).payload
            if (payload.get('node') == mac and isinstance(payload.get('boot'), str)
                    and protocol.TOKEN.fullmatch(payload['boot'])):
                result[mac] = payload
        except (ValueError, TypeError, KeyError, InvalidTag):
            continue
    return result


def canonical_members(own, peers, registry, records):
    aliases = {own: own}
    def add(alias, mac):
        if not protocol.MAC.fullmatch(alias) or aliases.get(alias, mac) != mac:
            raise ValueError('ambiguous participant identity')
        aliases[alias] = mac
    for line in registry.splitlines():
        match = re.fullmatch(r'NODE_([0-9a-f]{12})_MAC_ADDRESSES=(.*)', line)
        if match:
            mac = ':'.join(match[1][i:i + 2] for i in range(0, 12, 2))
            values = shlex.split(match[2])
            if len(values) != 1:
                raise ValueError('invalid registry identity')
            for alias in [mac, *values[0].lower().split(',')]:
                if alias:
                    add(alias, mac)
    for mac, record in records.items():
        for alias in [mac, *record.get('aliases', [])]:
            add(alias, mac)
    if any(peer not in aliases for peer in peers):
        raise ValueError('reachable originator lacks a canonical identity')
    return sorted({own, *(aliases[p] for p in peers)})


def recovery_destination(records, local, now, own, excluded=(), own_size=None):
    """Choose a fresh helper by size then MAC; delayed cached beacons expire."""
    choices = []
    for mac, record in records.items():
        channels, size = record.get('channels'), record.get('size')
        if mac == own or mac in excluded or type(size) is not int or not 1 <= size <= protocol.MAX_MEMBERS:
            continue
        if (not isinstance(channels, dict) or not channels
                or any(b not in protocol.CHANNELS or type(f) is not int or f not in protocol.CHANNELS[b]
                       for b, f in channels.items())
                or not protocol.compatible(channels, local)
                or not set(channels).intersection(local['current'])):
            continue
        if own_size is not None and (size < own_size or (size == own_size and mac >= own)):
            continue
        if (not local.get('discovery', False)
                and all(local['current'].get(b) == f for b, f in channels.items() if b in local['current'])):
            continue
        choices.append((-size, mac, channels))
    return min(choices)[2] if choices else None


class Runtime:
    def __init__(self):
        self.run_dir = Path(os.environ.get('MANET_ACS_RUN_DIR', '/run'))
        self.state_dir = Path(os.environ.get('MANET_ACS_STATE_DIR', '/var/lib/manet-acs'))
        self.roles = Path(os.environ.get('MANET_IFACE_STATE_DIR', '/var/lib'))
        self.wpa = Path(os.environ.get('MANET_WPA_DIR', '/etc/wpa_supplicant'))
        self.sysnet = Path(os.environ.get('MANET_SYS_NET', '/sys/class/net'))
        self.conf = Path(os.environ.get('MANET_MESH_CONF', '/etc/mesh.conf'))
        self.registry = Path(os.environ.get('REGISTRY_FILE', '/run/mesh_node_registry'))
        self.radio_state = Path(os.environ.get('MANET_RADIO_STATE_FILE', '/var/lib/mesh_radio_state.json'))
        self.lock_path = Path(os.environ.get('MANET_ACS_LOCK_FILE', '/run/channel-election.lock'))
        self.batctl = os.environ.get('BATCTL_PATH', '/usr/sbin/batctl')
        self.transport = AdminTransport(str(self.conf), os.environ.get('MANET_ADMIN_STATE_DIR', '/var/lib/manet-admin'))
        self.own = (self.sysnet / 'br0/address').read_text().strip().lower()
        self.boot = Path(os.environ.get('MANET_BOOT_ID_FILE', '/proc/sys/kernel/random/boot_id')).read_text().strip().replace('-', '')
        if not protocol.MAC.fullmatch(self.own) or not protocol.TOKEN.fullmatch(self.boot):
            raise ValueError('invalid local identity')
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / 'agreement.json'
        self.busy_path = self.run_dir / 'manet-acs-busy'
        self.request_path = self.run_dir / 'manet-acs-request.json'
        self.state = read_json(self.path)
        if not isinstance(self.state, dict):
            raise ValueError('invalid persisted agreement state')
        self.persisted = json.dumps(self.state, sort_keys=True)
        self.capabilities = {}
        self.last_publish = 0
        self.last_probe_check = 0
        self.discovery = rendezvous.Discovery(self.run_dir)
        self.rendezvous_allowed = {}
        self.halow_health = None

    @contextmanager
    def channel_lock(self):
        with self.lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def interfaces(self):
        desired = read_json(self.radio_state).get('desired', {})
        result = {}
        for band, role in [('2.4', 'mesh_24_if'), ('5', 'mesh_5_if')]:
            try:
                iface = (self.roles / role).read_text().strip()
            except FileNotFoundError:
                continue
            if not iface or desired.get(iface) == 'down':
                continue
            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', iface):
                raise ValueError('invalid mesh interface')
            path = self.wpa / f'wpa_supplicant-{iface}.conf'
            match = re.search(r'^\s*frequency=(\d+)\s*$', path.read_text(), re.M)
            if not match:
                raise ValueError('missing mesh frequency')
            result[band] = (iface, path, int(match[1]))
        if len({v[0] for v in result.values()}) != len(result):
            raise ValueError('one radio assigned to two bands')
        return result

    def status(self, now):
        acs = bool(re.search(r'^acs=["\']?[yY]["\']?\s*$', self.conf.read_text(), re.M))
        requested = read_json(self.request_path).get('round') == now // protocol.ROUND_SECONDS
        current, configured_channels, allowed, stable = {}, {}, {}, True
        for band, (iface, path, configured) in self.interfaces().items():
            info = command(['iw', 'dev', iface, 'info'], timeout=2)
            actual = re.search(r'channel.*\((\d+) MHz\)', info)
            phy = re.search(r'\bwiphy (\d+)', info)
            if not actual or not phy:
                raise ValueError(f'cannot read radio {iface}')
            current[band] = int(actual[1])
            configured_channels[band] = configured
            stable &= current[band] == configured
            key = (iface, band, phy[1])
            cached = self.capabilities.get(key)
            if cached is None or now - cached[0] >= 60:
                data = command(['iw', 'phy', 'phy' + phy[1], 'info'], timeout=2)
                cached = (now, sorted(rendezvous.permitted_frequencies(data)))
                self.capabilities[key] = cached
            allowed[band] = [f for f in cached[1] if f in protocol.CHANNELS[band]]
            self.rendezvous_allowed[band] = [f for f in cached[1] if f in rendezvous.CHANNELS[band]]
        cooling = now < self.state.get('hold_until', 0)
        if self.state.get('protocol', {}).get('phase') not in ('prepared', 'committed'):
            cooling |= self.busy(now)
        checked = time.monotonic()
        if self.halow_health is None or checked - self.halow_health[0] >= rendezvous.HALOW_HEALTH_SECONDS:
            self.halow_health = (checked, rendezvous.halow_ready())
        status = {'boot': self.boot, 'acs': acs, 'ready': bool(clock_ready() and stable and (not acs or not current or (requested and not cooling))),
                  'current': current, 'allowed': allowed, 'stable': stable,
                  'halow_ready': self.halow_health[1],
                  'discovery': self.discovery.mode(configured_channels) == 'search'}
        if not protocol.valid_status(status):
            raise ValueError('invalid local radio state')
        return status

    def busy(self, now):
        try:
            expiry = int(self.busy_path.read_text())
            return 0 <= expiry - now <= 125
        except (OSError, ValueError):
            return False

    def aliases(self):
        return sorted({p.read_text().strip().lower() for p in self.sysnet.glob('*/address')
                       if protocol.MAC.fullmatch(p.read_text().strip().lower())
                       and p.read_text().strip() != '00:00:00:00:00:00'})

    def receive(self, type_id, now):
        raw = command(['alfred', '-r', type_id], timeout=2)
        return authenticated_records(self.transport, type_id, raw, now)

    def tourguide_exclusions(self, now):
        # Existing status carries this flag; no separate discovery broadcast.
        # Local health is checked live by the caller, including before the hop.
        try:
            records = self.receive(74, now)
        except (OSError, subprocess.SubprocessError):
            return []
        return sorted(mac for mac, record in records.items() if mac != self.own
                      and protocol.valid_status(record.get('status'))
                      and record['status'].get('halow_ready') is True)

    def members(self, records):
        peers = command([sys.executable, TOOLS / 'mesh-peer-count.py', '--batctl', self.batctl, '--list'], timeout=7).split()
        registry = self.registry.read_text() if self.registry.exists() else ''
        return canonical_members(self.own, peers, registry, records)

    def score(self, view, local):
        # One coordinator freezes the score result. Receivers never re-score it.
        env = dict(os.environ)
        # Registry RF observations include every reachable node, regardless of
        # which radio carries Alfred. Exclude cached records of departed groups.
        env['ACS_MEMBERS'] = ' '.join('NODE_' + mac.replace(':', '') for mac in view)
        for band, suffix in [('2.4', '2_4'), ('5', '5_0')]:
            supported = sorted({f for s in view.values() if s and s['ready'] for f in s['allowed'].get(band, [])})
            incumbents = Counter(s['current'][band] for s in view.values() if s and band in s['current'])
            incumbent = min(incumbents, key=lambda f: (-incumbents[f], f)) if incumbents else ''
            env['ACS_CHANNELS_' + suffix] = ' '.join(map(str, supported))
            env['ACS_CURRENT_' + suffix] = str(incumbent)
        fd, path = tempfile.mkstemp(dir=self.run_dir, prefix='.acs-score-')
        os.close(fd)
        try:
            env['OUTPUT_FILE'] = path
            subprocess.run(['bash', str(TOOLS / 'channel-election.sh'), '--score'], env=env,
                           check=True, capture_output=True, text=True, timeout=15)
            values = dict(line.split('=', 1) for line in Path(path).read_text().splitlines())
            channels = {}
            for band, suffix in [('2.4', '2_4'), ('5', '5_0')]:
                value = values.get('WINNER_' + suffix, '')
                if value and int(value) in protocol.CHANNELS[band]:
                    channels[band] = int(value)
            if not channels:
                return None
            return channels, values.get('LIMP_MODE') == 'true'
        finally:
            Path(path).unlink(missing_ok=True)

    def save(self):
        serialized = json.dumps(self.state, sort_keys=True)
        if serialized != self.persisted:
            private_json_write(self.path, self.state)
            self.persisted = serialized

    def apply(self, channels, limp=False, *, challenged=False, discovering=False):
        # Clockless callers may follow a consumed nonce reply or park a
        # disconnected searcher at fixed anchors, never run a timed election.
        if not challenged and not discovering:
            require_clock()
        interfaces = self.interfaces()
        local = self.status(int(time.time()))
        if discovering:
            compatible = all(b in interfaces and f in self.rendezvous_allowed.get(b, [])
                             and (clock_ready() or f == rendezvous.ANCHORS[b]) for b, f in channels.items())
        else:
            compatible = protocol.compatible(channels, local)
        if not local['acs'] or not compatible:
            raise ValueError('channel plan is incompatible with this node')
        changes = []
        for band, freq in channels.items():
            if band not in interfaces:
                continue
            iface, path, configured = interfaces[band]
            if configured != freq:
                body, count = re.subn(r'(?m)^(\s*frequency=)\d+\s*$', lambda m: m[1] + str(freq), path.read_text())
                if count != 1:
                    raise ValueError('ambiguous WPA frequency')
                fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
                try:
                    with os.fdopen(fd, 'w') as out:
                        out.write(body)
                        out.flush()
                        os.fsync(out.fileno())
                    os.replace(temporary, path)
                finally:
                    Path(temporary).unlink(missing_ok=True)
            if configured != freq or local['current'][band] != freq:
                changes.append((iface, freq))
        # Write every config, then start every reconfigure before polling.
        for iface, freq in changes:
            command(['iw', 'dev', iface, 'set', 'bitrates'], timeout=2)
            try:
                answer = command(['wpa_cli', '-i', iface, 'reconfigure'], timeout=2)
                if 'FAIL' in answer:
                    raise ValueError('supplicant rejected reconfigure')
            except (subprocess.SubprocessError, ValueError):
                command(['systemctl', 'restart', f'wpa_supplicant@{iface}.service'], timeout=15)
        for iface, freq in changes:
            def landed():
                return bool(re.search(r'channel.*\(' + str(freq) + r' MHz\)', command(['iw', 'dev', iface, 'info'], timeout=2)))
            for attempt in range(20):
                if landed():
                    break
                time.sleep(.5)
            else:
                command(['systemctl', 'restart', f'wpa_supplicant@{iface}.service'], timeout=15)
                for attempt in range(20):
                    if landed():
                        break
                    time.sleep(.5)
                else:
                    raise ValueError(f'{iface} did not reach {freq}')
        self.discovery.set_mode('search' if discovering else 'data')
        if discovering:
            print('ACS discovery channels ' + json.dumps(channels), flush=True)
            return
        output = Path(os.environ.get('OUTPUT_FILE', str(self.run_dir / 'mesh_channel_election')))
        output.write_text(f"WINNER_2_4={channels.get('2.4', '')}\nWINNER_5_0={channels.get('5', '')}\nLIMP_MODE={str(limp).lower()}\n")
        if changes:
            (self.run_dir / 'mesh_limp_mode.state').unlink(missing_ok=True)
        print('ACS applied channels ' + json.dumps(channels), flush=True)

    def discovery_step(self, now, local, connected):
        # Any surviving BATMAN path, including HaLow, takes precedence. Hold
        # still while peers exchange a plan or bootstrap a new connected mesh.
        if not local['acs'] or not local['discovery'] or connected or self.busy(now):
            return False
        targets = {b: f for b, f in rendezvous.search_channels(now, clock_ready()).items()
                   if b in local['current'] and f in self.rendezvous_allowed.get(b, [])}
        if not targets or all(local['current'][b] == f for b, f in targets.items()):
            return False
        retry_path = self.run_dir / 'manet-rendezvous-retry.json'
        retry = read_json(retry_path)
        if retry.get('boot') == self.boot and time.monotonic() < retry.get('after', 0):
            return False
        private_json_write(retry_path, {'boot': self.boot, 'after': time.monotonic() + 30})
        self.apply(targets, discovering=True)
        return True

    def tick(self, now):
        if not clock_ready():
            with self.channel_lock():
                destination = self.bootstrap_select()
                if destination:
                    self.apply(destination, challenged=True)
                else:
                    local = self.status(now)
                    if local['acs'] and local['discovery']:
                        peers = command([sys.executable, TOOLS / 'mesh-peer-count.py', '--batctl', self.batctl, '--list'], timeout=7).split()
                        self.discovery_step(now, local, bool(peers))
            return  # Never persist rounds or poison the timed sender history.
        with self.channel_lock():
            if self.state.get('clock_boot') != self.boot:
                # A new boot has a new voting session. Its old wall clock may
                # have been ahead of GPS; don't inherit future rounds/holds.
                previous = self.state.get('destination')
                self.state = {'clock_boot': self.boot}
                # Re-attest only a validated plan actually operating after
                # reboot. Old votes, future rounds and cooldowns are discarded.
                if protocol.live_destination(previous, self.status(now), now):
                    self.state['destination'] = previous
                self.save()
                self.last_publish = 0
                self.capabilities.clear()
            local = self.status(now)
            try:
                records = self.receive(74, now)
            except (OSError, subprocess.SubprocessError):
                records = {}
            try:
                members = self.members(records)
                view = {mac: records.get(mac, {}).get('status') for mac in members}
                view[self.own] = local
                # Malformed/unavailable status remains in the denominator.
                view = {mac: s if protocol.valid_status(s) else None for mac, s in view.items()}
                protocol.validate_view(view)
                if local['discovery'] and len(view) == 1:
                    local['ready'] = False  # A searching solo node never elects itself out of discovery.
            except (OSError, ValueError, subprocess.SubprocessError):
                view = None
            now = int(time.time())  # Discovery can time out; never activate using its start time.
            destinations = self.connected_destinations(view, records, local, now)
            destinations = protocol.network_destinations(destinations, view or {})
            conflicting = protocol.conflicting_destinations(destinations.values())
            if conflicting and self.state.get('hold_until', 0) > now:
                # The recollection hold must not keep reunited
                # groups on different channels. Their next common scan/round
                # includes RF observations from the whole connected network.
                self.state.pop('hold_until', None)
                local = self.status(now)
                view[self.own] = local
            saved = self.state.get('protocol', {})
            proposal = None
            if (view and protocol.coordinator(view) == self.own
                    and protocol.PROPOSE_FROM <= now % protocol.ROUND_SECONDS <= protocol.PROPOSE_UNTIL
                    and (saved.get('round', -1) < now // protocol.ROUND_SECONDS or saved.get('phase') == 'idle')):
                scored = self.score(view, local)
                if scored:
                    now = int(time.time())
                    # The proposal window is validated again after slow scoring.
                    proposal = protocol.make_plan(view, *scored, now, secrets.token_hex(16))
            fields = {mac: r.get('protocol', {}) for mac, r in records.items() if isinstance(r.get('protocol', {}), dict)}
            updated, outgoing, plan = protocol.advance(saved, self.own, now, view, fields, proposal, local)
            self.state['protocol'] = updated
            if updated.get('phase') in ('prepared', 'committed'):
                self.busy_path.write_text(str(updated['plan']['activate_at'] + protocol.APPLY_GRACE))
            elif self.busy_path.exists():
                expiry = int(self.busy_path.read_text())
                if expiry < now or expiry - now > 125:
                    self.busy_path.unlink(missing_ok=True)
            if plan is not None and local['acs']:
                self.state['destination'] = {'plan': plan, 'commit': updated['commit']}
                self.state['recovered_round'] = plan['round']
                if plan['moving']:
                    self.state['hold_until'] = now + protocol.RECOVERY_SECONDS
                self.state['repair_until'] = now + 120
            elif plan is not None:
                plan = None  # A compatible static participant ACKs without applying ACS.
            self.save()  # Retry failed writes even if memory already contains the new state.
            # A live plan remains recoverable after its recollection
            # hold. Competing working plans require one new common election,
            # never independent "newest plan wins" decisions on each group.
            if (plan is None and view and local['acs'] and not self.busy(now)
                    and not conflicting and now >= self.state.get('retry_at', 0)
                    and updated.get('phase') not in ('prepared', 'committed')):
                candidates = []
                learned = []
                for mac, destination in destinations.items():
                    if mac == self.own:
                        continue
                    p = destination['plan']
                    if (not protocol.compatible(p['channels'], local)
                            or not set(p['channels']).intersection(local['current'])):
                        continue
                    entry = (p['round'], protocol.digest(p), destination)
                    if all(local['current'].get(b) == f for b, f in p['channels'].items() if b in local['current']):
                        learned.append(entry)
                    else:
                        candidates.append(entry)
                if candidates:
                    destination = max(candidates, key=lambda c: c[:2])[2]
                    plan = destination['plan']
                    self.state.update(destination=destination, recovered_round=plan['round'],
                                      hold_until=now + protocol.RECOVERY_SECONDS, repair_until=now + 120)
                    self.save()
                elif learned and self.own not in destinations:
                    # A node recovered by a clockless/lobby reply can now keep
                    # the agreed plan available even after its helper leaves.
                    self.state['destination'] = max(learned, key=lambda c: c[:2])[2]
                    self.save()
                    if local['discovery']:
                        # Rendezvous can coincide with the operating data
                        # channel: adopt its authority and stop searching,
                        # without a needless reconfigure or a later hop away.
                        plan = self.state['destination']['plan']
            if plan is None and now <= self.state.get('repair_until', -1) and now >= self.state.get('retry_at', 0):
                destination = self.state.get('destination')
                if destination:
                    target = destination['plan']['channels']
                    configured = {b: data[2] for b, data in self.interfaces().items()}
                    if (all(configured.get(b, f) == f for b, f in target.items())
                            and any(local['current'].get(b) != f for b, f in target.items() if b in local['current'])):
                        plan = destination['plan']
            if plan is not None:
                self.state['retry_at'] = now + 30
                self.save()
                moved = any(local['current'].get(b, f) != f for b, f in plan['channels'].items())
                self.apply(plan['channels'], plan['limp'])
                self.state.pop('repair_until', None)
                local = self.status(int(time.time()))
                if moved:
                    self.state['settle_until'] = int(time.time()) + 30
                    self.busy_path.write_text(str(self.state['settle_until']))
                self.save()
                outgoing = {}
            if (plan is None and view and not self.busy(now)
                    and updated.get('phase') not in ('prepared', 'committed')
                    and self.discovery_step(now, local, len(view) > 1)):
                local = self.status(int(time.time()))
            # No new gossip stream: current plans ride the existing type 74.
            # Readbacks while radios are moving cannot advertise a destination.
            destination = protocol.live_destination(self.state.get('destination'), local, int(time.time()))
            if outgoing != self.state.get('last_outgoing') or now - self.last_publish >= 5:
                payload = {'kind': 'acs_state', 'node': self.own, 'aliases': self.aliases(), 'status': local, 'protocol': outgoing}
                if destination:
                    plan_id = protocol.digest(destination['plan'])
                    payload['destination_id'] = plan_id
                    holders = {self.own}
                    for mac, status in (view or {}).items():
                        if (records.get(mac, {}).get('destination_id') == plan_id
                                and records.get(mac, {}).get('_fresh_until', now) >= now
                                and protocol.live_destination(destination, status, now)):
                            holders.add(mac)
                    # One full certificate per operating plan; other holders
                    # advertise only its ID. Stale/unreachable publishers drop
                    # out automatically. Avoid two 64-member certificates in
                    # one envelope during the next round's commit.
                    if self.own == min(holders) and 'commit' not in outgoing:
                        payload['destination'] = destination
                envelope = self.transport.seal(74, payload)
                command(['alfred', '-s', 74], input=json.dumps(envelope), timeout=2)
                self.state['last_outgoing'] = outgoing
                self.last_publish = now
            # Recovery requests arriving over HaLow need no physical tourguide
            # visit. One reachable, compatible source replies for each request.
            if view and destination and not conflicting and not self.busy(now) and now - self.last_probe_check >= 5:
                self.last_probe_check = now
                destinations[self.own] = destination
                self.answer_probes(destination['plan']['channels'], len(view), destinations)

    def connected_destinations(self, view, records, local, now):
        if view is None:
            return {}
        result = {}
        for mac in view:
            if mac != self.own and records.get(mac, {}).get('_fresh_until', now) < now:
                continue
            destination = self.state.get('destination') if mac == self.own else records.get(mac, {}).get('destination')
            status = local if mac == self.own else view[mac]
            if protocol.live_destination(destination, status, now):
                result[mac] = destination
        return result

    def bootstrap_select(self):
        """A cold node can follow a live reachable helper without knowing UTC."""
        with (self.run_dir / '.acs-probe.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            local = self.status(int(time.time()))
            if clock_ready() or not local['acs'] or not local['current']:
                return None
            path = self.run_dir / 'manet-acs-probe.json'
            probe = read_json(path)
            now = time.monotonic()
            if probe.get('boot') != self.boot:
                probe = {}
            if (probe.get('nonce') and not probe.get('used')
                    and 0 <= now - probe['started'] < PROBE_LIFETIME):
                raw = command(['alfred', '-r', 77], timeout=2)
                records = challenge_records(self.transport, 77, raw)
                expected = {'boot': self.boot, 'nonce': probe['nonce']}
                records = {mac: r for mac, r in records.items()
                           if isinstance(r.get('answers'), dict) and r['answers'].get(self.own) == expected}
                destination = recovery_destination(records, local, 0, self.own)
                if destination and time.monotonic() < probe['started'] + PROBE_LIFETIME:
                    probe['used'] = True
                    private_json_write(path, probe)  # Consume before returning an actionable destination.
                    return destination
            if now < probe.get('next_probe', 0):
                return None
            # No peers means no one can answer. This query is local to BATMAN.
            peers = command([sys.executable, TOOLS / 'mesh-peer-count.py', '--batctl', self.batctl, '--list'], timeout=7).split()
            if not peers:
                return None
            now = time.monotonic()
            probe = {'boot': self.boot, 'nonce': secrets.token_hex(16), 'started': now,
                     'next_probe': now + PROBE_INTERVAL, 'used': False}
            envelope = self.transport.seal_challenge(76, {'kind': 'acs_probe', 'node': self.own,
                'boot': self.boot, 'nonce': probe['nonce'], 'aliases': self.aliases(),
                'current': local['current'], 'discovery': local['discovery']})
            private_json_write(path, probe)
            command(['alfred', '-s', 76], input=json.dumps(envelope), timeout=2)
            return None

    def answer_probes(self, channels, size, sources=None):
        """Answer on a surviving mesh path, or during a tourguide visit."""
        require_clock()
        path = self.run_dir / 'manet-acs-probe-replies.json'
        saved = read_json(path)
        now = time.monotonic()
        if saved.get('boot') == self.boot and now < saved.get('next_reply', 0):
            return
        records = challenge_records(self.transport, 76, command(['alfred', '-r', 76], timeout=2))
        if not records:
            return
        peers = set(command([sys.executable, TOOLS / 'mesh-peer-count.py', '--batctl', self.batctl, '--list'], timeout=7).split())
        previous = saved.get('answered', {}) if saved.get('boot') == self.boot else {}
        answered = {key: stamp for key, stamp in previous.items() if 0 <= now - stamp < 600}
        answers = {}
        for mac, record in sorted(records.items()):
            nonce, aliases = record.get('nonce'), record.get('aliases')
            if (mac == self.own or not isinstance(nonce, str) or not protocol.TOKEN.fullmatch(nonce)
                    or not isinstance(aliases, list) or len(aliases) > 32
                    or any(not isinstance(a, str) or not protocol.MAC.fullmatch(a) for a in aliases)
                    or not peers.intersection([mac, *aliases])):
                continue
            if sources is not None:
                current = record.get('current')
                if not isinstance(current, dict) or not current:
                    continue
                eligible = [source for source, target in sources.items()
                            if any(b in current and (current[b] != f or record.get('discovery') is True)
                                   for b, f in target['plan']['channels'].items())]
                if not eligible or self.own != min(eligible):
                    continue
            key = mac + '/' + record['boot'] + '/' + nonce
            if key in answered:
                continue
            answers[mac] = {'boot': record['boot'], 'nonce': nonce}
            answered[key] = now
            if len(answers) >= protocol.MAX_MEMBERS:
                break
        if not answers:
            return
        envelope = self.transport.seal_challenge(77, {'kind': 'acs_probe_reply', 'node': self.own,
            'boot': self.boot, 'channels': channels, 'size': size, 'answers': answers})
        # Bound replay-triggered traffic and saved state. A probe has no command.
        answered = dict(sorted(answered.items(), key=lambda item: item[1], reverse=True)[:640])
        private_json_write(path, {'boot': self.boot, 'answered': answered, 'next_reply': now + 5})
        command(['alfred', '-s', 77], input=json.dumps(envelope), timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'request', 'members', 'helper-encode', 'helper-select', 'tourguide-exclusions'))
    parser.add_argument('--channels', default='{}', type=json.loads)
    parser.add_argument('--size', type=int)
    parser.add_argument('--exclude', default='')
    parser.add_argument('--stdin', action='store_true')
    parser.add_argument('--current', type=json.loads)
    args = parser.parse_args()
    if args.action == 'request':
        directory = Path(os.environ.get('MANET_ACS_RUN_DIR', '/run'))
        if not clock_ready():
            (directory / 'manet-acs-request.json').unlink(missing_ok=True)
            return 0
        private_json_write(directory / 'manet-acs-request.json', {'round': int(time.time()) // protocol.ROUND_SECONDS})
        return 0
    runtime = Runtime()
    now = int(time.time())
    if args.action == 'tourguide-exclusions':
        require_clock()
        print(' '.join(runtime.tourguide_exclusions(now)))
    elif args.action == 'members':
        print(' '.join(runtime.members({})))
    elif args.action == 'helper-encode':
        require_clock()
        if (type(args.size) is not int or not 1 <= args.size <= protocol.MAX_MEMBERS or not args.channels
                or any(b not in protocol.CHANNELS or f not in protocol.CHANNELS[b] for b, f in args.channels.items())):
            raise ValueError('invalid helper destination')
        payload = {'kind': 'acs_helper', 'node': runtime.own, 'channels': args.channels, 'size': args.size}
        print(json.dumps(runtime.transport.seal(75, payload)))
        try:
            runtime.answer_probes(args.channels, args.size)
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
            print('ACS bootstrap reply deferred: ' + str(exc), file=sys.stderr)
    elif args.action == 'helper-select':
        if not clock_ready():
            destination = runtime.bootstrap_select() if args.current is None and not args.stdin else None
            if destination:
                print(str(destination.get('2.4', '')) + '|' + str(destination.get('5', '')))
            return 0
        records = authenticated_records(runtime.transport, 75, sys.stdin.read(), now) if args.stdin else runtime.receive(75, now)
        local = runtime.status(now)
        if args.current is not None:
            local['current'] = args.current  # Pre-hop channels, still checked against local capabilities.
            local['discovery'] = False  # Comparing established partitions, not adopting a search channel.
        destination = recovery_destination(records, local, now,
                                           runtime.own, args.exclude.split(), args.size)
        if destination:
            print(str(destination.get('2.4', '')) + '|' + str(destination.get('5', '')))
    elif args.action == 'run':
        with (runtime.state_dir / '.daemon.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            last_error = None
            while True:
                try:
                    runtime.tick(int(time.time()))
                    last_error = None
                except BlockingIOError:
                    pass  # Tourguide or another local radio operation owns the lock.
                except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
                    message = str(exc)
                    if message != last_error:
                        print('ACS deferred: ' + message, file=sys.stderr, flush=True)
                    last_error = message
                time.sleep(1)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        print('ACS unavailable: ' + str(exc), file=sys.stderr)
        sys.exit(1)
