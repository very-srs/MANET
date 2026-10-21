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
import manet_clock
import manet_rendezvous as rendezvous
import manet_dfs as dfs
import manet_dfs_safety as dfs_safety

TOOLS = Path(__file__).resolve().parent
# Maximum age in wall-clock seconds for authenticated type 74/75 records.
FRESH_SECONDS = 45
PROBE_LIFETIME = 60  # Request/reply replication plus a 15-second manager wakeup.
# Minimum monotonic seconds between new clockless recovery probes.
PROBE_INTERVAL = 60
# CHANNEL_BIAS_SCORE in channel-election.sh supplies a ten-point margin.
# Preference moves also need this many consecutive coordinator rounds.
CHALLENGE_ROUNDS = 3
# Pass spacing in seconds. Default passes match the 5-second publish
# cadence. One-second passes, as in a fixed one-second loop, cover the
# proposal window and a live plan from GUARD_SECONDS before its vote deadline
# through its apply window: last votes, commit, commit pickup and apply.
IDLE_INTERVAL = 5
ACTIVE_INTERVAL = 1
# Alfred synchronizes every 10 seconds. Leave the rest of the 15-second
# clearance lease for publication, receipt and decision work, throughout CAC
# preparation and the whole vote/commit/activation window.
DFS_INTERVAL = min(ACTIVE_INTERVAL, protocol.DFS_LEASE_SECONDS / 15)
GUARD_SECONDS = 5
# Three mesh beacons can take over three seconds. TX stays blocked while
# waiting for the accepted CSA; expiry uses interface-down recovery.
DFS_CSA_WAIT_SECONDS = 10
WAKE_MARGIN = 0.05  # Lands a pass after a whole-second boundary, not before.
# Local drift needs sustained idle observations; attempts use boot seconds.
DRIFT_GRACE = 60
DRIFT_BACKOFF = 300
# Boot-clock deadlines and their plan identity survive same-boot restarts.
LOCAL_TIMERS = ('hold_until', 'repair_until', 'retry_at', 'settle_until',
                'busy_until', 'busy_plan', 'drift_retry_at',
                'dfs_cooldown_until')
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


def read_background_hold(path, boot, epoch, now, raw=None):
    """Read schema 2; kernel receiver epochs replace the schema-1 reader token."""
    try:
        raw = dfs.hold_snapshot(path) if raw is None else dict(raw)
        if (type(raw.get('schema')) is not int or raw['schema'] != 2 or
                any(type(raw.get(k)) is not bool for k in
                    ('enabled', 'owner', 'fault', 'live', 'receiver_live', 'revoked')) or
                not isinstance(raw.get('epoch'), str) or
                not protocol.TOKEN.fullmatch(raw['epoch']) or
                any(type(raw.get(k)) is not int or raw[k] < 0 for k in
                    ('observed_boottime_us', 'heartbeat_boottime_us',
                     'heartbeat_max_age_us', 'generation')) or
                raw['heartbeat_max_age_us'] != 1500000 or
                type(raw.get('freq_mhz')) is not int or
                type(raw.get('center_mhz')) is not int or
                type(raw.get('width_mhz')) is not int or
                raw.get('width_mhz') != 20 or
                raw.get('center_mhz') != raw.get('freq_mhz')):
            return None
        if (raw.get('dfs_mesh_v1') is True and raw.get('main_active') is True and
                (not raw.get('owner') or raw.get('freq_mhz') == raw.get('main_freq_mhz'))):
            raw.update(epoch=raw['main_epoch'], generation=raw['main_generation'],
                       freq_mhz=raw['main_freq_mhz'], center_mhz=raw['main_freq_mhz'],
                       owner=True, enabled=True, revoked=False, fault=False,
                       live=True, receiver_live=True,
                       heartbeat_boottime_us=raw['observed_boottime_us'])
        checked = int(boot_time() * 1000000)
        at = raw['heartbeat_boottime_us']
        fresh = (0 < at <= raw['observed_boottime_us'] <= checked and
                 checked - at < raw['heartbeat_max_age_us'])
        live = (fresh and raw['enabled'] and raw['owner'] and raw['live'] and
                raw['receiver_live'] and not raw['revoked'] and not raw['fault'])
        state = ('fault' if raw['fault'] else 'held' if live else
                 'cac' if raw['enabled'] and raw['owner'] and not raw['revoked']
                 and not raw['live'] else 'revoked')
        hold = dict(version=protocol.DFS_VERSION, frequency=raw['freq_mhz'],
                    width=20, center=raw['freq_mhz'], state=state,
                    boot=boot, epoch=raw['epoch'], generation=raw['generation'],
                    live=live, observed_at=now,
                    lease_until=now + protocol.DFS_LEASE_SECONDS if live else now)
        return hold if protocol.valid_hold(hold) else None
    except BlockingIOError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def kernel_radar_event(raw):
    """Return only an explicit kernel radar latch, never infer radar from loss."""
    if (type(raw) is not dict or type(raw.get('schema')) is not int or
            raw.get('schema') != 2 or
            raw.get('radar_cause') not in ('background-radar', 'main-radar') or
            not isinstance(raw.get('radar_epoch'), str) or
            not protocol.TOKEN.fullmatch(raw['radar_epoch']) or
            any(type(raw.get(k)) is not int or raw[k] <= 0 for k in
                ('radar_sequence', 'radar_generation', 'radar_boottime_us')) or
            type(raw.get('radar_freq_mhz')) is not int or
            raw.get('radar_freq_mhz') not in protocol.DFS_CHANNELS):
        return None
    return (raw['radar_epoch'], raw['radar_generation'],
            raw['radar_sequence'], raw['radar_freq_mhz'])


def deliver_background_radar(runtime, event, delivered):
    """Fence TX before a queued event or slow publication can delay this one."""
    for plan_id, plan in runtime.dfs_plans().items():
        key = (runtime.boot, *event, plan_id)
        if (plan['dfs']['holds'].get(runtime.own) != list(event[:2]) or
                plan['channels'].get('5') != event[3]):
            continue
        if key in delivered:
            return
        # A full local event slot must never defer TX shutdown. Delivery can
        # wait for the daemon to claim the previous slot after the radio stops.
        runtime.vacate_dfs(plan)
        pending = any((runtime.run_dir / name).exists() for name in
                      ('manet-dfs-radar.json', 'manet-dfs-radar.processing.json'))
        if pending:
            wake_daemon(runtime.run_dir)
            return
        runtime.radar_detected(plan_id, event[0], event[1])
        delivered.add(key)
        if len(delivered) > 128:
            delivered.intersection_update({key})
        return
    runtime.fence_unowned_radar(event[3])


def record_dfs_peer_switch(change, run):
    """Bind kernel-confirmed TX blocking to the armed fallback and interface.

    This is local switch evidence, not a radar report or abort authorization.
    A competing local request can fail with EBUSY after these queues are
    blocked. Keep that distinction from its own block-TX acknowledgment.
    """
    contract = read_json(run / 'manet-dfs-contract.json')
    iface = contract.get('iface')
    if not iface or dfs.socket.if_nametoindex(iface) != change['ifindex']:
        return
    path = run / 'manet-dfs-peer-csa.json'
    boot = Path(os.environ.get('MANET_BOOT_ID_FILE', '/proc/sys/kernel/random/boot_id')).read_text().strip().replace('-', '')
    plan = contract.get('operating') or contract.get('pending')
    try:
        protocol.validate_plan(plan)
    except (ValueError, TypeError, KeyError):
        path.unlink(missing_ok=True)
        return
    if (contract.get('boot') != boot or
            plan['participants'].get(contract.get('own')) != boot or
            'dfs' not in plan or not change['block_tx'] or change['width'] != 1 or
            change['center'] != change['frequency'] or
            change['frequency'] != plan['dfs']['fallback']['5']):
        path.unlink(missing_ok=True)
        return
    record = dict(boot=boot, plan=protocol.digest(plan), iface=iface,
                  ifindex=change['ifindex'], frequency=change['frequency'],
                  block_tx=True, started_boottime=change['started_boottime'])
    previous = read_json(path)
    if all(previous.get(k) == v for k, v in record.items() if k != 'started_boottime'):
        # Repeated notifications cannot renew the original bounded window.
        record['started_boottime'] = min(previous['started_boottime'], record['started_boottime'])
    private_json_write(path, record)


