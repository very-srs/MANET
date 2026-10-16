#!/usr/bin/env python3
"""Service elections: start gating, cadence, overlap lock and convergence."""

import fcntl
import base64
import contextlib
import io
import socket
import struct
from unittest.mock import patch, Mock

import manet_service_handover as handover
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


TOOLS = Path(__file__).resolve().parent
SELF = '02:00:00:00:00:01'
PEER = '02:00:00:00:00:02'
MANAGERS = ('node-manager-static.sh', 'node-manager-acs.sh')


class Fixture(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.run_dir = self.root / 'run'
        self.run_dir.mkdir()
        self.env = dict(os.environ, PATH=os.pathsep.join(
            (str(self.bin), str(TOOLS), str(Path(sys.executable).parent), os.environ['PATH'])))

    def stub(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/bash\n' + body)
        path.chmod(0o755)


class ElectionGateTests(Fixture):
    """The managers' run_service_elections, extracted from each script."""

    def setUp(self):
        super().setUp()
        self.elections = self.root / 'elections'
        self.elections.mkdir()
        record = self.elections / 'test-election.sh'
        record.write_text('#!/bin/bash\necho run >> "$REVIEW_LOG"\n')
        record.chmod(0o755)
        (self.elections / 'channel-election.sh').write_text(record.read_text())
        (self.elections / 'channel-election.sh').chmod(0o755)
        self.log = self.root / 'runs'
        self.uptime = self.root / 'uptime'

    def function(self, script):
        """Load each manager's schedule and its shared election helper."""
        source = (TOOLS / script).read_text()
        schedule = re.search(
            r'^ELECTION_INTERVAL=.*?^LAST_ELECTION_RUN=.*?\n',
            source, re.M | re.S)[0]
        shared = (TOOLS / 'manet-manager-common.sh').read_text()
        body = re.search(r'^run_service_elections\(\) \{\n.*?^\}\n',
                         shared, re.M | re.S)[0]
        return (schedule + body).replace('/var/run/', str(self.run_dir) + '/')

    def passes(self, script, uptimes):
        calls = ''.join(f'echo "{u}.40 1.0" > "$MESH_UPTIME_FILE"\nrun_service_elections\n'
                        for u in uptimes)
        env = dict(self.env, ELECTION_DIR=str(self.elections), REVIEW_LOG=str(self.log),
                   MESH_UPTIME_FILE=str(self.uptime))
        result = subprocess.run(['bash', '-c', self.function(script) + calls + 'wait\n'],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return len(self.log.read_text().splitlines()) if self.log.exists() else 0

    def test_no_elections_before_an_allocation(self):
        for script in MANAGERS:
            with self.subTest(script=script):
                (self.run_dir / 'my_ipv4_chunk').unlink(missing_ok=True)
                self.log.unlink(missing_ok=True)
                self.assertEqual(self.passes(script, [10, 11, 12, 40]), 0)

    def test_steady_cadence_after_allocation(self):
        # Allocation follows either complete discovery or its bounded deadline
        # (both covered by test_mesh_ip_startup); the gate only needs the claim.
        (self.run_dir / 'my_ipv4_chunk').write_text('0\n')
        for script in MANAGERS:
            with self.subTest(script=script):
                self.log.unlink(missing_ok=True)
                # One-second startup passes: first runs, the rest wait 15 s.
                # channel-election is never started from here.
                self.assertEqual(self.passes(script, [100, 101, 102, 114, 115, 116, 130]), 3)


class MediaMtxElectionTests(Fixture):
    VIP = '10.30.0.2'
    VIP6 = 'fd00:1:2:3::64'

    def setUp(self):
        super().setUp()
        self.addresses = self.root / 'addresses'
        self.addresses.write_text('')
        self.services = self.root / 'services'
        self.registry = self.root / 'registry'
        (self.root / 'mesh.conf').write_text('ipv4_network=10.30.0.0/24\n')
        (self.root / 'mac').write_text(SELF + '\n')
        (self.root / 'uptime').write_text('10000.50 1.00\n')
        self.env['MESH_UPTIME_FILE'] = str(self.root / 'uptime')
        self.tools = self.root / 'tools'
        self.tools.mkdir()
        for path in TOOLS.iterdir():
            if path.name not in ('manet_service_handover.py', 'manet-service-common.sh'):
                (self.tools / path.name).symlink_to(path)
        (self.tools / 'manet-service-common.sh').write_text(
            (TOOLS / 'manet-service-common.sh').read_text().replace(
                '/etc/mesh.conf', str(self.root / 'mesh.conf')))
        # Real ranking and registry-fallback checks. Packet I/O is covered
        # separately below, without requiring raw-socket privileges.
        (self.tools / 'manet_service_handover.py').write_text(f"""
import sys
from pathlib import Path
sys.path.insert(0, {str(TOOLS)!r})
import manet_service_handover as real
args = sys.argv[1:]
with open({str(self.root / 'events')!r}, 'a') as out:
    out.write('helper ' + ' '.join(args) + '\u005cn')
if args[0] in ('announce', 'publish'):
    sys.exit(0)
if args[0] == 'wait':
    (_, service, registry, winner, vip4, vip6, iface, since,
     wake, ignored) = args
    row = real.handover_ranking(service, registry)
    if row[0] != winner:
        print('recheck')
    elif row[3] and row[4] > float(since):
        print('ready:registry')
    else:
        event = Path({str(self.root / 'wait-result')!r})
        print(event.read_text().strip() if event.exists() else 'waiting')
else:
    sys.exit(real.handover_main(args))
""")
        self.env['MANET_TOOLS_DIR'] = str(self.tools)
        self.env['MANET_RUNTIME_DIR'] = str(self.root / 'no-runtime')
        self.env['TEST_EVENTS'] = str(self.root / 'events')
        self.events = self.root / 'events'
        self.stub('sleep', ':\n')
        self.stub('yq', 'exit 0\n')
        self.stub('ss', f'''
echo "ss $*" >> "{self.events}"
[ "${{NO_LISTENER:-0}}" = 0 ] || exit 0
grep -qx active "{self.services}" 2>/dev/null || exit 0
port="${{*: -1}}"; port="${{port##*:}}"
echo "LISTEN 0 128 *:$port *:*"
''')
        self.journal = self.root / 'journal'
        self.stub('systemd-cat', f'cat >> "{self.journal}"\n')
        self.stub('mtx-ip', 'echo fd00:0001:0002:0003::0064/128\n')
        # ip: remember added/removed addresses and show them back.
        self.stub('ip', f'''
echo "ip $*" >> "{self.events}"
case "$*" in
  *"addr show"*) while read -r a; do case "$a" in *:*) echo "1: br0 inet6 $a/128";; *) echo "1: br0 inet $a/24";; esac; done < "{self.addresses}" ;;
  *"addr add"*) a="${{3%/*}}"; if [[ "$a" == *:* && "${{FAIL_V6:-0}}" = 1 ]]; then exit 1; fi; echo "$a" >> "{self.addresses}" ;;
  *"addr del"*) a="${{3%/*}}"; grep -vxF "$a" "{self.addresses}" > "{self.addresses}.new"; mv "{self.addresses}.new" "{self.addresses}" ;;
esac
''')
        self.stub('systemctl', f'''
echo "systemctl $*" >> "{self.events}"
case "$1" in
  is-active) grep -qx active "{self.services}" 2>/dev/null ;;
  restart|start) [ "${{FAIL_START:-0}}" = 0 ] || exit 1; echo active > "{self.services}" ;;
  stop) echo inactive > "{self.services}" ;;
  *) exit 0 ;;
esac
''')
        self.stub('arping', 'exit 0\n')

    def node(self, mac, mbps, age=0, server=False):
        key = 'NODE_' + mac.replace(':', '')
        return (f"{key}_MAC_ADDRESS='{mac}'\n{key}_MEAN_THROUGHPUT_MBPS='{mbps}'\n"
                f"{key}_OBSERVED_AT_UPTIME='{10000 - age}'\n"
                f"{key}_LAST_SEEN_TIMESTAMP='100'\n"
                f"{key}_IS_MEDIAMTX_SERVER='{'true' if server else 'false'}'\n")

    def elect(self, lock=None):
        source = (TOOLS / 'mediamtx-election.sh').read_text()
        for old, new in (('/var/run/mesh_node_registry', str(self.registry)),
                         ('/sys/class/net/${CONTROL_IFACE}/address', str(self.root / 'mac')),
                         ('/usr/local/bin/mtx-ip.sh', str(self.bin / 'mtx-ip')),
                         ('/etc/mesh.conf', str(self.root / 'mesh.conf')),
                         ('/var/run/mediamtx-election.lock', lock or str(self.root / 'lock'))):
            source = source.replace(old, new)
        return subprocess.run(['bash', '-c', source], env=self.env,
                              capture_output=True, text=True, timeout=15)

    def holds_vip(self):
        return self.VIP in self.addresses.read_text().split()

    def test_cidr_change_after_vip_calculation_cannot_claim_stale_vip(self):
        """The lock guard rejects an election's obsolete CIDR snapshot."""
        self.registry.write_text(self.node(SELF, 50))
        self.stub('manet-ipcalc.sh',
                  f'"{TOOLS}/manet-ipcalc.sh" "$@"\n'
                  f'echo ipv4_network=10.45.0.0/16 > "{self.root}/mesh.conf"\n')
        result = self.elect()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.holds_vip())
        self.assertIn('CIDR changed during election', self.journal.read_text())

    def test_overlapping_run_exits_without_touching_state(self):
        lock = self.root / 'held.lock'
        self.registry.write_text(self.node(SELF, 50))
        with open(lock, 'w') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            result = self.elect(lock=str(lock))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.holds_vip())

    def test_lone_winner_yields_once_registry_shows_the_incumbent(self):
        # A node seeing only itself wins. Once the registry converges and shows
        # a better-connected incumbent, it withdraws the VIP and service.
        self.registry.write_text(self.node(SELF, 50))
        self.assertEqual(self.elect().returncode, 0)
        self.assertTrue(self.holds_vip())
        self.registry.write_text(self.node(SELF, 50) + self.node(PEER, 45, server=True))
        # The first old flag is insufficient. Require an observation made
        # after entering the wait, then fall back on the next cycle.
        self.assertEqual(self.elect().returncode, 0)
        self.assertTrue(self.holds_vip())
        (self.root / 'uptime').write_text('10002 1\n')
        self.registry.write_text(self.node(SELF, 50) + self.node(PEER, 45, age=-1, server=True))
        self.assertEqual(self.elect().returncode, 0)
        self.assertFalse(self.holds_vip())
        self.assertEqual(self.services.read_text().strip(), 'inactive')

    def test_candidate_freshness_is_local_observation(self):
        # The peer's sender clock (LAST_SEEN_TIMESTAMP='100') is ignored; only
        # an observed age beyond the threshold disqualifies it.
        self.registry.write_text(self.node(SELF, 10) + self.node(PEER, 90))
        self.elect()
        self.assertFalse(self.holds_vip())
        self.registry.write_text(self.node(SELF, 10) + self.node(PEER, 90, age=601))
        self.elect()
        self.assertTrue(self.holds_vip())

    def test_departed_host_releases_the_service_at_the_next_election(self):
        # D1: a shutting-down incumbent with a zero metric and the lower MAC
        # used to keep winning the tie for up to ten minutes.
        departed, survivor = '02:00:00:00:00:00', SELF
        record = self.node(departed, 0, server=True).replace("'false'", "'true'")
        key = 'NODE_' + departed.replace(':', '')
        self.registry.write_text(record + f"{key}_NODE_STATE='SHUTTING_DOWN'\n" + self.node(survivor, 0))
        self.assertEqual(self.elect().returncode, 0)
        self.assertTrue(self.holds_vip())

    def test_unreadable_registry_leaves_the_service_alone(self):
        self.registry.write_text(self.node(SELF, 50))
        self.elect()
        self.assertTrue(self.holds_vip())
        (self.root / 'uptime').write_text('garbage\n')
        result = self.elect()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.holds_vip())

    def test_service_listens_before_vip_claim_and_announcements(self):
        self.registry.write_text(self.node(SELF, 50))
        result = self.elect()
        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.events.read_text().splitlines()
        start = next(i for i, event in enumerate(events) if event.startswith(('systemctl start ', 'systemctl restart ')))
        claim = next(i for i, event in enumerate(events) if event.startswith('ip addr add '))
        announce = next(i for i, event in enumerate(events) if event.startswith('helper announce '))
        self.assertLess(start, claim)
        self.assertLess(claim, announce)
        self.assertTrue(any(event.startswith('ss ') for event in events[start + 1:claim]))
        self.assertTrue(any('nodad' in event for event in events))
        if self.VIP.endswith('.3'):
            self.assertLess(events.index('db-ready'), start)

    def become_incumbent(self):
        self.registry.write_text(self.node(SELF, 50))
        result = self.elect()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.events.write_text('')
        self.registry.write_text(self.node(SELF, 10, server=True) + self.node(PEER, 90))

    def test_loser_keeps_service_and_vips_indefinitely_without_successor(self):
        self.become_incumbent()
        for _ in range(3):
            self.assertEqual(self.elect().returncode, 0)
            self.assertTrue(self.holds_vip())
            self.assertEqual(self.services.read_text().strip(), 'active')
        self.assertNotIn('helper announce', self.events.read_text())
        self.assertEqual(self.journal.read_text().count('Waiting for successor'), 1)

    def test_loser_releases_on_confirmed_winner_arp(self):
        self.become_incumbent()
        (self.root / 'wait-result').write_text('ready:ARP')
        self.assertEqual(self.elect().returncode, 0)
        self.assertFalse(self.holds_vip())
        self.assertEqual(self.services.read_text().strip(), 'inactive')
        self.assertIn('claimed via ARP,', self.journal.read_text())

    def test_loser_releases_on_confirmed_winner_na(self):
        """The shared shell consumes NA readiness and logs its signal once."""
        self.become_incumbent()
        (self.root / 'wait-result').write_text('ready:NA')
        self.assertEqual(self.elect().returncode, 0)
        self.assertFalse(self.holds_vip())
        self.assertEqual(self.services.read_text().strip(), 'inactive')
        self.assertEqual(self.elect().returncode, 0)
        self.assertEqual(self.journal.read_text().count('claimed via NA,'), 1)
        events = self.events.read_text().splitlines()
        waits = [event.split() for event in events
                 if event.startswith('helper wait ')]
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0][5:7], [self.VIP, self.VIP6])
        if self.VIP.endswith('.3'):
            stop = next(i for i, event in enumerate(events)
                        if event.startswith('systemctl stop '))
            exported = next(i for i, event in enumerate(events)
                            if event.startswith('db-publish'))
            self.assertLess(stop, exported)
        self.assertNotIn('helper announce ', self.events.read_text())

    def test_ranking_flip_retains_vips_and_service_without_restart(self):
        self.become_incumbent()
        self.elect()
        self.registry.write_text(self.node(SELF, 95, server=True) + self.node(PEER, 90))
        result = self.elect()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.holds_vip())
        events = self.events.read_text()
        self.assertNotIn('systemctl stop ', events)
        self.assertNotIn('systemctl restart ', events)
        self.assertIn('helper announce ', events)

    def test_failed_start_or_missing_listener_never_claims_vips(self):
        self.registry.write_text(self.node(SELF, 50))
        for key in ('FAIL_START', 'NO_LISTENER'):
            with self.subTest(key=key):
                self.env[key] = '1'
                result = self.elect()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.holds_vip())
                self.assertNotIn('helper announce ', self.events.read_text())
                self.assertEqual(self.services.read_text().strip(),
                                 'inactive')
                events = self.events.read_text()
                self.assertIn('systemctl stop ', events)
                self.assertIn('systemctl reset-failed ', events)
                self.assertLess(events.rindex('systemctl stop '),
                                events.rindex('systemctl reset-failed '))
                self.assertFalse((self.root / 'lock.prepared').exists())
                del self.env[key]

    def test_empty_registry_cannot_remove_a_serving_incumbent(self):
        self.become_incumbent()
        self.registry.write_text('')
        self.assertEqual(self.elect().returncode, 0)
        self.assertTrue(self.holds_vip())

    def test_failed_winner_leaves_the_other_node_serving(self):
        self.become_incumbent()
        candidate = self.__class__()
        candidate.setUp()
        self.addCleanup(candidate.doCleanups)
        (candidate.root / 'mac').write_text(PEER + '\n')
        candidate.registry.write_text(self.registry.read_text())
        candidate.env['FAIL_START'] = '1'
        self.assertNotEqual(candidate.elect().returncode, 0)
        self.assertFalse(candidate.holds_vip())
        self.assertEqual(self.elect().returncode, 0)
        self.assertTrue(self.holds_vip())
        self.assertEqual(self.services.read_text().strip(), 'active')

    def test_failed_readiness_stops_restarts_until_next_election(self):
        """Failed candidates stop each pass, log once, then retry cleanly."""
        self.registry.write_text(self.node(SELF, 50))
        self.env['NO_LISTENER'] = '1'
        for _ in range(3):
            self.assertNotEqual(self.elect().returncode, 0)
            self.assertFalse(self.holds_vip())
            self.assertEqual(self.services.read_text().strip(), 'inactive')
            self.assertFalse((self.root / 'lock.prepared').exists())
        events = self.events.read_text()
        self.assertEqual(events.count('systemctl stop '), 3)
        self.assertNotIn('db-publish', events)
        self.assertEqual(events.count('systemctl reset-failed '), 3)
        self.assertEqual(self.journal.read_text().count(
            'Service is not listening, no VIP claim'), 1)
        self.events.write_text('')
        del self.env['NO_LISTENER']
        result = self.elect()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.holds_vip())
        self.assertEqual(self.services.read_text().strip(), 'active')
        self.assertRegex(self.events.read_text(), 'systemctl (re)?start ')
        self.assertTrue((self.root / 'lock.prepared').exists())
        if self.VIP.endswith('.3'):
            self.assertIn('db-ready', self.events.read_text())

    def test_failed_ipv6_claim_rolls_back_new_ipv4_then_reuses_service(self):
        self.registry.write_text(self.node(SELF, 50))
        self.env['FAIL_V6'] = '1'
        self.assertNotEqual(self.elect().returncode, 0)
        self.assertFalse(self.holds_vip())
        self.assertNotIn('helper announce ', self.events.read_text())
        self.events.write_text('')
        del self.env['FAIL_V6']
        self.assertEqual(self.elect().returncode, 0)
        self.assertTrue(self.holds_vip())
        for event in ('systemctl start ', 'systemctl restart ', 'db-ready'):
            self.assertNotIn(event, self.events.read_text())

    def test_fallback_accepts_a_claim_received_between_election_cycles(self):
        self.become_incumbent()
        (self.root / 'uptime').write_text('10010 1\n')
        self.registry.write_text(self.node(SELF, 10, server=True) +
                                 self.node(PEER, 90, age=-9, server=True))
        self.assertEqual(self.elect().returncode, 0)
        self.assertFalse(self.holds_vip())
        self.assertEqual(self.services.read_text().strip(), 'inactive')
        self.assertIn('claimed via registry,', self.journal.read_text())

    def test_solicited_probe_claim_releases_and_logs_its_source(self):
        """A successor that claimed before our wait still permits release."""
        self.become_incumbent()
        (self.root / 'wait-result').write_text('ready:NS probe')
        result = self.elect()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.holds_vip())
        self.assertEqual(self.services.read_text().strip(), 'inactive')
        self.assertIn('claimed via NS probe,', self.journal.read_text())


