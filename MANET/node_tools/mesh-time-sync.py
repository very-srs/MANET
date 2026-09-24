#!/usr/bin/env python3
"""Own chrony roles: local GPS/uplink sources, or occasional bounded peer syncs.

No advertisements are sent here. The node manager publishes IS_NTP_SERVER in
its existing Alfred telemetry from the two verified source markers.
"""
import argparse
import fcntl
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import time

MAC = re.compile(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}')
FIELD = re.compile(r'NODE_([0-9a-fA-F]{12})_(MAC_ADDRESSES|IPV4_ADDRESS|IS_NTP_SERVER|NODE_STATE)=(.*)')
MAX_CORRECTION = 0.1  # Remaining system-clock correction, not an absolute accuracy claim.
ATTEMPT_SECONDS = 90
RETRY_SECONDS = 300
REFRESH_SECONDS = 6 * 60 * 60
REFRESH_JITTER_SECONDS = 10 * 60


def atomic_text(path, text):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def candidates(registry, rows, own, network):
    """Rank canonical nodes by their selected BATMAN_V route, not table columns."""
    if not MAC.fullmatch(own) or not isinstance(rows, list):
        raise ValueError('invalid local identity or originators response')
    nodes, aliases, owners = {}, {}, {}
    for line in registry.splitlines():
        match = FIELD.fullmatch(line)
        if not match:
            continue
        key, field, raw = match.groups()
        mac = ':'.join(key[i:i + 2] for i in range(0, 12, 2)).lower()
        values = shlex.split(raw)
        if len(values) != 1:
            raise ValueError('invalid registry assignment')
        nodes.setdefault(mac, {})[field] = values[0]
    for mac, fields in nodes.items():
        for alias in [mac, *fields.get('MAC_ADDRESSES', '').lower().split(',')]:
            if not alias:
                continue
            if not MAC.fullmatch(alias) or aliases.get(alias, mac) != mac:
                raise ValueError('invalid or ambiguous registry identity')
            aliases[alias] = mac
        try:
            addr = ipaddress.IPv4Address(fields.get('IPV4_ADDRESS', ''))
        except ipaddress.AddressValueError:
            continue
        if addr in owners:
            owners[addr] = None  # Conflicting allocations cannot be used as an NTP identity.
        else:
            owners[addr] = mac
    routes = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('orig_address'), str) or not MAC.fullmatch(row['orig_address'].lower()):
            raise ValueError('invalid originator record')
        if row.get('best') is not True:
            continue
        metric, age = row.get('throughput'), row.get('last_seen_msecs')
        if type(metric) is not int or metric <= 0 or type(age) is not int or not 0 <= age <= 30000:
            continue
        mac = aliases.get(row['orig_address'].lower())
        if mac:
            routes[mac] = max(routes.get(mac, 0), metric)
    result = []
    for mac, metric in routes.items():
        fields = nodes[mac]
        if mac == own or fields.get('IS_NTP_SERVER') != 'true' or fields.get('NODE_STATE') == 'SHUTTING_DOWN':
            continue
        try:
            addr = ipaddress.IPv4Address(fields.get('IPV4_ADDRESS', ''))
        except ipaddress.AddressValueError:
            continue
        if addr not in network or addr in (network.network_address, network.broadcast_address) or owners.get(addr) != mac:
            continue
        # Registry timestamps/STALE use the clock we are trying to fix. Current
        # kernel route age and fresh NTP measurements provide bootstrap liveness.
        result.append({'mac': mac, 'address': str(addr), 'metric': metric})
    return sorted(result, key=lambda item: (-item['metric'], item['mac']))


def selected_source(sources):
    # -n sources: mode+selection, numeric address/refid, stratum, poll, reach, LastRx.
    for line in sources.splitlines():
        match = re.fullmatch(r'([\^=#])\*\s+(\S+)\s+\d+\s+(-?\d+)\s+([0-7]+)\s+(\d+)([mhd]?)\s+.*', line.strip())
        if not match:
            continue
        mode, address, poll, reach, age, unit = match.groups()
        poll = int(poll)
        if not -7 <= poll <= 24 or int(reach, 8) == 0:
            return None
        age = int(age) * {'': 1, 'm': 60, 'h': 3600, 'd': 86400}[unit]
        if age > max(30, 2 * 2 ** poll + 10):
            return None
        return {'mode': mode, 'address': address, 'age': age}
    return None


def settled(tracking):
    fields = dict(line.split(':', 1) for line in tracking.splitlines() if ':' in line)
    fields = {k.strip(): v.strip() for k, v in fields.items()}
    try:
        refid = fields['Reference ID'].split()[0].upper()
        correction = float(fields['System time'].split()[0])
        stratum = int(fields['Stratum'])
        return (bool(re.fullmatch('[0-9A-F]{8}', refid)) and refid not in ('00000000', '7F7F0101')
                and 1 <= stratum <= 15 and math.isfinite(correction) and abs(correction) <= MAX_CORRECTION
                and fields['Leap status'] in ('Normal', 'Insert second', 'Delete second'))
    except (KeyError, ValueError, IndexError):
        return False