def watch_background_radar(path=None):
    """Subscribe before reading the latch, then sleep until an actual event.

    A busy wiphy read requests one fresh snapshot from the agreement's next
    successful status pass. File changes deliver that reply and pending-slot
    acknowledgements. No timer, sleep loop or periodic status-file wakeup.
    """
    run = Path(os.environ.get('MANET_ACS_RUN_DIR', '/run'))
    state = Path(os.environ.get('MANET_ACS_STATE_DIR', '/var/lib/manet-acs'))
    roles = Path(os.environ.get('MANET_IFACE_STATE_DIR', '/var/lib'))
    reply = run / 'manet-dfs-kernel.json'
    files = [state / 'agreement.json', run / 'manet-dfs-contract.json', roles / 'mesh_5_if', reply,
             run / 'manet-dfs-radar.json', run / 'manet-dfs-radar.processing.json']
    delivered = set()
    last_error = None
    with dfs.RadarEvents(files) as events:
        events.on_switch = lambda change: record_dfs_peer_switch(change, run)
        def watcher_runtime():
            runner = Runtime()
            runner.dfs_events = events
            return runner
        starting = True
        changes = [dict(kind='change')]  # Catch a latch predating subscription.
        while True:
            try:
                selected = Path(path) if path else background_hold_path()
                if selected is None and any(c.get('kind') == 'radar' and
                        c.get('event') == 0 for c in changes):
                    dfs_safety.stop_dfs()
                if selected is not None:
                    phy = re.fullmatch(r'phy(\d+)', selected.parent.name)
                    # Fence a kernel-reported main-channel radar before reading
                    # debugfs. EBUSY must never postpone local evacuation.
                    fence_errors = []
                    for change in changes:
                        if (change.get('kind') == 'radar' and change['event'] == 0
                                and phy and change['phy'] == int(phy[1])):
                            try:
                                runner = watcher_runtime()
                                for plan in runner.dfs_plans().values():
                                    if plan['channels'].get('5') == change['frequency']:
                                        runner.vacate_dfs(plan)
                                        break
                                else:
                                    runner.fence_unowned_radar(change['frequency'])
                            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                                dfs_safety.stop_dfs()
                                fence_errors.append(exc)
                    response = any(c.get('name') == reply.name for c in changes)
                    snapshot = read_json(reply) if response else {}
                    raw = (snapshot['raw'] if snapshot.get('path') == str(selected)
                           else dfs.hold_snapshot(selected))
                    event = kernel_radar_event(raw)
                    if event:
                        # Pending-slot deletion or a new saved plan can complete
                        # delivery even when no further RF notification occurs.
                        try:
                            runner = watcher_runtime()
                            if not runner.superseded_radar_latch(event, raw):
                                deliver_background_radar(runner, event, delivered)
                        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
                            dfs_safety.stop_dfs()
                            raise
                    if fence_errors:
                        raise fence_errors[0]
                    last_error = None
            except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
                if starting:
                    # Do not report ready with an unreadable preexisting latch.
                    dfs_safety.stop_dfs()
                # Keep draining events while the agreement services this one
                # outstanding read. A newer radar event is never queued behind
                # an old busy debugfs read or occupied pending slot.
                (run / 'manet-dfs-recheck').touch(mode=0o600)
                wake_daemon(run)
                if str(exc) != last_error:
                    print('DFS kernel event deferred: ' + str(exc), file=sys.stderr, flush=True)
                    last_error = str(exc)
            if starting:
                dfs_safety.notify_ready()
                starting = False
            changes = events.wait()