class MumbleElectionTests(MediaMtxElectionTests):
    """The real mumble-election.sh with the shared helper. Database sync and
    the Syncthing folder are stubbed; ranking and VIP/service handling run
    unchanged. Every MediaMTX scenario above runs here too."""
    VIP = '10.30.0.3'
    VIP6 = 'fd00:1:2:3::65'

    def setUp(self):
        super().setUp()
        (self.root / 'mesh.conf').write_text('ipv4_network=10.30.0.0/24\nmumble=y\n')
        self.stub('mumble-ip', 'echo fd00:0001:0002:0003::0065/128\n')
        self.stub('sqlite3', 'echo ok\n')
        (self.root / 'mumble.ini').write_text('host=10.1.0.3\n')
        (self.root / 'working.sqlite').touch()
        self.stub('bc', 'exit 0\n')
        self.stub('logger', 'cat > /dev/null\n')
        self.stub('sudo', '[ "$1" = -u ] && shift 2\n"$@"\n')

    def node(self, mac, mbps, age=0, server=False):
        return super().node(mac, mbps, age, server).replace('IS_MEDIAMTX_SERVER', 'IS_MUMBLE_SERVER')

    def elect(self, lock=None):
        source = (TOOLS / 'mumble-election.sh').read_text()
        source = source.replace('# --- Main Election Logic ---', """
ensure_shared_directory() { mkdir -p "$SHARED_DB_DIR"; }
sync_database_from_shared() { echo db-ready >> "$TEST_EVENTS"; }
sync_database_to_shared() {
    echo "db-publish ${1:-retired} $SHARED_DB" >> "$TEST_EVENTS"
    [ "${FAIL_DB_EXPORT:-}" != 1 ] || return 1
    touch "$SHARED_DB"
    if [ -n "${2:-}" ]; then
        python3 "$MANET_TOOLS_DIR/manet_service_handover.py" \\
            db-signature "$WORKING_DB" > "$2"
    fi
}
create_initial_database() { :; }
# --- Main Election Logic ---""")
        for old, new in (('/var/run/mesh_node_registry', str(self.registry)),
                         ('/sys/class/net/${CONTROL_IFACE}/address', str(self.root / 'mac')),
                         ('/usr/local/bin/mumble-ip.sh', str(self.bin / 'mumble-ip')),
                         ('/etc/mesh.conf', str(self.root / 'mesh.conf')),
                         ('$MANET_SSH_HOME/Sync/mumble', str(self.root / 'shared')),
                         ('/etc/mumble/mumble-server.ini', str(self.root / 'mumble.ini')),
                         ('/var/lib/mumble-server/mumble-server.sqlite', str(self.root / 'working.sqlite')),
                         ('/var/run/mumble-election.lock', lock or str(self.root / 'lock'))):
            source = source.replace(old, new)
        return subprocess.run(['bash', '-c', source], env=self.env,
                              capture_output=True, text=True, timeout=15)

    def test_cadence_export_tracks_changes_and_stops_when_waiting(self):
        """Only a serving winner publishes changed canonical snapshots."""
        self.registry.write_text(self.node(SELF, 50))
        self.assertEqual(self.elect().returncode, 0)
        self.assertIn('db-publish live', self.events.read_text())
        self.events.write_text('')
        self.assertEqual(self.elect().returncode, 0)
        self.assertNotIn('db-publish', self.events.read_text())
        (self.root / 'working.sqlite-wal').write_text('new change')
        self.assertEqual(self.elect().returncode, 0)
        self.assertIn('db-publish live', self.events.read_text())
        self.events.write_text('')
        (self.root / 'working.sqlite-wal').write_text('late change')
        self.registry.write_text(self.node(SELF, 10, server=True) +
                                 self.node(PEER, 90))
        self.assertEqual(self.elect().returncode, 0)
        self.assertTrue(self.holds_vip())
        self.assertNotIn('db-publish', self.events.read_text())

    def test_failed_final_export_retries_without_canonical_overwrite(self):
        """Retiring owners preserve final data even after stopping the unit."""
        self.become_incumbent()
        (self.root / 'wait-result').write_text('ready:NS probe')
        self.env['FAIL_DB_EXPORT'] = '1'
        self.assertNotEqual(self.elect().returncode, 0)
        self.assertFalse(self.holds_vip())
        self.assertTrue((self.root / 'lock.release-pending').exists())
        self.events.write_text('')
        del self.env['FAIL_DB_EXPORT']
        self.assertEqual(self.elect().returncode, 0)
        self.assertFalse((self.root / 'lock.release-pending').exists())
        self.assertIn('db-publish retired', self.events.read_text())
        self.assertIn('/retired/020000000001/', self.events.read_text())
        self.assertNotIn('db-publish live', self.events.read_text())
        self.assertIn(f'helper publish mumble {SELF} false',
                      self.events.read_text())


