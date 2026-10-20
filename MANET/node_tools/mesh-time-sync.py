#!/usr/bin/env python3
"""Own chrony roles: local GPS/uplink sources, or on-demand mesh time windows.

No advertisements are sent here. The node manager publishes IS_NTP_SERVER in
its existing Alfred telemetry from the two verified source markers. Other
nodes keep chronyd stopped behind chrony.socket: a peer's request wakes it,
and chrony's orphan mode picks one mesh clock when no real source exists.
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

# Canonical MACs use six lowercase hex octets separated by colons.
MAC = re.compile(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}')
# Read only the four registry fields used for peer eligibility and identity.
FIELD = re.compile(r'NODE_([0-9a-fA-F]{12})_(MAC_ADDRESSES|IPV4_ADDRESS|IS_NTP_SERVER|NODE_STATE)=(.*)')
MAX_CORRECTION = 0.1  # Remaining correction in seconds, not accuracy.
# A refresh window that found no peer retries after 5 minutes.
RETRY_SECONDS = 300
# Verified sync defers the next refresh for a day plus up to ten minutes.
REFRESH_SECONDS = 24 * 60 * 60
REFRESH_JITTER_SECONDS = 10 * 60
# Later windows and windows opened by a peer's request last 3 minutes. The
# first window of a boot runs 20 minutes so chrony learns the crystal's drift.
WINDOW_SECONDS = 180
FIRST_WINDOW_SECONDS = 20 * 60
# With no synced clock in reach for 3 minutes, unsynced nodes free-run.
COLD_SECONDS = 180
# A free-running reference counts as synced after 60 s without a better source.
MASTER_SECONDS = 60
# A peer unseen for 10 minutes counts as a new arrival when it returns.
JOIN_SECONDS = 10 * 60
MAX_PEERS = 8
# Orphan strata: a lower stratum always wins, and among equal strata the
# lowest address serves. Real GPS and NTS sources sit at strata 1 to 3.
STRATUM_TRUSTED = 8  # synced from GPS or internet earlier this boot
STRATUM_MESH = 10    # synced only from other nodes
STRATUM_COLD = 12    # never synced, and no synced clock in reach
# One source per operator: three remain to vote if one is unreachable.
# Operators: Cloudflare, Netnod, PTB and SIDN Labs.
NTS_SERVERS = ('time.cloudflare.com', 'sth1.nts.netnod.se', 'ptbtime1.ptb.de', 'ntppool1.time.nl')


def atomic_text(path, text):
    """Write and fsync a sibling file before replacing the destination."""
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def candidates(registry, rows, own, network, servers_only=True):
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
        # Decode shell quoting as data without executing registry assignments.
        values = shlex.split(raw)
        if len(values) != 1:
            raise ValueError('invalid registry assignment')
        nodes.setdefault(mac, {})[field] = values[0]
    for mac, fields in nodes.items():
        # Radio MAC aliases map route rows back to the advertised node.
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
            owners[addr] = None  # Conflicting NTP identity.
        else:
            owners[addr] = mac
    routes = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('orig_address'), str) or not MAC.fullmatch(row['orig_address'].lower()):
            raise ValueError('invalid originator record')
        if row.get('best') is not True:
            continue
        metric, age = row.get('throughput'), row.get('last_seen_msecs')
        # Only positive selected-route metrics seen within 30 s can rank.
        if type(metric) is not int or metric <= 0 or type(age) is not int or not 0 <= age <= 30000:
            continue
        mac = aliases.get(row['orig_address'].lower())
        if mac:
            # Several radio routes can belong to one node. Keep its best rate.
            routes[mac] = max(routes.get(mac, 0), metric)
    result = []
    for mac, metric in routes.items():
        fields = nodes[mac]
        server = fields.get('IS_NTP_SERVER') == 'true'
        if mac == own or (servers_only and not server) or fields.get('NODE_STATE') == 'SHUTTING_DOWN':
            continue
        try:
            addr = ipaddress.IPv4Address(fields.get('IPV4_ADDRESS', ''))
        except ipaddress.AddressValueError:
            continue
        # Reject non-host addresses and allocations claimed by several nodes.
        if addr not in network or addr in (network.network_address, network.broadcast_address) or owners.get(addr) != mac:
            continue
        # Kernel route age and fresh NTP measurements establish bootstrap
        # liveness even when registry telemetry is marked STALE.
        result.append({'mac': mac, 'address': str(addr), 'metric': metric, 'server': server})
    # Highest route throughput wins, with a stable canonical-MAC tie break.
    return sorted(result, key=lambda item: (-item['metric'], item['mac']))


def selected_source(sources):
    """Parse a recent selected chrony source with nonzero reachability."""
    # -n sources: mode+selection, address/refid, stratum, poll, reach, LastRx.
    for line in sources.splitlines():
        # The asterisk marks selection. Reach is octal and LastRx has an
        # optional minute, hour or day suffix. Headers cannot match.
        match = re.fullmatch(r'([\^=#])\*\s+(\S+)\s+\d+\s+(-?\d+)\s+([0-7]+)\s+(\d+)([mhd]?)\s+.*', line.strip())
        if not match:
            continue
        mode, address, poll, reach, age, unit = match.groups()
        poll = int(poll)
        if not -7 <= poll <= 24 or int(reach, 8) == 0:
            return None
        age = int(age) * {'': 1, 'm': 60, 'h': 3600, 'd': 86400}[unit]
        # Poll is a base-two seconds exponent. Allow two intervals plus 10 s,
        # with a 30 s floor for short polls.
        if age > max(30, 2 * 2 ** poll + 10):
            return None
        return {'mode': mode, 'address': address, 'age': age}
    return None


def settled(tracking):
    """Validate chrony's reference, stratum, leap state and correction size."""
    fields = dict(line.split(':', 1) for line in tracking.splitlines() if ':' in line)
    fields = {k.strip(): v.strip() for k, v in fields.items()}
    try:
        refid = fields['Reference ID'].split()[0].upper()
        correction = float(fields['System time'].split()[0])
        stratum = int(fields['Stratum'])
        # Reject zero and local-reference IDs even with a small correction.
        return (bool(re.fullmatch('[0-9A-F]{8}', refid)) and refid not in ('00000000', '7F7F0101')
                and 1 <= stratum <= 15 and math.isfinite(correction) and abs(correction) <= MAX_CORRECTION
                and fields['Leap status'] in ('Normal', 'Insert second', 'Delete second'))
    except (KeyError, ValueError, IndexError):
        return False