def chrony_config(network, gps=False, uplink='', peer='', initial=True):
    lines = ['# Managed by mesh-time-sync.py; role changes replace this file.',
             'driftfile /var/lib/chrony/chrony.drift', 'leapsecmode slew']
    if initial:
        lines.append('makestep 0.1 3')
    if gps:
        lines.append('refclock SHM 0 refid GPS precision 1e-1 delay 0.2 poll 4 offset 0.0')
    if uplink:
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', uplink):
            raise ValueError('invalid uplink interface')
        lines += ['pool pool.ntp.org iburst maxsources 2', 'bindacqdevice ' + uplink]
    if peer:
        # Prefer settling within one poll interval instead of the default three;
        # the ordinary Linux slew-rate cap still applies to large corrections.
        lines += ['server ' + str(ipaddress.IPv4Address(peer)) + ' iburst', 'corrtimeratio 1']
    if gps or uplink:
        lines += ['allow ' + str(network), 'allow fd01:ed20:ecb4::/64']
    else:
        lines.append('deny all')
    # No local stratum fallback: an unqualified clock must not masquerade as a source.
    return '\n'.join(lines) + '\n'


class TimeService:
    def __init__(self):
        self.run = Path(os.environ.get('MANET_TIME_RUN_DIR', '/run'))
        self.run.mkdir(parents=True, exist_ok=True)
        self.conf = Path(os.environ.get('MANET_MESH_CONF', '/etc/mesh.conf'))
        self.chrony_conf = Path(os.environ.get('MANET_CHRONY_CONF', '/etc/chrony/chrony.conf'))
        self.sysnet = Path(os.environ.get('MANET_SYS_NET', '/sys/class/net'))
        self.batctl = os.environ.get('BATCTL_PATH', '/usr/sbin/batctl')
        self.registry = Path(os.environ.get('REGISTRY_STATE_FILE', '/run/mesh_node_registry'))
        self.marker = self.run / 'initial_time_synced'
        self.state_path = self.run / 'mesh-time-client.json'
        self.last_message = None
        self.active_config = None
        try:
            self.state = json.loads(self.state_path.read_text())
        except FileNotFoundError:
            self.state = {}
        if not isinstance(self.state, dict):
            raise ValueError('invalid time service state')

    def command(self, args, timeout=5):
        return subprocess.run(args, capture_output=True, text=True, check=True, timeout=timeout,
                              env=dict(os.environ, LC_ALL='C')).stdout

    def log(self, message):
        if message != self.last_message:
            print('TIME-SYNC: ' + message, flush=True)
            self.last_message = message

    def save(self):
        atomic_text(self.state_path, json.dumps(self.state))

    def network(self):
        for line in self.conf.read_text().splitlines():
            key, sep, value = line.partition('=')
            if sep and key.strip() == 'ipv4_network':
                return ipaddress.IPv4Network(value.strip().strip('"\''), strict=False)
        return ipaddress.IPv4Network('10.43.1.0/16', strict=False)

    def roles(self):
        gps = False
        try:
            data = json.loads((self.run / 'gps_status.json').read_text())
            age = time.time() - float(data.get('timestamp', 0))
            gps = data.get('has_fix') is True and math.isfinite(age) and -5 <= age <= 60
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        uplink = ''
        if (self.run / 'mesh-gateway.state').exists():
            try:
                iface = (self.run / 'upstream_iface').read_text().strip()
                if (re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', iface)
                        and (self.sysnet / iface / 'carrier').read_text().strip() == '1'):
                    uplink = iface
            except OSError:
                pass
        return gps, uplink

    def flags(self, gps=False, internet=False):
        for name, enabled in [('mesh-ntp-gps.state', gps), ('mesh-ntp.state', internet)]:
            path = self.run / name
            if enabled:
                path.touch()
            else:
                path.unlink(missing_ok=True)

    def running(self):
        try:
            self.command(['systemctl', 'is-active', '--quiet', 'chrony.service'])
            return True
        except subprocess.CalledProcessError:
            return False

    def configure(self, text, start):
        changed = not self.chrony_conf.exists() or self.chrony_conf.read_text() != text
        if changed:
            self.flags()
            atomic_text(self.chrony_conf, text)
        if start:
            # Remember what this process successfully activated. If restart
            # fails after writing the file, the next pass retries the restart.
            if changed or self.active_config != text:
                self.command(['systemctl', 'restart', 'chrony.service'], timeout=15)
                self.active_config = text
            elif not self.running():
                self.command(['systemctl', 'start', 'chrony.service'], timeout=15)
        elif self.running():
            self.command(['systemctl', 'stop', 'chrony.service'], timeout=15)
            self.active_config = None

    def clock_source(self):
        tracking = self.command(['chronyc', '-n', 'tracking'])
        if not settled(tracking):
            return None
        return selected_source(self.command(['chronyc', '-n', 'sources']))

    def complete(self, source):
        initial = not self.marker.exists()
        if initial:
            # Disarm remaining startup steps in the live daemon as well as its
            # next config. A later GPS/uplink change or peer refresh must slew,
            # not jump backwards through ACS rounds or admin replay timestamps.
            self.command(['chronyc', 'makestep', '0.1', '0'])
            profile = self.chrony_conf.read_text().replace('makestep 0.1 3\n', '')
            atomic_text(self.chrony_conf, profile)
            self.active_config = profile
            atomic_text(self.marker, source + '\n')
        now = time.monotonic()
        if 'refresh_jitter' not in self.state:
            self.state['refresh_jitter'] = secrets.randbelow(REFRESH_JITTER_SECONDS + 1)
        self.state['last_sync'] = now
        self.state['refresh_at'] = now + REFRESH_SECONDS + self.state['refresh_jitter']
        self.state['last_source'] = source
        self.save()
        if initial:
            self.log('Clock settled from ' + source + '.')

    def step(self):
        now = time.monotonic()
        gps, uplink = self.roles()
        network = self.network()
        initial = not self.marker.exists()
        if gps or uplink:
            if self.state.pop('pending', None):
                self.save()
            try:
                self.configure(chrony_config(network, gps, uplink, initial=initial), start=True)
                source = self.clock_source()
            except (OSError, ValueError, subprocess.SubprocessError):
                self.flags()
                raise
            gps_ready = bool(source and gps and source['mode'] == '#' and source['address'] == 'GPS')
            internet_ready = bool(source and uplink and source['mode'] == '^')
            if gps_ready or internet_ready:
                try:
                    self.complete('GPS' if gps_ready else 'internet')
                except (OSError, ValueError, subprocess.SubprocessError):
                    self.flags()
                    raise
                self.flags(gps_ready, internet_ready)
                self.log('Serving time from ' + ('GPS.' if gps_ready else 'the direct uplink.'))
            else:
                self.flags()
                self.log('Waiting for the local GPS/uplink clock to settle; not advertising NTP.')
            return 15

        self.flags()
        idle = chrony_config(network, initial=initial)
        if not initial and now < self.state.get('refresh_at', 0):
            self.configure(idle, start=False)
            if self.state.pop('pending', None):
                self.save()
            self.log('Clock refresh is not due; mesh NTP polling is stopped.')
            return 15  # Local role checks only: GPS or an uplink can appear later.

        pending = self.state.get('pending')
        if pending:
            if now < pending['deadline']:
                try:
                    self.configure(chrony_config(network, peer=pending['address'], initial=initial), start=True)
                    source = self.clock_source()
                    now = time.monotonic()
                    if (now < pending['deadline'] and source and source['mode'] == '^'
                            and source['address'] == pending['address']
                            and source['age'] <= now - pending['started'] + 1):
                        self.complete(pending['address'])
                        self.configure(chrony_config(network, initial=False), start=False)
                        self.state.pop('pending')
                        self.save()
                        self.log('Peer sync complete; mesh NTP polling is stopped until the next refresh.')
                        return 15
                except (OSError, ValueError, subprocess.SubprocessError):
                    # Failed commands are not evidence of a synchronized clock.
                    pass
            now = time.monotonic()
            if now < pending['deadline']:
                return 2
            self.configure(idle, start=False)
            self.state.pop('pending')
            self.state.setdefault('retry_after', {})[pending['mac']] = now + RETRY_SECONDS
            self.save()
            self.log('Peer sync timed out; retrying discovery without continuous polling.')
            return 30

        self.configure(idle, start=False)
        try:
            own = (self.sysnet / 'br0/address').read_text().strip().lower()
            rows = json.loads(self.command([self.batctl, 'meshif', 'bat0', 'originators_json']))
            peers = candidates(self.registry.read_text(), rows, own, network)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self.log('Discovery unavailable; will retry: ' + str(exc))
            return 30
        peers = [p for p in peers if now >= self.state.get('retry_after', {}).get(p['mac'], 0)]
        if not peers:
            self.log('No usable advertised NTP peer yet; will retry.')
            return 30
        peer = peers[0]
        self.state['pending'] = dict(peer, started=now, deadline=now + ATTEMPT_SECONDS)
        self.save()  # Preserve the deadline across service restarts.
        self.configure(chrony_config(network, peer=peer['address'], initial=initial), start=True)
        self.log('Syncing from ' + peer['address'] + ' (' + peer['mac'] + ').')
        return 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', help='perform one nonblocking reconciliation')
    args = parser.parse_args()
    service = TimeService()
    with (service.run / 'mesh-time-sync.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        while True:
            try:
                delay = service.step()
            except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
                service.flags()
                service.log('Deferred: ' + str(exc))
                delay = 15
            if args.once:
                return 0
            time.sleep(delay)


if __name__ == '__main__':
    sys.exit(main())