class RetiredSnapshotTests(Fixture):
    """Prune only this owner's published snapshots after a successful copy."""
    NOW = 1_800_000_000

    def setUp(self):
        """Create an owner directory separate from canonical and peer data."""
        super().setUp()
        self.directory = self.root / 'retired' / SELF.replace(':', '')
        self.directory.mkdir(parents=True)

    def snapshot(self, number, days=0):
        """Create a UUID-named snapshot with a deterministic age."""
        name = f'{number:08x}-0000-4000-8000-000000000000.sqlite'
        path = self.directory / name
        path.write_bytes(b'snapshot')
        modified = self.NOW - days * 86400
        os.utime(path, (modified, modified))
        return path

    def test_keep_three_including_current_despite_its_old_timestamp(self):
        """The new export is protected even after a backwards clock step."""
        snapshots = [self.snapshot(i, days=i) for i in range(1, 7)]
        current = self.snapshot(10, days=60)
        handover.prune_mumble_retired(str(current), now=self.NOW)
        self.assertEqual(set(self.directory.iterdir()),
                         {current, *snapshots[:2]})

    def test_expire_older_than_thirty_days_even_below_count_limit(self):
        """Thirty days exactly survives, but older data expires at export."""
        current = self.snapshot(1)
        boundary = self.snapshot(2, days=30)
        expired = self.snapshot(3, days=30 + 1 / 86400)
        handover.prune_mumble_retired(str(current), now=self.NOW)
        self.assertTrue(current.exists())
        self.assertTrue(boundary.exists())
        self.assertFalse(expired.exists())

    def test_other_owners_conflicts_symlinks_and_non_snapshots_are_untouched(
            self):
        """Use direct regular UUID files only, never recurse or follow links."""
        current = self.snapshot(1)
        protected = []
        for name in ('notes.sqlite', 'snapshot.sync-conflict-copy.sqlite',
                     'incomplete.sqlite.uploading', 'name with\nspaces.sqlite'):
            path = self.directory / name
            path.write_bytes(b'not a retired snapshot')
            protected.append(path)
        peer = self.root / 'retired' / PEER.replace(':', '')
        peer.mkdir()
        (peer / current.name).write_bytes(b'peer snapshot')
        protected.append(peer / current.name)
        canonical = self.root / 'mumble-server.sqlite'
        canonical.write_bytes(b'canonical')
        link = self.snapshot(2, days=40)
        link.unlink()
        link.symlink_to(canonical)
        directory = self.snapshot(3, days=40)
        directory.unlink()
        directory.mkdir()
        protected += [canonical, link, directory]
        for number in range(4, 9):
            self.snapshot(number, days=40)
        handover.prune_mumble_retired(str(current), now=self.NOW)
        self.assertTrue(current.exists())
        self.assertTrue(all(path.exists() for path in protected))
        self.assertEqual(canonical.read_bytes(), b'canonical')

    def test_missing_or_symlink_current_cannot_authorize_deletion(self):
        """A failed or untrusted final export leaves earlier recovery intact."""
        old = self.snapshot(1, days=40)
        current = self.snapshot(2)
        current.unlink()
        with self.assertRaises(FileNotFoundError):
            handover.prune_mumble_retired(str(current), now=self.NOW)
        current.symlink_to(old)
        with self.assertRaises(ValueError):
            handover.prune_mumble_retired(str(current), now=self.NOW)
        self.assertTrue(old.exists())