def local_active(tracking):
    """Return whether this node is serving its own clock as the orphan reference."""
    fields = dict(line.split(':', 1) for line in tracking.splitlines() if ':' in line)
    fields = {k.strip(): v.strip() for k, v in fields.items()}
    try:
        return (fields['Reference ID'].split()[0].upper() == '7F7F0101'
                and fields['Leap status'] == 'Normal')
    except (KeyError, IndexError):
        return False


def nts_authenticated(authdata, address):
    """Require a matching NTS source with negotiated key type and length."""
    # Join -n authdata to -n sources by the full NTP address (also for IPv6),
    # not reverse DNS or the potentially different NTS-KE server address.
    for line in authdata.splitlines():
        fields = line.split()
        if len(fields) != 10 or fields[0] != address:
            continue
        try:
            # NTS alone can mean KE is still pending. Require negotiated keys.
            # KeyID/Last can be 0/- when cookies were restored from disk.
            return fields[1] == 'NTS' and int(fields[3]) > 0 and int(fields[4]) > 0
        except ValueError:
            return False
    return False


def chrony_config(network, gps=False, uplink='', peers=(), stratum=None, initial=True):
    """Build a chrony profile for local sources or mesh peers and orphan mode."""
    lines = ['# Managed by mesh-time-sync.py; role changes replace this file.',
             'driftfile /var/lib/chrony/chrony.drift', 'leapsecmode slew']
    if initial:
        # Permit corrections over 0.1 s to step only on the first three updates.
        lines.append('makestep 0.1 3')
    if gps:
        lines.append('refclock SHM 0 refid GPS precision 1e-1 delay 0.2 poll 4 offset 0.0')
    if uplink:
        # Restrict interface text before placing it in a configuration line.
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', uplink):
            raise ValueError('invalid uplink interface')
        lines += ['server ' + host + ' iburst nts' for host in NTS_SERVERS]
        # chrony 4.6 binds both NTP (UDP 123) and NTS-KE (TCP 4460) clients.
        lines += ['bindacqdevice ' + uplink, 'ntsdumpdir /var/lib/chrony']
        if initial:
            # Only dates are waived, until the first clock update. Certificate
            # signatures, hostname and trust chain are still verified.
            lines.append('nocerttimecheck 1')
    if peers:
        lines += ['server ' + str(ipaddress.IPv4Address(peer)) + ' iburst' for peer in peers]
        # Settle within one poll interval instead of the default three.
        # The Linux slew-rate cap still applies to large corrections.
        lines.append('corrtimeratio 1')
    if stratum is not None:
        # Every orphan polls the others; only the best one serves its own clock.
        lines.append('local stratum %d orphan distance 0.1' % int(stratum))
    if gps or uplink or stratum is not None:
        lines += ['allow ' + str(network), 'allow fd01:ed20:ecb4::/64']
    else:
        # An unsynced node never serves its own clock.
        lines.append('deny all')
    return '\n'.join(lines) + '\n'


