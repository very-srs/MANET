#!/usr/bin/env python3
"""One ACS decision domain across every BATMAN radio, with live recovery."""
import argparse
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import time

from manet_admin import AdminTransport, clock_ready, private_json_write, require_clock
from manet_radio import acs_enabled
import manet_acs_agreement as protocol
import manet_rendezvous as rendezvous

TOOLS = Path(__file__).resolve().parent
# Maximum age in wall-clock seconds for authenticated type 74/75 records.
FRESH_SECONDS = 45
PROBE_LIFETIME = 60  # Request/reply replication plus a 15-second manager wakeup.
# Minimum monotonic seconds between new clockless recovery probes.
PROBE_INTERVAL = 60
# Pass spacing in seconds. Default passes match the 5-second publish
# cadence. One-second passes, as in a fixed one-second loop, cover the
# proposal window and a live plan from GUARD_SECONDS before its vote deadline
# through its apply window: last votes, commit, commit pickup and apply.
IDLE_INTERVAL = 5
ACTIVE_INTERVAL = 1
GUARD_SECONDS = 5
WAKE_MARGIN = 0.05  # Lands a pass after a whole-second boundary, not before.
# Local drift needs sustained idle observations; attempts use boot seconds.
DRIFT_GRACE = 60
DRIFT_BACKOFF = 300
# Boot-clock deadlines and their plan identity survive same-boot restarts.
LOCAL_TIMERS = ('hold_until', 'repair_until', 'retry_at', 'settle_until',
                'busy_until', 'busy_plan', 'drift_retry_at')
# Alfred rows pair a sender MAC with a quoted, escaped JSON payload.
RECORD = re.compile(r'\{\s*"([0-9a-fA-F:]{17})"\s*,\s*("(?:\\.|[^"\\])*")\s*\}')


def command(args, timeout=5, input=None):
    """Return command stdout, raising on failure or timeout in seconds."""
    return subprocess.run([str(a) for a in args], input=input, capture_output=True,
                          text=True, check=True, timeout=timeout).stdout


def read_json(path, default=None):
    """Read JSON, using the default or an empty dict only for missing files."""
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {} if default is None else default


def authenticated_records(transport, type_id, raw, now):
    """Authenticate fresh Alfred records and keep the newest per sender."""
    from cryptography.exceptions import InvalidTag
    result, orders = {}, {}
    for match in RECORD.finditer(raw):
        mac = match[1].lower()
        try:
            # Unquote Alfred's string, then decode the encrypted envelope.
            message = transport.open(type_id, json.loads(json.loads(match[2])))
            payload = message.payload
            # Bind the payload to its Alfred sender. Allow 5 s of future skew
            # and order duplicates by nanosecond timestamp, then message ID.
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
            # Decode the same Alfred string wrapper as timed records. The
            # caller checks the outstanding nonce or bounds reply traffic.
            payload = transport.open_challenge(type_id, json.loads(json.loads(match[2]))).payload
            if (payload.get('node') == mac and isinstance(payload.get('boot'), str)
                    and protocol.TOKEN.fullmatch(payload['boot'])):
                result[mac] = payload
        except (ValueError, TypeError, KeyError, InvalidTag):
            continue
    return result


def canonical_members(own, peers, registry, records):
    """Map reachable radio MACs to unique node identities, rejecting gaps."""
    aliases = {own: own}
    def add(alias, mac):
        """Bind a valid alias to one canonical node or reject ambiguity."""
        if not protocol.MAC.fullmatch(alias) or aliases.get(alias, mac) != mac:
            raise ValueError('ambiguous participant identity')
        aliases[alias] = mac
    for line in registry.splitlines():
        # Registry keys omit MAC colons. Shell quoting wraps one comma list.
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
        # Authenticated status supplies aliases before the registry catches up.
        for alias in [mac, *record.get('aliases', [])]:
            add(alias, mac)
    if any(peer not in aliases for peer in peers):
        raise ValueError('reachable originator lacks a canonical identity')
    return sorted({own, *(aliases[p] for p in peers)})


def recovery_destination(records, local, own, excluded=(), own_size=None):
    """Choose a helper by size then MAC, ignoring expired beacons."""
    # Callers establish freshness through timed authentication or a live nonce.
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
        # A tourguide compares its pre-hop partition. Equal sizes move only
        # toward the lower MAC so both partitions cannot swap channels.
        if own_size is not None and (size < own_size or (size == own_size and mac >= own)):
            continue
        # Matching channels still let a searcher adopt the helper's data mode.
        if (not local.get('discovery', False)
                and all(local['current'].get(b) == f for b, f in channels.items() if b in local['current'])):
            continue
        choices.append((-size, mac, channels))
    # Negative size puts the largest partition first, then the lowest MAC.
    return min(choices)[2] if choices else None


def write_frequency(path, freq):
    """Atomically replace the one frequency assignment in a WPA config."""
    # Require exactly one frequency assignment. Replace the config
    # atomically from a flushed temporary file beside it.
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