class ServiceVipTests(Fixture):
    # MediaMTX and Mumble VIPs share one /64, so their offsets must not overlap.
    def vip(self, script, radvd):
        text = (TOOLS / script).read_text().replace('/etc/radvd-mesh.conf',
                                                    str(radvd))
        path = self.root / script
        path.write_text(text)
        out = subprocess.run(['bash', str(path)], check=True,
                             capture_output=True, text=True,
                             timeout=10).stdout.strip()
        addr, mask = out.split('/')
        self.assertEqual(mask, '128')
        return int(addr.rsplit(':', 1)[1], 16)

    def test_offsets_stay_in_disjoint_ranges(self):
        radvd = self.root / 'radvd-mesh.conf'
        for n in range(40):
            prefix = 'fd%02x:%x:%x::/48' % (n, n * 7919 & 0xffff,
                                            n * 104729 & 0xffff)
            radvd.write_text('interface br0 {\n  prefix %s {\n  };\n};\n'
                             % prefix)
            mtx = self.vip('mtx-ip.sh', radvd)
            mumble = self.vip('mumble-ip.sh', radvd)
            self.assertTrue(100 <= mtx <= 355, mtx)
            self.assertTrue(400 <= mumble <= 655, mumble)


class HandoverPacketTests(Fixture):
    """Exercise real packet parsing and wait decisions with bounded fake I/O."""
    VIP = '10.30.0.2'
    VIP6 = 'fd00::64'

    def frame(self, mac=PEER, vip=VIP):
        sender = bytes.fromhex(mac.replace(':', ''))
        return (b'\xff' * 6 + sender + bytes.fromhex('08060001080006040001') +
                sender + socket.inet_aton(vip) + b'\0' * 6 + socket.inet_aton(vip))

    def na_frame(self, mac=PEER, vip=VIP6, *, source=None,
                 destination='ff02::1', hop=255, next_header=58,
                 icmp_type=136, code=0, flags=0x20000000, options=None,
                 ethernet_destination='33:33:00:00:00:01'):
        """Build a complete wire NA with an independently computed checksum."""
        sender = bytes.fromhex(mac.replace(':', ''))
        source = socket.inet_pton(socket.AF_INET6, source or vip)
        destination = socket.inet_pton(socket.AF_INET6, destination)
        if options is None:
            options = b'\x02\x01' + sender
        packet = struct.pack('!BBHI', icmp_type, code, 0, flags)
        packet += socket.inet_pton(socket.AF_INET6, vip) + options
        pseudo = source + destination + struct.pack('!I3xB', len(packet), 58)
        data = pseudo + packet
        if len(data) % 2:
            data += b'\0'
        total = 0
        for offset in range(0, len(data), 2):
            total += (data[offset] << 8) | data[offset + 1]
            total = (total & 0xffff) + (total >> 16)
        checksum = struct.pack('!H', (~total) & 0xffff)
        packet = packet[:2] + checksum + packet[4:]
        header = struct.pack('!IHBB', 0x60000000, len(packet), next_header,
                             hop) + source + destination
        receiver = bytes.fromhex(ethernet_destination.replace(':', ''))
        return receiver + sender + b'\x86\xdd' + header + packet

    def filtered(self, frame):
        """Execute the small classic-BPF subset to check its packet boundary."""
        program = handover.vip_claim_filter(self.VIP, self.VIP6)
        pc, accumulator = 0, 0
        while pc < len(program):
            code, yes, no, value = program[pc]
            if code in (0x30, 0x28, 0x20):
                width = {0x30: 1, 0x28: 2, 0x20: 4}[code]
                if len(frame) < value + width:
                    return 0
                accumulator = int.from_bytes(frame[value:value + width], 'big')
            elif code == 0x54:
                accumulator &= value
            elif code == 0x15:
                pc += yes if accumulator == value else no
            elif code == 0x06:
                return value
            else:
                self.fail(f'Unexpected BPF opcode {code}')
            pc += 1
        self.fail('Filter did not return')

    def test_filter_rejects_unrelated_and_truncated_traffic(self):
        good = self.frame()
        self.assertEqual(self.filtered(good), 42)
        self.assertEqual(self.filtered(good[:20] + b'\0\2' + good[22:]), 42)
        for bad in (good[:12] + b'\x08\x00' + good[14:], good[:30],
                    self.frame(vip='10.30.0.3'), good[:20] + b'\0\3' + good[22:]):
            self.assertEqual(self.filtered(bad), 0)
            self.assertIsNone(handover.vip_arp_sender(bad, self.VIP))
        self.assertIsNone(handover.vip_arp_sender(good[:6] + b'\0' * 6 + good[12:], self.VIP))

    def test_filter_admits_only_the_service_na_form(self):
        """The kernel filter bounds wakeups to ARP and this IPv6 target."""
        good = self.na_frame()
        self.assertEqual(self.filtered(good), 2048)
        bad_frames = [good[:77], good[:12] + b'\x08\x00' + good[14:],
                      good[:14] + b'\x40' + good[15:],
                      self.na_frame(hop=254), self.na_frame(icmp_type=135),
                      self.na_frame(code=1)]
        for protocol in (0, 6, 17, 43, 44, 60):
            bad_frames.append(self.na_frame(next_header=protocol))
        for index in (65, 69, 73, 77):
            bad_frames.append(good[:index] + bytes([good[index] ^ 1])
                              + good[index + 1:])
        for frame in bad_frames:
            with self.subTest(frame=frame.hex()):
                self.assertEqual(self.filtered(frame), 0)
                self.assertIsNone(handover.vip_na_sender(frame, self.VIP6))

    def test_userspace_na_validation_rejects_invalid_claims(self):
        """Reject corrupt or unrelated NAs even if a frame reaches Python."""
        good = self.na_frame()
        for frame in (good, good + b'\0' * 8,
                      self.na_frame(options=b''),
                      self.na_frame(options=b'\x7f\x01' + b'\0' * 6)):
            self.assertEqual(handover.vip_na_sender(frame, self.VIP6), PEER)
        bad_frames = [
            good[:-1], good[:58] + b'\x21' + good[59:],  # Bad checksum
            good[:18] + b'\0\x10' + good[20:],  # Short declared payload
            good[:18] + b'\xff\xff' + good[20:],  # Truncated payload
            good[:6] + bytes.fromhex(SELF.replace(':', '')) + good[12:],
            b'\xff' * 6 + good[6:],
            self.na_frame(source='fd00::65'),
            self.na_frame(vip='fd00::65'),
            self.na_frame(destination='ff02::2'),
            self.na_frame(destination=self.VIP6),
            self.na_frame(flags=0x60000000),  # Solicited multicast
            self.na_frame(options=b'\x02\0' + b'\0' * 6),
            self.na_frame(options=b'\x02\x02' + b'\0' * 6),
            self.na_frame(options=b'\x02\x02' + b'\0' * 14),
            self.na_frame(options=b'\x02'),
            self.na_frame(options=b'\x02\x01' + bytes.fromhex(
                SELF.replace(':', ''))),
        ]
        for frame in bad_frames:
            with self.subTest(frame=frame.hex()):
                self.assertIsNone(handover.vip_na_sender(frame, self.VIP6))
        # Every truncation of the IPv6 header or fixed NA body is harmless.
        for length in range(78):
            self.assertIsNone(handover.vip_na_sender(good[:length], self.VIP6))

    def wait_packets(self, frames=(), rows=None, wake_first=False):
        """Run the production blocking loop, replacing only sockets/select."""
        wake_path = str(self.root / 'wake')
        ignored_path = str(self.root / 'ignored')
        wake = Mock()
        wake.__enter__ = Mock(return_value=wake)
        wake.__exit__ = Mock(return_value=False)
        wake.bind.side_effect = lambda path: Path(path).touch()
        packets = Mock()
        packets.__enter__ = Mock(return_value=packets)
        packets.__exit__ = Mock(return_value=False)
        self.probe_packets = packets
        packets.recv.side_effect = frames
        events = ([([wake], [], [])] if wake_first else
                  [([packets], [], []) for _ in frames] + [([], [], [])])
        row = (PEER, 90, SELF, False, 100, {PEER, '02:00:00:00:00:05'})
        output = io.StringIO()
        with patch.object(handover, 'handover_ranking', side_effect=rows or [row] * (len(frames) + 1)), \
                patch.object(handover.socket, 'socket', return_value=wake), \
                patch.object(handover.select, 'select', side_effect=events), \
                patch.object(handover, 'vip_probe_identity', return_value=(
                    socket.inet_pton(socket.AF_INET6, 'fe80::1'),
                    bytes.fromhex(SELF.replace(':', '')))), \
                contextlib.redirect_stderr(output):
            result = handover.wait_for_successor(
                'mediamtx', 'registry', PEER, self.VIP, self.VIP6, 'br0', 99,
                wake_path, ignored_path, factory=lambda *_: packets)
        self.assertFalse(Path(wake_path).exists())
        if frames or wake_first:
            packets.__exit__.assert_called_once()
        return result, output.getvalue()

    def test_probe_recovers_an_early_claim_from_each_winner_alias(self):
        """A solicited unicast NA ends a wait with no announcement received."""
        for mac in (PEER, '02:00:00:00:00:05'):
            frame = self.na_frame(mac, destination='fe80::1',
                                  ethernet_destination=SELF,
                                  flags=0x60000000)
            self.assertEqual(self.filtered(frame), 2048)
            self.assertEqual(self.wait_packets([frame]),
                             ('ready:NS probe', ''))
            sends = self.probe_packets.sendto.call_args_list
            self.assertEqual(len(sends), 2)
            for call in sends:
                packet, flags, address = call.args
                self.assertEqual(flags, socket.MSG_DONTWAIT)
                self.assertEqual(address, ('br0', 0x86dd))
                self.assertIn(packet[:6].hex(':'),
                              (PEER, '02:00:00:00:00:05'))
                self.assertEqual(packet[6:12].hex(':'), SELF)
                self.assertEqual(packet[20:22], b'\x3a\xff')
                self.assertEqual(socket.inet_ntop(socket.AF_INET6,
                                                 packet[22:38]), 'fe80::1')
                vip = socket.inet_pton(socket.AF_INET6, self.VIP6)
                self.assertEqual(packet[38:54], vip)
                self.assertEqual(packet[54:56], b'\x87\0')
                self.assertEqual(packet[58:62], b'\0' * 4)
                self.assertEqual(packet[62:78], vip)
                self.assertEqual(packet[78:80], b'\x01\x01')
                self.assertEqual(packet[80:86].hex(':'), SELF)
                # Validate generated NS checksum independently of production.
                data = packet[22:54] + struct.pack('!I3xB', 32, 58)
                data += packet[54:]
                total = sum(v[0] for v in struct.iter_unpack('!H', data))
                while total >> 16:
                    total = (total & 0xffff) + (total >> 16)
                self.assertEqual(total, 0xffff)

    def test_probe_rejects_other_destinations_self_and_bad_replies(self):
        """Solicited claims must target our link-local IP and Ethernet MAC."""
        options = dict(destination='fe80::1', ethernet_destination=SELF,
                       flags=0x60000000)
        frames = []
        for changes in (dict(destination='fe80::2'),
                        dict(destination=self.VIP6),
                        dict(ethernet_destination=PEER), dict(mac=SELF),
                        dict(flags=0x20000000), dict(hop=254),
                        dict(vip='fd00::65'), dict(code=1)):
            frames.append(self.na_frame(**dict(options, **changes)))
        good = self.na_frame(**options)
        frames += [good[:56] + b'\0\0' + good[58:], good[:-1]]
        self.assertEqual(self.wait_packets(frames), ('waiting', ''))
        self.assertEqual(self.probe_packets.sendto.call_count, 2)
        self.assertIsNone(handover.vip_na_sender(good, self.VIP6))

    def test_probe_reply_rechecks_aliases_and_new_ranking(self):
        """A probe cannot release VIPs after its expected winner changes."""
        frame = self.na_frame(destination='fe80::1',
                              ethernet_destination=SELF, flags=0x60000000)
        old = (PEER, 90, SELF, False, 100, {PEER})
        new = (SELF, 95, SELF, True, 101, {SELF})
        self.assertEqual(self.wait_packets([frame], [old, new])[0], 'recheck')
        changed = (PEER, 90, SELF, False, 100, {'02:00:00:00:00:05'})
        self.assertEqual(self.wait_packets([frame], [old, changed])[0],
                         'waiting')

    def test_probe_skips_own_mac_and_is_unneeded_after_registry_claim(self):
        """Never aim a probe at our kernel or send after a known fresh claim."""
        row = (PEER, 90, SELF, False, 100, {PEER, SELF})
        self.assertEqual(self.wait_packets(rows=[row])[0], 'waiting')
        self.assertEqual(self.probe_packets.sendto.call_count, 1)
        fresh = (PEER, 90, SELF, True, 101, {PEER})
        self.assertEqual(self.wait_packets(rows=[fresh])[0], 'ready:registry')
        self.probe_packets.sendto.assert_not_called()

    def test_probe_failure_keeps_passive_wait_and_logs_once(self):
        """A missing link-local address must not disable passive NA claims."""
        with patch.object(handover, 'send_vip_ns', side_effect=OSError('down')):
            result, log = self.wait_packets([self.na_frame()])
            self.assertEqual(result, 'ready:NA')
            self.assertIn('VIP NS probe unavailable: down', log)
            self.assertEqual(self.wait_packets(), ('waiting', ''))

    def test_probe_identity_excludes_unusable_or_wrong_interface_ips(self):
        """Select a valid bridge link-local address without using its VIP."""
        rows = ('fd000000000000000000000000000064 02 80 00 80 br0\n'
                'fe800000000000000000000000000001 03 40 20 80 eth0\n'
                'fe800000000000000000000000000002 02 40 20 40 br0\n'
                'fe800000000000000000000000000003 02 40 20 08 br0\n')
        mac = Mock(read_text=Mock(return_value=SELF + '\n'))
        addresses = Mock(read_text=Mock(return_value=rows))
        with patch.object(handover, 'Path', side_effect=[mac, addresses]):
            with self.assertRaises(OSError):
                handover.vip_probe_identity('br0')
        addresses.read_text.return_value += (
            'fe800000000000000000000000000004 02 40 20 80 br0\n')
        with patch.object(handover, 'Path', side_effect=[mac, addresses]):
            source, sender = handover.vip_probe_identity('br0')
        self.assertEqual(socket.inet_ntop(socket.AF_INET6, source), 'fe80::4')
        self.assertEqual(sender.hex(':'), SELF)

    def test_winner_arp_and_interface_alias_both_finish_wait(self):
        for mac in (PEER, '02:00:00:00:00:05'):
            self.assertEqual(self.wait_packets([self.frame(mac)])[0],
                             'ready:ARP')

    def test_winner_na_and_interface_alias_both_finish_wait(self):
        """An NA alone is enough when DAT absorbs the ARP burst."""
        for mac in (PEER, '02:00:00:00:00:05'):
            self.assertEqual(self.wait_packets([self.na_frame(mac)])[0],
                             'ready:NA')

    def test_na_ignores_nonwinner_and_invalid_packets(self):
        """Bad NAs cannot release the incumbent or spam transition logs."""
        invalid = [self.na_frame(vip='fd00::65'), self.na_frame(hop=254)]
        self.assertEqual(self.wait_packets(invalid), ('waiting', ''))
        frames = [self.na_frame(SELF), self.frame(SELF)]
        result, log = self.wait_packets(frames)
        self.assertEqual(result, 'waiting')
        self.assertEqual(log.count('Ignoring VIP NA'), 1)
        self.assertEqual(log.count('Ignoring VIP'), 1)
        self.assertEqual(self.wait_packets(frames), ('waiting', ''))
        self.assertEqual(self.wait_packets(frames + [self.na_frame()])[0],
                         'ready:NA')

    def test_na_rechecks_winner_and_current_aliases_before_release(self):
        """An old winner or removed alias cannot authorize withdrawal."""
        old = (PEER, 90, SELF, False, 100, {PEER})
        new = (SELF, 95, SELF, True, 101, {SELF})
        self.assertEqual(self.wait_packets([self.na_frame()], [old, new])[0],
                         'recheck')
        alias = '02:00:00:00:00:05'
        initial = (PEER, 90, SELF, False, 100, {PEER, alias})
        self.assertEqual(self.wait_packets(
            [self.na_frame(alias)], [initial, old])[0], 'waiting')

    def test_missed_na_releases_via_fresh_registry_on_next_pass(self):
        """A claim outside the bounded listener is covered by publication."""
        self.assertEqual(self.wait_packets()[0], 'waiting')
        fresh = (PEER, 90, SELF, True, 101, {PEER})
        self.assertEqual(self.wait_packets(rows=[fresh])[0], 'ready:registry')

    def test_wait_cli_carries_ipv6_and_reports_the_signal(self):
        """Keep both VIPs and the release reason across the shell boundary."""
        args = ['wait', 'mumble', 'registry', PEER, self.VIP, self.VIP6,
                'br0', '99', str(self.root / 'wake'),
                str(self.root / 'ignored')]
        output = io.StringIO()
        with patch.object(handover, 'wait_for_successor',
                          return_value='ready:NA') as wait, \
                contextlib.redirect_stdout(output):
            self.assertEqual(handover.handover_main(args), 0)
        self.assertEqual(output.getvalue(), 'ready:NA\n')
        wait.assert_called_once_with(*args[1:7], 99.0, *args[8:])
        errors = io.StringIO()
        with patch.object(handover, 'wait_for_successor',
                          side_effect=OSError('packet socket unavailable')), \
                contextlib.redirect_stderr(errors):
            self.assertEqual(handover.handover_main(args), 1)
            self.assertEqual(handover.handover_main(args), 1)
        self.assertEqual(errors.getvalue().count('packet socket unavailable'),
                         1)

    def test_nonwinner_is_ignored_and_logged_once_across_waits(self):
        frames = [self.frame(SELF), self.frame(SELF)]
        result, log = self.wait_packets(frames)
        self.assertEqual(result, 'waiting')
        self.assertEqual(log.count('Ignoring VIP ARP'), 1)
        self.assertEqual(self.wait_packets(frames), ('waiting', ''))
        frames = [self.frame(SELF), self.frame(PEER)]
        self.assertEqual(self.wait_packets(frames)[0], 'ready:ARP')

    def test_rank_flip_during_wait_rechecks_instead_of_releasing(self):
        old = (PEER, 90, SELF, False, 100, {PEER})
        new = (SELF, 95, SELF, True, 101, {SELF})
        self.assertEqual(self.wait_packets([self.frame()], [old, new])[0], 'recheck')
        self.assertEqual(self.wait_packets(wake_first=True)[0], 'recheck')

    def test_registry_fallback_requires_a_new_serving_observation(self):
        for serving, observed, expected in ((False, 100, 'waiting'),
                                            (True, 98, 'waiting'),
                                            (True, 100, 'ready:registry')):
            row = (PEER, 90, SELF, serving, observed, {PEER})
            self.assertEqual(self.wait_packets(rows=[row])[0], expected)

    def test_packet_socket_is_filtered_before_receiving(self):
        sock = Mock()
        self.assertIs(handover.open_vip_claim_socket(
            'br0', self.VIP, self.VIP6, factory=lambda *args: sock), sock)
        calls = sock.mock_calls
        self.assertEqual(calls[0].args[:2], (socket.SOL_SOCKET, 26))
        self.assertEqual(calls[1].args, (socket.SOL_SOCKET, 44, 1))
        self.assertEqual(calls[-1].args, (('br0', 3),))

    def test_unsolicited_na_uses_target_vip_mac_and_override(self):
        sock = Mock()
        sock.__enter__ = Mock(return_value=sock)
        sock.__exit__ = Mock(return_value=False)
        mac_file = Mock(read_text=Mock(return_value=SELF))
        with patch.object(handover, 'Path', return_value=mac_file), \
                patch.object(handover.socket, 'if_nametoindex', return_value=7):
            handover.send_vip_na('br0', 'fd00::64', factory=lambda *args: sock)
        packet, destination = sock.sendto.call_args.args
        self.assertEqual(destination, ('ff02::1', 0, 0, 7))
        self.assertEqual(struct.unpack('!BBHI', packet[:8]), (136, 0, 0, 0x20000000))
        self.assertEqual(packet[8:24], socket.inet_pton(socket.AF_INET6, 'fd00::64'))
        self.assertEqual(packet[24:], b'\x02\x01' + bytes.fromhex(SELF.replace(':', '')))
        sock.setsockopt.assert_any_call(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 255)

    def test_both_debian_arping_flavors_send_three_spaced_claims(self):
        for flavor, interface_option, interval_option in (('iputils', '-I', '-i'), ('Habets', '-i', '-W')):
            with patch.object(handover, 'send_vip_na') as na, \
                    patch.object(handover.subprocess, 'run', side_effect=[
                        subprocess.CompletedProcess([], 0, flavor, ''),
                        subprocess.CompletedProcess([], 1, '', '')]) as run:
                events = Mock()
                events.attach_mock(na, 'na')
                events.attach_mock(run, 'arping')
                handover.announce_service_vips('br0', self.VIP, 'fd00::64')
                self.assertEqual([c[0] for c in events.mock_calls],
                                 ['na', 'arping', 'arping'])
            args = run.call_args.args[0]
            self.assertEqual(args[:6], ['arping', '-U', '-c', '3', '-w', '4'])
            self.assertEqual(args[args.index(interface_option) + 1], 'br0')
            self.assertEqual(args[args.index(interval_option) + 1], '1')
            na.assert_called_once_with('br0', 'fd00::64')

    def test_transition_publication_preserves_other_telemetry(self):
        import NodeInfo_pb2
        original = NodeInfo_pb2.NodeTelemetry(
            mean_throughput_mbps=42, is_mumble_server=True,
            is_internet_gateway=True, gateway_iface='end0',
            config_ack_version='keep-me', uptime_seconds=4,
            last_seen_timestamp=123)
        original.location.latitude_e7 = 391234567
        payload = base64.b64encode(original.SerializeToString()).decode('ascii')
        uptime = self.root / 'uptime'
        uptime.write_text('10000.5 1\n')
        raw = f'{{ "{SELF}", "{payload}" }},\n'
        with patch.dict(os.environ, MESH_UPTIME_FILE=str(uptime)), \
                patch.object(handover.time, 'time', return_value=234), \
                patch.object(handover.subprocess, 'run', side_effect=[
                    subprocess.CompletedProcess([], 0, raw),
                    subprocess.CompletedProcess([], 0, '')]) as run:
            handover.publish_service_flag('mediamtx', SELF, 'true', str(self.root / 'publish.lock'))
        updated = NodeInfo_pb2.NodeTelemetry.FromString(base64.b64decode(run.call_args.kwargs['input']))
        self.assertTrue(updated.is_mediamtx_server)
        self.assertEqual(updated.uptime_seconds, 10000)
        self.assertEqual(updated.last_seen_timestamp, 234)
        updated.is_mediamtx_server = False
        updated.uptime_seconds = original.uptime_seconds
        updated.last_seen_timestamp = original.last_seen_timestamp
        self.assertEqual(updated, original)

    def test_transition_publication_cannot_revive_a_shutdown_record(self):
        import NodeInfo_pb2
        message = NodeInfo_pb2.NodeTelemetry(node_state=NodeInfo_pb2.NodeTelemetry.SHUTTING_DOWN)
        payload = base64.b64encode(message.SerializeToString()).decode('ascii')
        raw = f'{{ "{SELF}", "{payload}" }},\n'
        with patch.object(handover.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, raw)) as run:
            with self.assertRaisesRegex(ValueError, 'shutdown'):
                handover.publish_service_flag('mediamtx', SELF, 'true', str(self.root / 'publish.lock'))
        self.assertEqual(run.call_count, 1)


if __name__ == '__main__':
    unittest.main()