def background_hold_path():
    """Resolve the current 5 GHz mesh role without constructing transport state."""
    from manet_bgobs import observer_path
    roles = Path(os.environ.get('MANET_IFACE_STATE_DIR', '/var/lib'))
    try:
        iface = (roles / 'mesh_5_if').read_text().strip()
    except FileNotFoundError:
        return None
    observer = observer_path(iface)
    return observer.parent.parent / 'background_hold' if observer else None


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
            dfs_cooling = self.state.get('dfs_cooldown_until', 0) > 0
            for key in LOCAL_TIMERS:
                self.state.pop(key, None)
            self.state['timer_boot'] = self.boot
            # Unknown-boot holds restart in full. Drop all other local timers.
            if held:
                self.state['hold_until'] = boot_time() + protocol.RECOVERY_SECONDS
            if dfs_cooling:
                self.state['dfs_cooldown_until'] = (
                    boot_time() + protocol.DFS_COOLDOWN_SECONDS)

    @contextmanager
    def channel_lock(self):
        """Hold the shared radio-operation lock or raise immediately if busy."""
        with self.lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def interfaces(self):
        """Return enabled Wi-Fi roles with config paths and configured MHz."""
        from manet_wifi_uplink import uplink_owned
        desired = read_json(self.radio_state).get('desired', {})
        result = {}
        for band, role in [('2.4', 'mesh_24_if'), ('5', 'mesh_5_if')]:
            try:
                iface = (self.roles / role).read_text().strip()
            except FileNotFoundError:
                continue
            if not iface or desired.get(iface) == 'down' or uplink_owned(iface, self.roles):
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
        from manet_wifi_uplink import uplink_owned
        if uplink_owned(iface, self.roles):
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
        dfs_allowed, hold, dfs_candidates, dfs_capable = [], None, [], False
        hold_unavailable = False
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
                cached = (checked, sorted(rendezvous.permitted_frequencies(data)), data)
                self.capabilities[key] = cached
            # Election candidates and rendezvous channels use different sets.
            allowed[band] = [f for f in cached[1] if f in protocol.CHANNELS[band]]
            self.rendezvous_allowed[band] = [f for f in cached[1] if f in rendezvous.CHANNELS[band]]
            if band == '5':
                hold_path = Path(os.environ.get('MANET_DFS_HOLD_PATH',
                    f'/sys/kernel/debug/ieee80211/phy{phy[1]}/background_hold'))
                try:
                    raw_hold = dfs.hold_snapshot(hold_path)
                except BlockingIOError:
                    # Publish ordinary readiness without a stale DFS token.
                    # Leave a watcher's recheck request pending for a fresh read.
                    hold_unavailable = True
                    continue
                # Only an outstanding event asks for this response. There is
                # no periodic watcher wake or cached clearance authority.
                recheck = self.run_dir / 'manet-dfs-recheck'
                if recheck.exists():
                    # Consume before publishing: the watcher can request a new
                    # read as soon as inotify sees the atomic reply rename.
                    recheck.unlink(missing_ok=True)
                    try:
                        private_json_write(self.run_dir / 'manet-dfs-kernel.json',
                                           dict(path=str(hold_path), raw=raw_hold))
                    except OSError:
                        recheck.touch(mode=0o600)
                        raise
                dfs_capable = dfs.capable(hold_path, cached[2], raw=raw_hold, iface=iface) if len(cached) > 2 else False
                if dfs_capable and boot_time() >= self.state.get('dfs_cooldown_until', 0):
                    dfs_candidates = dfs.dfs_frequencies(cached[2])
                # This does not start CAC or take the receiver from its owner.
                hold = self.dfs_hold('phy' + phy[1], now, raw=raw_hold)
                armed = self.state.get('protocol', {})
                plan = armed.get('plan', {})
                if (hold and current[band] != configured and
                        current[band] == plan.get('channels', {}).get('5') and
                        'dfs' in plan and protocol.valid_commit(armed.get('commit'), plan) and
                        plan['activate_at'] <= now <= plan['activate_at'] + protocol.APPLY_GRACE and
                        plan['dfs']['holds'].get(self.own) == [hold['epoch'], hold['generation']]):
                    write_frequency(path, current[band])
                    configured_channels[band] = current[band]
                    stable = all(current[b] == configured_channels[b] for b in current)
                if hold and boot_time() >= self.state.get(
                        'dfs_cooldown_until', 0):
                    # The kernel hold supplies the exact cleared chandef.
                    # No broad DFS whitelist enters ordinary radio recovery.
                    dfs_allowed = [hold['frequency']]
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
        readiness = ('clock-unready' if not clock_ready() else
                     'radio-drift' if not stable else
                     'ready' if not acs or not current else
                     'recovery-hold' if boot_time() < self.state.get('hold_until', 0) else
                     'busy' if cooling else
                     'request-missing' if not requested else 'ready')
        status = {'boot': self.boot, 'acs': acs, 'ready': readiness == 'ready',
                  'readiness': readiness,
                  'current': current, 'allowed': allowed, 'stable': stable,
                  'halow_ready': self.halow_health[1],
                  'discovery': self.discovery.mode(configured_channels) == 'search'}
        status.update(dfs_versions=[protocol.DFS_VERSION] if dfs_capable else [],
                      dfs_capable=dfs_capable,
                      dfs_reason=('hold-unavailable' if hold_unavailable else
                                  'ready' if dfs_capable else 'release-kernel-or-regdb-missing'),
                      dfs_candidates=dfs_candidates,
                      dfs_allowed=dfs_allowed if dfs_capable else [])
        if hold:
            status['dfs_hold'] = hold
        private_json_write(self.run_dir / 'manet-dfs-hold.json', hold)
        if not protocol.valid_status(status):
            raise ValueError('invalid local radio state')
        self.ui_observed_at = time.monotonic()
        self.ui_status.update(current=current, halow_ready=status['halow_ready'],
                              searching=status['discovery'])
        return status

    def dfs_hold(self, phy, now, raw=None):
        """Read the kernel generation and bounded receiver heartbeat."""
        path = Path(os.environ.get('MANET_DFS_HOLD_PATH',
                    f'/sys/kernel/debug/ieee80211/{phy}/background_hold'))
        hold = read_background_hold(path, self.boot, None, now, raw=raw)
        revoked = self.state.get('dfs_revoked', [])
        pending_radar = False
        for name in ('manet-dfs-radar.json', 'manet-dfs-radar.processing.json'):
            event = read_json(self.run_dir / name)
            abort = event.get('abort', {})
            pending_radar |= bool(
                hold and event.get('boot') == self.boot and
                abort.get('reason') == 'radar' and
                [abort.get('epoch'), abort.get('generation')] ==
                [hold['epoch'], hold['generation']])
        if hold and ([hold['epoch'], hold['generation']] in revoked or
                     pending_radar or
                     boot_time() < self.state.get('dfs_cooldown_until', 0)):
            hold.update(state='revoked', live=False, lease_until=now)
        return hold

    def dfs_plans(self):
        """Return local session plans whose generation can accept an abort."""
        plans = {}
        # A fallback refresh can reuse the operating main token. Radar must
        # keep the active destination's fallback until that refresh activates.
        contract = read_json(self.run_dir / 'manet-dfs-contract.json')
        candidates = ([contract.get('operating'), contract.get('pending')]
                      if contract.get('boot') == self.boot and contract.get('own') == self.own else [])
        candidates += [self.state.get(key, {}).get('plan')
                       for key in ('destination', 'protocol')]
        candidates.append(self.state.get('dfs_radar', {}).get('contract'))
        for plan in candidates:
            try:
                protocol.validate_plan(plan)
                if ('dfs' in plan and
                        plan['participants'].get(self.own) == self.boot):
                    plans[protocol.digest(plan)] = plan
            except (ValueError, TypeError, KeyError):
                pass
        return plans

    def superseded_radar_latch(self, event, raw):
        """An older latch cannot condemn a newer, verified clearance token.

        A peer abort can replace our saved abort contract while the kernel
        retains an earlier local radar latch. Only a live receiver bound to
        another accepted local plan can prove that latch has been superseded.
        Immediate nl80211 radar fencing is independent of this replay check.
        """
        hold = read_background_hold(None, self.boot, None, int(time.time()), raw=raw)
        if (not hold or not hold['live'] or hold['frequency'] != event[3]
                or hold['generation'] <= event[1]):
            return False
        return any(plan['channels'].get('5') == hold['frequency'] and
                   plan['dfs']['holds'].get(self.own) == [hold['epoch'], hold['generation']]
                   for plan in self.dfs_plans().values())

    def fence_unowned_radar(self, frequency):
        """Missing agreement metadata never authorizes continued DFS TX."""
        interface = self.interfaces().get('5')
        if not interface:
            dfs_safety.stop_dfs()
            return
        if interface:
            iface, _, configured = interface
            if configured == frequency:
                self.vacate_dfs()
                return
            try:
                info = command(['iw', 'dev', iface, 'info'], timeout=2)
                match = re.search(r'channel.*\((\d+) MHz\)', info)
                if match and int(match[1]) != frequency:
                    return
            except (OSError, subprocess.SubprocessError):
                pass
            self.vacate_dfs()

    def store_dfs_contract(self, plan, iface, *, operating=False):
        """Arm before CSA; replace the operating contract only after readback."""
        path = self.run_dir / 'manet-dfs-contract.json'
        saved = read_json(path)
        if saved.get('boot') != self.boot or saved.get('own') != self.own:
            saved = dict(boot=self.boot, own=self.own, iface=iface)
        saved['operating' if operating else 'pending'] = plan
        if operating:
            saved.pop('pending', None)
        private_json_write(path, saved)

    def arm_dfs_contract(self, plan):
        """Arm before publishing a commit so a peer CSA is protected too."""
        if not dfs.guardian_ready():
            raise ValueError('DFS guardian is not ready')
        iface = self.interfaces()['5'][0]
        self.store_dfs_contract(plan, iface)
        dfs.external_owner(iface, True)
        if not dfs.guardian_ready():
            dfs_safety.stop_dfs()
            raise ValueError('DFS guardian stopped while arming ownership')

    def retire_dfs_contract(self, iface):
        """The caller has verified non-DFS landing before releasing ownership."""
        path = self.run_dir / 'manet-dfs-contract.json'
        if path.exists():
            dfs.external_owner(iface, False)
            path.unlink()

    def check_dfs_activation(self, plan, records):
        """Re-read membership and hold under the caller's channel lock."""
        for name in ('manet-dfs-radar.json', 'manet-dfs-radar.processing.json'):
            pending = read_json(self.run_dir / name)
            if (pending.get('boot') == self.boot and
                    protocol.valid_dfs_abort(pending.get('abort'), plan,
                                             self.own)):
                raise protocol.AgreementError('DFS radar pending at activation')
        records = self.receive(74, int(time.time()))
        for sender, record in records.items():
            if any(protocol.valid_dfs_abort(abort, plan, sender) for abort in
                   (record.get('dfs_abort'), record.get('protocol', {}).get('abort'))):
                raise protocol.AgreementError('DFS abort received at activation')
        fresh = self.status(int(time.time()))
        members = self.members(records)
        view = {m: records.get(m, {}).get('status') for m in members}
        view[self.own] = fresh
        protocol.require_dfs_activation(plan, view, int(time.time()))

    def peer_csa_started(self, plan, iface):
        """Return the original start only for proven, matching TX-blocked CSA."""
        try:
            events = getattr(self, 'dfs_events', None)
            if events is not None:
                # In the guardian, a peer radar notification can precede its CSA
                # notification in the same kernel operation. Drain after EBUSY.
                events.drain_wireless()
            saved = read_json(self.run_dir / 'manet-dfs-peer-csa.json')
            started = saved.get('started_boottime')
            if (saved.get('boot') == self.boot and saved.get('plan') == protocol.digest(plan)
                    and saved.get('iface') == iface and saved.get('block_tx') is True
                    and saved.get('frequency') == plan['dfs']['fallback']['5']
                    and type(started) in (int, float)
                    and 0 <= boot_time() - started < DFS_CSA_WAIT_SECONDS
                    and saved.get('ifindex') == dfs.socket.if_nametoindex(iface)):
                return started
        except (OSError, ValueError, TypeError, KeyError):
            pass  # Unreadable evidence cannot postpone the fence.
        return None

    def vacate_dfs(self, plan=None, *, check_current=False):
        """Stop local DFS TX before locks, persistence or network I/O."""
        interface = self.interfaces().get('5')
        if not interface:
            return False
        iface, _, configured = interface
        try:
            if dfs.supplicant_control(iface, 'MANET_DFS_STATUS') == 'MANET_DFS_V3 owner=0':
                # Ownership is per running BSS. Static meshes and a new BSS
                # after a restart retain native evacuation, even if an older
                # contract remains on disk. Unknown ownership still fences.
                return False
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        pending = read_json(self.run_dir / 'manet-dfs-csa.json')
        if (plan and pending.get('boot') == self.boot and
                pending.get('plan') == protocol.digest(plan) and
                pending.get('frequency') == plan['dfs']['fallback']['5'] and
                0 <= boot_time() - pending.get('requested_boottime', -10) < DFS_CSA_WAIT_SECONDS):
            return True
        on_dfs = configured in protocol.DFS_CHANNELS
        if check_current and plan:
            # A peer may already have carried us to the fallback before its
            # Alfred abort arrives. Do not start another CSA on that channel.
            # The detecting watcher skips this read to fence TX immediately.
            try:
                info = command(['iw', 'dev', iface, 'info'], timeout=2)
                if re.search(r'channel.*\(' + str(plan['dfs']['fallback']['5']) + r' MHz\)', info):
                    write_frequency(interface[1], plan['dfs']['fallback']['5'])
                    self.sync_parked({'5': plan['dfs']['fallback']['5']})
                    return False
            except (OSError, subprocess.SubprocessError):
                pass
        if on_dfs and plan and configured != plan['channels']['5']:
            # Radar on a different background candidate does not condemn the
            # currently operating DFS channel.
            info = command(['iw', 'dev', iface, 'info'], timeout=2)
            match = re.search(r'channel.*\((\d+) MHz\)', info)
            if match and int(match[1]) != plan['channels']['5']:
                return False
        if not on_dfs:
            try:
                info = command(['iw', 'dev', iface, 'info'], timeout=2)
                match = re.search(r'channel.*\((\d+) MHz\)', info)
                on_dfs = not match or int(match[1]) in protocol.DFS_CHANNELS
            except (OSError, subprocess.SubprocessError):
                on_dfs = True  # Unknown readback cannot justify transmission.
        if on_dfs and plan:
            if self.peer_csa_started(plan, iface) is not None:
                return True
            try:
                frequency = plan['dfs']['fallback']['5']
                requested_at = boot_time()
                command(['iw', 'dev', iface, 'switch', 'freq', str(frequency),
                         'HT20', 'beacons', '3', 'block-tx'], timeout=2)
                acknowledged_at = boot_time()
                # Persist only after the kernel accepted TX blocking and CSA.
                write_frequency(interface[1], frequency)
                private_json_write(self.run_dir / 'manet-dfs-csa.json',
                    dict(plan=protocol.digest(plan), boot=self.boot,
                         frequency=frequency, requested_boottime=requested_at,
                         acknowledged_boottime=acknowledged_at))
                return True
            except (OSError, ValueError, subprocess.SubprocessError):
                # A peer's CSA may have completed first. Preserve its landing.
                try:
                    info = command(['iw', 'dev', iface, 'info'], timeout=2)
                    if re.search(r'channel.*\(' + str(frequency) + r' MHz\)', info):
                        write_frequency(interface[1], frequency)
                        return False
                except (OSError, subprocess.SubprocessError):
                    pass
                if self.peer_csa_started(plan, iface) is not None:
                    return True
        if on_dfs:
            command(['ip', 'link', 'set', 'dev', iface, 'down'], timeout=2)
            if plan is None:
                command(['systemctl', 'stop', f'wpa_supplicant@{iface}.service'], timeout=15)
                command(['ip', 'link', 'set', 'dev', iface, 'down'], timeout=2)
        return on_dfs

    def publish_dfs_abort(self, abort):
        """Publish both abort types over all surviving BATMAN paths."""
        # Alfred floods the connected component over 2.4 GHz, 5 GHz, HaLow
        # and wired BATMAN links. No Wi-Fi-only membership filter is used.
        errors = []
        for type_id, kind in ((74, 'acs_state'), (75, 'acs_helper')):
            try:
                envelope = self.transport.seal(type_id, {
                    'kind': kind, 'node': self.own,
                    'dfs_versions': [protocol.DFS_VERSION],
                    'dfs_abort': abort})
                command(['alfred', '-s', type_id], input=json.dumps(envelope),
                        timeout=2)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                errors.append(str(exc))
        if errors:
            self.ui_status['dfs_abort_delivery'] = '; '.join(errors)

    def radar_detected(self, plan_id, epoch, generation):
        """Accept a generation-bound local detector event without the ACS lock.

        The detector adapter must pass the plan and receiver identity it
        armed. This host hook never treats a CAC_FINISHED event as readiness.
        """
        plan = self.dfs_plans().get(plan_id)
        if (not plan or plan['dfs']['holds'][self.own] !=
                [epoch, generation]):
            raise ValueError('radar event does not match the armed DFS plan')
        if plan_id in self.state.get('dfs_aborted_plans', []):
            return
        vacated = self.vacate_dfs(plan)
        abort = protocol.dfs_abort(plan, self.own, 'radar')
        # The block-TX request precedes this atomic event and publication.
        # Persistence lets agreement processing survive contention or restart.
        try:
            private_json_write(self.run_dir / 'manet-dfs-radar.json',
                               dict(plan=plan_id, abort=abort,
                                    vacated=vacated, boot=self.boot))
        finally:
            self.publish_dfs_abort(abort)
            wake_daemon(self.run_dir)

    def radar_fallback(self, plan):
        """Retune 5 GHz to the agreed non-DFS fallback, or leave it down."""
        iface, path, _ = self.interfaces()['5']
        frequency = plan['dfs']['fallback']['5']
        csa = read_json(self.run_dir / 'manet-dfs-csa.json')
        deadlines = []
        if (csa.get('boot') == self.boot and
                csa.get('plan') == protocol.digest(plan) and
                csa.get('frequency') == frequency):
            deadlines.append(min(boot_time(), csa.get('requested_boottime', 0)) + DFS_CSA_WAIT_SECONDS)
        peer_started = self.peer_csa_started(plan, iface)
        if peer_started is not None:
            deadlines.append(peer_started + DFS_CSA_WAIT_SECONDS)
        deadline = min(deadlines, default=boot_time())
        while True:
            try:
                info = command(['iw', 'dev', iface, 'info'], timeout=2)
                if re.search(r'channel.*\(' + str(frequency) + r' MHz\)', info):
                    write_frequency(path, frequency)
                    self.sync_parked({'5': frequency})
                    self.retire_dfs_contract(iface)
                    return
            except (OSError, subprocess.SubprocessError):
                pass
            if boot_time() >= deadline:
                break
            time.sleep(min(.1, max(0, deadline - boot_time())))
        # Only a failed/expired CSA needs a new BSS. A partial reconfigure
        # during a mesh countdown can leave stale SAE/key state behind.
        command(['ip', 'link', 'set', 'dev', iface, 'down'], timeout=2)
        try:
            write_frequency(path, frequency)
            command(['systemctl', 'restart',
                     f'wpa_supplicant@{iface}.service'], timeout=15)
            command(['ip', 'link', 'set', 'dev', iface, 'up'], timeout=2)
            for _ in range(20):
                info = command(['iw', 'dev', iface, 'info'], timeout=2)
                if re.search(r'channel.*\(' + str(frequency) + r' MHz\)',
                             info):
                    self.sync_parked({'5': frequency})
                    self.retire_dfs_contract(iface)
                    self.state['settle_until'] = boot_time() + 30
                    self.write_busy(self.state['settle_until'])
                    return
                time.sleep(.5)
            raise ValueError('radar fallback did not land')
        except Exception:
            command(['ip', 'link', 'set', 'dev', iface, 'down'], timeout=2)
            raise

    def process_dfs_aborts(self, records, now):
        """Latch radar before voting and retry an unfinished exit."""
        plans = self.dfs_plans()
        if not plans:
            return
        events = []
        pending_path = self.run_dir / 'manet-dfs-radar.json'
        processing_path = self.run_dir / 'manet-dfs-radar.processing.json'
        # Only the lock owner consumes this file. A concurrent producer can
        # replace pending_path without losing its event to end-of-pass cleanup.
        if not processing_path.exists():
            try:
                pending_path.replace(processing_path)
            except FileNotFoundError:
                pass
        pending = read_json(processing_path)
        if pending.get('boot') == self.boot:
            events.append((self.own, pending.get('abort'),
                           pending.get('vacated') is True))
        # Type 75 also reaches a lobby helper. A missing ordinary status does
        # not erase an authenticated radar report from a frozen participant.
        try:
            helpers = self.receive(75, now) if clock_ready() else {}
        except (OSError, subprocess.SubprocessError):
            helpers = {}
        for source in (records, helpers):
            for sender, record in source.items():
                abort = record.get('dfs_abort')
                if abort is None:
                    abort = record.get('protocol', {}).get('abort')
                events.append((sender, abort, False))
        for sender, abort, vacated in events:
            if not isinstance(abort, dict):
                continue
            plan = plans.get(abort.get('plan'))
            if (not plan or abort.get('reason') != 'radar' or
                    not protocol.valid_dfs_abort(abort, plan, sender)):
                continue
            old = self.state.get('dfs_radar', {})
            seen = self.state.get('dfs_aborted_plans', [])
            if old.get('plan') == abort['plan'] or abort['plan'] in seen:
                continue  # Replays cannot extend cooldown or repeat a move.
            needs_fallback = (self.vacate_dfs(plan, check_current=True)
                              if sender != self.own else self.vacate_dfs(plan)) or vacated
            local_abort = protocol.dfs_abort(plan, self.own, 'radar')
            self.state['dfs_radar'] = dict(
                plan=abort['plan'], abort=local_abort,
                contract=plan,
                pending=needs_fallback,
                publish_until=boot_time() + protocol.DFS_COOLDOWN_SECONDS)
            self.state['dfs_cooldown_until'] = (
                boot_time() + protocol.DFS_COOLDOWN_SECONDS)
            seen.append(abort['plan'])
            self.state['dfs_aborted_plans'] = seen[-64:]
            revoked = self.state.setdefault('dfs_revoked', [])
            binding = plan['dfs']['holds'][self.own]
            if binding not in revoked:
                revoked.append(binding)
                del revoked[:-64]
            saved = self.state.get('protocol', {})
            if saved.get('plan') == plan:
                saved.update(phase='aborted', abort=local_abort)
            self.state.pop('destination', None)
            self.state.pop('repair_until', None)
            self.save()
            self.publish_dfs_abort(local_abort)
        radar = self.state.get('dfs_radar', {})
        if radar.get('pending') and radar.get('plan') in plans:
            self.radar_fallback(plans[radar['plan']])
            radar['pending'] = False
            self.save()
        processing_path.unlink(missing_ok=True)

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

    def release_dfs_candidate(self, reason='no-intention'):
        """Retire rejected preparation without touching an agreed main RDD."""
        if self.state.get('protocol', {}).get('phase') in ('prepared', 'committed'):
            return
        path = background_hold_path()
        if path:
            raw = read_json(path)
            if raw.get('owner'):
                from manet_bgobs import write_command
                write_command(path, 'stop')
                print(f'ACS DFS stopped candidate {raw.get("freq_mhz")}: {reason}', flush=True)

    def prepare_dfs(self, view, records, now):
        """Reconcile one authenticated component-wide background CAC intent."""
        prep = self.state.get('dfs_prepare')
        if (not view or any(x is None or x.get('dfs_reason') == 'hold-unavailable'
                            for x in view.values())) and (
                isinstance(prep, dict) and type(prep.get('expires')) is int and
                now < prep['expires'] <= now + 1200):
            # Missing observations cannot revoke an already authorized CAC.
            # They still prevent voting or activation. Its fixed expiry stays.
            return
        if not view or any(not x or not x.get('dfs_capable') for x in view.values()):
            self.state.pop('dfs_prepare', None)
            self.release_dfs_candidate('capability-or-view-lost')
            return
        participants = {m: x['boot'] for m, x in view.items()}
        prep = self.state.get('dfs_prepare')
        for sender, record in records.items():
            offered = record.get('dfs_prepare')
            if (isinstance(offered, dict) and offered.get('leader') == sender and
                    offered.get('participants') == participants and
                    sender == min(view)):
                prep = offered
        valid = (isinstance(prep, dict) and prep.get('participants') == participants and
                 prep.get('leader') == min(view) and
                 type(prep.get('expires')) is int and now < prep['expires'] <= now + 1200 and
                 type(prep.get('created')) is int and prep['created'] <= now and
                 prep.get('frequency') in protocol.DFS_CHANNELS and
                 isinstance(prep.get('nonce'), str) and protocol.TOKEN.fullmatch(prep['nonce']) and
                 all(prep['frequency'] in x.get('dfs_candidates', []) for x in view.values()) and
                 all(protocol.compatible(prep.get('fallback', {}), x) for x in view.values()) and
                 set(prep.get('fallback', {})) == {'5'} and
                 prep['fallback']['5'] in protocol.CHANNELS['5'])
        if not valid or boot_time() < self.state.get('dfs_cooldown_until', 0):
            reason = 'malformed-intention'
            if prep is None:
                reason = 'no-intention'
            elif isinstance(prep, dict):
                if prep.get('participants') != participants:
                    reason = 'participant-boots-changed'
                elif prep.get('leader') != min(view):
                    reason = 'coordinator-changed'
                elif type(prep.get('expires')) is int and now >= prep['expires']:
                    reason = 'intention-expired'
                elif any(prep.get('frequency') not in x.get('dfs_candidates', []) for x in view.values()):
                    reason = 'candidate-no-longer-permitted'
            if boot_time() < self.state.get('dfs_cooldown_until', 0):
                reason = 'radar-cooldown'
            self.state.pop('dfs_prepare', None)
            self.release_dfs_candidate(reason)
            return
        self.state['dfs_prepare'] = prep
        self.save()
        iface = self.interfaces().get('5', (None,))[0]
        from manet_bgobs import observer_path, write_command
        from manet_rx_policy import reconcile_receive_paths
        observer = observer_path(iface) if iface else None
        if observer is None:
            return
        path = observer.parent.parent / 'background_hold'
        with (self.run_dir / ('manet-bgobs-' + observer.parents[1].name + '.lock')).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            raw = read_json(path)
            if raw.get('main_active') and raw.get('main_freq_mhz') == prep['frequency']:
                return
            if raw.get('owner'):
                if raw.get('freq_mhz') != prep['frequency']:
                    write_command(path, 'stop')
                return
            if reconcile_receive_paths(observer, True) != 'reserved':
                return
            if not raw.get('enabled'):
                write_command(path, 'enable')
            dfs.start_cac(iface, prep['frequency'])

    def prepared_dfs_proposal(self, view, now):
        """Finish the frozen selection after CAC, without rescoring its RDD2."""
        prep = self.state['dfs_prepare']
        return protocol.make_plan(view, {'5': prep['frequency']},
                                  prep.get('limp', False), now, secrets.token_hex(16),
                                  fallback=prep['fallback'])

    def score(self, view, *, candidates=False, fallback=False):
        """Score the connected view and return per-band MHz plus limp mode."""
        # One coordinator freezes the score result. Receivers never re-score it.
        env = dict(os.environ)
        incumbents_by_band = {}
        dfs_candidate = None
        holds = [protocol.live_hold(s, int(time.time()))
                 for s in view.values()]
        if holds and all(holds):
            candidate = holds[0]['frequency']
            try:
                protocol.dfs_contract(view, candidate,
                                      protocol.dfs_fallback(view),
                                      int(time.time()))
                dfs_candidate = candidate
            except protocol.AgreementError:
                pass
        dfs_supported = set()
        if candidates and view and all(x and x.get('dfs_capable') for x in view.values()):
            dfs_supported = set.intersection(*(set(x.get('dfs_candidates', [])) for x in view.values()))
        if dfs_candidate and not fallback:
            dfs_supported.add(dfs_candidate)
        # DFS candidates require measured occupancy, never the scorer's
        # unknown-busy substitution. Fallback scoring remains non-DFS.
        env['ACS_DFS_CHANNELS'] = ' '.join(map(str, sorted(dfs_supported))) if not fallback else ''
        # Registry RF observations include every reachable node, regardless of
        # which radio carries Alfred. Exclude cached records of departed groups.
        env['ACS_MEMBERS'] = ' '.join('NODE_' + mac.replace(':', '') for mac in view)
        for band, suffix in [('2.4', '2_4'), ('5', '5_0')]:
            # Candidate support is the union from ready nodes. The common
            # incumbent is the most reported MHz, breaking ties by lower MHz.
            supported = sorted({f for s in view.values() if s and s['ready'] for f in s['allowed'].get(band, [])})
            if band == '5':
                if fallback:
                    supported = sorted(set.intersection(*(set(x['allowed'].get('5', [])) for x in view.values())))
                else:
                    supported = sorted(set(supported) | dfs_supported)
            incumbents = Counter(s['current'][band] for s in view.values() if s and band in s['current'])
            incumbent = min(incumbents, key=lambda f: (-incumbents[f], f)) if incumbents else ''
            incumbents_by_band[band] = incumbent
            env['ACS_CHANNELS_' + suffix] = ' '.join(map(str, supported))
            env['ACS_CURRENT_' + suffix] = '' if fallback and band == '5' else str(incumbent)
        fd, path = tempfile.mkstemp(dir=self.run_dir, prefix='.acs-score-')
        os.close(fd)
        try:
            # --score writes WINNER_*, URGENT_* and LIMP_MODE assignments
            # to this private output path. Bound the scoring process to 15 s.
            env['OUTPUT_FILE'] = path
            subprocess.run(['bash', str(TOOLS / 'channel-election.sh'), '--score'], env=env,
                           check=True, capture_output=True, text=True, timeout=15)
            values = dict(line.split('=', 1) for line in Path(path).read_text().splitlines())
            channels = {}
            round_id = int(time.time()) // protocol.ROUND_SECONDS
            challenge = {} if fallback else self.state.setdefault('challenge', {})
            for band, suffix in [('2.4', '2_4'), ('5', '5_0')]:
                value = values.get('WINNER_' + suffix, '')
                choices = protocol.CHANNELS[band] | (dfs_supported if band == '5' and not fallback else set())
                if value and int(value) in choices:
                    winner = int(value)
                    incumbent = incumbents_by_band[band]
                    if (fallback or winner == incumbent or not incumbent
                            or values.get('URGENT_' + suffix) == 'true'):
                        challenge.pop(band, None)
                    else:
                        previous = challenge.get(band, {})
                        last_round = previous.get('round')
                        count = 1
                        if previous.get('incumbent') == incumbent:
                            if last_round == round_id:
                                count = previous['count']
                            elif last_round == round_id - 1:
                                count = previous['count'] + 1
                        # The challenger may change between rounds. Count any
                        # preference win against this same incumbent.
                        challenge[band] = dict(incumbent=incumbent,
                                               round=round_id, count=count)
                        if count < CHALLENGE_ROUNDS:
                            if last_round != round_id:
                                print(f'ACS holding {band} GHz {incumbent}: '
                                      f'{winner} preferred '
                                      f'({count}/{CHALLENGE_ROUNDS})',
                                      flush=True)
                            winner = incumbent
                    channels[band] = winner
                else:
                    challenge.pop(band, None)
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

    def adopt_destination(self, destination, local, cause):
        """Persist one adoption and its hold before reconciling the radios."""
        plan = destination['plan']
        previous = self.state.get('destination', {}).get('plan')
        fresh = previous is None or protocol.digest(previous) != protocol.digest(plan)
        self.state.update(destination=destination, recovered_round=plan['round'])
        if fresh:
            elapsed = boot_time()
            if plan['moving']:
                self.state['hold_until'] = elapsed + protocol.RECOVERY_SECONDS
            self.state['repair_until'] = elapsed + 120
        self.save()
        if fresh:
            return dict(plan=protocol.digest(plan), round=plan['round'], cause=cause,
                        old_live=local['current'],
                        old_configured={b: data[2] for b, data in self.interfaces().items()})
        return None

    def log_transition(self, saved, updated, local):
        """Journal each ordinary or DFS protocol transition once."""
        plan = updated.get('plan')
        if not plan or all(saved.get(k) == updated.get(k) for k in ('round', 'phase', 'plan')):
            return
        print('ACS transition ' + json.dumps(dict(
            plan=protocol.digest(plan), round=plan['round'], proposer=plan['leader'],
            channels=plan['channels'], previous=saved.get('phase', 'idle'),
            phase=updated['phase'], reason=updated.get('expiry_reason',
                updated.get('abort', {}).get('reason', '')),
            votes=len(updated.get('commit', {}).get('approvals', updated.get('votes', {}))),
            required=protocol.majority(plan), participants=len(plan['participants']),
            readiness=local.get('readiness', 'ready' if local['ready'] else 'unready'),
            hold_seconds=max(0, self.state.get('hold_until', 0) - boot_time())), sort_keys=True), flush=True)

    def apply(self, channels, limp=False, *, challenged=False, discovering=False, reason='Agreed channel plan'):
        """Apply compatible Wi-Fi MHz, verify landing and publish the result."""
        # Clockless callers may follow a consumed nonce reply or park a
        # disconnected searcher at fixed anchors, never run a timed election.
        if not challenged and not discovering:
            require_clock()
        if channels.get('5') in protocol.DFS_CHANNELS:
            armed = self.state.get('protocol', {})
            plan = armed.get('plan', {})
            if (plan.get('channels') != channels or
                    not protocol.valid_commit(armed.get('commit'), plan) or
                    not plan.get('activate_at', 0) <= time.time() <= plan.get('activate_at', 0) + protocol.APPLY_GRACE):
                raise ValueError('DFS cannot activate without a live agreed commitment')
            if not dfs.guardian_ready():
                raise ValueError('DFS guardian is not ready')
        interfaces = self.interfaces()
        local = self.status(int(time.time()))
        if discovering:
            compatible = all(b in interfaces and f in self.rendezvous_allowed.get(b, [])
                             and (clock_ready() or f == rendezvous.ANCHORS[b]) for b, f in channels.items())
        else:
            compatible = protocol.compatible(channels, local)
        if not local['acs'] or not compatible:
            raise ValueError('channel plan is incompatible with this node')
        if channels.get('5') in protocol.DFS_CHANNELS:
            self.arm_dfs_contract(plan)
        changes = []
        for band, freq in channels.items():
            if band not in interfaces:
                continue
            iface, path, configured = interfaces[band]
            if configured != freq:
                write_frequency(path, freq)
            if local['current'][band] != freq:
                changes.append((iface, freq))
        # Every participant initiates mesh CSA at the agreed time. Never
        # restart a supplicant to enter DFS: that destroys CAC ownership.
        for iface, freq in changes:
            command(['iw', 'dev', iface, 'set', 'bitrates'], timeout=2)
            try:
                if freq in protocol.DFS_CHANNELS:
                    dfs.switch_dfs(iface, freq)
                else:
                    command(['iw', 'dev', iface, 'switch', 'freq', str(freq),
                             'HT20', 'beacons', '5'], timeout=2)
            except (OSError, subprocess.SubprocessError, ValueError):
                if freq in protocol.DFS_CHANNELS:
                    # A peer CSA may already be pending. Verify it below.
                    continue
                try:
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
                if freq in protocol.DFS_CHANNELS:
                    command(['ip', 'link', 'set', 'dev', iface, 'down'], timeout=2)
                    raise ValueError('DFS CSA failed to land')
                command(['systemctl', 'restart', f'wpa_supplicant@{iface}.service'], timeout=15)
                for attempt in range(20):
                    if landed():
                        break
                    time.sleep(.5)
                else:
                    raise ValueError(f'{iface} did not reach {freq}')
        if channels.get('5') in protocol.DFS_CHANNELS:
            expected = plan['dfs']['holds'][self.own]
            path = background_hold_path()
            verified = False
            for _ in range(10):
                try:
                    raw = read_json(path) if path else {}
                    verified = (raw.get('main_active') is True and
                        raw.get('main_freq_mhz') == channels['5'] and
                        [raw.get('main_epoch'), raw.get('main_generation')] == expected)
                    if verified:
                        break
                except OSError:
                    pass
                time.sleep(.1)
            if not verified:
                command(['ip', 'link', 'set', 'dev', interfaces['5'][0], 'down'], timeout=2)
                raise ValueError('DFS CSA lacks the agreed main detector token')
            self.store_dfs_contract(plan, interfaces['5'][0], operating=True)
        elif '5' in channels and '5' in interfaces:
            self.retire_dfs_contract(interfaces['5'][0])
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
        if local['current'].get('5') in protocol.DFS_CHANNELS:
            return False  # Peer loss is never a DFS fallback authority.
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
            if actual is None or actual == target or target in protocol.DFS_CHANNELS:
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
        # A lock holder or failed read must not erase the last observations.
        # Carry only display values, not errors or membership freshness: the
        # manager's peer scan gate still requires a validated view this tick.
        # Drop them 60 s after the last radio read, so repeated failures do
        # not keep an old channel and peer count looking fresh.
        # Channels and membership come from separate reads, so each keeps its
        # own clock: a working radio read must not keep old peers alive.
        clock = time.monotonic()
        radio = clock - getattr(self, 'ui_observed_at', -1e9) <= 60
        peers = clock - getattr(self, 'ui_members_at', -1e9) <= 60
        self.ui_status = {'clock_ready': timed,
                          'current': self.ui_status.get('current', {}) if radio else {},
                          'members': self.ui_status.get('members', []) if peers else [],
                          'reachable': self.ui_status.get('reachable') if peers else None}
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
        except Exception as exc:
            # A lock holder or failed observation breaks the idle grace period.
            self.drift_since.clear()
            self.ui_status['error'] = 'Channel recovery could not read or apply radio state: ' + str(exc)
            raise
        finally:
            saved = self.state.get('protocol', {})
            plan = saved.get('plan', {})
            hold_left = max(0, self.state.get('hold_until', 0) - boot_time())
            self.ui_status.update(boot=self.boot, monotonic=time.monotonic(),
                                  phase=saved.get('phase', 'idle'),
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
                self.process_dfs_aborts({}, now)
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
            try:
                records = self.receive(74, now)
            except (OSError, subprocess.SubprocessError):
                records = {}
            self.process_dfs_aborts(records, now)
            epoch = manet_clock.observe()['epoch']
            if self.state.get('clock_boot') != self.boot or self.state.get('clock_epoch', epoch) != epoch:
                # A new boot or a clock jump starts a voting session. Drop
                # rounds from the old clock, which may have been ahead.
                previous = self.state.get('destination')
                timers = {key: self.state[key] for key in LOCAL_TIMERS if key in self.state}
                for key in ('dfs_revoked', 'dfs_aborted_plans'):
                    if key in self.state:
                        timers[key] = self.state[key]
                radar = self.state.get('dfs_radar', {})
                if radar.get('abort', {}).get('boot') == self.boot:
                    timers['dfs_radar'] = radar
                self.state = dict(timers, clock_boot=self.boot, timer_boot=self.boot, clock_epoch=epoch)
                # Re-attest only a validated plan operating after boot-timer
                # reconciliation.
                if protocol.live_destination(previous, self.status(now), now):
                    self.state['destination'] = previous
                self.save()
                self.last_publish = None
                self.capabilities.clear()
            self.state.setdefault('clock_epoch', epoch)
            local = self.status(now)
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
            # Reuse this validated connected view for the manager's RF gate.
            # Missing membership closes only the peer-triggered scan path.
            if view is not None:
                self.ui_members_at = time.monotonic()
                self.ui_status.update(
                    members=['NODE_' + mac.replace(':', '') for mac in view],
                    reachable=len(view) - 1, observed_boot=boot_time())
            now = int(time.time())  # Discovery may time out. Refresh time.
            destinations = self.connected_destinations(view, records, local, now)
            destinations = protocol.network_destinations(destinations, view or {})
            conflicting = protocol.conflicting_destinations(destinations.values())
            self.ui_status['conflicting'] = conflicting
            if conflicting and self.state.get('hold_until', 0) > boot_time():
                # Lift the recollection hold when groups reunite on different
                # channels. Their next shared scan/round includes the whole
                # connected network.
                self.state.pop('hold_until', None)
                local = self.status(now)
                view[self.own] = local
            try:
                self.prepare_dfs(view, records, now)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                self.ui_status['dfs_prepare'] = str(exc)
            saved = self.state.get('protocol', {})
            proposal = None
            challenge_before = deepcopy(self.state.get('challenge', {}))
            leader = protocol.coordinator(view) if view else None
            proposing = (protocol.PROPOSE_FROM <= now % protocol.ROUND_SECONDS
                         <= protocol.PROPOSE_UNTIL)
            if proposing and leader and leader != self.own:
                # Another coordinator starts its own streak. Readiness resets
                # between rounds, so only count leadership during proposals.
                self.state.pop('challenge', None)
            if (view and leader == self.own and proposing and protocol.proposal_possible(view)
                    and (saved.get('round', -1) < now // protocol.ROUND_SECONDS or saved.get('phase') == 'idle')):
                if self.state.get('dfs_prepare'):
                    # Selection and measured fallback were frozen before CAC.
                    # RDD2 now measures only that candidate. Missing other RF
                    # reports or preference hysteresis must not erase it.
                    try:
                        proposal = self.prepared_dfs_proposal(view, now)
                    except protocol.AgreementError:
                        pass  # Wait for every fresh live hold and readiness.
                else:
                    scored = self.score(view, candidates=True)
                    if (scored and scored[0].get('5') not in protocol.DFS_CHANNELS
                            and not protocol.proposal_possible(view, scored[0])):
                        scored = None
                    if scored and scored[0].get('5') in protocol.DFS_CHANNELS:
                        fallback_score = self.score(view, fallback=True)
                        fallback = {'5': fallback_score[0]['5']} if fallback_score and fallback_score[0].get('5') else None
                        frequency = scored[0]['5']
                        try:
                            protocol.dfs_contract(view, frequency, fallback, now)
                        except protocol.AgreementError:
                            if self.own == min(view) and fallback:
                                self.state['dfs_prepare'] = dict(leader=self.own,
                                    participants={m: x['boot'] for m, x in view.items()},
                                    frequency=frequency, fallback=fallback, limp=scored[1], created=now,
                                    expires=now + 1200, nonce=secrets.token_hex(16))
                                self.prepare_dfs(view, {}, now)
                            scored = None
                    else:
                        fallback = None
                        self.state.pop('dfs_prepare', None)
                    if scored:
                        now = int(time.time())
                        # The proposal window is validated again after slow scoring.
                        try:
                            proposal = protocol.make_plan(
                                view, *scored, now, secrets.token_hex(16),
                                fallback=fallback)
                        except protocol.AgreementError:
                            pass  # Slow scoring may leave the proposal window.
                if proposal is None:
                    self.state['challenge'] = challenge_before
            if ('dfs' in saved.get('plan', {}) or
                    (proposal is not None and 'dfs' in proposal)):
                # Discovery and scoring can consume the remainder of a peer
                # lease. Sample Alfred again before deciding, including aborts
                # that arrived during this pass. Never renew a cached record.
                records = self.receive(74, int(time.time()))
                self.process_dfs_aborts(records, int(time.time()))
                saved = self.state.get('protocol', {})
                local = self.status(int(time.time()))
                try:
                    members = self.members(records)
                    view = {m: records.get(m, {}).get('status') for m in members}
                    view[self.own] = local
                    protocol.validate_view(view)
                except (OSError, ValueError, subprocess.SubprocessError):
                    view = None
                now = int(time.time())
            fields = {mac: dict(r.get('protocol', {}))
                      for mac, r in records.items()
                      if isinstance(r.get('protocol', {}), dict)}
            for mac, record in records.items():
                if isinstance(record.get('dfs_abort'), dict):
                    fields.setdefault(mac, {})['abort'] = record['dfs_abort']
            # The protocol returns state to persist, wire fields and a due plan.
            if ('dfs' in saved.get('plan', {}) and
                    saved.get('phase') in ('prepared', 'committed') and
                    local.get('dfs_reason') == 'hold-unavailable' and
                    now <= saved['plan']['activate_at'] + protocol.APPLY_GRACE):
                # Preserve the bounded promise, but publish no clearance or
                # new vote while the detector cannot be read.
                updated, outgoing, plan = saved, {}, None
            else:
                updated, outgoing, plan = protocol.advance(saved, self.own, now, view, fields, proposal, local)
            if plan is not None and 'dfs' in plan:
                # Re-read under the channel lock after discovery, scoring and
                # persistence delays. Neither a prior lease nor FINISHED is
                # activation authority. Every live token must still match.
                try:
                    self.check_dfs_activation(plan, records)
                except BlockingIOError:
                    # No fresh token, no CSA. Retry the saved commitment only
                    # inside its existing apply window. Never renew a lease.
                    raise
                except (ValueError, OSError, subprocess.SubprocessError) as exc:
                    updated['phase'] = 'aborted'
                    updated['abort'] = protocol.dfs_abort(
                        plan, self.own, 'clearance_lost')
                    outgoing = {'abort': updated['abort']}
                    self.ui_status['dfs_activation'] = str(exc)
                    plan = None
            self.state['protocol'] = updated
            if ('dfs' in updated.get('plan', {}) and updated.get('phase') == 'committed'
                    and local.get('dfs_capable')
                    and protocol.valid_commit(updated.get('commit'), updated['plan'])):
                self.arm_dfs_contract(updated['plan'])
            if 'dfs' in updated.get('plan', {}) and updated.get('phase') in ('prepared', 'committed', 'attempted'):
                self.state.pop('dfs_prepare', None)
            if not saved.get('plan') and 'dfs' in updated.get('plan', {}):
                # An abort can arrive with the first proposal, before this
                # node has a saved contract. Check both types before its vote
                # is persisted/published, including stale live hold readback.
                self.process_dfs_aborts(records, int(time.time()))
                if updated.get('phase') == 'aborted':
                    outgoing = ({'abort': updated['abort']}
                                if updated.get('abort') else {})
                    plan = None
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
            adoption = None
            if plan is not None and local['acs']:
                adoption = self.adopt_destination(
                    {'plan': plan, 'commit': updated['commit']}, local, 'commit')
            elif plan is not None:
                plan = None  # A compatible static participant ACKs without applying ACS.
            self.save()  # Retry failed writes even if memory already contains the new state.
            self.log_transition(saved, updated, local)
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
                    # Both paths adopt config and the certificate. Only a
                    # mismatch in live MHz requires tuning.
                    if all(local['current'].get(b) == f for b, f in p['channels'].items() if b in local['current']):
                        learned.append(entry)
                    else:
                        candidates.append(entry)
                if candidates:
                    apply_reason = 'Returning to the connected mesh channel plan'
                    # Prefer the latest round, with the digest breaking ties.
                    destination = max(candidates, key=lambda c: c[:2])[2]
                    plan = destination['plan']
                    adoption = self.adopt_destination(destination, local, 'connected-recovery')
                elif learned and self.own not in destinations:
                    # A node recovered by a clockless/lobby reply can now keep
                    # the agreed plan available even after its helper leaves.
                    destination = max(learned, key=lambda c: c[:2])[2]
                    plan = destination['plan']
                    adoption = self.adopt_destination(destination, local, 'already-landed')
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
                if adoption:
                    adoption.update(new_live=local['current'],
                        new_configured={b: data[2] for b, data in self.interfaces().items()},
                        hold_seconds=max(0, self.state.get('hold_until', 0) - boot_time()))
                    print('ACS adoption ' + json.dumps(adoption, sort_keys=True), flush=True)
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
                if self.state.get('dfs_prepare'):
                    payload['dfs_prepare'] = self.state['dfs_prepare']
                radar = self.state.get('dfs_radar', {})
                if boot_time() < radar.get('publish_until', 0):
                    payload['dfs_abort'] = radar['abort']
                    self.publish_dfs_abort(radar['abort'])
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
                    # A DFS proposal already binds every receiver. Defer the
                    # old full certificate to stay within the wire budget.
                    dfs_proposal = 'dfs' in outgoing.get('proposal', {})
                    if (self.own == min(holders) and 'commit' not in outgoing
                            and not dfs_proposal):
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
        prep = self.state.get('dfs_prepare', {})
        if (prep.get('expires', 0) > now or
                (saved.get('phase') in ('prepared', 'committed') and
                 isinstance(plan, dict) and 'dfs' in plan and
                 saved.get('round') == now // protocol.ROUND_SECONDS and
                 now <= plan['activate_at'] + protocol.APPLY_GRACE)):
            return DFS_INTERVAL
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
    parser.add_argument('action', choices=(
        'run', 'request', 'members', 'helper-encode', 'helper-select',
        'tourguide-exclusions', 'dfs-radar', 'dfs-watch'))
    parser.add_argument('--channels', default='{}', type=json.loads)
    parser.add_argument('--size', type=int)
    parser.add_argument('--exclude', default='')
    parser.add_argument('--stdin', action='store_true')
    parser.add_argument('--current', type=json.loads)
    parser.add_argument('--plan-id')
    parser.add_argument('--epoch')
    parser.add_argument('--generation', type=int)
    parser.add_argument('--hold-path')
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
    if args.action == 'dfs-watch':
        path = args.hold_path or os.environ.get('MANET_DFS_HOLD_PATH')
        return watch_background_radar(path)
    runtime = Runtime()
    now = int(time.time())
    if args.action == 'dfs-radar':
        runtime.radar_detected(args.plan_id, args.epoch, args.generation)
    elif args.action == 'tourguide-exclusions':
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
        payload['dfs_versions'] = [protocol.DFS_VERSION]
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