class TimeService:
    """Reconcile chrony roles, verified source flags and mesh time windows."""

    def __init__(self):
        """Locate node state and restore this boot's saved client deadlines."""
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
        self.socket_ready = False
        try:
            self.state = json.loads(self.state_path.read_text())
        except FileNotFoundError:
            self.state = {}
        if not isinstance(self.state, dict):
            raise ValueError('invalid time service state')

    def command(self, args, timeout=5):
        """Run a checked command in the C locale, with a default 5 s limit."""
        return subprocess.run(args, capture_output=True, text=True, check=True, timeout=timeout,
                              env=dict(os.environ, LC_ALL='C')).stdout

    def log(self, message):
        """Print a status message only when it differs from the last one."""
        if message != self.last_message:
            print('TIME-SYNC: ' + message, flush=True)
            self.last_message = message

    def save(self):
        """Persist client deadlines and peer backoffs for service restarts."""
        atomic_text(self.state_path, json.dumps(self.state))

    def network(self):
        """Read the configured IPv4 mesh, falling back to the default /16."""
        for line in self.conf.read_text().splitlines():
            key, sep, value = line.partition('=')
            if sep and key.strip() == 'ipv4_network':
                return ipaddress.IPv4Network(value.strip().strip('"\''), strict=False)
        return ipaddress.IPv4Network('10.43.1.0/16', strict=False)

    def roles(self):
        """Detect usable GPS time and a directly connected gateway uplink."""
        gps = False
        try:
            data = json.loads((self.run / 'gps_status.json').read_text())
            # Age on the boot clock: chrony stepping the wall clock must not
            # make a live fix look stale. Serve GPS time only when the reader
            # says the receiver's time is fit to serve (time_ok: settled, no
            # recent clock step, drift or discontinuity).
            age = time.clock_gettime(time.CLOCK_BOOTTIME) - float(data['written_boot'])
            # Accept status up to 60 s old or 5 s ahead on the boot clock.
            gps = (data.get('has_fix') is True and data.get('time_ok') is True
                   and math.isfinite(age) and -5 <= age <= 60)
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            pass
        uplink = ''
        if (self.run / 'manet-uplink/mesh-gateway.state').exists():
            try:
                iface = (self.run / 'upstream_iface').read_text().strip()
                if (re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', iface)
                        and (self.sysnet / iface / 'carrier').read_text().strip() == '1'):
                    uplink = iface
            except OSError:
                pass
        return gps, uplink

    def flags(self, gps=False, internet=False):
        """Set the source markers consumed by node-manager's NTP telemetry."""
        for name, enabled in [('mesh-ntp-gps.state', gps), ('mesh-ntp.state', internet)]:
            path = self.run / name
            if enabled:
                path.touch()
            else:
                path.unlink(missing_ok=True)

    def running(self):
        """Return whether systemd reports chrony.service active."""
        try:
            self.command(['systemctl', 'is-active', '--quiet', 'chrony.service'])
            return True
        except subprocess.CalledProcessError:
            return False

    def arm_socket(self):
        """Hold UDP 123 in chrony.socket so a peer's request can wake chronyd.

        Return True when a running chronyd was restarted with the file on disk.
        """
        if self.socket_ready:
            return False
        restarted = False
        try:
            self.command(['systemctl', 'is-active', '--quiet', 'chrony.socket'])
        except subprocess.CalledProcessError:
            # The socket cannot bind while a running chronyd owns the port.
            # chronyd receives the socket descriptors when it starts again.
            restarted = self.running()
            if restarted:
                self.command(['systemctl', 'stop', 'chrony.service'], timeout=15)
            self.command(['systemctl', 'start', 'chrony.socket'], timeout=15)
            if restarted:
                self.command(['systemctl', 'start', 'chrony.service'], timeout=15)
        self.socket_ready = True
        return restarted

    def configure(self, text, start):
        """Install a role profile and reconcile chrony's requested state."""
        changed = not self.chrony_conf.exists() or self.chrony_conf.read_text() != text
        if changed:
            self.flags()
            atomic_text(self.chrony_conf, text)
        # Arm only after this boot's profile is on disk: a woken chronyd loads it.
        if self.arm_socket():
            self.active_config = text
        if start:
            # Service operations get 15 s, longer than ordinary status probes.
            # Remember what this process successfully activated. If restart
            # fails after writing the file, the next pass retries the restart.
            if not changed and self.active_config is None and self.running():
                # Every write to a running daemon is followed by a restart, so
                # a daemon found running (woken by chrony.socket) loaded this file.
                self.active_config = text
            elif changed or self.active_config != text:
                self.command(['systemctl', 'restart', 'chrony.service'], timeout=15)
                self.active_config = text
            elif not self.running():
                self.command(['systemctl', 'start', 'chrony.service'], timeout=15)
        elif self.running():
            self.command(['systemctl', 'stop', 'chrony.service'], timeout=15)
            self.active_config = None

    def clock_source(self):
        """Return a recent selected source only after tracking has settled."""
        tracking = self.command(['chronyc', '-n', 'tracking'])
        if not settled(tracking):
            return None
        return selected_source(self.command(['chronyc', '-n', 'sources']))

    def complete(self, source):
        """Record verified sync, disable initial stepping and defer refresh."""
        initial = not self.marker.exists()
        if initial:
            # Disarm remaining startup steps in the live daemon as well as its
            # next config. A later GPS/uplink change or peer refresh must slew,
            # not jump backwards through ACS rounds or admin replay timestamps.
            self.command(['chronyc', 'makestep', '0.1', '0'])
            profile = self.chrony_conf.read_text().replace('makestep 0.1 3\n', '')
            # The live daemon has already consumed its first clock update.
            # Do not reopen the certificate-date exception on its next restart.
            profile = profile.replace('nocerttimecheck 1\n', '')
            atomic_text(self.chrony_conf, profile)
            self.active_config = profile
            atomic_text(self.marker, source + '\n')
        if source in ('GPS', 'internet'):
            # Remember for this boot that the clock came from a real source.
            self.state['trusted'] = True
        now = time.monotonic()
        # Persist one jitter value per boot so restarts do not postpone refresh.
        if 'refresh_jitter' not in self.state:
            self.state['refresh_jitter'] = secrets.randbelow(REFRESH_JITTER_SECONDS + 1)
        self.state['last_sync'] = now
        self.state['refresh_at'] = now + REFRESH_SECONDS + self.state['refresh_jitter']
        self.state['last_source'] = source
        self.save()
        if initial:
            self.log('Clock settled from ' + source + '.')

    def step(self):
        """Reconcile one role or peer attempt and return the next delay in s."""
        now = time.monotonic()
        gps, uplink = self.roles()
        network = self.network()
        initial = not self.marker.exists()
        if gps or uplink:
            # Local source roles supersede any open mesh time window.
            if self.state.pop('window', None):
                self.save()
            try:
                self.configure(chrony_config(network, gps, uplink, initial=initial), start=True)
                source = self.clock_source()
                internet_ready = bool(source and uplink and source['mode'] == '^'
                                      and nts_authenticated(self.command(['chronyc', '-n', 'authdata']),
                                                            source['address']))
            except (OSError, ValueError, subprocess.SubprocessError):
                self.flags()
                raise
            gps_ready = bool(source and gps and source['mode'] == '#' and source['address'] == 'GPS')
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
        # Discovery uses BATMAN JSON field names, independent of table layout.
        try:
            own = (self.sysnet / 'br0/address').read_text().strip().lower()
            rows = json.loads(self.command([self.batctl, 'meshif', 'bat0', 'originators_json']))
            peers = candidates(self.registry.read_text(), rows, own, network, servers_only=False)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            # Missing registry or kernel data is retried after 30 s.
            self.log('Discovery unavailable; will retry: ' + str(exc))
            return 30
        state = self.state
        if initial:
            state.setdefault('unsynced_since', now)
        # Arrivals and new real sources are judged on the boot clock. A known
        # peer arrives again only after checks saw it missing for 10 minutes,
        # so a paused or restarted service does not treat everyone as new.
        checked = state.get('checked', now)

        def arrivals(macs, seen):
            new = [mac for mac in macs if mac not in seen
                   or (seen[mac] < checked and now - seen[mac] > JOIN_SECONDS)]
            seen.update(dict.fromkeys(macs, now))
            return new

        arrived = arrivals([p['mac'] for p in peers], state.setdefault('seen', {}))
        new_source = arrivals([p['mac'] for p in peers if p['server']], state.setdefault('sources_seen', {}))
        state['checked'] = now

        trusted = state.get('trusted') or (not initial and self.marker.read_text().strip() in ('GPS', 'internet'))
        if trusted:
            stratum = STRATUM_TRUSTED
        elif not initial:
            stratum = STRATUM_MESH
        elif now - state['unsynced_since'] >= COLD_SECONDS:
            stratum = STRATUM_COLD
        else:
            stratum = None
        # Real sources first, then the best routes, so a join wakes a bounded set.
        chosen = [p for p in peers if p['server']] + [p for p in peers if not p['server']]
        # Sorted lines keep the profile stable while route metrics fluctuate.
        addresses = sorted((p['address'] for p in chosen[:MAX_PEERS]), key=ipaddress.IPv4Address)
        profile = chrony_config(network, peers=addresses, stratum=stratum, initial=initial)

        reason = None
        if initial:
            reason = 'boot'
        elif now >= state.get('refresh_at', 0):
            reason = 'refresh'
        elif arrived:
            reason = 'join'
        elif new_source:
            reason = 'source'
        window = state.get('window')
        if reason is None and window is None and self.running():
            # Socket activation: a peer's request started chronyd.
            reason = 'wake'
        if reason and (window is None or reason in ('join', 'source')):
            if 'first_until' not in state:
                state['first_until'] = now + FIRST_WINDOW_SECONDS
            until = max(now + WINDOW_SECONDS, state['first_until'])
            if window is None:
                self.log('Opening a mesh time window (' + reason + ').')
            window = dict(reason=reason, started=now, until=max(until, (window or {}).get('until', 0)))
            state['window'] = window
        if window is None:
            self.configure(profile, start=False)
            self.save()
            self.log('No mesh time window; chrony waits on its socket.')
            return 15

        self.configure(profile, start=True)
        self.save()
        try:
            tracking = self.command(['chronyc', '-n', 'tracking'])
            report = self.command(['chronyc', '-n', 'sources']) if settled(tracking) else ''
        except subprocess.SubprocessError:
            # Failed commands are not evidence of a synchronized clock.
            tracking = report = ''
        now = time.monotonic()
        if local_active(tracking):
            window.setdefault('local_since', now)
        else:
            window.pop('local_since', None)
        source = selected_source(report)
        if (source and source['mode'] == '^' and source['address'] in addresses
                and source['age'] <= now - window['started'] + 1):
            # A peer sample from this window, after tracking settled.
            if not window.get('synced'):
                self.complete(source['address'])
                self.log('Synced from ' + source['address'] + '.')
            window['synced'] = True
        elif (stratum is not None and 'local_since' in window
              and now - window['local_since'] >= MASTER_SECONDS):
            # This node is the reference for its reachable mesh.
            if not window.get('synced'):
                self.complete('mesh')
                self.log('Serving the mesh reference clock.')
            window['synced'] = True
        self.save()
        finished = now >= window['until'] or (window.get('synced') and window['reason'] != 'wake')
        if self.marker.exists() and finished and now >= state['first_until']:
            if not window.get('synced') and window['reason'] == 'refresh':
                state['refresh_at'] = now + RETRY_SECONDS
            state.pop('window')
            self.configure(chrony_config(network, peers=addresses, stratum=stratum, initial=False), start=False)
            self.save()
            self.log('Mesh time window closed; chrony waits on its socket.')
            return 15
        return 5


def main():
    """Run the sole chrony controller once or continuously under a file lock."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', help='perform one nonblocking reconciliation')
    args = parser.parse_args()
    service = TimeService()
    with (service.run / 'mesh-time-sync.lock').open('a') as lock:
        try:
            # A duplicate owner exits immediately without touching chrony.
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