def wake_daemon(directory):
    """Ask the run loop for an immediate pass so readiness publishes at once.

    A missing, stale or reused PID is ignored. The next timed pass then
    reads the request instead.
    """
    try:
        pid = int((Path(directory) / 'manet-acs-agreement.pid').read_text())
        # The pidfd pins the checked process: if it exits, the signal fails
        # rather than reaching a process that reused the PID.
        handle = os.pidfd_open(pid)
    except (OSError, ValueError):
        return
    try:
        # Signal only the run daemon. SIGUSR1 would end any other process.
        if Path(f'/proc/{pid}/cmdline').read_bytes().endswith(b'/mesh-channel-agreement.py\0run\0'):
            signal.pidfd_send_signal(handle, signal.SIGUSR1)
    except OSError:
        pass
    finally:
        os.close(handle)


def boot_time():
    """Return elapsed boot seconds, including suspend, for local deadlines."""
    return time.clock_gettime(time.CLOCK_BOOTTIME)


class Runtime:
    """Connect ACS agreement and recovery to radios, Alfred and local state."""
    def __init__(self):
        """Load paths and identity, reconcile boot timers and start caches."""
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
        # The bridge MAC identifies the node across radios. The boot UUID is
        # a 32-hex token on the wire and distinguishes local timer epochs.
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
        if self.state and self.state.get('timer_boot') != self.boot:
            try:
                with self.channel_lock():
                    # Helper calls also construct Runtime. Preserve newer daemon
                    # state when reconciling the first reader after reboot.
                    self.state = read_json(self.path)
                    self.persisted = json.dumps(self.state, sort_keys=True)
                    self.reset_timers()
                    self.save()
            except BlockingIOError:
                # The lock may belong to the caller: tourguide runs
                # members/helper-encode under it. Reconcile in memory and let
                # the daemon save on its next pass.
                self.reset_timers()
        else:
            self.reset_timers()
        self.capabilities = {}
        self.last_publish = None
        self.last_status = None
        self.last_probe_check = None
        self.discovery = rendezvous.Discovery(self.run_dir)
        self.rendezvous_allowed = {}
        self.halow_health = None
        self.ui_status = {}
        self.drift_since = {}

    def reset_timers(self):
        """Discard deadlines from another boot, restarting any recovery hold."""
        if not isinstance(self.state, dict):
            raise ValueError('invalid persisted agreement state')
        if self.state.get('timer_boot') != self.boot:
            held = self.state.get('hold_until', 0) > 0
            for key in LOCAL_TIMERS:
                self.state.pop(key, None)
            self.state['timer_boot'] = self.boot
            # Unknown-boot holds restart in full. Drop all other local timers.
            if held:
                self.state['hold_until'] = boot_time() + protocol.RECOVERY_SECONDS

    @contextmanager
    def channel_lock(self):
        """Hold the shared radio-operation lock or raise immediately if busy."""
        with self.lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def interfaces(self):
        """Return enabled Wi-Fi roles with config paths and configured MHz."""
        desired = read_json(self.radio_state).get('desired', {})
        result = {}
        for band, role in [('2.4', 'mesh_24_if'), ('5', 'mesh_5_if')]:
            try:
                iface = (self.roles / role).read_text().strip()
            except FileNotFoundError:
                continue
            if not iface or desired.get(iface) == 'down':
                continue
            # Limit the role to a Linux interface name before using it in paths.
            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', iface):
                raise ValueError('invalid mesh interface')
            path = self.wpa / f'wpa_supplicant-{iface}.conf'
            # Read a complete numeric frequency assignment, allowing whitespace.
            match = re.search(r'^\s*frequency=(\d+)\s*$', path.read_text(), re.M)
            if not match:
                raise ValueError('missing mesh frequency')
            result[band] = (iface, path, int(match[1]))
        if len({v[0] for v in result.values()}) != len(result):
            raise ValueError('one radio assigned to two bands')
        return result

    def parked(self):
        """Return (band, config) for an AP radio withheld from its mesh band.

        In auto EUD mode a gateway serves the AP on a radio provisioned for a
        mesh band. Its supplicant config follows agreed plans while parked,
        so the radio rejoins on the current channel when it returns.
        """
        try:
            iface = (self.roles / 'ap_interface').read_text().strip()
            band = (self.roles / 'ap_mesh_band').read_text().strip()
        except FileNotFoundError:
            return None
        role = {'2.4': 'mesh_24_if', '5': 'mesh_5_if'}.get(band)
        if not role or not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', iface):
            return None
        try:
            owner = (self.roles / role).read_text().strip()
        except FileNotFoundError:
            owner = ''
        path = self.wpa / f'wpa_supplicant-{iface}.conf'
        # A radio holding its mesh role is updated by the normal apply path.
        if owner or not path.exists():
            return None
        return band, path

    def sync_parked(self, channels):
        """Write an agreed band frequency into a parked AP radio's config.

        Callers hold the channel lock and pass authenticated plan channels.
        The radio itself is not touched. Its return still selects from the
        authenticated destination, so this file is a record, not authority.
        """
        if not isinstance(channels, dict):
            return
        parked = self.parked()
        if not parked or parked[0] not in channels:
            return
        band, path = parked
        freq = channels[band]
        if type(freq) is not int or freq not in protocol.CHANNELS[band]:
            return
        saved = re.search(r'^\s*frequency=(\d+)\s*$', path.read_text(), re.M)
        if not saved or int(saved[1]) != freq:
            write_frequency(path, freq)
            print(f'ACS updated parked {band} GHz config to {freq} MHz', flush=True)

    def status(self, now):
        """Collect radio MHz, capabilities and readiness at wall time now."""
        acs = acs_enabled(self.conf)
        requested = read_json(self.request_path).get('round') == now // protocol.ROUND_SECONDS
        current, configured_channels, allowed, stable = {}, {}, {}, True
        for band, (iface, path, configured) in self.interfaces().items():
            # Bound iw queries to 2 s. The channel line carries actual MHz,
            # while wiphy identifies the physical radio for capability lookup.
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
            checked = time.monotonic()
            # Refresh PHY capabilities once per monotonic minute. Live tuning
            # is still read each pass so a pending retune cannot look stable.
            if cached is None or checked - cached[0] >= 60:
                data = command(['iw', 'phy', 'phy' + phy[1], 'info'], timeout=2)
                cached = (checked, sorted(rendezvous.permitted_frequencies(data)))
                self.capabilities[key] = cached
            # Election candidates and rendezvous channels use different sets.
            allowed[band] = [f for f in cached[1] if f in protocol.CHANNELS[band]]
            self.rendezvous_allowed[band] = [f for f in cached[1] if f in rendezvous.CHANNELS[band]]
        cooling = boot_time() < self.state.get('hold_until', 0)
        # A plan's own busy marker must not withdraw readiness mid-agreement.
        if self.state.get('protocol', {}).get('phase') not in ('prepared', 'committed'):
            cooling |= self.busy()
        checked = time.monotonic()
        # Cache HaLow liveness for 15 monotonic seconds between probes.
        if self.halow_health is None or checked - self.halow_health[0] >= rendezvous.HALOW_HEALTH_SECONDS:
            self.halow_health = (checked, rendezvous.halow_ready())
        # Static and Wi-Fi-free nodes can vote without a manager request.
        # Every voter still needs time sync and stable radio readback.
        status = {'boot': self.boot, 'acs': acs, 'ready': bool(clock_ready() and stable and (not acs or not current or (requested and not cooling))),
                  'current': current, 'allowed': allowed, 'stable': stable,
                  'halow_ready': self.halow_health[1],
                  'discovery': self.discovery.mode(configured_channels) == 'search'}
        if not protocol.valid_status(status):
            raise ValueError('invalid local radio state')
        self.ui_status.update(current=current, halow_ready=status['halow_ready'],
                              searching=status['discovery'])
        return status

    def busy(self):
        """Check the shared busy marker's boot and bounded expiry."""
        try:
            boot, expiry = self.busy_path.read_text().split()
            # Match mesh-acs-common.sh: at most 125 boot seconds may remain.
            return boot == self.boot and 0 <= float(expiry) - boot_time() <= 125
        except (OSError, ValueError):
            return False

    def write_busy(self, expiry):
        """Publish a boot ID and boot-clock deadline for other radio workers."""
        self.busy_path.write_text(f'{self.boot} {expiry:.6f}\n')

    def aliases(self):
        """Return unique nonzero local MACs for identity mapping."""
        values = {p.read_text().strip().lower() for p in self.sysnet.glob('*/address')}
        return sorted(mac for mac in values if protocol.MAC.fullmatch(mac) and mac != '00:00:00:00:00:00')

    def receive(self, type_id, now):
        """Read timed Alfred records, reusing unchanged verified content."""
        raw = command(['alfred', '-r', type_id], timeout=2)
        require_clock()
        # Invalidate cached authentication on credential replacement or edits.
        info = self.conf.stat()
        generation = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
        cached = getattr(self, '_received', {}).get(type_id)
        if cached and cached[:2] == (raw, generation) and now >= cached[2]:
            # Cached envelopes match this credential generation. Check freshness
            # each tick, including forward clock steps. Reauthenticate if the
            # clock moves before verification time.
            return deepcopy({mac: record for mac, record in cached[3].items()
                             if now <= record['_fresh_until']})
        records = authenticated_records(self.transport, type_id, raw, now)
        # Never cache a rejection: a future-dated or rate-limited envelope may
        # become acceptable later. Duplicate senders also take the normal path.
        if len(records) == len(RECORD.findall(raw)):
            self._received = {type_id: (raw, generation, now, deepcopy(records))}
        else:
            self._received = {}
        return records

    def tourguide_exclusions(self, now):
        """List fresh peers reporting healthy HaLow for tourguide exclusion."""
        # Existing status carries this flag without a separate broadcast.
        # The caller checks local health live, including before the hop.
        try:
            records = self.receive(74, now)
        except (OSError, subprocess.SubprocessError):
            return []
        return sorted(mac for mac, record in records.items() if mac != self.own
                      and protocol.valid_status(record.get('status'))
                      and record['status'].get('halow_ready') is True)

    def members(self, records):
        """Discover canonical participants across every BATMAN radio path."""
        # Multiple routes can share an originator. Validate before deduplication
        # so a failed or malformed query cannot imply a solo component.
        rows = json.loads(command([self.batctl, 'meshif', 'bat0', 'originators_json']))
        if not isinstance(rows, list) or any(not isinstance(r, dict) or
                not isinstance(r.get('orig_address'), str) or
                not protocol.MAC.fullmatch(r['orig_address'].lower()) for r in rows):
            raise ValueError('invalid originator table')
        peers = sorted({r['orig_address'].lower() for r in rows})
        self.ui_status['route_interfaces'] = sorted({r['hard_ifname'] for r in rows
            if r.get('best') is True and isinstance(r.get('hard_ifname'), str)})
        registry = self.registry.read_text() if self.registry.exists() else ''
        return canonical_members(self.own, peers, registry, records)

    def score(self, view):
        """Score the connected view and return per-band MHz plus limp mode."""
        # One coordinator freezes the score result. Receivers never re-score it.
        env = dict(os.environ)
        # Registry RF observations include every reachable node, regardless of
        # which radio carries Alfred. Exclude cached records of departed groups.
        env['ACS_MEMBERS'] = ' '.join('NODE_' + mac.replace(':', '') for mac in view)
        for band, suffix in [('2.4', '2_4'), ('5', '5_0')]:
            # Candidate support is the union from ready nodes. The common
            # incumbent is the most reported MHz, breaking ties by lower MHz.
            supported = sorted({f for s in view.values() if s and s['ready'] for f in s['allowed'].get(band, [])})
            incumbents = Counter(s['current'][band] for s in view.values() if s and band in s['current'])
            incumbent = min(incumbents, key=lambda f: (-incumbents[f], f)) if incumbents else ''
            env['ACS_CHANNELS_' + suffix] = ' '.join(map(str, supported))
            env['ACS_CURRENT_' + suffix] = str(incumbent)
        fd, path = tempfile.mkstemp(dir=self.run_dir, prefix='.acs-score-')
        os.close(fd)
        try:
            # --score writes WINNER_2_4, WINNER_5_0 and LIMP_MODE assignments
            # to this private output path. Bound the scoring process to 15 s.
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
        """Atomically save changed state and remember successful writes."""
        serialized = json.dumps(self.state, sort_keys=True)
        if serialized != self.persisted:
            private_json_write(self.path, self.state)
            self.persisted = serialized

    def apply(self, channels, limp=False, *, challenged=False, discovering=False, reason='Agreed channel plan'):
        """Apply compatible Wi-Fi MHz, verify landing and publish the result."""
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
                write_frequency(path, freq)
            if configured != freq or local['current'][band] != freq:
                changes.append((iface, freq))
        # Write every config, then start every reconfigure before polling.
        for iface, freq in changes:
            # Clear bitrate restrictions before reconfiguration.
            command(['iw', 'dev', iface, 'set', 'bitrates'], timeout=2)
            try:
                # A rejected or timed-out reconfigure triggers a restart,
                # bounded to 15 s by the fallback command below.
                answer = command(['wpa_cli', '-i', iface, 'reconfigure'], timeout=2)
                if 'FAIL' in answer:
                    raise ValueError('supplicant rejected reconfigure')
            except (subprocess.SubprocessError, ValueError):
                command(['systemctl', 'restart', f'wpa_supplicant@{iface}.service'], timeout=15)
        for iface, freq in changes:
            def landed():
                """Match the target MHz in this interface's iw readback."""
                return bool(re.search(r'channel.*\(' + str(freq) + r' MHz\)', command(['iw', 'dev', iface, 'info'], timeout=2)))
            # Allow 20 reads with 0.5 s sleeps, then restart and repeat once.
            # Command time adds to the 10 s sleep budget of each attempt set.
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
        # Discovery anchors are not the mesh's data channel: never park them.
        if not discovering:
            self.sync_parked(channels)
        self.discovery.set_mode('search' if discovering else 'data')
        if changes:
            event = {'at': int(time.time()), 'monotonic': time.monotonic(), 'channels': channels,
                     'reason': ('Lobby rotation' if discovering else
                                'Clockless peer recovery' if challenged else reason)}
            try:
                private_json_write(self.run_dir / 'manet-last-channel-change.json', event)
            except OSError:
                pass  # A display failure must not interrupt channel recovery.

        if discovering:
            print('ACS discovery channels ' + json.dumps(channels), flush=True)
            return
        # Publish the shell consumers' election format after verified landing.
        output = Path(os.environ.get('OUTPUT_FILE', str(self.run_dir / 'mesh_channel_election')))
        output.write_text(f"WINNER_2_4={channels.get('2.4', '')}\nWINNER_5_0={channels.get('5', '')}\nLIMP_MODE={str(limp).lower()}\n")
        if changes:
            # Retuning clears bitrate restrictions, invalidating this state.
            (self.run_dir / 'mesh_limp_mode.state').unlink(missing_ok=True)
        print('ACS applied channels ' + json.dumps(channels), flush=True)

    def discovery_step(self, now, local, connected):
        """Move an isolated searcher to its permitted lobby slot when due."""
        # Any surviving BATMAN path, including HaLow, takes precedence. Hold
        # still while peers exchange a plan or bootstrap a new connected mesh.
        if not local['acs'] or not local['discovery'] or connected or self.busy():
            return False
        targets = {b: f for b, f in rendezvous.search_channels(now, clock_ready()).items()
                   if b in local['current'] and f in self.rendezvous_allowed.get(b, [])}
        if not targets or all(local['current'][b] == f for b, f in targets.items()):
            return False
        retry_path = self.run_dir / 'manet-rendezvous-retry.json'
        retry = read_json(retry_path)
        if retry.get('boot') == self.boot and time.monotonic() < retry.get('after', 0):
            return False
        # Persist a 30 s retry delay before touching radios, including failures.
        private_json_write(retry_path, {'boot': self.boot, 'after': time.monotonic() + 30})
        self.apply(targets, discovering=True)
        return True

    def repair_drift(self, local):
        """Reconfigure sustained idle drift once per radio backoff interval.

        The caller holds the channel lock, including against tourguide visits.
        Configured MHz remains the target; this does not select a new plan.
        """
        elapsed = boot_time()
        phase = self.state.get('protocol', {}).get('phase')
        if (local['discovery'] or self.busy()
                or phase in ('prepared', 'committed')
                or elapsed <= self.state.get('repair_until', -1)
                or elapsed < self.state.get('settle_until', 0)):
            self.drift_since.clear()
            return False
        drift = {}
        for band, (iface, path, target) in self.interfaces().items():
            actual = local['current'].get(band)
            if actual is None or actual == target:
                continue
            previous, since = self.drift_since.get(iface, (target, elapsed))
            drift[iface] = (target, since if previous == target else elapsed)
        self.drift_since = drift
        for iface, (target, since) in list(drift.items()):
            retry = self.state.get('drift_retry_at', {}).get(iface, 0)
            if elapsed - since < DRIFT_GRACE or elapsed < retry:
                continue
            # Recheck live MHz after potentially slow peer/protocol work.
            info = command(['iw', 'dev', iface, 'info'], timeout=2)
            actual = re.search(r'channel.*\((\d+) MHz\)', info)
            if not actual or int(actual[1]) == target:
                self.drift_since.pop(iface, None)
                continue
            # Persist before the command, including failed/timed-out attempts.
            self.state.setdefault('drift_retry_at', {})[iface] = (
                boot_time() + DRIFT_BACKOFF)
            self.state['settle_until'] = boot_time() + 30
            self.save()
            self.write_busy(self.state['settle_until'])
            print(f'ACS drift repair: {iface} at {actual[1]} MHz, '
                  f'configured {target} MHz; reconfigure, '
                  f'backoff {DRIFT_BACKOFF}s', file=sys.stderr, flush=True)
            try:
                answer = command(['wpa_cli', '-i', iface, 'reconfigure'],
                                 timeout=2)
                if 'OK' not in answer.splitlines():
                    raise ValueError('supplicant rejected reconfigure')
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                print(f'ACS drift repair: {iface} failed: {exc}',
                      file=sys.stderr, flush=True)
            # Normal status reads verify landing; never loop or restart here.
            self.drift_since.clear()
            return True
        return False

    def tick(self, now):
        """Run one pass and publish a UI snapshot even when radio work fails."""
        # A tick that started clockless may have applied a recovery reply
        # that is newer than the saved timed destination.
        timed = clock_ready()
        self.ui_status = {'clock_ready': timed}
        try:
            self._tick(now)
            # Recovery can adopt a destination without retuning any radio.
            # Clockless recovery applies its own reply; the saved timed
            # destination may be older than that and must not overwrite it.
            destination = self.state.get('destination')
            if (timed and clock_ready() and isinstance(destination, dict)
                    and isinstance(destination.get('plan'), dict)):
                try:
                    with self.channel_lock():
                        self.sync_parked(destination['plan'].get('channels', {}))
                except BlockingIOError:
                    pass  # A radio operation holds the lock; retry next tick.
        except Exception:
            # A lock holder or failed observation breaks the idle grace period.
            self.drift_since.clear()
            self.ui_status['error'] = 'Channel recovery could not read or apply radio state'
            raise
        finally:
            saved = self.state.get('protocol', {})
            plan = saved.get('plan', {})
            hold_left = max(0, self.state.get('hold_until', 0) - boot_time())
            self.ui_status.update(monotonic=time.monotonic(), phase=saved.get('phase', 'idle'),
                                  target=self.state.get('destination', {}).get('plan', {}).get('channels', {}),
                                  pending_channels=plan.get('channels', {}),
                                  votes=len(saved.get('votes', {})), participants=len(plan.get('participants', {})),
                                  deadline=plan.get('deadline'), activate_at=plan.get('activate_at'),
                                  # The existing UI expects a wall-clock projection.
                                  hold_until=time.time() + hold_left if hold_left else 0)
            try:
                private_json_write(self.run_dir / 'manet-acs-status.json', self.ui_status)
            except OSError:
                pass

    def _tick(self, now):
        """Advance recovery or timed agreement under the channel lock."""
        if not clock_ready():
            with self.channel_lock():
                local = self.status(now)
                destination = self.bootstrap_select(local)
                if destination:
                    self.drift_since.clear()
                    self.apply(destination, challenged=True)
                else:
                    if local['acs'] and local['discovery']:
                        peers = command([str(TOOLS / 'mesh-peer-count.sh'), '--batctl', self.batctl, '--list'], timeout=7).split()
                        self.ui_status['reachable'] = len(peers)
                        self.discovery_step(now, local, bool(peers))
                    self.repair_drift(local)
            return  # Never persist rounds or poison the timed sender history.
        with self.channel_lock():
            if self.state.get('clock_boot') != self.boot:
                # A new boot starts a voting session. Drop rounds from its old
                # clock, which may have been ahead of GPS.
                previous = self.state.get('destination')
                timers = {key: self.state[key] for key in LOCAL_TIMERS if key in self.state}
                self.state = dict(timers, clock_boot=self.boot, timer_boot=self.boot)
                # Re-attest only a validated plan operating after boot-timer
                # reconciliation.
                if protocol.live_destination(previous, self.status(now), now):
                    self.state['destination'] = previous
                self.save()
                self.last_publish = None
                self.capabilities.clear()
            local = self.status(now)
            try:
                records = self.receive(74, now)
            except (OSError, subprocess.SubprocessError):
                # Peer discovery still determines membership without status.
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
                # Unknown membership defers new proposals, never implies solo.
                view = None
            now = int(time.time())  # Discovery may time out. Refresh time.
            destinations = self.connected_destinations(view, records, local, now)
            destinations = protocol.network_destinations(destinations, view or {})
            conflicting = protocol.conflicting_destinations(destinations.values())
            self.ui_status.update(reachable=len(view) - 1 if view else None, conflicting=conflicting)
            if conflicting and self.state.get('hold_until', 0) > boot_time():
                # Lift the recollection hold when groups reunite on different
                # channels. Their next shared scan/round includes the whole
                # connected network.
                self.state.pop('hold_until', None)
                local = self.status(now)
                view[self.own] = local
            saved = self.state.get('protocol', {})
            proposal = None
            if (view and protocol.coordinator(view) == self.own
                    and protocol.PROPOSE_FROM <= now % protocol.ROUND_SECONDS <= protocol.PROPOSE_UNTIL
                    and (saved.get('round', -1) < now // protocol.ROUND_SECONDS or saved.get('phase') == 'idle')):
                scored = self.score(view)
                if scored:
                    now = int(time.time())
                    # The proposal window is validated again after slow scoring.
                    proposal = protocol.make_plan(view, *scored, now, secrets.token_hex(16))
            fields = {mac: r.get('protocol', {}) for mac, r in records.items() if isinstance(r.get('protocol', {}), dict)}
            # The protocol returns state to persist, wire fields and a due plan.
            updated, outgoing, plan = protocol.advance(saved, self.own, now, view, fields, proposal, local)
            self.state['protocol'] = updated
            elapsed = boot_time()
            if updated.get('phase') in ('prepared', 'committed'):
                plan_id = protocol.digest(updated['plan'])
                if self.state.get('busy_plan') != plan_id:
                    # Translate once per plan, including across daemon restarts.
                    self.state['busy_plan'] = plan_id
                    remaining = updated['plan']['activate_at'] + protocol.APPLY_GRACE - time.time()
                    self.state['busy_until'] = boot_time() + remaining
                self.write_busy(self.state['busy_until'])
            elif not self.busy():
                self.busy_path.unlink(missing_ok=True)
            apply_reason = 'Agreed channel plan'
            if plan is not None and local['acs']:
                self.state['destination'] = {'plan': plan, 'commit': updated['commit']}
                self.state['recovered_round'] = plan['round']
                if plan['moving']:
                    self.state['hold_until'] = elapsed + protocol.RECOVERY_SECONDS
                # Keep a 120 s repair window if application fails after saving.
                self.state['repair_until'] = elapsed + 120
            elif plan is not None:
                plan = None  # A compatible static participant ACKs without applying ACS.
            self.save()  # Retry failed writes even if memory already contains the new state.
            # Keep a live plan recoverable after its recollection hold.
            # Competing working plans require one common election across the
            # connected groups.
            if (plan is None and view and local['acs'] and not self.busy()
                    and not conflicting and elapsed >= self.state.get('retry_at', 0)
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
                    # Matching channels need only the certificate. Mismatches
                    # need a retune before this node can advertise the plan.
                    if all(local['current'].get(b) == f for b, f in p['channels'].items() if b in local['current']):
                        learned.append(entry)
                    else:
                        candidates.append(entry)
                if candidates:
                    apply_reason = 'Returning to the connected mesh channel plan'
                    # Prefer the latest round, with the digest breaking ties.
                    destination = max(candidates, key=lambda c: c[:2])[2]
                    plan = destination['plan']
                    self.state.update(destination=destination, recovered_round=plan['round'],
                                      hold_until=elapsed + protocol.RECOVERY_SECONDS, repair_until=elapsed + 120)
                    self.save()
                elif learned and self.own not in destinations:
                    # A node recovered by a clockless/lobby reply can now keep
                    # the agreed plan available even after its helper leaves.
                    self.state['destination'] = max(learned, key=lambda c: c[:2])[2]
                    self.save()
                    if local['discovery']:
                        # If rendezvous matches the data channel, adopt its
                        # authority and stop searching without reconfiguring or
                        # hopping away later.
                        plan = self.state['destination']['plan']
            # Repair a radio still behind its saved target, only while that
            # target remains configured and the bounded repair window is open.
            if plan is None and elapsed <= self.state.get('repair_until', -1) and elapsed >= self.state.get('retry_at', 0):
                destination = self.state.get('destination')
                if destination:
                    target = destination['plan']['channels']
                    configured = {b: data[2] for b, data in self.interfaces().items()}
                    if (all(configured.get(b, f) == f for b, f in target.items())
                            and any(local['current'].get(b) != f for b, f in target.items() if b in local['current'])):
                        plan = destination['plan']
            if plan is not None:
                self.drift_since.clear()
                # Save a 30 boot-second retry delay before any radio mutation.
                self.state['retry_at'] = boot_time() + 30
                self.save()
                moved = any(local['current'].get(b, f) != f for b, f in plan['channels'].items())
                self.apply(plan['channels'], plan['limp'], reason=apply_reason)
                self.state.pop('repair_until', None)
                local = self.status(int(time.time()))
                if moved:
                    # Suppress other channel work for 30 s while links reform.
                    self.state['settle_until'] = boot_time() + 30
                    self.write_busy(self.state['settle_until'])
                self.save()
                # This pass publishes post-apply status without pre-apply votes.
                outgoing = {}
            if (plan is None and view and not self.busy()
                    and updated.get('phase') not in ('prepared', 'committed')
                    and self.discovery_step(now, local, len(view) > 1)):
                local = self.status(int(time.time()))
            if plan is None and self.repair_drift(local):
                local = self.status(int(time.time()))
                outgoing = {}
            # Advertise current plans through the existing type 74 exchange.
            # Readbacks while radios are moving cannot advertise a destination.
            destination = protocol.live_destination(self.state.get('destination'), local, int(time.time()))
            checked = time.monotonic()
            # A status change, such as readiness after a request wake,
            # publishes at once instead of waiting out the 5-second refresh.
            if (outgoing != self.state.get('last_outgoing') or local != self.last_status
                    or self.last_publish is None or checked - self.last_publish >= 5):
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
                    # Send one full certificate per operating plan. Other
                    # holders send its ID, dropping stale/unreachable
                    # publishers. This avoids two 64-member certificates in one
                    # envelope during the next round's commit.
                    if self.own == min(holders) and 'commit' not in outgoing:
                        payload['destination'] = destination
                envelope = self.transport.seal(74, payload)
                command(['alfred', '-s', 74], input=json.dumps(envelope), timeout=2)
                self.state['last_outgoing'] = outgoing
                self.last_status = local
                self.last_publish = checked
            # Recovery requests arriving over HaLow need no physical tourguide
            # visit. One reachable, compatible source replies for each request.
            checked = time.monotonic()
            if (view and destination and not conflicting and not self.busy()
                    and (self.last_probe_check is None or checked - self.last_probe_check >= 5)):
                self.last_probe_check = checked
                destinations[self.own] = destination
                self.answer_probes(destination['plan']['channels'], len(view), destinations)

    def interval(self, now):
        """Return seconds until the next pass from the round and plan windows.

        Only a timed plan of the current round adds windows. A stale phase left
        by a reboot or a clock step falls back to the default spacing.
        """
        if not clock_ready():
            return IDLE_INTERVAL
        start = now - now % protocol.ROUND_SECONDS
        windows = [(begin + protocol.PROPOSE_FROM, begin + protocol.PROPOSE_UNTIL + 1)
                   for begin in (start, start + protocol.ROUND_SECONDS)]
        saved = self.state.get('protocol', {})
        plan = saved.get('plan')
        if (saved.get('phase') in ('prepared', 'committed') and isinstance(plan, dict)
                and saved.get('round') == now // protocol.ROUND_SECONDS):
            try:
                deadline, activate = int(plan['deadline']), int(plan['activate_at'])
            except (KeyError, TypeError, ValueError):
                return IDLE_INTERVAL
            windows.append((deadline - GUARD_SECONDS, activate + protocol.APPLY_GRACE + 1))
        if any(begin <= now < end for begin, end in windows):
            return ACTIVE_INTERVAL
        return min([IDLE_INTERVAL] + [begin + WAKE_MARGIN - now for begin, _ in windows if begin > now])

    def connected_destinations(self, view, records, local, now):
        """Collect validated live plans from this node and reachable peers."""
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

    def bootstrap_select(self, local=None):
        """A cold node can follow a live reachable helper without knowing UTC."""
        # The daemon and manager helper share one outstanding probe. Serialize
        # nonce creation and consumption without waiting behind another caller.
        with (self.run_dir / '.acs-probe.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if local is None:
                local = self.status(int(time.time()))
            if clock_ready() or not local['acs'] or not local['current']:
                return None
            path = self.run_dir / 'manet-acs-probe.json'
            probe = read_json(path)
            now = time.monotonic()
            # Monotonic probe deadlines and nonces belong to one boot only.
            if probe.get('boot') != self.boot:
                probe = {}
            if (probe.get('nonce') and not probe.get('used')
                    and 0 <= now - probe['started'] < PROBE_LIFETIME):
                raw = command(['alfred', '-r', 77], timeout=2)
                records = challenge_records(self.transport, 77, raw)
                # The reply must name this node, its boot and its live nonce.
                expected = {'boot': self.boot, 'nonce': probe['nonce']}
                records = {mac: r for mac, r in records.items()
                           if isinstance(r.get('answers'), dict) and r['answers'].get(self.own) == expected}
                destination = recovery_destination(records, local, self.own)
                # Recheck after I/O so slow reads cannot extend nonce validity.
                if destination and time.monotonic() < probe['started'] + PROBE_LIFETIME:
                    probe['used'] = True
                    private_json_write(path, probe)  # Consume before returning an actionable destination.
                    return destination
            if now < probe.get('next_probe', 0):
                return None
            # No peers means no one can answer. This query is local to BATMAN.
            peers = command([str(TOOLS / 'mesh-peer-count.sh'), '--batctl', self.batctl, '--list'], timeout=7).split()
            if not peers:
                return None
            now = time.monotonic()
            # Type 76 asks for a type 77 reply without updating timed history.
            probe = {'boot': self.boot, 'nonce': secrets.token_hex(16), 'started': now,
                     'next_probe': now + PROBE_INTERVAL, 'used': False}
            envelope = self.transport.seal_challenge(76, {'kind': 'acs_probe', 'node': self.own,
                'boot': self.boot, 'nonce': probe['nonce'], 'aliases': self.aliases(),
                'current': local['current'], 'discovery': local['discovery']})
            # Save before sending so a reply always has a persisted challenge.
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
        peers = set(command([str(TOOLS / 'mesh-peer-count.sh'), '--batctl', self.batctl, '--list'], timeout=7).split())
        previous = saved.get('answered', {}) if saved.get('boot') == self.boot else {}
        # Keep ten monotonic minutes of answered probes from this boot.
        answered = {key: stamp for key, stamp in previous.items() if 0 <= now - stamp < 600}
        answers = {}
        # MAC order determines selection when the reply reaches its cap.
        for mac, record in sorted(records.items()):
            nonce, aliases = record.get('nonce'), record.get('aliases')
            # Require a live BATMAN route to the sender or one of at most 32
            # authenticated aliases. Cached unreachable probes get no reply.
            if (mac == self.own or not isinstance(nonce, str) or not protocol.TOKEN.fullmatch(nonce)
                    or not isinstance(aliases, list) or len(aliases) > 32
                    or any(not isinstance(a, str) or not protocol.MAC.fullmatch(a) for a in aliases)
                    or not peers.intersection([mac, *aliases])):
                continue
            if sources is not None:
                current = record.get('current')
                if not isinstance(current, dict) or not current:
                    continue
                # Among plan holders sharing a band needing recovery, only the
                # lowest MAC replies. Searchers also need matching-channel help.
                eligible = [source for source, target in sources.items()
                            if any(b in current and (current[b] != f or record.get('discovery') is True)
                                   for b, f in target['plan']['channels'].items())]
                if not eligible or self.own != min(eligible):
                    continue
            # Deduplicate by requester, boot and nonce, across daemon restarts.
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
        # Keep the newest 640 entries and reserve the 5 s reply interval before
        # publishing, so send failures still consume this batch's reply budget.
        answered = dict(sorted(answered.items(), key=lambda item: item[1], reverse=True)[:640])
        private_json_write(path, {'boot': self.boot, 'answered': answered, 'next_reply': now + 5})
        command(['alfred', '-s', 77], input=json.dumps(envelope), timeout=2)


def main():
    """Dispatch manager and tourguide helpers or run the agreement daemon."""
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
        # A request made before initial sync cannot authorize a timed round.
        if not clock_ready():
            (directory / 'manet-acs-request.json').unlink(missing_ok=True)
            return 0
        private_json_write(directory / 'manet-acs-request.json', {'round': int(time.time()) // protocol.ROUND_SECONDS})
        wake_daemon(directory)
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
        # The tourguide publishes the type 75 envelope from stdout. Clockless
        # probe replies are a separate best-effort action during the same visit.
        print(json.dumps(runtime.transport.seal(75, payload)))
        try:
            runtime.answer_probes(args.channels, args.size)
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
            print('ACS bootstrap reply deferred: ' + str(exc), file=sys.stderr)
    elif args.action == 'helper-select':
        if not clock_ready():
            # Only ordinary recovery uses a nonce before sync. A tourguide's
            # supplied records or pre-hop channels cannot bypass timed checks.
            destination = runtime.bootstrap_select() if args.current is None and not args.stdin else None
            if destination:
                print(str(destination.get('2.4', '')) + '|' + str(destination.get('5', '')))
            return 0
        records = authenticated_records(runtime.transport, 75, sys.stdin.read(), now) if args.stdin else runtime.receive(75, now)
        local = runtime.status(now)
        if args.current is not None:
            local['current'] = args.current  # Pre-hop channels, still checked against local capabilities.
            local['discovery'] = False  # Comparing established partitions, not adopting a search channel.
        destination = recovery_destination(records, local,
                                           runtime.own, args.exclude.split(), args.size)
        if destination:
            # Shell callers split the 2.4 GHz and 5 GHz MHz values on a pipe.
            print(str(destination.get('2.4', '')) + '|' + str(destination.get('5', '')))
    elif args.action == 'run':
        # One daemon owns the worker and signal pipe for this state directory.
        with (runtime.state_dir / '.daemon.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            from manet_runtime import start
            start()  # Blocking worker here, without another polling loop.
            # SIGUSR1 from the request action cuts the current sleep short. The
            # wakeup pipe carries it to select, so the handler takes no locks.
            wake_read, wake_write = os.pipe()
            for descriptor in (wake_read, wake_write):
                os.set_blocking(descriptor, False)
            signal.signal(signal.SIGUSR1, lambda *_: None)
            signal.set_wakeup_fd(wake_write)
            (runtime.run_dir / 'manet-acs-agreement.pid').write_text(f'{os.getpid()}\n')
            last_error = None
            while True:
                delay = None
                try:
                    runtime.tick(int(time.time()))
                    last_error = None
                except BlockingIOError:
                    # Tourguide or another local radio operation owns the lock.
                    # Retry in a second so a drained wake is not lost to it.
                    delay = ACTIVE_INTERVAL
                except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
                    message = str(exc)
                    if message != last_error:
                        print('ACS deferred: ' + message, file=sys.stderr, flush=True)
                    last_error = message
                if delay is None:
                    delay = runtime.interval(time.time())
                if select.select([wake_read], [], [], delay)[0]:
                    try:
                        os.read(wake_read, 512)
                    except BlockingIOError:
                        pass
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        print('ACS unavailable: ' + str(exc), file=sys.stderr)
        sys.exit(1)
